"""guest 真实出图验证：现铸一个访客身份（真 Chrome）→ 走适配器访客门 → 按用例打生成。

三类用例（链式，后者的输入图来自前者的产物）：

    t2i    文生图        无输入                   1 发
    i2i    图生图        输入 = t2i 的产物          1 发
    multi  多图生图      输入 = t2i + i2i 两张产物   1 发

**额度**：每类各 1 发，默认三轮共 3 发。访客额度绑设备身份（约 4~5 张/天），
所以默认拒绝执行，必须显式 `--yes`；且**任一发被挡即停手**（不连发、不加深 WAF 累计）。
单轮计划数 > 3 时另需 `--allow-burst`（多档 `--tiers` 会成倍放大）。

**素材体积**：上游脚本的单次 PUT 上限 2MB（`SIMPLE_PUT_LIMIT`），而 1K 产物 PNG
常超过它 ⇒ 超限的产物用 Pillow 压到预算以下再喂回去（打印压缩前后体积）。
不这样做的话，用例会因体积而非链路问题失败 —— 那是假失败。

前提（缺一发都白搭，先跑 `tools/qwen_egress_check.py`）：
    python tools/qwen_egress_check.py --rounds 3     # 退出码 0 = 有干净出口

用法：
    python tools/qwen_guest_smoke.py --yes                        # 三类全跑（3 发）
    python tools/qwen_guest_smoke.py --yes --cases t2i             # 只验文生图（1 发）
    python tools/qwen_guest_smoke.py --yes --cases i2i             # 自动补 t2i 作素材（2 发）
    python tools/qwen_guest_smoke.py --yes --tiers 1K,2K --allow-burst   # 两档 × 三类（6 发）

成功输出 `data[].url`；被挡会打印上游原话（滑块页 / x5sec 各自含义见 docs/10 §6）。
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
#: 仓根也要在路径上：`adapter` 是要 import 的包，而直接跑脚本时 sys.path[0] 是 `tools/`
#: ⇒ 只插 tools/ 会 `ModuleNotFoundError: No module named 'adapter'`（2026-09-19 实测，
#: 这正是本工具"照文档跑不起来"的原因）。
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(REPO))

UPSTREAM = "https://chat.qwen.ai/api/v2/chat/completions"

DEFAULT_PROMPT = "一只在窗台上打盹的橘猫，水彩风格"
I2I_PROMPT = "把背景换成下雪的夜晚，保留主体的姿势与构图"
MULTI_PROMPT = "把这两张图的主体合成到同一个场景里，风格统一"

#: 执行顺序固定（后者依赖前者的产物），`parse_cases` 按此排序。
CASES = ("t2i", "i2i", "multi")
#: 依赖闭包：要跑某类，必须先跑它的前置（输入图只能由前置的产物提供）。
CASE_NEEDS = {"t2i": (), "i2i": ("t2i",), "multi": ("t2i", "i2i")}

#: 与 `script_store/qwen/images@v1.py` 的 SIMPLE_PUT_LIMIT 一致（2MB 硬墙）。
PUT_LIMIT = 2 * 1024 * 1024
#: 留 5% 余量：base64 是喂给适配器的形态，落到脚本手里的字节数与原图一致，
#: 但上游按 content-length 判定时偶有偏差，卡在边界上不值得。
ASSET_BUDGET = int(PUT_LIMIT * 0.95)

#: 单轮发数上限（guest 全天额度约 4~5 张，一次跑满就没得复测了）。
BURST_LIMIT = 3

ASSET_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")

#: 魔数兜底只在本工具的 HTTP 头缺失时用（框架侧 `ctx.sniff_mime` 才是权威）。
#: 刻意**不**跟框架一样兜底成 `application/octet-stream` —— 这里判不出图片类型时
#: 必须回落到图片 mime，否则脚本会把输入当 `file` 类上传（`_filetype_of`）。
_MAGIC = ((b"\x89PNG\r\n\x1a\n", "image/png"),
          (b"\xff\xd8\xff", "image/jpeg"),
          (b"GIF87a", "image/gif"),
          (b"GIF89a", "image/gif"),
          (b"\x00\x00\x01\x00", "image/x-icon"))


# ------------------------------------------------------------------ 计划（纯逻辑）


def parse_cases(spec: str) -> list[str]:
    """`"multi"` -> `["t2i", "i2i", "multi"]`：补齐前置闭包并按固定顺序排列。

    补齐是**必须的**，不是贴心：i2i 的输入图只能来自一次真实的 t2i 出图
    （本工具不发假素材），multi 要两张**不同**的图才能验保序。空串 = 全部。
    """
    asked = [part.strip().lower() for part in (spec or "").split(",")]
    asked = [part for part in asked if part]
    unknown = [part for part in asked if part not in CASES]
    if unknown:
        raise ValueError("未知用例 " + "/".join(unknown) + "（可选：" + "/".join(CASES) + "）")
    if not asked:
        asked = list(CASES)
    ordered: list[str] = []
    for case in CASES:                      # 固定顺序 = 依赖顺序
        if case in asked or any(case in CASE_NEEDS[want] for want in asked):
            ordered.append(case)
    return ordered


def plan_budget(cases: list[str], tiers: list[str]) -> int:
    """本轮的真实生成发数 = 用例数 × 档位数（每发都计费）。"""
    return len(cases) * len(tiers)


def to_data_uri(data: bytes, mime: str) -> str:
    return "data:" + (mime or "image/png") + ";base64," + base64.b64encode(data).decode()


def mime_of(data: bytes) -> str:
    """魔数嗅探；不认识的返回 `""`（由调用方回落到图片 mime）。"""
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    # 容器格式的标记在固定偏移处，不在起始字节。
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    # AVIF 同属容器格式；框架的 `ctx.sniff_mime` 早已认它（`codec._MAGIC` 之外的
    # 那两条偏移判据之一），工具漏了会在"字节优先"下把它误标成 PNG。
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return "image/avif"
    return ""


def shrink_to_budget(data: bytes, mime: str, budget: int = ASSET_BUDGET):
    """把超过单次 PUT 上限的产物压到预算内：返回 `(data, mime, note)`。

    先降 JPEG quality（不动尺寸，保真优先），再逐级缩边。**只在超限时才动**，
    且在 `note` 里如实记录做了什么 —— 静默改素材会让"图生图跑通了"这句话
    变成一句空话（用户看到的图已经不是模型出的那张）。
    """
    if len(data) <= budget:
        return data, mime, ""
    from PIL import Image

    img = Image.open(io.BytesIO(data))
    img.load()
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    last = (data, mime)
    for scale in (1.0, 0.8, 0.6, 0.45, 0.3):
        frame = img
        if scale < 1.0:
            frame = img.resize((max(1, int(img.width * scale)),
                                max(1, int(img.height * scale))), Image.LANCZOS)
        for quality in (88, 78, 68):
            buf = io.BytesIO()
            frame.save(buf, format="JPEG", quality=quality, optimize=True)
            out = buf.getvalue()
            last = (out, "image/jpeg")
            if len(out) <= budget:
                note = (f" 压缩 {len(data) / 1048576:.2f}MB→{len(out) / 1048576:.2f}MB"
                        f" (jpeg q{quality}, {scale:.2f}×, {img.width}×{img.height}"
                        f"→{frame.width}×{frame.height})")
                return out, "image/jpeg", note
    note = (f" 压缩到 {len(last[0]) / 1048576:.2f}MB 仍超上限"
            f"{budget / 1048576:.2f}MB ⇒ 可能被上游拒（如实上报）")
    return last[0], last[1], note


def case_payload(
    case: str, tier: str, prompt: str, assets: list[str], response_format: str = ""
) -> dict:
    """按用例构造 canonical 请求体；素材不足时**明确报错**，不发半截请求。

    `response_format` 留空 ⇒ 不带该键，即 adapter 的默认：产物直通上游原生链接。
    传 `b64_json` ⇒ 产物由 adapter 自己取回并编码，链式用例就**不再依赖客户端**去下载
    `cdn.qwenlm.ai` —— 那条路会撞上已知的 404 死链（`docs/10` §4：要 `b64_json` 时
    死链变成明确失败，要 `url` 时它被当成功转出）。实测 2026-09-20：访客门 + t2i 的
    产物链接**紧跟生成后立刻** 404，客户端无从下载，链子就此断掉。
    """
    if case not in CASES:
        raise ValueError("未知用例 " + str(case))
    body: dict = {"model": "qwen-image", "size": tier}
    if response_format:
        body["response_format"] = response_format
    if case == "t2i":
        body["prompt"] = prompt
        return body
    if case == "i2i":
        body["prompt"] = I2I_PROMPT
        body["image"] = _asset_at(assets, 0)
        return body
    body["prompt"] = MULTI_PROMPT
    body["image"] = [_asset_at(assets, 0), _asset_at(assets, 1)]
    return body


def _asset_at(assets: list[str], index: int) -> str:
    if len(assets) <= index:
        raise ValueError(f"缺少第 {index + 1} 张素材：前置用例未产出图（先跑 t2i/i2i）")
    return assets[index]


# ------------------------------------------------------- 产物 -> 素材（要网络）


def download_asset(url: str, timeout: float = 60.0):
    """取产物字节：先直连、再试环境代理（两条出口本就不同，见 qwen_egress_check）。"""
    import httpx

    errors: list[str] = []
    for label, trust_env in (("直连", False), ("环境代理", True)):
        try:
            with httpx.Client(trust_env=trust_env, timeout=timeout,
                              follow_redirects=True) as client:
                resp = client.get(url, headers={"User-Agent": ASSET_UA})
            if resp.status_code == 200 and resp.content:
                mime = (resp.headers.get("content-type") or "").split(";")[0].strip()
                # 🔴 **字节优先，头只兜底**（2026-09-19 01:26 guest 1K 三连实测）：
                # 真图回来时 CDN 的 `Content-Type` 就是 **application/octet-stream**
                # （与参考仓 §3.0 ⑧ 同一观测），于是"头优先"会把喂给适配器的 data URI
                # 声明成 octet-stream —— 而本文件开头那条「判不出图片类型时必须回落到
                # 图片 mime」的守卫**根本没机会跑**（`mime_of` 只在头为空时才被调用）。
                # 今天没出事是因为 qwen 脚本读的是 `ctx.sniff_mime(bytes)`（已实测
                # filetype=image）；换个信头不信字节的脚本就会把它当 `file` 类上传，
                # 那正是这条守卫想防的事。⇒ 顺序反过来：先魔数，再头，最后图片 mime。
                mime = mime_of(resp.content) or (
                    mime if mime.startswith("image/") else "") or "image/png"
                return resp.content, mime, label
            errors.append(f"{label}: HTTP {resp.status_code}")
        except Exception as exc:  # noqa: BLE001 - 两种出口的失败都要报出来
            errors.append(f"{label}: {type(exc).__name__}")
    raise RuntimeError("下载产物失败（" + "; ".join(errors) + "）")


def product_count(doc: dict) -> int:
    """产物条数 ＝ `data[]` 的条目数，**不按 URL 数**。

    两种形态都要算：`url`（有链接）与 `b64_json`（没有链接）。按 URL 数会在
    `b64_json` 那一轮打印"出图 0 张"，把一次成功的生成读成失败 —— 2026-09-20 实跑
    撞到：素材明明取到了 1.14MB，行首却写着 0 张。
    """
    return len(doc.get("data") or [])


def asset_from_response(doc: dict):
    """上游 `data[0]` -> `(bytes, mime, via)`。url 与 b64_json 两种形态都接。"""
    items = doc.get("data") or []
    if not items:
        raise RuntimeError("响应里没有 data[]")
    item = items[0]
    if item.get("url"):
        return download_asset(item["url"])
    if item.get("b64_json"):
        data = base64.b64decode(item["b64_json"])
        return data, mime_of(data) or "image/png", "b64_json"
    raise RuntimeError("data[0] 既无 url 也无 b64_json")


def format_plan(cases: list[str], tiers: list[str], planned: int) -> str:
    return (f"计划：用例 {'/'.join(cases)} × 档位 {'/'.join(tiers)} = "
            f"**{planned} 次真实生成**（各用例的输入图来自前一个用例的产物）")


# ------------------------------------------------------------------------ main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--yes", action="store_true",
                       help="确认消耗真实生成额度（见计划打印的发数）")
    parser.add_argument("--cases", default=",".join(CASES),
                       help="要跑的用例，逗号分隔：t2i,i2i,multi（选后者会自动补前置）")
    parser.add_argument("--tiers", default="1K",
                       help="要出的档位，逗号分隔：1K,2K（1K≈9s / 2K≈80s，均由 extra.meta.model 决定）")
    parser.add_argument("--pace", type=float, default=15.0,
                       help="用例/档位之间的间隔秒数（不连打）")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT,
                       help="文生图的提示词（图生图/多图生图用内置编辑提示词）")
    parser.add_argument("--response-format", default="",
                       choices=("", "url", "b64_json"),
                       help="产物形态：空＝不传（adapter 默认，直通上游链接）；"
                            "b64_json＝由 adapter 取回并编码 ⇒ 链式用例避开"
                            "客户端下载 cdn 的 404（docs/10 §4）")
    parser.add_argument("--allow-burst", action="store_true",
                       help=f"允许单轮超过 {BURST_LIMIT} 发（多档会成倍放大，谨慎）")
    parser.add_argument("--headful", action="store_true",
                       help="显示浏览器窗口（默认 headless —— 与 identity_service "
                            "和参考仓 make_identities 一致；调试才加这个）")
    parser.add_argument("--skip-egress-check", action="store_true",
                       help="跳过出口门禁（不建议；用来区分'出口挡'与'访客门挡'）")
    parser.add_argument("--proxy", default="",
                       help="让本渠道走出站代理，如 http://127.0.0.1:11080"
                            "（本地 SOCKS→HTTP 桥见 tools/socks_http_bridge.py）；"
                            "空＝不走代理（默认，与存量渠道一致）")
    parser.add_argument("--proxy-mode", default="per-request",
                       choices=("shared", "per-request"),
                       help="per-request（默认）＝每个客户端请求一条新连接 ⇒ 换出口；"
                            "shared＝连接复用、出口稳定")
    args = parser.parse_args(argv)

    try:
        cases = parse_cases(args.cases)
    except ValueError as exc:
        print("用例名非法：" + str(exc))
        return 2
    tiers = [t.strip() for t in args.tiers.split(",") if t.strip()] or ["1K"]
    planned = plan_budget(cases, tiers)
    print(format_plan(cases, tiers, planned))

    if not args.yes:
        print(f"拒绝执行：这会消耗 {planned} 次真实生成额度。确认后加 --yes。")
        return 2
    if planned > BURST_LIMIT and not args.allow_burst:
        print(f"拒绝执行：{planned} 发 > 单轮上限 {BURST_LIMIT} 发"
              f"（访客额度绑设备身份，全天约 4~5 张）。用 --cases/--tiers 收敛；"
              "确信要跑就加 --allow-burst。")
        return 2

    if not args.skip_egress_check:
        import subprocess
        print("门槛检查：先看现在有没有能过 WAF 的出口（零额度）……")
        probe = subprocess.run([sys.executable, str(REPO / "tools" / "qwen_egress_check.py"),
                                "--rounds", "2", "--quiet"],
                               capture_output=True, text=True, timeout=240)
        print(probe.stdout.strip() or probe.stderr.strip()[:200])
        if probe.returncode != 0:
            print("⇒ 出口全被挡：此刻发也是白花额度，先等冷却。"
                  "（若确信要区分是谁挡的，用 --skip-egress-check）")
            return 1

    import identity_service as ids
    from starlette.testclient import TestClient

    from adapter.main import app
    from adapter.settings import Settings

    app.state.settings = Settings(
        environment="dev", adapter_key_required=False, adapter_key="",
        allow_inline_script=True, upstream_allow_private_network=True,
        redis_url="", storage_backend="minio", minio_endpoint="", fal_key="",
        # Only meaningful when the run passes `--proxy`, and harmless otherwise:
        # an allowlist trusting loopback is what lets the header be set at all
        # (an empty list refuses it outright). The bypass list keeps the OSS
        # upload off the proxy -- the deployment shape, so `--proxy` exercises
        # the same paths a real channel would.
        upstream_proxy_allowlist="127.0.0.1,localhost",
        upstream_proxy_bypass_hosts="*.aliyuncs.com",
    )
    minter = ids.BrowserMinter(channel="chrome", headless=not args.headful,
                              settle_ms=7000)
    sent = 0
    try:
        started = time.time()
        ident = minter.mint()
        print(f"访客身份就绪 {time.time() - started:.1f}s：cookie={len(ident['cookie'])} "
              f"bx_ua={len(ident['bx_ua'])} umid={ident['bx_umidtoken'][:8]}…")
        headers = {
            "X-Upstream-Url": UPSTREAM,
            "X-Script-Ref": "qwen/images@v1",
            "X-Channel-Options": json.dumps({
                "chat_mode": "guest", "cookie": ident["cookie"],
                "bx_ua": ident["bx_ua"], "bx_umidtoken": ident["bx_umidtoken"]}),
            "X-Auth-Emit": "none",
            "Authorization": "Bearer guest",
        }
        if args.proxy:
            # Exactly what a deployment sets on the channel: the engine's own
            # call and everything the script does through ctx.http leave through
            # the proxy, while the OSS upload stays direct (bypass list above).
            headers["X-Upstream-Proxy"] = args.proxy
            headers["X-Upstream-Proxy-Mode"] = args.proxy_mode
            print(f"经代理：{args.proxy}（模式 {args.proxy_mode}）")
        with TestClient(app, raise_server_exceptions=False) as client:
            for tier in tiers:
                assets: list[str] = []       # 本档内累积：i2i 用 assets[0]，multi 用前两张
                for case in cases:
                    if sent:
                        print(f"… 停 {args.pace:.0f}s（不连打）")
                        time.sleep(args.pace)
                    try:
                        body = case_payload(
                            case, tier, args.prompt, assets, args.response_format)
                    except ValueError as exc:
                        print(f"[{tier}] {case} 跳过：{exc}")
                        return 1
                    sent += 1
                    sent_at = time.time()
                    resp = client.post("/v1/images/generations", headers=headers, json=body)
                    elapsed = time.time() - sent_at
                    label = f"[{tier}] {case}"
                    if resp.status_code != 200:
                        print(f"{label} HTTP {resp.status_code}  {elapsed:.1f}s")
                        print("   ❌", resp.text[:300])
                        print("   ⇒ 被挡即停手（不连发）")
                        return 1
                    doc = resp.json()
                    urls = [item.get("url") for item in (doc.get("data") or [])
                            if item.get("url")]
                    # 单图的 `image` 是**字符串**（data URI），`len()` 会打出 170 万"张"
                    # （2026-09-19 实跑撞到）⇒ 按形态数，不按长度。
                    imgs = body.get("image")
                    n_imgs = len(imgs) if isinstance(imgs, list) else (1 if imgs else 0)
                    extra = f"  输入 {n_imgs} 张" if n_imgs else ""
                    print(f"{label} HTTP 200  {elapsed:.1f}s  "
                          f"出图 {product_count(doc)} 张{extra}")
                    for url in urls:
                        print("   ✅", url)
                    if doc.get("data") and not urls:
                        print(f"   └ 产物为 b64_json 形态（{product_count(doc)} 条，无 URL）")
                    if doc.get("usage"):
                        print("   usage:", doc["usage"])
                    if case in ("t2i", "i2i"):
                        data, mime, via = asset_from_response(doc)
                        data, mime, note = shrink_to_budget(data, mime)
                        assets.append(to_data_uri(data, mime))
                        print(f"   └ 素材 #{len(assets)}: {len(data) / 1048576:.2f}MB "
                              f"{mime}（{via}）{note or ' 原样'} ⇒ 用作后续用例的输入")
        return 0
    finally:
        minter.close()
        app.state.settings = None


if __name__ == "__main__":
    raise SystemExit(main())
