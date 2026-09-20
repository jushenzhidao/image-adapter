"""`ModerationMixin`：审核拒答的**判定**与**出口契约**（全渠道共用一份）。

为什么值得单独测：这是**跨渠道契约**——三个脚本（qwen / google / ark）都靠它给客户端同一个说法。
判定用哪张词表、出口给什么状态码，任何一处漂移都会变成"某个渠道偷偷让下游重试一次拒答"，
而那正是这条链路上代价最不对称的错误（白烧一次生成 + 告诉调用方"稍后能成"）。
"""

from __future__ import annotations

import json

import pytest

from adapter.ctxapi.fault import FaultMixin
from adapter.ctxapi.moderation import (
    MODERATION_WORDINGS,
    NO_RETRY_SUFFIX,
    ModerationMixin,
)
from adapter.errors import AdapterError


class _Logfire:
    def __init__(self):
        self.notes: list[dict] = []

    def info(self, message, **attrs):
        self.notes.append({"message": message, **attrs})


class _Settings:
    def __init__(self, roots=()):
        self.capability_roots = roots


class _Ctx(FaultMixin, ModerationMixin):
    """真 `FaultMixin` + 真 `ModerationMixin` —— 出口契约就是被测对象，不能替身。"""

    def __init__(self, roots=()):
        self.settings = _Settings(roots)
        self.logfire = _Logfire()


def _shared_dir(tmp_path, wordings):
    root = tmp_path / "capabilities"
    root.mkdir()
    (root / "_shared.json").write_text(
        json.dumps({"schema_version": 1, "moderation_wordings": wordings}),
        encoding="utf-8")
    return (root,)


# ------------------------------------------------------------------ 判定

def test_vendor_code_marks_a_refusal():
    """厂商 code 由调用方传入（跨厂商共享一张 code 表才是猜）。"""
    ctx = _Ctx()
    doc = {"error": {"code": "data_inspection_failed", "details": "whatever"}}
    assert ctx.matched_moderation(doc, codes=("data_inspection_failed",)) == "data_inspection_failed"


def test_builtin_wordings_apply_when_no_table_is_mounted():
    """**缺表不许降级成沉默**：没有能力表时走实测默认词表（否则拒答会被当成可重试的上游抖动）。"""
    ctx = _Ctx(roots=())
    doc = {"data": {"code": "RateLimited", "details": "内容安全警告：输入数据可能包含不适当的内容！"}}
    assert ctx.matched_moderation(doc, codes=("data_inspection_failed",)) in MODERATION_WORDINGS


def test_shared_table_wordings_are_used_and_override_nothing(tmp_path):
    """共享词表来自 `capabilities/_shared.json`（改数据不发版）⇒ 新词立刻生效，无需改代码。"""
    ctx = _Ctx(roots=_shared_dir(tmp_path, ["输出包含违规内容"]))
    doc = {"error": {"code": "SomethingElse", "details": "输出包含违规内容，已中止"}}
    assert ctx.matched_moderation(doc, codes=()) == "输出包含违规内容"
    # 内置实测词仍然生效（并集，不是替换）
    assert ctx.matched_moderation({"details": "内容安全警告"}) == "内容安全警告"


def test_ordinary_errors_do_not_match():
    """守卫：过载/额度这类**可重试**的错误不许被误判成审核拒答（否则下游会停止重试）。"""
    ctx = _Ctx()
    doc = {"data": {"code": "RateLimited", "details": "目前服务访问量较大，请稍后再试。"}}
    assert ctx.matched_moderation(doc, codes=("data_inspection_failed",)) == ""


def test_matching_is_bounded_and_survives_base64_payloads():
    """有界扫描：几 MB 的 base64 不该被逐字扫，且扫描不能抛。"""
    ctx = _Ctx()
    blob = {"data": [{"b64_json": "A" * 5000}], "error": {"code": "data_inspection_failed"}}
    assert ctx.matched_moderation(blob, codes=("data_inspection_failed",)) == "data_inspection_failed"


# ------------------------------------------------------------------ 出口契约

def test_fail_moderation_is_a_400_content_filter_with_the_canonical_suffix():
    """出口契约：**400** + `content_filter` + `invalid_request_error` + `param=prompt`
    + 统一后缀（把"重试无用"说到客户端的同一句话里）。"""
    ctx = _Ctx()
    with pytest.raises(AdapterError) as caught:
        ctx.fail_moderation("qwen 内容审核未通过（data_inspection_failed）")
    err = caught.value
    assert err.status == 400
    assert err.code == "content_filter"
    assert err.err_type == "invalid_request_error"
    assert err.param == "prompt"
    assert "重试必再被拒" in err.message and err.message.endswith(NO_RETRY_SUFFIX)


def test_fail_moderation_leaves_the_vendor_facts_in_the_note():
    """note 自证：厂商事实（被拒的模态 / 拒在输入还是输出）随 note 留档。"""
    ctx = _Ctx()
    with pytest.raises(AdapterError):
        ctx.fail_moderation("qwen 内容审核未通过", modality="text", refused_at="input",
                            error_code="data_inspection_failed")
    (note,) = ctx.logfire.notes
    assert note["outcome"] == "content_refused"
    assert note["modality"] == "text"
    assert note["refused_at"] == "input"
    assert note["error_code"] == "data_inspection_failed"
    assert note["param"] == "prompt"
