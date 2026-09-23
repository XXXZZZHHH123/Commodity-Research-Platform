"""OpenAI 兼容端点的三条模式与各家的坑（阿里云百炼 / 本地 vLLM 共用这条路）。

这里守的都是「错了很难发现」的类型：JSON 被静默截断、围栏没剥导致一次成功的调用
被判成失败。出问题时表现都是「模型不听话」，而实际上是我们发错了请求。
"""

import json
from types import SimpleNamespace

import pytest

from tin.llm.base import LlmRequest, Message
from tin.llm.openai_provider import OpenAICompatibleProvider, _unfence

SCHEMA = {"type": "object", "properties": {"tone": {"type": "string"}},
          "required": ["tone"], "additionalProperties": False}


class StubClient:
    """记录最后一次请求参数，并回放预置响应。"""

    def __init__(self, content: str = '{"tone": "区间震荡"}'):
        self.calls: list[dict] = []
        self._content = content
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            model="qwen-plus", choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content=self._content, refusal=None))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5,
                                  prompt_tokens_details=None))


def build(mode: str, content: str = '{"tone": "区间震荡"}'):
    stub = StubClient(content)
    p = OpenAICompatibleProvider(model="qwen-plus", json_mode=mode, client=stub)
    return p, stub


def request(schema=SCHEMA) -> LlmRequest:
    return LlmRequest(purpose="compose_brief", messages=(Message(role="user", content="出简报"),),
                      output_schema=schema, schema_name="brief", max_tokens=16000)


def test_structured_output_never_sends_max_tokens():
    """百炼文档把这条写成硬性要求：设了 max_tokens 会让 JSON 中途被截断，产出无效 JSON。

    这是最阴的一种失败——不报错，只是偶尔给你半截 JSON，看起来像「模型不稳定」。
    """
    for mode in ("json_schema", "json_object", "none"):
        p, stub = build(mode)
        p.complete(request())
        assert "max_tokens" not in stub.calls[0], f"{mode} 模式仍然发了 max_tokens"


def test_free_text_still_caps_max_tokens():
    """自由文本没有截断成无效结构的问题，该有的成本上限要留着。"""
    p, stub = build("json_schema")
    p.complete(LlmRequest(purpose="t", messages=(Message(role="user", content="x"),)))
    assert stub.calls[0]["max_tokens"] == 16000


def test_json_schema_mode_sends_strict_schema():
    p, stub = build("json_schema")
    p.complete(request())
    rf = stub.calls[0]["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["schema"] == SCHEMA


def test_json_object_mode_carries_the_schema_and_the_literal_word_json():
    """百炼要求 system/user 里必须出现「JSON」关键词，否则直接报错；
    而这条模式不保证形状，所以 schema 得贴给模型看。"""
    p, stub = build("json_object")
    p.complete(request())
    assert stub.calls[0]["response_format"] == {"type": "json_object"}
    system = next(m["content"] for m in stub.calls[0]["messages"] if m["role"] == "system")
    assert "JSON" in system, "缺少 JSON 关键词，百炼会直接拒绝这次请求"
    assert "tone" in system, "schema 必须贴给模型，否则形状全靠猜"


def test_none_mode_sends_no_response_format_but_still_describes_the_shape():
    p, stub = build("none")
    p.complete(request())
    assert stub.calls[0].get("response_format") is None
    system = next(m["content"] for m in stub.calls[0]["messages"] if m["role"] == "system")
    assert "JSON" in system


@pytest.mark.parametrize("wrapped", [
    '```json\n{"tone": "区间震荡"}\n```',
    '```\n{"tone": "区间震荡"}\n```',
    '  ```JSON\n{"tone": "区间震荡"}```  ',
])
def test_markdown_fences_are_stripped_before_parsing(wrapped):
    """千问一类模型习惯套围栏。不剥就是 JSONDecodeError——
    把一次本来成功的调用判成失败，白白多烧一轮重试。"""
    p, _ = build("json_object", content=wrapped)
    r = p.complete(request())
    assert r.status == "ok" and r.data == {"tone": "区间震荡"}


def test_plain_json_without_fences_still_parses():
    p, _ = build("json_schema")
    assert p.complete(request()).data == {"tone": "区间震荡"}


def test_unparseable_output_is_an_error_not_an_exception():
    """拿到响应但解析不了要落成 error 让原始响应进库，而不是抛出去丢掉证据。"""
    p, _ = build("json_object", content="模型今天想聊聊天")
    r = p.complete(request())
    assert r.status == "error" and "JSON" in r.error
    assert r.raw is not None, "解析失败时原始响应必须留证"


def test_unknown_json_mode_is_refused_at_construction():
    from tin.llm.base import ProviderUnavailable
    with pytest.raises(ProviderUnavailable, match="json_schema"):
        OpenAICompatibleProvider(model="m", json_mode="magic", client=StubClient())


def test_unfence_leaves_ordinary_text_alone():
    assert _unfence('{"a": 1}') == '{"a": 1}'
    assert _unfence('  {"a": 1}  ') == '{"a": 1}'
