#!/usr/bin/env python3
"""qwen 渠道「密钥形态」smoke：**哪种凭据形态真能出图，以及走的是哪个门**。

一个工具覆盖脚本认得的四种 Bearer 形态（`script_store/qwen/images@v1.py` §3 的表）：

    jwt    `eyJ…`          登录态：直接就是 token（写进 Cookie 的 `token=`）
    pair   `<user>|<pass>` 登录态：脚本自己 signin 换 JWT（有 IP 级频率墙）
    jar    整串 cookie     登录态：这串**就是** jar，**原样**上线（取其 `token=` 判归属）
    empty  空密钥           凭据**不放密钥、放进渠道选项** `X-Channel-Options.cookie`
                           （jar 里没有 `token=` 就落到访客门；访客门还要 bx_*，缺则 400）
    guest  （不在这里）      访客门请用 `tools/qwen_guest_smoke.py`（要现铸设备身份）

**为什么不能只看"200 出图"**：pair 形态 signin 失败会 `_effective_key → "guest"` **静默降级**；
jwt 形态如果 token 已失效，脚本同样可能落到访客门。所以本工具每次都核**门归属**：

    产物 URL 里 `key=<JWT>` 的 `resource_user_id`  ==  凭据自己的账号 id？

相等 ⇒ 走的是账号门；不等 ⇒ 回退到了访客门（如实报出来）。第二条独立证据是脚本自己上报的
trace：`stage="request"` 的 `chat_mode` 是 `normal`（账号）还是 `guest`（访客）。

凭据**只从环境变量读**，不写进任何文件（本仓纪律：凭据不落仓）：

    QWEN_JWT='eyJ…'                     .venv/bin/python tools/qwen_key_form_smoke.py --yes --key-form jwt
    QWEN_ACCOUNT='…' QWEN_PASSWORD='…'  .venv/bin/python tools/qwen_key_form_smoke.py --yes --key-form pair
    QWEN_COOKIE='token=eyJ…; aui=…'     .venv/bin/python tools/qwen_key_form_smoke.py --yes --key-form jar

⚠️ `--yes` 是硬门槛，每次消耗 **1 发**账号额度（`--tier` 默认 1K，约 10–30s）。
⚠️ 产物与 meta 落在 `--report-dir`（默认 `reports/<日期>_qwen-key-<form>/`），文件名**带档位**
   （`<form>_<tier>_t2i.png` / `<form>_<tier>_run.json`）⇒ 同一目录里跑 1K 与 2K 不会互相覆盖；
   里面**不含凭据、也不含带签名的产物 URL**。
"""
from __future__ import annotations

import argparse
import base64
import datetime
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

UPSTREAM = "https://chat.qwen.ai/api/v2/chat/completions"
BASE = "https://chat.qwen.ai/api"
WARM_PATH = "/auth"
SIGNIN_PATH = "/v2/auths/signin"
DEFAULT_PROMPT = "一只戴围巾的柴犬，冬天傍晚，插画风格"

#: 与脚本同源的头（直接引用脚本模块的常量，避免两边漂移）。
_spec = importlib.util.spec_from_file_location(
    "qwen_images_v1", REPO / "script_store" / "qwen" / "images@v1.py")
q = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(q)


# ------------------------------------------------------------------ JWT / 凭据

def jwt_payload(token: str) -> dict:
    """JWT 的载荷。不校验签名 —— 这里只读服务端自己签的内容做归属核对。"""
    try:
        part = str(token).split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:  # noqa: BLE001
        return {}


def token_of_jar(jar: str) -> str:
    """整串 cookie 里的 `token=`。`bx-umidtoken=` 里也含这个子串 ⇒ 按条目名切。"""
    for part in str(jar).split(";"):
        name, _, value = part.partition("=")
        if name.strip() == "token":
            return value.strip()
    return ""


