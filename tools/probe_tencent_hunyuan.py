#!/usr/bin/env python3
"""tencent_hunyuan「多形态真实下发」probe：进程内管线打真上游（每形态 1 发）。

与 `qwen_frontdoor_smoke.py` 同一思路，但走**完整适配器管线**（FastAPI TestClient
+ lifespan）：门面校验 -> 渠道头解析 -> X-Model-Map -> 脚本 transform -> 真实
TokenHub -> response 相位 -> 错误映射，全程不落存储、不外发 trace：

    MINIO_ENDPOINT/REDIS_URL/LOGFIRE_TOKEN 在 import 前被置空（env 优先于 .env），
    storage=off（upload 降级 data URI）、cache=内存、logfire 只进本地 stdout。
    aiohttp 会话 trust_env 默认 False，不吃 shell 的 HTTP_PROXY。

形态矩阵（默认全跑，每发独立成败）：
    t2i_basic      纯 prompt 文生图（默认尺寸/档位）
    t2i_size_seed  size=1024x1024 + seed=42（验证参数采纳）
    i2i_data_uri   小 PNG data URI 参考图（图生图）
    i2i_bare_b64   bare base64 参考图（脚本补 data: 前缀路径）
    b64_out        response_format=b64_json（适配器下载+编码路径）
    size_invalid   size=100x100（预期上游 400：错误透传形态，应不计费）

凭据只从环境变量读：HUNYUAN_TOKEN（Bearer key）。
产物与 meta 落 reports/<date>_tencent-hunyuan-probe/（不含凭据；签名 URL 只存本地文件不外发）。

    HUNYUAN_TOKEN='sk-…' .venv/bin/python tools/probe_tencent_hunyuan.py --plan
    HUNYUAN_TOKEN='sk-…' .venv/bin/python tools/probe_tencent_hunyuan.py --yes
    HUNYUAN_TOKEN='sk-…' .venv/bin/python tools/probe_tencent_hunyuan.py --yes --cases t2i_basic,b64_out
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import time
from pathlib import Path

# --- 副作用开关：必须在 import adapter 之前 --------------------------------
os.environ["LOGFIRE_TOKEN"] = ""
os.environ["MINIO_ENDPOINT"] = ""
os.environ["REDIS_URL"] = ""
os.environ["ENVIRONMENT"] = "dev"

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

REPORT_DIR = REPO / "reports" / "2026-09-30_tencent-hunyuan-probe"
UPSTREAM = "https://tokenhub.tencentmaas.com/v1/wand/hunyuan-image/v35-generation"
SCRIPT_REF = "tencent_hunyuan/images@v1"
MODEL = "hy-image-v3.5-preview"
REQUEST_TIMEOUT = 240.0  # 同步生成实测可能 30-90s+


def _tiny_png(label_rgb=(220, 120, 40), size=96) -> bytes:
    """自包含参考图：纯色底 + 深色圆，几 KB 级。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (size, size), label_rgb)
    d = ImageDraw.Draw(img)
    r = size // 4
    d.ellipse([size // 2 - r, size // 2 - r, size // 2 + r, size // 2 + r], fill=(40, 40, 60))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


PNG = _tiny_png()
DATA_URI = "data:image/png;base64," + base64.b64encode(PNG).decode()
BARE_B64 = base64.b64encode(PNG).decode()

CASES: dict[str, dict] = {
    "t2i_basic": {"body": {"model": MODEL, "prompt": "画一只橘猫，卡通风格，简洁背景"}},
    "t2i_size_seed": {
        "body": {
            "model": MODEL,
            "prompt": "一只戴帽子的柯基，像素画风格",
            "size": "1024x1024",
            "seed": 42,
        }
    },
    "i2i_data_uri": {
        "body": {
            "model": MODEL,
            "prompt": "参考这张图的构图与配色，画一只真实的柴犬照片",
            "image": DATA_URI,
        }
    },
    "i2i_bare_b64": {
        "body": {
            "model": MODEL,
            "prompt": "参考这张图的风格，画一座雪山",
            "image": BARE_B64,
        }
    },
    "b64_out": {
        "body": {
            "model": MODEL,
            "prompt": "一朵发光的蓝色蘑菇，奇幻插画",
            "response_format": "b64_json",
        }
    },
    # 预期上游 400（宽高 < 256），验错误透传形态；正常不计费。
    "size_invalid": {
        "body": {"model": MODEL, "prompt": "测试", "size": "100x100"},
        "expect_error": True,
    },
}


def ensure_dir(path: Path) -> None:
    if path.is_dir():
        return
    try:
        path.mkdir(parents=True, exist_ok=True)
    except FileExistsError:
        if not path.is_dir():
            raise


def channel_headers(token: str) -> dict[str, str]:
    return {
        "X-Upstream-Url": UPSTREAM,
        "X-Script-Ref": SCRIPT_REF,
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def run_case(client, name: str, spec: dict, headers: dict) -> dict:
    body = spec["body"]
    t0 = time.monotonic()
    record: dict = {"case": name, "expect_error": spec.get("expect_error", False)}
    try:
        resp = client.post(
            "/v1/images/generations", json=body, headers=headers, timeout=REQUEST_TIMEOUT
        )
    except Exception as exc:  # noqa: BLE001
        record.update(status="transport-error", error=f"{type(exc).__name__}: {exc}")
        return record
    record["elapsed_s"] = round(time.monotonic() - t0, 1)
    record["http_status"] = resp.status_code
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        record["status"] = "non-json"
        record["body_head"] = resp.text[:300]
        return record

    record["request_id"] = resp.headers.get("x-request-id", "")
    data = payload.get("data") or []
    usage = payload.get("usage") or {}
    record["usage_total_tokens"] = usage.get("total_tokens")

    if resp.status_code != 200 or payload.get("error"):
        record["status"] = "error-returned"
        record["error"] = payload.get("error")
        record["verdict"] = (
            "OK (expected refusal shape)" if spec.get("expect_error") else "FAIL"
        )
        return record

    item = data[0] if data else {}
    url = item.get("url")
    b64 = item.get("b64_json")
    record["has_url"] = bool(url)
    record["has_b64"] = bool(b64)
    record["width"] = item.get("width")
    record["height"] = item.get("height")

    # 产物落盘（URL 下载或 b64 解码），并核真实图片尺寸。
    out: Path | None = None
    try:
        if b64:
            raw = base64.b64decode(b64)
        elif url and url.startswith("data:"):
            raw = base64.b64decode(url.split(";base64,", 1)[-1])
        elif url:
            import urllib.request

            req = urllib.request.Request(url)  # noqa: S310 - 测试工具，固定上游产物
            with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310
                raw = r.read()
        else:
            raw = b""
        if raw:
            from PIL import Image

            with Image.open(io.BytesIO(raw)) as im:
                record["image_format"], record["image_size"] = im.format, im.size
            out = REPORT_DIR / f"{name}.{(im.format or 'png').lower()}"
            out.write_bytes(raw)
    except Exception as exc:  # noqa: BLE001
        record["artifact_error"] = f"{type(exc).__name__}: {exc}"

    record["artifact"] = str(out.relative_to(REPO)) if out else None
    expected = spec.get("expect_error", False)
    record["verdict"] = "FAIL (expected error)" if expected else ("OK" if (url or b64) else "FAIL (no image)")
    return record


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="真实下发（每形态 1 发）")
    ap.add_argument("--plan", action="store_true", help="只打印计划")
    ap.add_argument("--cases", default=",".join(CASES))
    args = ap.parse_args()

    token = os.environ.get("HUNYUAN_TOKEN", "").strip()
    names = [n for n in args.cases.split(",") if n]
    unknown = [n for n in names if n not in CASES]
    if unknown:
        print(f"unknown cases: {unknown}", file=sys.stderr)
        return 2

    print(f"plan: {len(names)} real request(s) -> {UPSTREAM}")
    for n in names:
        b = CASES[n]["body"]
        desc = b.get("prompt", "")[:24]
        extra = [k for k in b if k not in ("model", "prompt")]
        print(f"  - {n:14s} prompt={desc!r} extra={extra}")

    if args.plan or not args.yes:
        print("(plan only; pass --yes to send)")
        return 0
    if not token:
        print("HUNYUAN_TOKEN is required for --yes", file=sys.stderr)
        return 2

    ensure_dir(REPORT_DIR)
    from adapter.main import app
    from starlette.testclient import TestClient

    headers = channel_headers(token)
    results: list[dict] = []
    with TestClient(app) as client:
        for n in names:
            print(f"[{n}] sending ...", flush=True)
            rec = run_case(client, n, CASES[n], headers)
            results.append(rec)
            print(
                f"[{n}] {rec.get('verdict')} http={rec.get('http_status')} "
                f"{rec.get('elapsed_s')}s tokens={rec.get('usage_total_tokens')} "
                f"size={rec.get('image_size')} artifact={rec.get('artifact')}",
                flush=True,
            )
            (REPORT_DIR / "meta.json").write_text(
                json.dumps(results, ensure_ascii=False, indent=2) + "\n"
            )

    bad = [r for r in results if not str(r.get("verdict", "")).startswith("OK") and not r.get("verdict", "").startswith("FAIL (expected")]
    ok = [r for r in results if str(r.get("verdict", "")).startswith("OK")]
    print(f"\ndone: {len(ok)}/{len(results)} verdict-OK; report -> {REPORT_DIR / 'meta.json'}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
