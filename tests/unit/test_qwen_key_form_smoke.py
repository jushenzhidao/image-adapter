"""Unit tests for `tools/qwen_key_form_smoke.py`（登录态密钥形态的 smoke 工具）。

这个工具是**取证**用的：它给出的结论（"这一发走的是账号门"）会被写进报告、进 docs，
所以结论性那一步必须有自己的测试 —— 否则"两侧都是空串也算相等"这种错会静默把
回退到访客门的请求报成账号门。真发请求的部分不在这里（那是 L4，计费），
这里只钉纯逻辑：JWT 载荷解码、整串 cookie 取 `token=`、门归属判定。
"""

from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[2] / "tools" / "qwen_key_form_smoke.py"


def _load():
    spec = importlib.util.spec_from_file_location("qwen_key_form_smoke", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m = _load()


def _jwt(payload: dict) -> str:
    """造一枚**结构正确**的 JWT（签名段随便给：工具不验签，只读载荷）。"""
    def seg(obj) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"{seg({'alg': 'HS256', 'typ': 'JWT'})}.{seg(payload)}.sig"


# ------------------------------------------------------------------ JWT 载荷

def test_jwt_payload_reads_the_three_fields_the_vendor_signs():
    token = _jwt({"id": "fbd31d55-8064-4fe7-8878-137be3283be5",
                  "last_password_change": 1750660873, "exp": 1792345736})
    assert m.jwt_payload(token)["id"] == "fbd31d55-8064-4fe7-8878-137be3283be5"
    assert m.jwt_payload(token)["exp"] == 1792345736


def test_jwt_payload_pads_base64url_without_padding():
    """上游签的 JWT 不带 `=` 填充，`b64decode` 必须先补齐 —— 少一个字符就解不出来。"""
    token = _jwt({"id": "a", "exp": 1})          # 载荷长度刻意不整除 4
    assert m.jwt_payload(token) == {"id": "a", "exp": 1}


def test_jwt_payload_is_empty_on_garbage():
    """解不出来 ⇒ 空 dict（调用方据此判"拿不到账号 id"，而不是抛异常）。"""
    assert m.jwt_payload("not-a-jwt") == {}
    assert m.jwt_payload("") == {}
    assert m.jwt_payload("a.!!!.c") == {}


# ------------------------------------------------------- 整串 cookie 取 token=

def test_token_of_jar_takes_the_token_entry():
    assert m.token_of_jar("aui=x; token=eyJabc; ssxmod_itna=y") == "eyJabc"


def test_token_of_jar_is_not_fooled_by_bx_umidtoken():
    """`bx-umidtoken=` 里含 `token=` 这个子串 —— 按子串找会取到设备指纹（旧坑）。"""
    jar = "bx-umidtoken=T2gAxxx; aui=1"
    assert m.token_of_jar(jar) == ""


def test_token_of_jar_returns_empty_for_a_guest_jar():
    assert m.token_of_jar("aui=1; ssxmod_itna=2") == ""


# ------------------------------------------------------------------ 门归属

def test_door_is_account_when_the_owner_matches():
    assert m.door_of("fbd31d55-8064", "fbd31d55-8064") == "account"


def test_door_is_unclear_when_the_owner_differs():
    """不等 ⇒ 回退到了访客门（或取错了字段），**不许**报成账号门。"""
    assert m.door_of("wv1ag6aa-742c", "fbd31d55-8064") == "unclear"


def test_two_empty_strings_are_not_equality():
    """两侧都空（产物 URL 里没有 `key=`、凭据又解不出 id）时，凭"相等"下结论是最坏的一种假绿。"""
    assert m.door_of("", "") == "unclear"


def test_door_needs_both_sides():
    assert m.door_of("", "fbd31d55") == "unclear"
    assert m.door_of("fbd31d55", "") == "unclear"


# --------------------------------------------------------------- ensure_dir

def test_ensure_dir_creates_a_missing_directory(tmp_path):
    target = tmp_path / "a" / "b"
    m.ensure_dir(target)
    assert target.is_dir()


def test_ensure_dir_tolerates_the_sandbox_shim_refusing_an_existing_dir(tmp_path, monkeypatch):
    """2026-09-19 的真实事故：harness 的 brokered `mkdir` 对**已存在**的目录抛
    `PermissionError: EEXIST`（`exist_ok` 拦不住，异常是 shim 自己抛的）⇒ 一个已经跑完、
    已经花掉一发的请求崩在落盘那一步，产物与 URL 全丢。修法：异常之后**以磁盘为准**复核。
    """
    target = tmp_path / "outputs"
    target.mkdir()

    def refuse(self, *a, **kw):           # 复刻 shim 的行为
        raise PermissionError(f"EEXIST: file already exists, mkdir {self!r}")

    monkeypatch.setattr(Path, "mkdir", refuse)
    m.ensure_dir(target)                  # 目录在 ⇒ 不许抛
    assert target.is_dir()


def test_ensure_dir_still_raises_when_the_directory_is_really_missing(tmp_path, monkeypatch):
    """不许把"建不出来"也吞掉：目录既建不出、又确实不在 ⇒ 必须抛，否则后面会在
    写文件时给出更难懂的报错。"""
    def refuse(self, *a, **kw):
        raise PermissionError("EEXIST: file already exists")

    monkeypatch.setattr(Path, "mkdir", refuse)
    with pytest.raises(PermissionError):
        m.ensure_dir(tmp_path / "nope")