def sign_in(account: str, password: str) -> tuple[str, str]:
    """独立 signin（pair 形态的先验步骤）：返回 `(token, note)`。"""
    import httpx

    headers = {"Accept": "application/json", "Content-Type": "application/json",
               "Origin": "https://chat.qwen.ai", "Referer": "https://chat.qwen.ai/auth",
               "User-Agent": q.UA}
    headers.update(q.SIGNIN_HEADERS)
    headers.update(q.BROWSER_HINTS)
    warm = dict(headers)
    warm.update(q.SIGNIN_WARM_HEADERS)
    with httpx.Client(trust_env=False, timeout=60, follow_redirects=True) as c:
        try:
            c.get(BASE + WARM_PATH, headers=warm)
        except Exception:  # noqa: BLE001 - 预热是 best-effort，脚本里也一样
            pass
        r = c.post(BASE + SIGNIN_PATH,
                   json={"email": account,
                         "password": hashlib.sha256(password.encode()).hexdigest()},
                   headers=headers)
    if r.status_code != 200:
        return "", f"HTTP {r.status_code} {r.text[:160]}"
    token = ""
    for raw in r.headers.get_list("set-cookie"):
        m = re.search(r"(?:^|[;\s])token=([^;]+)", raw)
        if m:
            token = m.group(1).strip()
            break
    if not token:
        low = (r.text or "")[:200].lower()
        kind = ("WAF 挑战页" if ("<!doctype" in low or "aliyun_waf" in low or "captcha" in low)
                else "无 token（口令错 / 形态变了）")
        return "", f"{kind}: {r.text[:160]}"
    return token, "ok"


def resolve_credential(form: str) -> tuple[str, str, dict] | None:
    """`(bearer_key, 摘要里的名字, 账号自述)`；拿不到凭据时返回 None。"""
    if form == "jwt":
        token = os.environ.get("QWEN_JWT", "").strip()
        if not token:
            print("拒绝执行：请用环境变量 QWEN_JWT 传 token（不落盘）。")
            return None
        return token, "jwt", jwt_payload(token)
    if form == "jar":
        jar = os.environ.get("QWEN_COOKIE", "").strip()
        if not jar or ";" not in jar:
            print("拒绝执行：请用环境变量 QWEN_COOKIE 传**整串** cookie（含分号）。")
            return None
        return jar, "jar", jwt_payload(token_of_jar(jar))
    if form == "empty":
        # 空密钥：凭据**不进 Bearer**，而进 `X-Channel-Options.cookie`（脚本的第二种取值来源）。
        # 归属核对只能靠 jar 自己的 `token=`；jar 里没有它 ⇒ 无账号 id ⇒ 门判定必然是
        # `unclear`（这是对的：那种情况脚本会落到访客门）。
        cookie = os.environ.get("QWEN_COOKIE", "").strip()
        if not cookie:
            print("拒绝执行：请用环境变量 QWEN_COOKIE 传要放进渠道选项的整串 cookie。")
            return None
        return "", "empty", jwt_payload(token_of_jar(cookie))
    account = os.environ.get("QWEN_ACCOUNT", "").strip()
    password = os.environ.get("QWEN_PASSWORD", "").strip()
    if not account or not password:
        print("拒绝执行：请用环境变量 QWEN_ACCOUNT / QWEN_PASSWORD 传凭据（不落盘）。")
        return None
    print(f"[A] 独立 signin 验凭据（零额度）：{account[:6]}…@{account.split('@')[-1]}")
    token, note = sign_in(account, password)
    if not token:
        print("   ❌ 登录失败：", note)
        print("   ⇒ 凭据/出口有问题，**未发任何生成请求**。")
        return None
    payload = jwt_payload(token)
    print(f"   ✅ 登录成功：account_id={str(payload.get('id'))[:8]}… exp={payload.get('exp')}")
    return f"{account}|{password}", "pair", payload


# ------------------------------------------------------------------ trace 旁路

