#!/usr/bin/env python3
"""qwen 的 `RateLimited` 是**按出口 IP** 还是**按账号**？——同窗口换出口对照（零额度）。

为什么值得单独测：处置完全不同 ——
  * **按 IP** ⇒ 换出口（代理池 / 系统代理）即可继续，等冷却只是备选；
  * **按账号** ⇒ 换出口没用，只能等窗口（本仓在 `signin` 上实测过**两者都有**：
    signin 的墙是 **IP 级**（换出口才恢复），而 x5sec 是按**账号**记窗）。

手法：先用脚本自己的头形状把限额**打出来**（`getstsToken` 连打直到 `success:false`），
然后**同一个窗口内**立刻分别从三个出口各发一次，看谁还能拿到凭证。

⚠️ 全程**不发生成请求 ⇒ 零额度**；但会在几分钟内连打几十次 `getstsToken`（该端点免费）。
⚠️ 只跑一轮：命中后连发只会延长窗口（本仓既有结论），目的是**判定归属**，不是压测。

用法：
    QWEN_JWT='eyJ…' .venv/bin/python tools/probe_qwen_sts_egress.py
    QWEN_JWT='eyJ…' .venv/bin/python tools/probe_qwen_sts_egress.py --socks socks5h://pool.livetest.cn:2088
"""
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import pathlib
import socket
import sys
import time

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(REPO))

import token_service as ts  # noqa: E402

HOST = "chat.qwen.ai"
PATH = "/api/v2/files/getstsToken"
IP_ECHO = ("api.ipify.org", "/")
PNG_LEN = 68


def _headers(jwt: str) -> dict:
    return {
        "Accept": "application/json", "Content-Type": "application/json",
        "Origin": f"https://{HOST}", "Referer": f"https://{HOST}/c/new-chat",
        "source": "web", "version": "0.2.0", "bx-v": "2.5.37",
        "Accept-Language": "zh-CN,zh;q=0.9", "Timezone": "Thu Sep 18 2026 12:00:00 GMT+0800",
        "X-Request-Id": "egress-probe", "Cookie": "token=" + jwt,
        "sec-ch-ua": '"Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"',
        "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"macOS"',
        "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin",
    }


def _dial(socks_url: str | None) -> socket.socket:
    if socks_url:
        return ts.Socks5Dialer(socks_url, timeout=40).open(HOST)
    return socket.create_connection((HOST, 443), 40)


def _mint_guest() -> dict:
    """现铸一份**访客身份**（Playwright，与 `tools/qwen_guest_smoke.py` 同路）。

    用途是把「按账号」与「按出口 IP」分开：访客与账号**在同一台机、同一个出口 IP** 上，
    所以"账号被拒而访客照旧"只能解释成**按账号/身份**。
    """
    sys.path.insert(0, str(REPO / "tools"))
    import identity_service as ids
    minter = ids.BrowserMinter(channel="chrome", headless=True, settle_ms=7000)
    try:
        return minter.mint()
    finally:
        minter.close()


def _getsts_guest(socks_url: str | None, ident: dict,
                  timeout: float = 30.0) -> tuple[int, dict, float]:
    """用访客身份发一次 getstsToken（无 token= 的 jar + bx-ua/bx-umidtoken）。"""
    body = json.dumps({"filename": "input.png", "filesize": str(PNG_LEN),
                       "filetype": "image"}).encode()
    head = _headers("")           # 不带 token=
    head.pop("Cookie", None)
    head["Cookie"] = ident["cookie"]
    head["bx-ua"] = ident["bx_ua"]
    head["bx-umidtoken"] = ident["bx_umidtoken"]
    head["Referer"] = f"https://{HOST}/c/guest"
    started = time.time()
    status, _, payload = ts.http_over_socket(
        _dial(socks_url), HOST, "POST", PATH, body=body, headers=head, timeout=timeout)
    doc = json.loads(payload.decode("utf-8", "replace") or "{}")
    data = doc.get("data") if isinstance(doc.get("data"), dict) else {}
    return status, dict(data or {}), time.time() - started


