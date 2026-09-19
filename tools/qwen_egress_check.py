"""出口健康度巡检：现在有没有**能过 WAF 的出口**？

为什么需要它（2026-09-18 实测）：
  - 参考仓记着"写类端点有**累计效应**，命中即停手等冷却"；当天 16:40 直连还能过
    `POST /v2/chats/new`，17:14 同一出口就变滑块页 ⇒ **出口会被累计惩罚**；
  - 池子（数据中心 IP）当天 20/20 条连接**全部**滑块页 ⇒ 轮询代理解决不了；
  - 所以"能不能出图"的第一问是"现在有没有干净出口"，而不是"补哪个头"。

**只打免费的请求**（`GET /auth` + `POST /v2/chats/new`），**不发出任何生成请求 ⇒ 不耗额度**。

⚠️ **这个工具的判据成立吗？（2026-09-18 23:57 实证：成立）**
它是**匿名**请求（无 Cookie、无 `bx-*`），而"匿名被挑"与"出口被挑"本该是两件事，
所以专门做了一次单变量对照：现铸一个**真访客身份**（Playwright，cookie 1291 字符 +
`bx-ua` + `bx-umidtoken`），同一发请求分别用**直连**与**系统代理**两个出口、带身份与不带身份各打一次
⇒ **四种组合全部是同一张 `aliyun_waf_aa/bb` 滑块页**，而且**连 `GET /auth`（纯文档导航）都被挑**。
结论：这道门在**身份之前**就判了 ⇒ 本工具用匿名请求判出口是充分的，不必也不该再重复这个实验。
（想看原始对照用 `--quiet` 之外的输出即可；那次对照是一次性脚本，未入库。）

用法：
    python tools/qwen_egress_check.py                    # 直连 + 池子（QWEN_SOCKS 或 --socks）
    python tools/qwen_egress_check.py --rounds 5 --direct-only
退出码：0 = 至少一个出口通过；1 = 全被挡。
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import ssl
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import token_service as ts  # noqa: E402

HOST = "chat.qwen.ai"
UA = ts.UA


def _open(socks_url: str | None):
    if socks_url:
        return ts.Socks5Dialer(socks_url, timeout=40).open(HOST)
    return socket.create_connection((HOST, 443), 40)


def probe(socks_url: str | None) -> str:
    """一发免费的建会话，返回判定词。

    ⚠️ **头形状必须与脚本一致，否则得到的是假阴性**（2026-09-19 00:25 实测）：
    同一分钟、同一出口，只差三个头（`bx-v` / `Timezone` / `X-Request-Id`），
    `chats/new` 就从未通过/被挑战变成 **HTTP 200 + JSON**。
    早期"出口全被挡"的结论正是用缺这三个头的形状测出来的 ⇒ 已补上。
    """
    try:
        tls = ssl.create_default_context().wrap_socket(
            _open(socks_url), server_hostname=HOST)
        conn = http.client.HTTPSConnection(HOST, timeout=45)
        conn.sock = tls
    except Exception as exc:  # noqa: BLE001
        return f"隧道异常 {type(exc).__name__}"
    base = {"Accept-Language": "zh-CN,zh;q=0.9", "Origin": f"https://{HOST}",
            "Referer": f"https://{HOST}/c/guest", "source": "web",
            "version": "0.2.0", "User-Agent": UA,
            "sec-ch-ua": '"Chromium";v="152", "Not?A_Brand";v="24", '
                         '"Google Chrome";v="152"',
            "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"macOS"',
            # ↓ 这三个是脚本 `_headers()` 有、而本工具曾经没有的（见 docstring）
            "bx-v": "2.5.37",
            "Timezone": time.strftime("%a %b %d %Y %H:%M:%S GMT%z"),
            "X-Request-Id": str(uuid.uuid4())}
    try:
        conn.request("GET", "/auth", headers={
            **base, "Accept": "text/html,*/*;q=0.8",
            "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none"})
        conn.getresponse().read()
        conn.request("POST", "/api/v2/chats/new", body=json.dumps({
            "title": "New Chat", "models": ["qwen3.7-plus"], "chat_mode": "guest",
            "chat_type": "t2i", "timestamp": int(time.time() * 1000),
            "project_id": ""}).encode(),
            headers={**base, "Content-Type": "application/json",
                     "Accept": "application/json, text/plain, */*",
                     "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
                     "Sec-Fetch-Site": "same-origin"})
        resp = conn.getresponse()
        text = resp.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return f"请求异常 {type(exc).__name__}"
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    if resp.status == 200 and '"id"' in text:
        return "OK"
    if "aliyun_waf" in text:
        return "WAF 滑块页"
    if "RGV587" in text:
        return "x5sec"
    # 匿名请求被"正常拒绝"（§2.13：anon 在 chats/new 就被拒 Unauthorized）：
    # 这证明**出口与请求形状都是通的**，只是本工具不发身份 ⇒ 必须算"通"，
    # 否则又会得到"出口全挡"的假阴性。
    if resp.status == 200 and '"success":false' in text.replace(" ", ""):
        return "OK(匿名被拒=出口通)"
    return f"HTTP {resp.status}"


def local_system_proxy() -> str | None:
    """macOS 上系统代理（`scutil --proxy`）——**这才是浏览器实际用的出口**。

    2026-09-18 的教训：本机 python 的 socket 是**直连**（aiohttp/自建 socket 都不读系统代理），
    而 Chrome 走系统代理 ⇒ 两者出口不同（harness 117.88.13.57 / 本地代理 82.153.135.168），
    于是"浏览器能出图、脚本被滑块拦"看起来像客户端问题。所以巡检必须把这一个也测上。
    """
    if sys.platform != "darwin":
        return None
    import re as _re
    import subprocess
    try:
        out = subprocess.run(["scutil", "--proxy"], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001
        return None
    if "HTTPEnable : 1" not in out:
        return None
    host = _re.search(r"HTTPProxy : (\S+)", out)
    port = _re.search(r"HTTPPort : (\d+)", out)
    if not (host and port):
        return None
    return f"socks5h://{host.group(1)}:{port.group(1)}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--socks", default=os.environ.get("QWEN_SOCKS", ""),
                        help="轮询代理串（不填则只测直连）")
    parser.add_argument("--rounds", type=int, default=3,
                        help="每种出口测几条连接（每条新连接 = 换一次出口 IP）")
    parser.add_argument("--direct-only", action="store_true")
    parser.add_argument("--extra-proxy", action="append", default=[],
                        help="额外要测的出口（可重复）")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    targets: list[tuple[str, str | None]] = [("直连", None)]
    if args.socks and not args.direct_only:
        targets.append(("池子", args.socks))
    for url in args.extra_proxy:
        targets.append(("指定", url))
    system = local_system_proxy()
    if system and not args.direct_only:
        targets.append(("系统代理", system))

    healthy: list[str] = []
    for name, url in targets:
        for index in range(1, args.rounds + 1):
            verdict = probe(url)
            if verdict.startswith("OK"):
                healthy.append(f"{name}#{index}")
            if not args.quiet:
                print(f"  {name}#{index}: {verdict}")
            time.sleep(0.8)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    if healthy:
        print(f"[{stamp}] 有可用出口：{', '.join(healthy)} ⇒ 现在可以发一次生成（一个身份、一条连接、一发）")
        return 0
    names = "、".join(name for name, _u in targets)
    print(f"[{stamp}] 全部被挡（{names}）⇒ 继续等冷却，别连发")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
