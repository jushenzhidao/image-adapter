#!/usr/bin/env python3
"""qwen「四个入口」smoke：**折叠层打真上游**（每个门 1 发）。

本仓的设计是"四个入口都是前门"（`adapter/api/frontdoor.py`）：规范契约只有
`/v1/images/generations`，`edits` / `chat` / `responses` 在**准入检查之后**被折叠成
同一个 canonical body，再交给同一个脚本。折叠的正确性此前只有单测/假上游层面的证据
（`tests/integration/`），**没打过真上游** —— 本工具补这一格。

    generations  规范门（已多次实测，可作对照）
    edits        多部件上传：`image` 文件 + `prompt`
    chat         `messages[]{content:[{type:text},{type:image_url,image_url:{url}}]}`
    responses    `input[]{content:[{type:input_text},{type:input_image,image_url:"…"}]}`
                 （`image_url` 收 `{"url":…}` 与裸串两种写法，见 `_image_ref`）

每个门判四件事：
  1. HTTP 与耗时；
  2. 产物是否落盘（按门各自的响应形状取 URL：`data[0].url` 或响应里第一个 cdn 链接）；
  3. 脚本 trace 自报的 `chat_type` / `input_images`（折叠对了就该是 `image_edit` + 1）；
  4. **门归属**（产物 `key=<JWT>` 的 `resource_user_id` == 凭据账号 id）。

凭据只从环境变量读；产物与 meta 落 `--report-dir`（不含凭据、不含带签名的 URL）。

    QWEN_JWT='eyJ…' .venv/bin/python tools/qwen_frontdoor_smoke.py --plan
    QWEN_JWT='eyJ…' .venv/bin/python tools/qwen_frontdoor_smoke.py --yes --doors edits,chat,responses

⚠️ `--yes` 是硬门槛：**每个门消耗 1 发**账号额度（垫图只是输入，不计费）。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

DEFAULT_PROMPT = "把这张图的主体保持原样，只把背景换成明亮的沙漠正午，插画风格"
DOORS = ("generations", "edits", "chat", "responses")


def ensure_dir(path: Path) -> None:
    """与 `qwen_key_form_smoke.py` 同款：沙箱 shim 会对已存在目录抛 EEXIST，失败后以磁盘为准。"""
    if path.is_dir():
        return
    try:
        path.mkdir(parents=True, exist_ok=True)
    except (PermissionError, FileExistsError):
        if not path.is_dir():
            raise


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "qwen_images_v1", REPO / "script_store" / "qwen" / "images@v1.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


q = _load_script()


def jwt_payload(token: str) -> dict:
    part = token.split(".")[1] if token.count(".") >= 2 else ""
    try:
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:  # noqa: BLE001
        return {}


class TapLogfire:
    """旁路一份 `ctx.logfire` 的 note（照旧转发），用来读脚本自报的 chat_type/input_images。"""

    def __init__(self, real):
        self.real = real
        self.notes: list[dict] = []

    def info(self, message, **attrs):
        self.notes.append({"message": message, **attrs})
        try:
            self.real.info(message, **attrs)
        except Exception:  # noqa: BLE001
            pass

    def __getattr__(self, name):
        return getattr(self.real, name)


def _data_uri(path: Path) -> str:
    raw = path.read_bytes()
    mime = ("image/png" if raw.startswith(b"\x89PNG\r\n\x1a\n")
            else "image/jpeg" if raw.startswith(b"\xff\xd8\xff") else "image/png")
    return "data:" + mime + ";base64," + base64.b64encode(raw).decode()


def build(door: str, *, model: str, size: str, prompt: str,
          image_path: Path, data_uri: str):
    """每个门的请求形状。返回 `(path, json_body, files, form_data)`（后三者按门给）。"""
    if door == "generations":
        return "/v1/images/generations", {
            "model": model, "size": size, "prompt": prompt, "image": [data_uri]}, None, None
    if door == "edits":
        # 多部件：`image` 是文件（折叠层会转成 data URI），其余进表单
        return ("/v1/images/edits", None,
                [("image", (image_path.name, image_path.read_bytes(), "image/png"))],
                {"model": model, "size": size, "prompt": prompt})
    if door == "chat":
        return "/v1/chat/completions", {
            "model": model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_uri}}]}]}, None, None
    if door == "responses":
        return "/v1/responses", {
            "model": model,
            "input": [{"role": "user", "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image", "image_url": data_uri}]}]}, None, None
    raise SystemExit("未知的门：" + door)


def pick_url(door: str, text: str) -> str:
    """按门各自的响应形状取产物 URL；取不到就扫第一个 cdn 链接（如实报出来）。"""
    if door in ("generations", "edits"):
        try:
            doc = json.loads(text)
            items = doc.get("data") or []
            if items and isinstance(items[0], dict) and items[0].get("url"):
                return str(items[0]["url"])
        except Exception:  # noqa: BLE001
            pass
    # 🔴 chat 的响应里 URL 后面**紧跟**被转义的 JSON（`…MRgGU%22}}]},%22finish_reason%22…`），
    # 用 `\S+` 会把整段尾巴吞进 URL ⇒ 下回来一个 226B 的错误页（而不是产物）。
    # 终止字符里必须包含 `%22` 与 `)`、引号、反斜杠、花括号。
    m = re.search(r"https://cdn\.qwenlm\.ai/[^\s\"'\\)\}]+", text)
    if not m:
        return ""
    url = m.group(0)
    return url.split("%22")[0]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--doors", default="edits,chat,responses",
                    help="要跑的门（逗号分隔，默认三个折叠门；可含 generations 作对照）")
    ap.add_argument("--yes", action="store_true", help="硬门槛：不给只打印计划")
    ap.add_argument("--plan", action="store_true", help="只打印计划（同不给 --yes）")
    ap.add_argument("--tier", default="1K")
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--input-image", default="",
                    help="垫图本地路径（默认取仓库里现成的一张 1K 产物）")
    ap.add_argument("--report-dir", default="")
    args = ap.parse_args(argv)

    doors = [d.strip() for d in args.doors.split(",") if d.strip()]
    bad = [d for d in doors if d not in DOORS]
    if bad:
        print("未知的门：", bad, "（可选：", ", ".join(DOORS), "）")
        return 2
    jwt = os.environ.get("QWEN_JWT", "").strip()
    if not jwt:
        print("拒绝执行：请用环境变量 QWEN_JWT 传 token（不落盘）。")
        return 2
    account_id = str(jwt_payload(jwt).get("id") or "")

    image_path = Path(args.input_image) if args.input_image else (
        REPO / "reports" / "2026-09-19_qwen-key-jwt" / "outputs" / "jwt_1k_t2i.png")
    if not image_path.is_file():
        print(f"拒绝执行：垫图不存在 {image_path}（用 --input-image 指定）。")
        return 2
    report = Path(args.report_dir) if args.report_dir else (
        REPO / "reports" / f"{time.strftime('%Y-%m-%d')}_qwen-frontdoors")

    print(f"计划：{len(doors)} 个门 × 1 发 = **{len(doors)} 发**账号额度；"
          f"档位 {args.tier}；垫图 {image_path.name}（{image_path.stat().st_size}B）")
    for d in doors:
        print(f"   · {d}")
    if not args.yes or args.plan:
        print("（未加 --yes ⇒ 只打印，不发任何请求）")
        return 2

    import adapter.context as ctxmod
    tap = TapLogfire(ctxmod.logfire_module)
    ctxmod.logfire_module = tap

    from starlette.testclient import TestClient

    from adapter.main import app
    from adapter.settings import Settings

    app.state.settings = Settings(
        environment="dev", adapter_key_required=False, adapter_key="",
        allow_inline_script=True, upstream_allow_private_network=True,
        redis_url="", storage_backend="minio", minio_endpoint="", fal_key="",
    )
    headers = {
        "X-Upstream-Url": "https://chat.qwen.ai/api/v2/chat/completions",
        "X-Script-Ref": "qwen/images@v1",
        "X-Channel-Options": json.dumps({}),
        "X-Auth-Emit": "none",
        "Authorization": "Bearer " + jwt,
    }
    data_uri = _data_uri(image_path)
    ensure_dir(report / "outputs")
    ensure_dir(report / "meta")

    summary = []
    with TestClient(app, raise_server_exceptions=False) as client:
        for door in doors:
            path, body, files, form = build(door, model="qwen-image", size=args.tier,
                                            prompt=args.prompt, image_path=image_path,
                                            data_uri=data_uri)
            print(f"[{door}] POST {path}")
            started = time.time()
            if files:
                # 多部件：Content-Type 与 boundary 由客户端自己带（别预先写死）
                resp = client.post(path, headers=headers, files=files, data=form)
            else:
                resp = client.post(path, headers=headers, json=body)
            elapsed = time.time() - started
            text = resp.text
            print(f"   HTTP {resp.status_code}  {elapsed:.1f}s  "
                  f"X-Request-Id={resp.headers.get('x-request-id')}")
            url = pick_url(door, text) if resp.status_code == 200 else ""
            notes = [n for n in tap.notes if n.get("stage") == "request"]
            trace = notes[-1] if notes else {}
            print(f"   trace: chat_type={trace.get('chat_type')} "
                  f"input_images={trace.get('input_images')} size={trace.get('size')}")
            row = {"door": door, "path": path, "http": resp.status_code,
                   "elapsed_s": round(elapsed, 1),
                   "request_id": resp.headers.get("x-request-id"),
                   "trace": {k: v for k, v in trace.items() if k != "message"},
                   "response_head": text[:300]}
            if not url:
                print("   ❌ 没取到产物 URL：", text[:200])
                row["error"] = text[:300]
            else:
                owner = ""
                m = re.search(r"[?&]key=([^&]+)", url)
                if m:
                    owner = str(jwt_payload(m.group(1)).get("resource_user_id") or "")
                door_of = "account" if (owner and account_id and owner == account_id) else "unclear"
                # 下载产物：直连（不走代理），与其它工具同款
                import httpx
                with httpx.Client(trust_env=False, timeout=60) as c:
                    raw = c.get(url).content
                png = raw.startswith(b"\x89PNG\r\n\x1a\n")
                try:
                    import io as _io
                    from PIL import Image
                    im = Image.open(_io.BytesIO(raw))
                    dims = f"{im.width}x{im.height}"
                except Exception:  # noqa: BLE001
                    dims = "?"
                out = report / "outputs" / f"{door}_{args.tier.lower()}.png"
                out.write_bytes(raw)
                print(f"   ✅ 产物 {out.name}：{len(raw)}B magic={'PNG' if png else '?'} {dims}"
                      f" | 门归属={door_of}")
                row.update({"door_of": door_of, "bytes": len(raw), "dims": dims,
                            "sha256": hashlib.sha256(raw).hexdigest(),
                            "file": f"outputs/{out.name}"})
            summary.append(row)
            tap.notes.clear()

    (report / "meta" / "frontdoors_run.json").write_text(
        json.dumps({"doors": doors, "account_id_prefix": account_id[:8], "runs": summary},
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"meta 已落盘 {report}/meta/frontdoors_run.json（不含凭据、不含签名 URL）")
    ok = sum(1 for r in summary if r.get("http") == 200 and r.get("door_of") == "account")
    print(f"⇒ {ok}/{len(summary)} 个门满足「200 且走账号门」")
    return 0 if ok == len(summary) else 1


if __name__ == "__main__":
    raise SystemExit(main())
