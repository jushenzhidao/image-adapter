#!/usr/bin/env python3
"""L6：`completions` **认不认陈旧的 `file_id`**？（1 发）

**为什么问这个**：上传缓存/预生成能不能成立，全看这一个前提 ——
客户端第 1 轮上传的图，第 N 轮（分钟~小时之后）还能不能直接引用，而不必重新上传。

**做法**：从**历史会话**里取出一条真实上传过的 `files[]` 元素（原样，含它当时的 `id`/`url`），
用它顶替新上传，按脚本自己的形状发一次 `chat_type=image_edit` 生成：

  · 200 + 产物**主体与原图一致、背景按提示词改变** ⇒ **陈旧 file_id 被接受且被消费** ⇒ 缓存可行；
  · 200 但产物与参考图无关 ⇒ 被接受却**没被消费**（最危险的静默失败）；
  · 非 200 / 报错 ⇒ **陈旧被拒** ⇒ 缓存不可行（必须每次重传）。

**零成本对照**（跑生成之前先做）：对那条老 `files[].url` 调一次 `getfilelink` 并真取一次
⇒ 看对象本体是否还在（与"接受"是两件事）。

凭据只从环境变量读；产物与 meta 落 `--report-dir`。

    QWEN_JWT='eyJ…' .venv/bin/python tools/probe_qwen_stale_fileid.py --chat-id <id> --plan
    QWEN_JWT='eyJ…' .venv/bin/python tools/probe_qwen_stale_fileid.py --chat-id <id> --yes
"""
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import sys
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

import token_service as ts  # noqa: E402
import httpx  # noqa: E402

HOST = "https://chat.qwen.ai"
DEFAULT_PROMPT = "把这张图的主体保持原样，只把背景换成纯红色，插画风格"


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "qwen_images_v1", REPO / "script_store" / "qwen" / "images@v1.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


q = _load_script()


def ensure_dir(path: Path) -> None:
    if path.is_dir():
        return
    try:
        path.mkdir(parents=True, exist_ok=True)
    except (PermissionError, FileExistsError):
        if not path.is_dir():
            raise


def headers(jwt: str) -> dict:
    h = {"Accept": "application/json", "Content-Type": "application/json",
         "Origin": HOST, "Referer": HOST + "/", "source": "web", "version": "0.2.0",
         "bx-v": "2.5.37", "User-Agent": ts.UA, "Timezone":
         time.strftime("%a %b %d %Y %H:%M:%S GMT%z"),
         "X-Request-Id": str(uuid.uuid4()), "Cookie": "token=" + jwt}
    h.update({"sec-ch-ua": '"Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"',
              "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"macOS"',
              "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
              "Sec-Fetch-Site": "same-origin",
              # 🔴 这两个是**写路径的必备头**（脚本 `_headers` 的注释：同一凭据同一出口下，
              # 少了它们会恒定吃 `RGV587`）。第一版探针漏了 ⇒ 拿到的是"200 + 空响应体 + 0.2s"
              # 的静默丢弃，看起来像"陈旧 file_id 被拒" —— 其实是**我的头不全**。
              "Connection": "keep-alive", "X-Accel-Buffering": "no"})
    return h


def harvest(client, jwt: str, chat_id: str) -> list[dict]:
    """从会话历史里取**最后一条带 files[] 的用户消息**里的文件元素（原样）。"""
    r = client.get(HOST + "/api/v2/chats/" + chat_id, headers=headers(jwt))
    r.raise_for_status()
    doc = r.json().get("data") or {}
    # 🔴 消息挂在 `data.chat.history.messages`（不是 `data.history` —— 我第一版读错，
    # 得到 0 条消息却毫无报错）。与脚本 `_recover_urls` 的读法保持一致。
    chat = (doc.get("chat") if isinstance(doc, dict) else None) or doc
    msgs = ((chat.get("history") or {}).get("messages") or {})
    seq = list(msgs.values()) if isinstance(msgs, dict) else list(msgs or [])
    seq.sort(key=lambda m: m.get("timestamp") or 0)
    for m in reversed(seq):
        files = m.get("files") or []
        if m.get("role") == "user" and files:
            return files
    return []