class TapLogfire:
    """把 `ctx.logfire` 旁路一份本地副本，同时**照旧转发**给真 Logfire。

    "走的是哪个门"最直接的证据是脚本自己记的那条 note（`stage=request` 里的 `chat_mode`），
    而它只进 trace。这样既拿到证据，又不动真上报链路。
    """

    def __init__(self, real):
        self.real = real
        self.notes: list[dict] = []

    def info(self, message, **attrs):
        self.notes.append({"message": message, **attrs})
        return self.real.info(message, **attrs)

    def span(self, *a, **kw):
        return self.real.span(*a, **kw)


# ------------------------------------------------------------------------ main

def ensure_dir(path: Path) -> None:
    """建目录，**在沙箱 shim 下也要稳**（2026-09-19 实测被它咬过一次）。

    裸 `mkdir(exist_ok=True)` 在 WorkBuddy 的 harness 里会走 `sitecustomize` 的 brokered
    `mkdir`，对**已存在**的目录抛 `PermissionError: EEXIST`（`exist_ok` 拦不住它，因为异常
    是 shim 自己抛的）⇒ 一个已经跑完、已经花掉一发的请求，会因为"建一个已存在的目录"
    崩在落盘那一步，产物与 URL 全丢（**当时真的丢了，靠只读会话历史才捞回来**）。
    所以：失败后**以磁盘为准**复核，目录在就继续。
    """
    if path.is_dir():
        return
    try:
        path.mkdir(parents=True, exist_ok=True)
    except (PermissionError, FileExistsError):
        if not path.is_dir():
            raise


