"""Unit tests for tools/qwen_guest_smoke.py -- the real-image verification tool.

The tool itself spends real guest quota (约 4~5 张/天), so everything that can be
pinned without a network call is pinned here, and the things pinned are the ones
that would otherwise waste a generation:

  1. the case plan -- `i2i`/`multi` cannot run without their prerequisite's
     pictures, and the order t2i -> i2i -> multi is a dependency order, not a
     cosmetic one. Getting this wrong means the tool either fires a request with
     no input (a t2i dressed up as i2i) or dies half way through a paid round;
  2. the quota arithmetic that guards `--yes`. `--tiers 1K,2K` with three cases
     is six generations, not three;
  3. the 2MB PUT ceiling -- upstream refuses anything larger, so an oversized
     product has to be brought back under budget *loudly* (the note says what was
     done), otherwise "图生图跑通了" is a claim about a picture the model never
     produced.

No browser, no network, no filesystem: the payload builders are pure.
"""
from __future__ import annotations

import importlib.util
import io
import os
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[2] / "tools" / "qwen_guest_smoke.py"


def _load():
    spec = importlib.util.spec_from_file_location("qwen_guest_smoke", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke = _load()


# ------------------------------------------------------------------ 用例计划


def test_empty_spec_means_all_three():
    assert smoke.parse_cases("") == ["t2i", "i2i", "multi"]


def test_asking_for_i2i_pulls_in_the_picture_source():
    """i2i 的输入图只能来自一次真实 t2i ⇒ 前置是闭包的一部分，不是可选项。"""
    assert smoke.parse_cases("i2i") == ["t2i", "i2i"]


def test_asking_for_multi_pulls_in_both_prerequisites():
    assert smoke.parse_cases("multi") == ["t2i", "i2i", "multi"]


def test_case_order_is_the_dependency_order_not_the_typed_order():
    """倒着写也必须正着跑：multi 的素材由 i2i 产出，i2i 的由 t2i 产出。"""
    assert smoke.parse_cases("multi,t2i,i2i") == ["t2i", "i2i", "multi"]


def test_t2i_alone_stays_alone():
    assert smoke.parse_cases("t2i") == ["t2i"]


def test_unknown_case_is_refused_by_name():
    with pytest.raises(ValueError) as err:
        smoke.parse_cases("t2i,img2img")
    assert "img2img" in str(err.value)


def test_budget_counts_every_generation():
    """档位是乘数：6 发和 3 发不是同一个承诺，guard 必须按乘数算。"""
    assert smoke.plan_budget(["t2i", "i2i", "multi"], ["1K"]) == 3
    assert smoke.plan_budget(["t2i", "i2i", "multi"], ["1K", "2K"]) == 6


def test_the_plan_line_states_the_generation_count():
    line = smoke.format_plan(["t2i", "i2i"], ["1K"], 2)
    assert "2 次真实生成" in line


# ------------------------------------------------------------ 请求体（按用例）


def test_t2i_carries_no_image_field_at_all():
    body = smoke.case_payload("t2i", "1K", "一只猫", [])
    assert body["prompt"] == "一只猫"
    assert body["size"] == "1K"
    assert "image" not in body


def test_single_image_case_sends_one_string():
    body = smoke.case_payload("i2i", "1K", "x", ["data:image/png;base64,AAA"])
    assert body["image"] == "data:image/png;base64,AAA"
    assert body["prompt"] == smoke.I2I_PROMPT


def test_multi_image_case_sends_two_distinct_pictures():
    """两张**不同**的图才验得了保序；同一张传两次是假的多图。"""
    a, b = "data:image/png;base64,AAA", "data:image/png;base64,BBB"
    body = smoke.case_payload("multi", "1K", "x", [a, b])
    assert body["image"] == [a, b]
    assert body["prompt"] == smoke.MULTI_PROMPT


def test_missing_asset_is_refused_before_the_request_is_built():
    with pytest.raises(ValueError) as err:
        smoke.case_payload("i2i", "1K", "x", [])
    assert "素材" in str(err.value)
    with pytest.raises(ValueError):
        smoke.case_payload("multi", "1K", "x", ["data:image/png;base64,AAA"])


# -------------------------------------------------------------------- 素材


def test_data_uri_prefix_carries_the_mime():
    uri = smoke.to_data_uri(b"\x89PNG\r\n\x1a\n", "image/jpeg")
    assert uri.startswith("data:image/jpeg;base64,")


def test_mime_is_sniffed_from_magic_bytes():
    assert smoke.mime_of(b"\x89PNG\r\n\x1a\nrest") == "image/png"
    assert smoke.mime_of(b"\xff\xd8\xff\xe0") == "image/jpeg"
    # WEBP 的标记在偏移 8，不在起始字节：只看 "RIFF" 前缀会误判别的 RIFF 容器
    assert smoke.mime_of(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert smoke.mime_of(b"RIFF\x00\x00\x00\x00WAVEfmt ") == ""
    assert smoke.mime_of(b"not-an-image") == ""


def test_unrecognized_bytes_fall_back_to_an_image_mime_not_octet_stream():
    """判不出类型时必须回落成图片 mime：`application/octet-stream` 会被脚本
    当 `file` 类上传（`_filetype_of`），上游那边的行为就没验过了。"""
    assert smoke.mime_of(b"?") == ""


def _noise_png(size: int = 1000) -> bytes:
    """噪声图：纯色 PNG 会被压缩到几十 KB，撑不出超限场景。"""
    from PIL import Image

    img = Image.frombytes("RGB", (size, size), os.urandom(size * size * 3))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_under_budget_bytes_are_handed_over_untouched():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buf, format="PNG")
    small = buf.getvalue()
    data, mime, note = smoke.shrink_to_budget(small, "image/png")
    assert data == small and mime == "image/png" and note == ""


def test_oversize_bytes_are_brought_under_budget_and_the_note_says_how():
    big = _noise_png()
    assert len(big) > smoke.ASSET_BUDGET            # 前置条件：这张图确实超限
    data, mime, note = smoke.shrink_to_budget(big, "image/png")
    assert len(data) <= smoke.ASSET_BUDGET
    assert mime == "image/jpeg"
    # 改素材必须留痕：note 里有前后体积与用了什么手段
    assert "压缩" in note and "jpeg" in note
    assert len(data) < len(big)


def test_the_budget_leaves_margin_under_the_vendor_ceiling():
    """预算必须**小于**上游的硬上限，否则压缩完还是被拒。"""
    assert smoke.ASSET_BUDGET < smoke.PUT_LIMIT == 2 * 1024 * 1024


def test_burst_limit_is_the_guest_daily_quota_shape():
    """默认三类用例 = 3 发，正好是单轮允许的上限（再多要 --allow-burst）。"""
    assert smoke.BURST_LIMIT == 3
    assert smoke.plan_budget(smoke.parse_cases(""), ["1K"]) == smoke.BURST_LIMIT


def test_no_response_format_means_the_key_is_absent():
    """默认不带该键 —— 存量行为（产物直通上游链接）必须逐字节不变。"""
    body = smoke.case_payload("t2i", "1K", "一只猫", [])
    assert "response_format" not in body


def test_b64_json_is_forwarded_so_the_adapter_fetches_the_product():
    """链式用例取产物**不能靠客户端**去下载 cdn 链接。

    实测 2026-09-20：访客门 + t2i 的产物链接**紧跟生成后立刻** 404（直连与系统代理都是），
    于是 t2i 成功、i2i 断在没有输入图上 —— 一轮额度只换来半条链。带 `b64_json` ⇒
    产物由 adapter 自己取回并编码，`asset_from_response` 对两种形态都已支持。
    """
    body = smoke.case_payload(
        "i2i", "1K", "x", ["data:image/png;base64,AAA"], "b64_json"
    )
    assert body["response_format"] == "b64_json"
    assert body["image"] == "data:image/png;base64,AAA"   # 其余字段不受影响


def test_the_product_count_does_not_depend_on_having_a_url():
    """按 URL 计数会漏掉 `b64_json` 形态 ⇒ 把成功读成失败。

    2026-09-20 实跑：`b64_json` 那轮拿到了 1.14MB 的素材，行首却打印"出图 0 张"。
    """
    with_url = {"data": [{"url": "https://cdn.example/a.png"}]}
    with_b64 = {"data": [{"b64_json": "AAAA"}]}
    assert smoke.product_count(with_url) == 1
    assert smoke.product_count(with_b64) == 1
    assert smoke.product_count({}) == 0
    assert smoke.product_count({"data": []}) == 0
