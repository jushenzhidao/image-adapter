#!/usr/bin/env python3
"""量 qwen `file_id` 的生命周期（**零额度**：只 `getstsToken` + PUT + GET，不生成）。

**为什么要量**：参考仓的上传器按内容哈希缓存上传结果（`QwenFileUploader`，`cache_size=64`），
把「file_id 能用多久」留成了未复核项（其 §8.3 U-6）。本仓脚本**不缓存**，所以正确性不依赖它，
但要做「同一张图跨轮不重传」就必须先知道这个数。

**这个探针能量到 / 量不到什么**（先分清，免得把结论说过头）：

| 可观测量 | 手段 | 说明 |
|---|---|---|
| ① 上游**还认不认**这个 file_id | `POST /api/v2/files/getfilelink`（零额度） | 上游文件登记表还在不在 —— 这是缓存能否成立的前置 |
| ② 对象**还能不能被读**（匿名） | 直连 `https://<bucket>.<endpoint>/<path>`，**不带签名** | 桶若公开就是无上限观测窗；私有则一律 403 |
| ③ 老 `file_url` 是否过期 | 直连那条带 `x-oss-expires=300` 的 URL | 校准"URL 短效"是否真的在 OSS 层生效（参考仓实测：提交侧忽略它） |
| ④ **凭证**（那份 STS）还能不能签 | 用**同一份凭证**再 PUT 一次同路径 | 顺带量出 STS 自身的有效期 |
| ❌ `completions` **还接不接受**旧 file_id | 只能发**一次生成** | **本探针不做**。①②④ 是它的前置条件，不是它本身 |

用法（凭据只从环境变量读）：

    QWEN_JWT='eyJ…' .venv/bin/python tools/probe_qwen_fileid_lifetime.py --minutes 30
    QWEN_JWT='eyJ…' .venv/bin/python tools/probe_qwen_fileid_lifetime.py \
        --extra-file-id 56579d26-db95-4d23-addf-3fbf606bbb4f:3   # 已存在 N 分钟的 id（省一次上传）

结果按 JSONL 追加写 `--out`（默认 `reports/<日期>_qwen-fileid-lifetime/`）。
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
from datetime import datetime, timezone

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

_spec = importlib.util.spec_from_file_location(
    "qwen_images_v1", REPO / "script_store" / "qwen" / "images@v1.py")
q = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(q)

import httpx  # noqa: E402

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="
)
STS_URL = "https://chat.qwen.ai/api/v2/files/getstsToken"
LINK_URL = "https://chat.qwen.ai/api/v2/files/getfilelink"


def _headers(jwt: str) -> dict:
    h = {"Accept": "application/json", "Content-Type": "application/json",
         "User-Agent": q.UA, "Origin": "https://chat.qwen.ai",
         "Referer": "https://chat.qwen.ai/c/new-chat", "source": "web",
         "version": "0.2.0", "bx-v": "2.5.37", "Connection": "keep-alive",
         "X-Accel-Buffering": "no", "X-Request-Id": "fileid-lifetime",
         "Cookie": "token=" + jwt}
    h.update(q.BROWSER_HINTS)
    h.update({"Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
              "Sec-Fetch-Site": "same-origin"})
    return h


async def mint_and_put(client, headers) -> dict:
    """一次 getstsToken + PUT，作为 t0 的样本。返回不含凭证的**可记录**字段。"""
    r = await client.post(STS_URL, json={"filename": "input.png",
                                         "filesize": str(len(PNG_1X1)),
                                         "filetype": "image"}, headers=headers)
    doc = r.json() if r.status_code == 200 else {}
    # 🔴 **200 不等于成功**：这个厂商的拒绝也走 200（`{"success":false,"data":{...}}`，
    # 与脚本 `_fail_qwen_error` 收口的是同一件事）。第一版探针只看状态码 ⇒ token 签错时
    # 拿到的是错误体，然后在 `_v4_headers` 里以 `KeyError: 'access_key_secret'` 炸掉 ——
    # 一个把"凭据无效"伪装成"脚本有 bug"的假故障。
    if r.status_code != 200 or not doc.get("success"):
        raise RuntimeError(f"getstsToken 未成功（HTTP {r.status_code}）：{r.text[:240]}")
    sts = doc.get("data") or {}
    missing = [k for k in ("access_key_id", "access_key_secret", "security_token",
                           "bucketname", "file_path") if not sts.get(k)]
    if missing:
        raise RuntimeError(f"getstsToken 成功但缺字段 {missing}：{r.text[:200]}")
    host, region, scheme = q._oss_target(sts)
    bucket = str(sts.get("bucketname") or "")
    path = str(sts.get("file_path") or "")
    url = scheme + "://" + host + q._encode_path(path)
    hdrs = q._v4_headers(sts, host, "/" + bucket + "/" + path, region, PNG_1X1, "image/png")
    p = await client.put(url, content=PNG_1X1,
                         headers={**hdrs, "Content-Length": str(len(PNG_1X1))})
    return {"sts": sts, "host": host, "bucket": bucket, "path": path, "region": region,
            "url": url, "put_status": p.status_code, "put_text": p.text[:200],
            "public_url": scheme + "://" + host + q._encode_path(path), "scheme": scheme}


async def probe(client, headers, sample, age_min: float) -> dict:
    """一个样本在某个时刻的四项观测。"""
    sts = sample["sts"]
    out = {"at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
           "age_min": round(age_min, 2),
           "file_id": str(sts.get("file_id"))[:8] + "…",
           "file_path_tail": sample["path"].rsplit("/", 1)[-1]}

    # ① 上游还认不认这个文件 —— 🔴 字段名是 **fileUrl**（实测 `{"file_id": …}` 回
    # `RequestValidationError: Field 'fileUrl': Field required`）。所以这里问的是
    # 「上游还能不能为**这个对象**再发一条链接」，它是"缓存还能不能用"的前置。
    try:
        r = await client.post(LINK_URL, json={"fileUrl": sts.get("file_url") or sample["url"]},
                              headers=headers)
        full = r.text                      # 解析要用**完整**响应，落盘才截断
        body = full[:240]
        out["getfilelink"] = {"status": r.status_code, "body": body,
                              "has_url": "http" in body}
        # 🔴 光看 `getfilelink` 会**误判**：实测一个"只铸了凭证、从未 PUT"的 file_id 也回
        # `has_url=True` ⇒ 这个接口是**重签名器**，不是登记表查询。所以拿到它给的 URL 后
        # **必须真去取一次**，那才是"这个对象还在不在"的硬判据。
        link = ""
        if out["getfilelink"]["has_url"]:
            try:
                # 🔴 上一版在这里踩了坑：拿 `body`（已截断到 240 字符）去 `json.loads`，
                # 抛异常 ⇒ link 永远为空 ⇒ `relink_get` 恒为 None（观测量静默失效）。
                link = json.loads(full).get("data", {}).get("fileUrl") or ""
            except Exception as exc:  # noqa: BLE001
                out["relink_parse_error"] = type(exc).__name__
                link = ""
        if link:
            try:
                r2 = await client.get(link)
                out["relink_get"] = {"status": r2.status_code, "bytes": len(r2.content)}
            except Exception as exc:  # noqa: BLE001
                out["relink_get"] = {"error": type(exc).__name__}
        else:
            out["relink_get"] = {"status": None, "note": "没有可取的链接" if out["getfilelink"]["has_url"] else "未拿到链接"}
    except Exception as exc:  # noqa: BLE001
        out["getfilelink"] = {"error": type(exc).__name__}

    # ② 匿名直读（不给签名）—— 桶公开就有无上限观测窗
    try:
        r = await client.get(sample["public_url"])
        out["anon_get"] = {"status": r.status_code, "bytes": len(r.content)}
    except Exception as exc:  # noqa: BLE001
        out["anon_get"] = {"error": type(exc).__name__}

    # ③ 老 file_url：用**响应里那条真 URL**（带 x-oss-* 查询串），不是我拼的无签名地址
    try:
        r = await client.get(str(sts.get("file_url") or sample["url"]))
        out["old_url_get"] = {"status": r.status_code, "bytes": len(r.content),
                              "content_type": r.headers.get("content-type")}
    except Exception as exc:  # noqa: BLE001
        out["old_url_get"] = {"error": type(exc).__name__}

    # ④ 同一份凭证还能不能签（顺带量 STS 自身有效期）
    try:
        host = sample["host"]
        hdrs = q._v4_headers(sts, host, "/" + sample["bucket"] + "/" + sample["path"],
                             sample.get("region") or "", PNG_1X1, "image/png")
        r = await client.put(sample["url"], content=PNG_1X1,
                             headers={**hdrs, "Content-Length": str(len(PNG_1X1))})
        out["cred_reput"] = {"status": r.status_code, "body": r.text[:160]}
    except Exception as exc:  # noqa: BLE001
        out["cred_reput"] = {"error": type(exc).__name__}
    return out


def _parse_extra(spec: str) -> tuple[str, float]:
    """`<file_id>` 或 `<file_id>:<已存在分钟数>`。"""
    fid, _, age = spec.partition(":")
    return fid.strip(), (float(age) if age.strip() else 0.0)


async def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--minutes", type=float, default=30.0, help="观测总时长（默认 30 分钟）")
    ap.add_argument("--offsets", default="0,2,5,10,20,30",
                    help="采样时刻（分钟，逗号分隔；0 = 立刻）")
    ap.add_argument("--extra-file-id", action="append", default=[],
                    help="额外跟踪已存在的 file_id（可带 `:<已存在分钟数>`），可重复")
    ap.add_argument("--out", default="", help="输出目录（默认 reports/<日期>_qwen-fileid-lifetime）")
    args = ap.parse_args(argv)

    jwt = os.environ.get("QWEN_JWT", "").strip()
    if not jwt:
        print("拒绝执行：请用环境变量 QWEN_JWT 传 token（不落盘）。")
        return 2
    out = pathlib.Path(args.out) if args.out else (
        REPO / "reports" / f"{time.strftime('%Y-%m-%d')}_qwen-fileid-lifetime")
    out.mkdir(parents=True, exist_ok=True)
    jsonl = out / "samples.jsonl"

    headers = _headers(jwt)
    async with httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(60.0),
                                 follow_redirects=True) as client:
        # t0 样本：现造一个（PUT 一个 1×1 PNG）
        fresh = await mint_and_put(client, headers)
        print(f"[t0] getstsToken+PUT → {fresh['put_status']}；"
              f"file_id={str(fresh['sts'].get('file_id'))[:8]}… path={fresh['path'].rsplit('/', 1)[-1]}")
        if fresh["put_status"] >= 300:
            print("   ❌ t0 上传就失败了，先别继续：", fresh["put_text"])
            return 1
        samples = [{"name": "fresh", "birth": time.time(), **fresh}]
        # 账号 id：file_path 的首段（本次实测与 jwt 的 `id` 一致）⇒ 用它给 extra 重建对象地址。
        account_id = fresh["path"].split("/", 1)[0]
        for spec in args.extra_file_id:
            fid, age = _parse_extra(spec)
            # ⚠️ extra 只有 ① 可观测，且**必须给它一个"真形状"的对象地址**：
            # 实测空 `fileUrl` 会回 `{"success":true,"data":null}`（200 的假阳性）。
            # 重建规则来自本次实测：`https://<bucket>.<host>/<账号 id>/<file_id>_input.png`
            # —— 只对 `filename="input.png"` 的上传成立（本探针与脚本都是这个名字）。
            obj = f"https://{fresh['bucket']}.{fresh['host']}/{account_id}/{fid}_input.png"
            print(f"[extra] 跟踪已存在的 file_id={fid[:8]}…（假定已存在 {age:.0f} 分钟）"
                  f" —— ② ③ ④ 无数据（没有它的凭证/URL），只有 ① 可观测（地址按规则重建）")
            samples.append({"name": f"extra-{fid[:8]}", "birth": time.time() - age * 60,
                            "sts": {"file_id": fid, "file_url": obj},
                            "path": f"{account_id}/{fid}_input.png",
                            "url": obj, "public_url": obj, "host": fresh["host"],
                            "bucket": fresh["bucket"], "region": fresh["region"]})

        offsets = sorted({float(x) for x in args.offsets.split(",") if x.strip()})
        started = time.time()
        now = 0.0
        for off in offsets:
            if off > args.minutes:
                continue
            wait = off * 60 - (time.time() - started)
            if wait > 0:
                await asyncio.sleep(wait)
            for s in samples:
                age = (time.time() - s["birth"]) / 60
                if s["name"].startswith("extra-"):
                    pass  # 年龄已按假定值算好
                row = await probe(client, headers, s, age)
                row["sample"] = s["name"]
                with jsonl.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                g = row.get("getfilelink", {})
                print(f"   +{age:6.1f}min {s['name']:16s} "
                      f"getfilelink={g.get('status')} "
                      f"relink_get={row.get('relink_get', {}).get('status')} "
                      f"anon={row.get('anon_get', {}).get('status')} "
                      f"old_url={row.get('old_url_get', {}).get('status')} "
                      f"cred_reput={row.get('cred_reput', {}).get('status')}")
        print(f"样本已落盘 {jsonl}（{len(offsets)} 个时刻 × {len(samples)} 个样本）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