def _getsts(socks_url: str | None, jwt: str, timeout: float = 30.0,
            head: dict | None = None) -> tuple[int, dict, float]:
    """发一次 getstsToken。返回 `(http 状态, data dict, 秒)`；拒绝时 data 里带 code。

    `head` 允许调用方换一套**请求形状**（例如补齐设备指纹与预热 cookie）——
    形状是这轮要判的自变量之一。
    """
    body = json.dumps({"filename": "input.png", "filesize": str(PNG_LEN),
                       "filetype": "image"}).encode()
    started = time.time()
    status, _, payload = ts.http_over_socket(
        _dial(socks_url), HOST, "POST", PATH, body=body, headers=head or _headers(jwt),
        timeout=timeout)
    doc = json.loads(payload.decode("utf-8", "replace") or "{}")
    data = doc.get("data") if isinstance(doc.get("data"), dict) else {}
    data = dict(data or {})
    data["_success"] = doc.get("success")
    return status, data, time.time() - started


def _egress_ip(socks_url: str | None) -> str:
    """这个出口的**公网 IP**（证明"换了出口"不是自说自话）。"""
    host, path = IP_ECHO
    try:
        sock = ts.Socks5Dialer(socks_url, timeout=40).open(host) if socks_url \
            else socket.create_connection((host, 443), 20)
        _, _, payload = ts.http_over_socket(sock, host, "GET", path, timeout=20)
        return payload.decode("utf-8", "replace").strip()[:40]
    except Exception as exc:  # noqa: BLE001
        return f"<取不到: {type(exc).__name__}>"