def door_of(owner: str, account_id: str) -> str:
    """产物归属 vs 凭据账号 id ⇒ `"account"` 还是 `"unclear"`。

    抽成函数只为可测：这条判定是本工具唯一"结论性"的逻辑，而它又必须在**两侧都非空**时才敢
    下结论 —— 产物 URL 里没有 `key=`（或 key 不是 JWT）时 `owner` 是空串，此时说"账号门"
    等于凭空断言；两个空串更不许相等就成立。
    """
    if owner and account_id and owner == account_id:
        return "account"
    return "unclear"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--key-form", choices=("jwt", "pair", "jar", "empty"), default="jwt",
                    help="凭据形态（默认 jwt）")
    ap.add_argument("--yes", action="store_true", help="确认消耗 1 发账号额度")
    ap.add_argument("--tier", default="1K", help="档位（默认 1K）")
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--report-dir", default="", help="产物与 meta 的落点")
    ap.add_argument("--case", choices=("t2i", "multi"), default="t2i",
                    help="用例：t2i（默认）或 multi（多图生图，需 --input-image）")
    ap.add_argument("--input-image", action="append", default=[],
                    help="垫图本地文件路径（可重复；multi 需要 ≥2 张）→ 转 data URI 进请求体")
    ap.add_argument("--proxy", default="",
                    help="让本渠道走出站代理，如 http://127.0.0.1:11080"
                         "（本地 SOCKS→HTTP 桥见 tools/socks_http_bridge.py）；"
                         "空＝不走代理（默认，与存量渠道一致）")
    args = ap.parse_args(argv)

    resolved = resolve_credential(args.key_form)
    if resolved is None:
        return 2
    bearer, label, payload = resolved
    account_id = str(payload.get("id") or "")
    exp = payload.get("exp")
    if exp:
        when = datetime.datetime.fromtimestamp(int(exp)).strftime("%Y-%m-%d %H:%M")
        left = (int(exp) - time.time()) / 86400
        print(f"[A] 凭据自述：account_id={account_id[:8]}… exp={exp}（{when}，"
              f"{'已过期' if left < 0 else f'{left:.1f} 天后过期'}）")
    if exp and int(exp) < time.time():
        print("   ❌ token 已过期，发出去只会白花一发。")
        return 1

    report = Path(args.report_dir) if args.report_dir else (
        REPO / "reports" / f"{time.strftime('%Y-%m-%d')}_qwen-key-{label}")
    if not args.yes:
        print(f"计划：`{label}` 形态 × {args.tier} × t2i = **1 发真实生成**。确认后加 --yes。")
        return 2

    # ---- B. 经适配器真发 1 发 -------------------------------------------
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
        # Only meaningful when the run passes `--proxy`, and harmless otherwise:
        # the proxy host must sit on the allowlist, while the qwen OSS upload
        # stays off the proxy — the deployment shape, so `--proxy` exercises it.
        upstream_proxy_allowlist="127.0.0.1,localhost",
        upstream_proxy_bypass_hosts="*.aliyuncs.com",
    )
    # 凭据放哪里**由形态决定**，这也是这两档唯一的实质差别：
    #   jwt / pair / jar ⇒ 放密钥（引擎会按 X-Auth-Emit 决定发不发 Authorization）
    #   empty          ⇒ 放 `X-Channel-Options.cookie`，密钥留空
    options = {"cookie": os.environ.get("QWEN_COOKIE", "").strip()} if label == "empty" else {}
    headers = {
        "X-Upstream-Url": UPSTREAM,
        "X-Script-Ref": "qwen/images@v1",
        "X-Channel-Options": json.dumps(options),
        "X-Auth-Emit": "none",
    }
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    if args.proxy:
        # 与 guest 冒烟同款：代理经请求头逐发声明（适配器按白名单放行，见上方 Settings）
        headers["X-Upstream-Proxy"] = args.proxy
        print(f"经代理：{args.proxy}")
    body = {"model": "qwen-image", "size": args.tier, "prompt": args.prompt}
    if args.case == "multi":
        if len(args.input_image) < 2:
            print("拒绝执行：multi 用例需要至少两张 --input-image（一张等于 i2i，测不出多图）。")
            return 2
        refs = []
        for path in args.input_image:
            raw = Path(path).read_bytes()
            mime = ("image/png" if raw.startswith(b"\x89PNG\r\n\x1a\n")
                    else "image/jpeg" if raw.startswith(b"\xff\xd8\xff") else "image/png")
            refs.append("data:" + mime + ";base64," + base64.b64encode(raw).decode())
            print(f"   垫图 {Path(path).name}：{len(raw)}B {mime}")
        body["image"] = refs
        body["prompt"] = "把这几张图的主体合成到同一个场景里，风格统一"
    print(f"[B] 经适配器发 1 发：`{label}` × {args.tier} × {args.case}"
          f"{'（垫图 ' + str(len(args.input_image)) + ' 张）' if args.case == 'multi' else ''}")
    sent_at = time.time()
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/v1/images/generations", headers=headers, json=body)
    elapsed = time.time() - sent_at
    print(f"   HTTP {resp.status_code}  {elapsed:.1f}s  "
          f"X-Request-Id={resp.headers.get('x-request-id')} "
          f"X-Script-Sha256={str(resp.headers.get('x-script-sha256'))[:12]}")
    # 先把两个目录建好：**失败也是证据**（瞬时过载 / 额度耗尽 / 风控是三件不同的事，
    # 判据不同），所以失败也要落一份记录再退出。
    ensure_dir(report / "outputs")
    ensure_dir(report / "meta")
    if resp.status_code != 200:
        (report / "meta" / f"{label}_{args.tier.lower()}_failed.json").write_text(
            json.dumps({"key_form": label, "tier": args.tier, "http_status": resp.status_code,
                        "elapsed_s": round(elapsed, 1),
                        "request_id": resp.headers.get("x-request-id"),
                        "body": resp.text[:600], "notes": tap.notes},
                       ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("   ❌", resp.text[:300])
        print(f"   （失败记录已落盘 {report}/meta/{label}_{args.tier.lower()}_failed.json）")
        return 1
    doc = resp.json()
    urls = [i.get("url") for i in (doc.get("data") or []) if i.get("url")]
    # 🔴 产物 URL **只打控制台、不落盘**（带签名的链接等同凭据），但必须打出来：
    # 2026-09-19 就发生过"请求 200、落盘时崩了"（mkdir 被沙箱 shim 拒），
    # 那一发产物是靠这条 URL 之外的路（只读会话历史）才捞回来的 —— 有 URL 就不用绕。
    for u in urls:
        print("   产物 URL（仅控制台，不落盘）:", u)

    # ---- C. 门归属 -------------------------------------------------------
    print("[C] 门归属（决定是账号门还是回退到访客门）")
    owner = ""
    if urls:
        m = re.search(r"[?&]key=([^&]+)", urls[0])
        if m:
            owner = str(jwt_payload(m.group(1)).get("resource_user_id") or "")
    door = door_of(owner, account_id)
    print(f"   产物 resource_user_id = {owner[:8]}…  凭据 account_id = {account_id[:8]}…")
    print("   ✅ **一致 ⇒ 走的是账号门**" if door == "account"
          else "   🔴 **不一致 ⇒ 没走账号门**（多半回退到了访客门）")

    # ---- D. 脚本上报的 trace ---------------------------------------------
    print("[D] 脚本上报的 trace（stage=…）")
    modes = []
    for note in tap.notes:
        shown = {k: v for k, v in note.items() if k != "message"}
        if note.get("stage") == "request" and shown.get("chat_mode"):
            modes.append(shown["chat_mode"])
        print("   ", note["message"], json.dumps(shown, ensure_ascii=False)[:200])
    if not [n for n in tap.notes if n.get("stage") == "auth"]:
        print("    （无 `stage=auth` note：该形态不发 signin —— 与 pair 形态的对照点）")
    if modes:
        print(f"   ⇒ 脚本自报 chat_mode={modes[0]}（`normal`＝账号门，`guest`＝访客门）；"
              f"与 [C] 的结论{'一致' if (modes[0] == 'normal') == (door == 'account') else '**冲突**'}")

    # ---- 产物与 meta（凭据与签名 URL 都不落盘）---------------------------
    ensure_dir(report / "outputs")
    ensure_dir(report / "meta")
    meta = {
        "key_form": label,
        "case": args.case,
        "input_images": len(args.input_image),
        "account_id_prefix": account_id[:8],
        "token_exp": exp,
        "tier": args.tier, "case": "t2i",
        "http_status": resp.status_code, "elapsed_s": round(elapsed, 1),
        "request_id": resp.headers.get("x-request-id"),
        "script_sha256_prefix": str(resp.headers.get("x-script-sha256"))[:12],
        "door": door, "product_owner_prefix": owner[:8],
        "notes": tap.notes,
    }
    if urls:
        import httpx
        with httpx.Client(trust_env=False, timeout=60, follow_redirects=True) as c:
            r = c.get(urls[0], headers={"User-Agent": q.UA})
        raw = r.content
        magic = ("PNG" if raw.startswith(b"\x89PNG\r\n\x1a\n")
                 else "JPEG" if raw.startswith(b"\xff\xd8\xff") else "?")
        try:
            import io as _io
            from PIL import Image
            im = Image.open(_io.BytesIO(raw))
            dims = f"{im.width}x{im.height}"
        except Exception:  # noqa: BLE001
            dims = "?"
        (report / "outputs" / f"{label}_{args.tier.lower()}_{args.case}.png").write_bytes(raw)
        meta["product"] = {"file": f"outputs/{label}_{args.tier.lower()}_{args.case}.png",
                           "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                           "magic": magic, "dims": dims}
        print(f"   产物已落盘 {meta['product']['file']}：{len(raw)}B magic={magic} {dims}")
    (report / "meta" / f"{label}_{args.tier.lower()}_{args.case}_run.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"   meta 已落盘 {report}/meta/{label}_{args.tier.lower()}_{args.case}_run.json"
          f"（不含凭据、不含签名 URL）")
    return 0 if door == "account" else 1


if __name__ == "__main__":
    raise SystemExit(main())
