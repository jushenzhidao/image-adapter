#!/usr/bin/env python3
"""qwen 上传链路探针：`getstsToken` **要不要每文件一次**？（零额度）

`getstsToken` 只发凭证、不产生生成调用 ⇒ **不耗额度**（脚本 docstring 原话：
"Free: the upload link is not metered, only the generation call is"）。所以这个探针
可以随便跑，用来回答三个只有实测能回答的问题：

  A. **同一份请求调多次，返回一样吗？** 若 `file_id`/`file_path`/`file_url` 每次都不同
     ⇒ 每次调用是在为**一个对象**铸身份，而不只是"借一套凭证"。
  B. **并发调会不会被限流？** 5 并发（= `fanout_concurrency`）各自的成败与耗时 ——
     这是垫图并发化之后新增的暴露面。
  C. **凭证能不能跨文件复用？** 用第 1 次调用拿到的 STS 凭证，去签第 2 次调用给的对象路径。
     成功 ⇒ 凭证是**前缀/账号级**的；失败（403 SignatureDoesNotMatch/AccessDenied）
     ⇒ 凭证与该次返回的 key 绑定。
     🚩 这一步会**真往厂商 bucket 写两个 1×1 PNG**（与前端正常上传同类、体积极小）。

凭据只从环境变量读，不落盘：

    QWEN_JWT='eyJ…' .venv/bin/python tools/probe_qwen_sts.py

⚠️ 本探针**不发任何生成请求 ⇒ 不花额度**；但 C 步会写入对象，跑它是你的选择。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import importlib.util
import json
import os
import pathlib
import sys
import time

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

_spec = importlib.util.spec_from_file_location(
    "qwen_images_v1", REPO / "script_store" / "qwen" / "images@v1.py")
q = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(q)

if importlib.util.find_spec("httpx") is None:  # pragma: no cover - 环境缺失才走
    raise SystemExit("需要 httpx（.venv 里已有）")
import httpx  # noqa: E402

#: 1×1 PNG —— 与集成测试里那份同源，足够小。
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="
)
FILENAME = "input.png"


def _headers(jwt: str) -> dict:
    """与脚本 `_headers()` 同形的浏览器请求头（少一个都可能吃 WAF 挑战页）。"""
    h = {"Accept": "application/json", "Content-Type": "application/json",
         "User-Agent": q.UA, "Origin": "https://chat.qwen.ai",
         "Referer": "https://chat.qwen.ai/c/new-chat", "source": "web",
         "version": "0.2.0", "bx-v": "2.5.37", "Connection": "keep-alive",
         "X-Accel-Buffering": "no", "X-Request-Id": "sts-probe",
         "Cookie": "token=" + jwt}
    h.update(q.BROWSER_HINTS)
    h.update({"Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
              "Sec-Fetch-Site": "same-origin"})
    return h


async def fetch_sts(client, headers, *, label):
    """一次 getstsToken；返回 `(status, doc, 秒)`。"""
    started = time.time()
    r = await client.post("https://chat.qwen.ai/api/v2/files/getstsToken",
                          json={"filename": FILENAME, "filesize": str(len(PNG_1X1)),
                                "filetype": "image"}, headers=headers)
    return r.status_code, (r.json() if r.status_code == 200 else r.text[:200]), time.time() - started


def _shape(doc) -> dict:
    """只留**可比较**的字段（凭证本身不打印）。"""
    d = (doc or {}).get("data") or {}
    return {k: d.get(k) for k in ("file_id", "file_path", "file_url", "bucketname",
                                 "region", "endpoint")}


async def _put_one(client, sts) -> tuple[int, float]:
    """用**该份凭证**往它自己的 key PUT 一个 68 字节的 1×1 PNG。返回 (状态, 秒)。"""
    host, region, scheme = q._oss_target(sts)
    bucket = str(sts.get("bucketname") or "")
    path = str(sts.get("file_path") or "")
    url = scheme + "://" + host + q._encode_path(path)
    hdrs = q._v4_headers(sts, host, "/" + bucket + "/" + path, region, PNG_1X1, "image/png")
    started = time.time()
    r = await client.put(url, content=PNG_1X1,
                         headers={**hdrs, "Content-Length": str(len(PNG_1X1))})
    return r.status_code, time.time() - started


async def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-put", action="store_true", help="跳过 C 步（不写对象）")
    ap.add_argument("--concurrent", default="5",
                    help="B 步的并发数，可给逗号列表做扫描（如 8,12,16,20）")
    ap.add_argument("--put-burst", default="",
                    help="额外做「并发 PUT」：名额列表（如 8,16,20）。"
                         "每个名额会**真往厂商 bucket 写 N 个 68 字节的 1×1 PNG**")
    args = ap.parse_args(argv)

    jwt = os.environ.get("QWEN_JWT", "").strip()
    if not jwt:
        print("拒绝执行：请用环境变量 QWEN_JWT 传 token（不落盘）。")
        return 2
    headers = _headers(jwt)
    timeout = httpx.Timeout(60.0)
    async with httpx.AsyncClient(trust_env=False, timeout=timeout,
                                 follow_redirects=True) as client:

        # ---- A. 同一份请求，串行三次：返回是否逐次不同？------------------
        print("[A] 同一份 {filename, filesize, filetype} 串行调 3 次")
        res, raw = [], []
        for i in range(3):
            status, doc, secs = await fetch_sts(client, headers, label=f"A{i}")
            shape = _shape(doc) if status == 200 else {"error": doc}
            # `raw` 留**完整**文档：C 步要用它里面的凭证字段去签，而 `shape` 是
            # 打印/比较用的裁剪版（凭证不进日志）。混用这两者正是本探针第一版的 bug。
            res.append(shape)
            raw.append((doc or {}).get("data") or {})
            print(f"   #{i + 1} HTTP {status} {secs:.2f}s  file_id={str(shape.get('file_id'))[:12]}… "
                  f"file_path={shape.get('file_path')}")
            print(f"         file_url 的查询串: {str(shape.get('file_url')).split('?')[-1][:90]}…")
            await asyncio.sleep(0.4)
        ids = [s.get("file_id") for s in res]
        paths = [s.get("file_path") for s in res]
        print(f"   ⇒ file_id 三次{'各不相同' if len(set(ids)) == 3 else '有重复'}；"
              f"file_path 三次{'各不相同' if len(set(paths)) == 3 else '有重复'}")
        print(f"   ⇒ 响应里有没有独立的过期字段: "
              f"{sorted(set().union(*[set(s) for s in res]))}")

        # ---- B. 并发 STS：扫一遍名额：限流了没有 ---------------------------
        for token in str(args.concurrent).split(","):
            token = token.strip()
            if not token:
                continue
            n = max(1, int(token))
            started = time.time()
            got = await asyncio.gather(*[fetch_sts(client, headers, label=f"B{i}")
                                         for i in range(n)])
            wall = time.time() - started
            codes = [g[0] for g in got]
            slow = [round(g[2], 2) for g in got]
            print(f"[B] STS 并发 {n:3d}：HTTP {sorted(set(codes))} 墙钟 {wall:.2f}s "
                  f"单发 {sorted(slow)[-1]:.2f}s(最慢) 中位 "
                  f"{sorted(slow)[len(slow) // 2]:.2f}s"
                  f"{'   ⇒ 全部 200' if set(codes) == {200} else '   ⇒ ⚠ 出现非 200'}")
            await asyncio.sleep(1.0)

        # ---- B2. 并发 PUT：真的写对象（每张自带一份凭证）-------------------
        for token in str(args.put_burst).split(","):
            token = token.strip()
            if not token:
                continue
            n = max(1, int(token))
            minted = await asyncio.gather(*[fetch_sts(client, headers, label=f"C{i}")
                                            for i in range(n)])
            creds, why = [], []
            for status, doc, _ in minted:
                d = (doc or {}).get("data") if isinstance(doc, dict) else None
                if status == 200 and d and d.get("access_key_secret"):
                    creds.append(d)
                else:
                    # 🔴 记住**为什么**没铸到：200 也可能是拒绝（本厂商的两种投递方式），
                    # 不显示原因的话，"0/8 份凭证"会被误读成脚本 bug。
                    if isinstance(doc, dict):
                        why.append(f"HTTP{status} success={doc.get('success')} "
                                   f"code={(d or {}).get('code') or (d or {}).get('details')}")
                    else:
                        why.append(f"HTTP{status} {str(doc)[:60]}")
            print(f"[C] PUT 并发 {n:3d}：先铸到 {len(creds)}/{n} 份凭证", end="")
            if why:
                print(f"\n     首次失败原因：{why[0]}（共 {len(why)} 次失败）", end="")
            if not creds:
                print(" ⇒ 跳过（没铸到凭证）")
                continue
            started = time.time()
            res = await asyncio.gather(*[_put_one(client, c) for c in creds])
            wall = time.time() - started
            codes = [r[0] for r in res]
            slow = sorted(round(r[1], 2) for r in res)
            print(f" → PUT HTTP {sorted(set(codes))} 墙钟 {wall:.2f}s "
                  f"最慢 {slow[-1]:.2f}s 中位 {slow[len(slow) // 2]:.2f}s"
                  f"{'   ⇒ 全部 2xx' if all(200 <= c < 300 for c in codes) else '   ⇒ ⚠ 有非 2xx'}")
            await asyncio.sleep(1.0)

        # ---- C. 凭证能否跨文件复用 ----------------------------------------
        if args.no_put:
            print("[C] 跳过（--no-put）")
            return 0
        if len(res) < 2 or not res[1].get("file_path"):
            print("[C] 跳过：前两步没拿到两个可用的 file_path")
            return 1
        sts_a, path_b = raw[0], res[1]["file_path"]
        print("[C] 用 #1 的凭证签 #2 的对象路径（跨文件复用测试）")
        host, region, scheme = q._oss_target(sts_a)
        bucket = str(sts_a.get("bucketname") or "").strip()
        url = scheme + "://" + host + q._encode_path(path_b)
        hdrs = q._v4_headers(sts_a, host, "/" + bucket + "/" + path_b, region,
                             PNG_1X1, "image/png")
        r = await client.put(url, content=PNG_1X1, headers={**hdrs, "Content-Length": str(len(PNG_1X1))})
        print(f"   PUT {path_b} → HTTP {r.status_code}")
        print(f"   ⇒ {'凭证可跨文件复用（前缀/账号级）' if r.status_code < 300 else '凭证与本次返回的 key 绑定，跨文件复用被拒'}")
        if r.status_code >= 300:
            print("   OSS 原话:", r.text[:300])
        else:
            # 回读一次确认对象真的在（也告诉我们直链是否可用）
            r2 = await client.get(res[1]["file_url"] or url)
            print(f"   回读该路径: HTTP {r2.status_code} {r2.headers.get('content-type')} "
                  f"{len(r2.content)}B")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