def _verdict(data: dict) -> str:
    if data.get("access_key_secret"):
        return "OK"
    return f"拒绝({data.get('code') or data.get('details') or '未知'})"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--socks", default=os.environ.get("QWEN_SOCKS", ""),
                    help="池子串（socks5h://host:port）；不填则只用直连+系统代理")
    ap.add_argument("--burst", type=int, default=40, help="直连上连打多少次（默认 40，命中即停）")
    ap.add_argument("--full-browser", action="store_true",
                    help="账号冷测时发**完整浏览器形状**：预热的 acw_tc/x-ap 一并进 Cookie，"
                         "并带上设备指纹 bx-ua / bx-umidtoken（对照「只带 token」的薄形状）")
    ap.add_argument("--account-count", type=int, default=0,
                    help="**账号**冷测预算：从 1 开始逐次调 `getstsToken` 到命中即停，"
                         "**不做恢复阶梯**（命中后连发只会延长窗口）")
    ap.add_argument("--guest-egress", choices=("direct", "system"), default="direct",
                    help="访客冷测走哪个出口（默认 direct；system = 系统代理，另一个公网 IP）")
    ap.add_argument("--guest-count", type=int, default=0,
                    help="**数准预算**：现铸一份访客身份（干净窗口），从 1 开始逐次调 "
                         "`getstsToken` 直到命中，然后做恢复阶梯（30/60/90/120s 各一发）")
    ap.add_argument("--guest", action="store_true",
                    help="额外的判定臂：先铸一份**访客身份**（同机同 IP），"
                         "命中后看它是否照旧能铸凭证 ⇒ 区分「按账号」与「按出口 IP」")
    args = ap.parse_args(argv)

    jwt = os.environ.get("QWEN_JWT", "").strip()
    # ⚠️ 访客计数模式**不需要**账号 token（它自带一份访客身份）⇒ 守卫不能放在这里一刀切，
    # 否则"用访客身份测频控"会因为缺 QWEN_JWT 直接被拒（2026-09-19 实测踩到）。
    if not jwt and not args.guest_count:
        print("拒绝执行：请用环境变量 QWEN_JWT 传 token（不落盘）；"
              "或改用 --guest-count（访客身份自带凭据）。")
        return 2

    system = None
    try:
        sys.path.insert(0, str(REPO / "tools"))
        import qwen_egress_check as qe
        system = qe.local_system_proxy()
    except Exception as exc:  # noqa: BLE001
        print("（读系统代理失败：", type(exc).__name__, "）")
    egresses: list[tuple[str, str | None]] = [("直连", None)]
    if system:
        egresses.append(("系统代理", system))
    if args.socks:
        egresses.append(("池子", args.socks))

    # ---- 模式 A0：账号冷测（逐次计数，命中即停，不加重窗口）---------------
    if args.account_count:
        if not jwt:
            print("拒绝执行：账号冷测需要 QWEN_JWT。")
            return 2
        head = _headers(jwt)
        if args.full_browser:
            # ① 预热拿真实的 acw_tc / x-ap 进 jar（浏览器有这个）
            try:
                warm = {"Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                                   "image/avif,image/webp,*/*;q=0.8"),
                        "User-Agent": ts.UA, "Origin": f"https://{HOST}",
                        "Referer": f"https://{HOST}/auth", "source": "web",
                        "version": "0.2.0", "bx-v": "2.5.37"}
                _, hdr, _ = ts.http_over_socket(
                    socket.create_connection((HOST, 443), 30), HOST, "GET", "/auth",
                    headers=warm, timeout=30)
                extra = []
                for raw in hdr.get("set-cookie", []):
                    name, _, rest = raw.partition("=")
                    val = rest.split(";", 1)[0].strip()
                    if name.strip() and val:
                        extra.append(f"{name.strip()}={val}")
                if extra:
                    head["Cookie"] = head["Cookie"] + "; " + "; ".join(extra)
                    print(f"   [形状] 预热 cookie 已进 jar：{len(extra)} 项")
            except Exception as exc:  # noqa: BLE001
                print(f"   [形状] 预热失败（继续）：{type(exc).__name__}")
            # ② 设备指纹（bx-ua / bx-umidtoken）—— 浏览器一定有，我此前的薄形状没有
            try:
                ident = _mint_guest()
                head["bx-ua"] = ident["bx_ua"]
                head["bx-umidtoken"] = ident["bx_umidtoken"]
                print(f"   [形状] 已带设备指纹：bx_ua={len(ident['bx_ua'])} "
                      f"umid={ident['bx_umidtoken'][:8]}…")
            except Exception as exc:  # noqa: BLE001
                print(f"   [形状] 取指纹失败（继续）：{type(exc).__name__}")
        print("[A0] 账号冷测：逐次调用，命中即停（不做恢复阶梯，避免延长窗口）；"
              f"形状={'完整浏览器' if args.full_browser else '薄（仅 token）'}")
        for i in range(1, args.account_count + 1):
            try:
                status, data, secs = _getsts(None, jwt, head=head)
            except Exception as exc:  # noqa: BLE001
                print(f"   #{i:2d} 异常 {type(exc).__name__}: {str(exc)[:80]}")
                continue
            ok = bool(data.get("access_key_secret"))
            print(f"   #{i:2d} HTTP {status} {secs:.2f}s "
                  f"{'OK' if ok else _verdict(data)}")
            if not ok:
                print(f"   ⇒ **账号这次冷窗口的预算是 {i - 1} 次**（第 {i} 次被拒）")
                return 0
        print(f"   ⇒ 到 #{args.account_count} 仍未命中（预算 > {args.account_count}）")
        return 0

    # ---- 模式 A：用访客身份把预算数准（干净窗口）--------------------------
    if args.guest_count:
        egress_url = None
        if args.guest_egress == "system":
            import qwen_egress_check as qe
            egress_url = qe.local_system_proxy()
            if not egress_url:
                print("拒绝执行：--guest-egress system 但读不到系统代理。")
                return 2
        print(f"[A] 现铸访客身份（Playwright）—— 全新身份 ⇒ 窗口干净；"
              f"出口 = {args.guest_egress}{'（' + egress_url + '）' if egress_url else ''}")
        print(f"    该出口公网 IP：{_egress_ip(egress_url)}")
        ident = _mint_guest()
        print(f"    cookie={len(ident['cookie'])} bx_ua={len(ident['bx_ua'])} "
              f"umid={ident['bx_umidtoken'][:8]}…")
        trip = 0
        for i in range(1, args.guest_count + 1):
            try:
                status, data, secs = _getsts_guest(egress_url, ident)
            except Exception as exc:  # noqa: BLE001
                print(f"   #{i:2d} 异常 {type(exc).__name__}: {str(exc)[:80]}")
                continue
            ok = bool(data.get("access_key_secret"))
            print(f"   #{i:2d} HTTP {status} {secs:.2f}s "
                  f"{'OK' if ok else _verdict(data)}")
            if not ok:
                trip = i
                break
        if not trip:
            print(f"   ⇒ 到 #{args.guest_count} 仍未命中（预算 > {args.guest_count}）")
        else:
            print(f"   ⇒ **这个身份的预算是 {trip - 1} 次**（第 {trip} 次被拒）")
            print("[B] 恢复阶梯（每档一发，命中即停）")
            for wait in (30, 60, 90, 120):
                time.sleep(wait)
                try:
                    status, data, secs = _getsts_guest(egress_url, ident)
                    ok = bool(data.get("access_key_secret"))
                    print(f"   +{sum([30, 60, 90, 120][:[30,60,90,120].index(wait) + 1])}s "
                          f"HTTP {status} {secs:.2f}s {'✅ 恢复' if ok else _verdict(data)}")
                    if ok:
                        break
                except Exception as exc:  # noqa: BLE001
                    print(f"   +{wait}s 异常 {type(exc).__name__}")
            # 命中后**换回直连**再发一发：同身份跨出口若仍被拒 ⇒ 预算挂在身份上、与 IP 无关。
            try:
                status, data, secs = _getsts_guest(None, ident)
                ok = bool(data.get("access_key_secret"))
                print(f"   [跨出口] 换回直连发一发：HTTP {status} {secs:.2f}s "
                      f"{'✅ OK（⇒ 预算按 (身份,IP) 计）' if ok else '仍被拒（⇒ 预算挂身份、与 IP 无关）'}")
            except Exception as exc:  # noqa: BLE001
                print(f"   [跨出口] 异常 {type(exc).__name__}")
        return 0

    guest = None
    if args.guest:
        print("[0a] 先铸访客身份（Playwright，~10s）——它与账号**同机同 IP**，是判定归属的关键对照")
        try:
            guest = _mint_guest()
            print(f"    访客身份就绪：cookie={len(guest['cookie'])} "
                  f"bx_ua={len(guest['bx_ua'])} umid={guest['bx_umidtoken'][:8]}…")
        except Exception as exc:  # noqa: BLE001
            print("    ⚠ 访客身份铸造失败：", type(exc).__name__, str(exc)[:120])

    print("[0] 各出口的公网 IP（证明它们确实不同）")
    for name, url in egresses:
        print(f"   {name:8s} {url or '（直连）':44s} → {_egress_ip(url)}")

    print("[1] 各出口各发一次（基线，应全部 OK）")
    for name, url in egresses:
        try:
            status, data, secs = _getsts(url, jwt)
            print(f"   {name:8s} HTTP {status} {secs:.2f}s {_verdict(data)}")
        except Exception as exc:  # noqa: BLE001
            print(f"   {name:8s} 异常 {type(exc).__name__}: {exc}")

    print(f"[2] 直连连打到命中（上限 {args.burst}）—— 目的是把限额打出来")
    hit_at = 0
    for i in range(1, args.burst + 1):
        try:
            status, data, _ = _getsts(None, jwt)
        except Exception as exc:  # noqa: BLE001
            print(f"   #{i} 异常 {type(exc).__name__}")
            continue
        if not data.get("access_key_secret"):
            hit_at = i
            print(f"   #{i} 命中：HTTP {status} {_verdict(data)}")
            break
        if i % 10 == 0:
            print(f"   …#{i} 仍 OK")
    if not hit_at:
        print("   ⇒ 本轮没打出来（额度未被触发）⇒ 无法判定归属；可加大 --burst 重试")
        return 1

    print("[3] 🔴 同一窗口内换出口对照（决定性一步）")
    for name, url in egresses:
        try:
            status, data, secs = _getsts(url, jwt)
            print(f"   {name:8s} HTTP {status} {secs:.2f}s {_verdict(data)}")
        except Exception as exc:  # noqa: BLE001
            print(f"   {name:8s} 异常 {type(exc).__name__}: {exc}")
    if guest:
        try:
            status, data, secs = _getsts_guest(None, guest)
            ok = "OK" if data.get("access_key_secret") else _verdict(data)
            print(f"   {'访客身份':8s} HTTP {status} {secs:.2f}s {ok}"
                  f"   ← 与账号**同机同 IP**")
        except Exception as exc:  # noqa: BLE001
            print(f"   {'访客身份':8s} 异常 {type(exc).__name__}: {exc}")
    print("   判读：直连被拒而别的出口 OK ⇒ **按出口 IP**；"
          "账号被拒而**访客照旧** ⇒ **按账号/身份**（同机同 IP，换出口无用）；全部被拒 ⇒ 无法区分口径")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
