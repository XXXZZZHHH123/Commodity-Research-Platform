"""LLM 接入层（SPEC §4.1 / F2）。全部走 FakeProvider：不联网、不要密钥。

这些用例护的是三条底线，每条都对应 SPEC 里一个硬指标：
- 落库完整性 —— 「为什么今天这么说」必须答得上来，失败路径也不例外；
- 证据校验闸门 —— 幻觉率 = 0（引用不存在 / 数值对不上 / 伪造 id，出现即为 bug）；
- provider 可换 —— 换成内网 30B 不改一行业务代码。
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from tin.config import SHANGHAI
from tin.llm import (
    LlmError, LlmRefused, LlmRequest, LlmResponse, Message, ProviderUnavailable,
    SystemBlock, build_provider,
)
from tin.llm import audit, guard
from tin.llm.base import STATUS_ERROR, STATUS_OK, STATUS_REFUSAL
from tin.llm.fake import FakeProvider, ok
from tin.llm.guard import EvidenceContext
from tin.models import LlmCall, PlaybookItem, Researcher

WARRANT_AS_OF = "2026-09-17T15:00:00+08:00"   # loaded 夹具里的真实时点
WARRANT_VALUE = 5221.0                        # 交易所公布值

SYSTEM = (
    SystemBlock("你是锡品种的研究员助手。只依据给出的事实与思路条目作答。", cache_breakpoint=True),
)


# ---------------------------------------------------------------- 夹具与小工具

@pytest.fixture
def researcher(session):
    row = Researcher(username="tester", display_name="测试研究员", varieties=["SN"],
                     created_at=datetime.now(timezone.utc))
    session.add(row)
    session.commit()
    return row


@pytest.fixture
def playbook(session, researcher):
    """两条思路条目：7 号会喂进 prompt，99 号存在但**不会**喂——用来测伪造引用。"""
    now = datetime.now(timezone.utc)
    items = [
        PlaybookItem(id=7, researcher_id=researcher.id, variety="SN",
                     text="锡看盘先看 SPX 再看产业", status="生效", created_at=now, updated_at=now),
        PlaybookItem(id=99, researcher_id=researcher.id, variety="SN",
                     text="社库累两周才算信号", status="生效", created_at=now, updated_at=now),
    ]
    session.add_all(items)
    session.commit()
    return items


@pytest.fixture
def ctx():
    # 本次只把 7 号条目喂进了 prompt。99 号在库里，但这次没给 LLM 看过。
    return EvidenceContext(playbook_item_ids={7}, signal_ids={3})


def make_request(**kw) -> LlmRequest:
    base = dict(
        purpose="compose_brief",
        system=SYSTEM,
        messages=(Message("user", "请出 9/18 的锡简报草稿。"),),
        output_schema={"type": "object"},
        schema_name="brief_draft",
        brief_id=None,
    )
    base.update(kw)
    return LlmRequest(**base)


def claim(text="仓单继续去化", *, series_id=None, value=None, as_of=None,
          playbook_item_id=None, signal_id=None, side="bull") -> dict:
    evidence: dict = {}
    if series_id is not None:
        evidence["series_id"] = series_id
    if value is not None:
        evidence["value"] = value
    if as_of is not None:
        evidence["as_of"] = as_of
    if playbook_item_id is not None:
        evidence["playbook_item_id"] = playbook_item_id
    if signal_id is not None:
        evidence["signal_id"] = signal_id
    return {"text": text, "side": side, "evidence": [evidence]}


def draft(*claims, **kw) -> dict:
    """一份**交付完整**的草稿：基调 + 参考运行区间 + 操作倾向，三件套齐。

    默认值必须齐全，否则 `missing_deliverables` 会报缺口，`guard.compose` 就会
    打回重试 —— 于是每个只预置一条响应的测试都会撞上「FakeProvider 脚本已用尽」，
    而它们的本意是「一次就过」。想测缺口的用例自己用 `**kw` 覆盖掉对应字段。
    """
    payload = {
        "tone": "偏多",
        "price_range": {"low": 400000, "high": 420000, "series_id": "SHFE.SN.main.settle"},
        "stance": "逢低做多",
        "summary": "仓单去化叠加宏观回暖，短期偏多。",
        "claims": list(claims),
        "used_playbook_item_ids": [7],
    }
    payload.update(kw)
    return payload


GOOD_CLAIM = claim(series_id="SHFE.SN.warrant", value=WARRANT_VALUE, as_of=WARRANT_AS_OF)


# ------------------------------------------------------------------ 落库完整性

def test_every_call_is_recorded_with_cache_counters(loaded):
    """成功路径：请求、响应、四个 token 计数、耗时、status 一样不少。"""
    provider = FakeProvider([ok({"tone": "偏多"}, text='{"tone": "偏多"}')])
    request = make_request(brief_id=None)

    response = audit.invoke(loaded, provider, request)

    row = loaded.scalars(select(LlmCall)).one()
    assert row.purpose == "compose_brief"
    assert (row.provider, row.model) == ("fake", "fake-model")
    assert row.status == STATUS_OK
    assert row.request["messages"][0]["content"].startswith("请出")
    assert row.request["system"][0]["cache_breakpoint"] is True
    assert row.response["data"] == {"tone": "偏多"}
    assert (row.input_tokens, row.output_tokens) == (1000, 200)
    # 缓存命中是成本的主要变量，必须记；长期为 0 说明 prompt 前缀里混进了每次都变的东西
    assert (row.cache_read_tokens, row.cache_write_tokens) == (900, 0)
    assert row.latency_ms is not None and row.error is None
    assert response.call_id == row.id


def test_failed_call_is_recorded_before_raising(session):
    """失败路径：先落库再抛。落库不成功就不算调用完成，所以顺序不能反。"""
    provider = FakeProvider([RuntimeError("端点连不上")])

    with pytest.raises(LlmError):
        audit.invoke(session, provider, make_request())

    row = session.scalars(select(LlmCall)).one()
    assert row.status == STATUS_ERROR
    assert "端点连不上" in row.error
    assert row.request["purpose"] == "compose_brief"   # 输入照样留痕，便于重放


def test_unusable_payload_is_recorded_then_raises(session):
    """拿到了响应但解析不了：响应体照样进库——排查「模型到底吐了什么」全靠它。"""
    bad = LlmResponse(status=STATUS_ERROR, text="{不是 JSON", provider="fake",
                      model="fake-model", error="结构化输出不是合法 JSON")
    provider = FakeProvider([bad])

    with pytest.raises(LlmError):
        audit.invoke(session, provider, make_request())

    row = session.scalars(select(LlmCall)).one()
    assert row.status == STATUS_ERROR
    assert row.response["text"] == "{不是 JSON"


def test_refusal_is_recorded_as_refusal_not_error(session, ctx):
    """拒答是第三种状态：模型能答但不答，与故障区分开，上层据此降级而不是重试。"""
    provider = FakeProvider([LlmResponse(status=STATUS_REFUSAL, provider="fake",
                                         model="fake-model", error="模型拒答")])

    with pytest.raises(LlmRefused):
        guard.compose(session, provider, make_request(), ctx)

    row = session.scalars(select(LlmCall)).one()
    assert row.status == STATUS_REFUSAL
    assert provider.calls == 1          # 拒答不重试


# ---------------------------------------------------------------- 证据校验闸门

def test_accepts_claim_whose_value_matches_the_database(loaded, ctx):
    """正向对照：引用真实存在、数值与库内一致，一次就过。"""
    provider = FakeProvider([draft(GOOD_CLAIM)])

    result = guard.compose(loaded, provider, make_request(), ctx)

    assert result.clean and result.attempts == 1
    assert [c.text for c in result.draft.claims] == ["仓单继续去化"]


def test_blocks_nonexistent_series_id(loaded, ctx):
    """引用了未登记的指标——一期靠外键拦研究员，这里靠查库拦 LLM。"""
    bogus = claim("镍矿供应转紧", series_id="SMM.NI.ore.fantasy", value=123.0)
    provider = FakeProvider([draft(bogus), draft(bogus)])

    result = guard.compose(loaded, provider, make_request(), ctx)

    assert result.draft.claims == []
    assert "未登记的指标 SMM.NI.ore.fantasy" in result.unverified[0]["reasons"][0]


def test_blocks_value_that_disagrees_with_the_database(loaded, ctx):
    """series_id 真实存在，但数值是编的——这是最危险的一类幻觉，光查引用拦不住。"""
    wrong = claim(series_id="SHFE.SN.warrant", value=4100.0, as_of=WARRANT_AS_OF)
    provider = FakeProvider([draft(wrong), draft(wrong)])

    result = guard.compose(loaded, provider, make_request(), ctx)

    reason = result.unverified[0]["reasons"][0]
    assert "库内值是 5221.0" in reason and "LLM 报的是 4100.0" in reason


def test_tolerates_float_noise_but_not_rounding(loaded, ctx):
    """容差要容得下浮点往返，容不下「四舍五入蒙混过关」。"""
    context = EvidenceContext(playbook_item_ids={7})
    noise = guard.check_evidence(
        loaded, _evidence(series_id="SHFE.SN.warrant", value=5221.0000000001,
                          as_of=WARRANT_AS_OF), context)
    rounded = guard.check_evidence(
        loaded, _evidence(series_id="SHFE.SN.warrant", value=5220.0,
                          as_of=WARRANT_AS_OF), context)
    assert noise == []
    assert rounded and "库内值" in rounded[0]


def test_matches_observation_by_calendar_day_when_timestamp_differs(loaded, ctx):
    """只给日期、或时刻写法不同，只要当天确有这个数就不算幻觉；库里没有才算。"""
    context = EvidenceContext()
    assert guard.check_evidence(
        loaded, _evidence(series_id="SHFE.SN.warrant", value=WARRANT_VALUE,
                          as_of="2026-09-17"), context) == []
    missing = guard.check_evidence(
        loaded, _evidence(series_id="SHFE.SN.warrant", value=WARRANT_VALUE,
                          as_of="2020-01-02"), context)
    assert missing and "没有观测" in missing[0]


def test_blocks_forged_playbook_item_id_even_though_it_exists(loaded, playbook, ctx):
    """99 号条目在库里真实存在，但这次没喂给 LLM。

    判定基准只能是「本次实际喂进 prompt 的集合」——查库会放它过去，那就等于
    允许 LLM 引用它没看过的东西编理由。
    """
    assert loaded.get(PlaybookItem, 99) is not None
    forged = claim("按老思路社库累两周才算信号", playbook_item_id=99)
    provider = FakeProvider([draft(forged), draft(forged)])

    result = guard.compose(loaded, provider, make_request(), ctx)

    assert result.draft.claims == []
    assert "没有喂进 prompt 的思路条目 #99" in result.unverified[0]["reasons"][0]


def test_blocks_forged_signal_id(loaded, ctx):
    forged = claim("阈值已触发", signal_id=404)
    provider = FakeProvider([draft(forged), draft(forged)])

    result = guard.compose(loaded, provider, make_request(), ctx)

    assert "没有喂进 prompt 的信号 #404" in result.unverified[0]["reasons"][0]


def test_blocks_value_without_series_id(loaded, ctx):
    """报了个数却没说是哪条序列——无从核对，等同于没有证据。"""
    vague = claim("库存降到 3000 附近", playbook_item_id=7, value=3000.0)
    provider = FakeProvider([draft(vague), draft(vague)])

    result = guard.compose(loaded, provider, make_request(), ctx)

    assert "没有 series_id" in result.unverified[0]["reasons"][0]


def test_blocks_price_range_on_unregistered_series(loaded, ctx):
    """参考区间挂在不存在的序列上——区间本身也是一条要溯源的论断。"""
    payload = draft(GOOD_CLAIM,
                    price_range={"low": 400000, "high": 420000, "series_id": "SHFE.SN.ghost"})
    provider = FakeProvider([payload, payload])

    result = guard.compose(loaded, provider, make_request(), ctx)

    assert result.draft.price_range is None          # 区间被摘掉
    assert [c.text for c in result.draft.claims] == ["仓单继续去化"]   # 论断照常保留
    assert result.unverified[0]["field"] == "price_range"


def test_intersects_self_reported_playbook_ids_with_what_was_fed(loaded, ctx):
    """LLM 自报「我用了 7 和 99 号」，99 号没喂过，直接从记录里丢掉。"""
    provider = FakeProvider([draft(GOOD_CLAIM, used_playbook_item_ids=[7, 99])])

    result = guard.compose(loaded, provider, make_request(), ctx)

    assert result.used_playbook_item_ids == [7]
    assert result.draft.used_playbook_item_ids == [7]


# -------------------------------------------------------------- 重试与降级

def test_retries_once_and_carries_the_reason_back(loaded, ctx):
    """打回重试一次，并且把「哪一条为什么不过」原样带回去；含糊的重试换不回好结果。"""
    bad = claim("矿端趋紧", series_id="SMM.SN.ore.nonexistent")
    provider = FakeProvider([draft(bad), draft(GOOD_CLAIM)])

    result = guard.compose(loaded, provider, make_request(), ctx)

    assert result.attempts == 2 and result.clean
    retry = provider.requests[1]
    assert retry.system == SYSTEM                       # 前缀一字未动，缓存照样命中
    assert len(retry.messages) == 3                     # user → assistant(上一版) → user(打回)
    assert "SMM.SN.ore.nonexistent" in retry.messages[-1].content
    assert len(loaded.scalars(select(LlmCall)).all()) == 2   # 两次调用都留痕
    assert result.call_ids == [r.id for r in loaded.scalars(select(LlmCall)).all()]


def test_degrades_the_bad_claim_instead_of_dropping_the_whole_brief(loaded, ctx):
    """重试仍不过 → 该条降级进 unverified 标灰，其余照常出。

    整份丢弃等于今天没有简报；一条论断没证据不代表另外几条也没有。
    """
    bad = claim("进口窗口打开", series_id="CUSTOMS.2609.import.MM", value=99999.0)
    payload = draft(GOOD_CLAIM, bad)
    provider = FakeProvider([payload, payload])

    result = guard.compose(loaded, provider, make_request(), ctx)

    assert result.attempts == 2
    assert [c.text for c in result.draft.claims] == ["仓单继续去化"]
    assert result.draft.tone == "偏多" and result.draft.summary        # 简报本体还在
    assert [u["claim"] for u in result.unverified] == ["进口窗口打开"]
    assert not result.clean                                           # 非空即质量警报


def test_retries_when_output_breaks_the_brief_contract(loaded, ctx):
    """结构不合契约（基调不在词表里）同样打回重试，而不是把脏数据塞进简报。"""
    provider = FakeProvider([draft(GOOD_CLAIM, tone="看涨吧"), draft(GOOD_CLAIM)])

    result = guard.compose(loaded, provider, make_request(), ctx)

    assert result.attempts == 2 and result.clean
    assert "简报契约" in provider.requests[1].messages[-1].content


def test_gives_up_after_the_retry_on_broken_contract(loaded, ctx):
    """连续两次都不合契约就报错——这时候没有任何可用的草稿可以降级。"""
    provider = FakeProvider([draft(GOOD_CLAIM, tone="看涨吧"), draft(GOOD_CLAIM, tone="更涨")])

    with pytest.raises(LlmError):
        guard.compose(loaded, provider, make_request(), ctx)

    assert len(loaded.scalars(select(LlmCall)).all()) == 2   # 两次都留痕


# ------------------------------------------------------------ provider 可换

class LocalProvider(FakeProvider):
    """假装是内网 30B：同一套接口、不同的 name / model。"""

    name = "openai_compatible"


def test_switching_provider_does_not_change_the_caller(loaded, ctx):
    """同一份业务代码、同一个请求，换 provider 只换落库里的 provider / model 两列。"""
    payload = draft(GOOD_CLAIM)
    request = make_request()

    cloud = guard.compose(loaded, FakeProvider([payload]), request, ctx)
    local = guard.compose(loaded, LocalProvider([payload], model="qwen3-30b"), request, ctx)

    assert cloud.draft == local.draft
    rows = loaded.scalars(select(LlmCall).order_by(LlmCall.id)).all()
    assert [(r.provider, r.model) for r in rows] == [
        ("fake", "fake-model"), ("openai_compatible", "qwen3-30b")]


def test_build_provider_is_the_only_construction_entry(session):
    assert isinstance(build_provider("fake", script=[]), FakeProvider)
    with pytest.raises(ProviderUnavailable):
        build_provider("没这个东西")
    # openai 包没装（或没配 base_url）时给清晰报错，而不是在 import 期炸掉整层
    with pytest.raises(ProviderUnavailable):
        build_provider("openai_compatible")


# ------------------------------------------------------ prompt 缓存纪律

def test_rejects_volatile_content_in_the_system_prefix():
    """system 里塞日期 = 整段前缀每次失效 = 缓存读永远为 0 = 成本翻十倍。"""
    with pytest.raises(ValueError, match="日期"):
        make_request(system=(SystemBlock("今天是 2026-09-18，请出简报。"),))
    with pytest.raises(ValueError, match="UUID"):
        make_request(system=(SystemBlock("本次 run: 3f2504e0-4f89-11d3-9a0c-0305e82c3301"),))
    # 确实需要时留了逃生口，代价是这次不命中缓存
    assert make_request(system=(SystemBlock("今天是 2026-09-18"),),
                        allow_volatile_system=True)


def test_followup_keeps_the_cached_prefix_untouched():
    request = make_request()
    again = request.followup("上一版草稿", "打回原因")
    assert again.system == request.system
    assert [m.role for m in again.messages] == ["user", "assistant", "user"]


def test_rejects_more_cache_breakpoints_than_supported():
    with pytest.raises(ValueError, match="缓存断点"):
        make_request(system=tuple(SystemBlock(f"第 {i} 段", cache_breakpoint=True)
                                  for i in range(5)))


# ------------------------------------------------- 真实 provider 的请求翻译

class _StubMessages:
    def __init__(self, reply):
        self.reply = reply
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.reply


class _StubClient:
    def __init__(self, reply):
        self.messages = _StubMessages(reply)
        self.beta = type("B", (), {"messages": self.messages})()


class _Block:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Reply:
    def __init__(self, text, stop_reason="end_turn"):
        self.content = [_Block(text)] if text is not None else []
        self.stop_reason = stop_reason
        self.model = "claude-opus-5"
        self.usage = type("U", (), {"input_tokens": 12000, "output_tokens": 800,
                                    "cache_read_input_tokens": 11000,
                                    "cache_creation_input_tokens": 0})()
        self.stop_details = None

    def to_dict(self):
        return {"stop_reason": self.stop_reason}


def test_anthropic_request_shape_is_cache_and_400_safe():
    """新模型上传 temperature / top_p / top_k 直接 400；缓存断点要落在 system 块上。"""
    from tin.llm.anthropic_provider import AnthropicProvider

    client = _StubClient(_Reply('{"tone": "偏多"}'))
    provider = AnthropicProvider(model="claude-opus-5", client=client, fallback=False)

    response = provider.complete(make_request())

    sent = client.messages.kwargs
    assert not {"temperature", "top_p", "top_k"} & set(sent)
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert response.status == STATUS_OK and response.data == {"tone": "偏多"}
    assert (response.cache_read_tokens, response.cache_write_tokens) == (11000, 0)


def test_anthropic_refusal_is_read_before_content():
    """拒答时 content 可能是空的，先判 stop_reason 才不会下标越界。"""
    from tin.llm.anthropic_provider import AnthropicProvider

    provider = AnthropicProvider(client=_StubClient(_Reply(None, stop_reason="refusal")),
                                 fallback=False)
    assert provider.complete(make_request()).status == STATUS_REFUSAL


def test_anthropic_bad_json_keeps_the_body_for_forensics():
    from tin.llm.anthropic_provider import AnthropicProvider

    provider = AnthropicProvider(client=_StubClient(_Reply("这不是 JSON")), fallback=False)
    response = provider.complete(make_request())
    assert response.status == STATUS_ERROR and response.text == "这不是 JSON"


# ---------------------------------------------------------------------- 辅助

def _evidence(**kw):
    from tin.schemas.brief import ClaimEvidence
    return ClaimEvidence(**kw)