def relink_check(client, jwt: str, url: str) -> dict:
    """零成本对照：对老 url 调 getfilelink（重签）并真取一次。"""
    out = {}
    try:
        r = client.post(HOST + "/api/v2/files/getfilelink", json={"fileUrl": url},
                        headers=headers(jwt))
        full = r.text
        out["getfilelink"] = r.status_code
        relink = ""
        try:
            relink = (json.loads(full).get("data") or {}).get("fileUrl") or ""
        except Exception:  # noqa: BLE001
            pass
        out["has_link"] = bool(relink)
        if relink:
            g = client.get(relink)
            out["relink_get"] = g.status_code
            out["bytes"] = len(g.content)
    except Exception as exc:  # noqa: BLE001
        out["error"] = type(exc).__name__
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--chat-id", required=True, help="历史会话 id（从会话列表里取）")
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--yes", action="store_true", help="硬门槛：不给只打印计划")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--report-dir", default="")
    args = ap.parse_args(argv)

    jwt = os.environ.get("QWEN_JWT", "").strip()
    if not jwt:
        print("拒绝执行：请用环境变量 QWEN_JWT 传 token（不落盘）。")
        return 2
    report = Path(args.report_dir) if args.report_dir else (
        REPO / "reports" / f"{time.strftime('%Y-%m-%d')}_qwen-stale-fileid")

    with httpx.Client(trust_env=False, timeout=90, follow_redirects=True) as client:
        files = harvest(client, jwt, args.chat_id)
        if not files:
            print(f"该会话里没有带 files[] 的用户消息：{args.chat_id}")
            return 1
        entry = files[-1]
        age_note = "（年龄未知：会话里只有 timestamp）"
        print(f"取到 {len(files)} 个文件元素；用最后那个：")
        print(f"   id={entry.get('id')} name={entry.get('name')} "
              f"status={entry.get('status')} type={entry.get('type')}")
        ctl = relink_check(client, jwt, str(entry.get("url") or ""))
        print(f"零成本对照（对象还在不在）：{ctl} {age_note}")

        print(f"计划：**1 发** —— 用上面那条**陈旧** files[] 发一次 image_edit；"
              f"prompt={args.prompt!r}")
        if not args.yes or args.plan:
            print("（未加 --yes ⇒ 只打印，不发任何生成请求）")
            return 2

        # 1) 新开一个会话（与脚本同形）
        chat = client.post(HOST + "/api/v2/chats/new", headers=headers(jwt), json={
            "title": "New Chat", "models": [q.CHAT_MODEL], "chat_mode": "normal",
            "chat_type": q.EDIT_TYPE, "timestamp": int(time.time() * 1000),
            "project_id": ""})
        chat_id = (chat.json().get("data") or {}).get("id")
        if not chat_id:
            print("chats/new 失败：", chat.text[:200])
            return 1
        print(f"新会话 {chat_id}")

        # 2) 用脚本自己的 `_message` 造消息（保证形状一致），files 用陈旧元素
        msg = q._message({"prompt": args.prompt}, q.EDIT_TYPE, "auto", None, [entry])
        body = {"stream": True, "version": "2.1", "incremental_output": True,
                "chatId": chat_id, "parentId": "", "chat_id": chat_id,
                "chat_mode": "normal", "model": q.CHAT_MODEL, "parent_id": None,
                "timestamp": msg["timestamp"], "messages": [msg]}
        started = time.time()
        http_status = 0
        with client.stream("POST", HOST + "/api/v2/chat/completions",
                           params={"chat_id": chat_id}, headers=headers(jwt),
                           json=body) as resp:
            http_status = resp.status_code
            print(f"完成调用 HTTP {http_status}  {time.time() - started:.1f}s")
            text = "".join(resp.iter_text())
        saved = {}
        ensure_dir(report / "raw")
        (report / "raw" / "completion_response.txt").write_text(text[:4000], encoding="utf-8")
        print(f"  响应体 {len(text)} 字节（前 200：{text[:200]!r}）")
        if text:
            urls, meta, error = q._read_stream(text)
            saved = {"urls": urls, "meta": meta,
                     "error": (str(error)[:200] if error else None)}
            print(f"  脚本解析：urls={len(urls)} meta={meta} error={saved['error']}")
        else:
            print("  空响应体（可能被拒）")

        ensure_dir(report / "outputs")
        ensure_dir(report / "meta")
        verdict = "unknown"
        if saved.get("urls"):
            url = saved["urls"][0]
            g = client.get(url)
            raw = g.content
            is_png = raw.startswith(b"\x89PNG\r\n\x1a\n")
            dims = "?"
            if is_png:
                import io
                from PIL import Image
                im = Image.open(io.BytesIO(raw))
                dims = f"{im.width}x{im.height}"
            (report / "outputs" / "stale_ref_result.png").write_bytes(raw)
            verdict = "accepted（需人工看：主体是否为参考图主体 + 背景是否变红）"
            print(f"  ✅ 产物 {len(raw)}B PNG={is_png} {dims} → "
                  f"outputs/stale_ref_result.png")
        else:
            verdict = "rejected-or-empty（看上面 error/HTTP）"
        (report / "meta" / "stale_fileid_run.json").write_text(json.dumps({
            "chat_id": chat_id, "source_chat_id": args.chat_id,
            "entry": {k: v for k, v in entry.items() if k != "url"},
            "control": ctl, "prompt": args.prompt,
            "http": http_status, "recovered": saved,
            "verdict": verdict}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"⇒ 判定：{verdict}")
        print(f"meta 已落盘 {report}/meta/stale_fileid_run.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
