"""每日简报的字段规格。LLM 的结构化输出按这里校验，页面与快照也读这个形状。

与 schemas/judgment.py 的分工：judgment 校验研究员写死的硬骨架（阈值、证伪条件），
brief 校验系统每天生成的草稿。两者共用 TONES 词表，保证人和机器说同一种话。

「每条论断必须带证据」这条硬规则在这里只做结构校验（至少有一个引用），
引用是否真实存在、数值是否与库内一致由 llm 层的校验闸门查库确认——
这跟 JudgmentSeriesRef 用外键强制「只能引用已登记指标」是同一个思路。
"""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tin.schemas.judgment import TONES

# 操作倾向的固定词表。行业惯例是倾向不是仓位——「逢低做多」而不是「买入 30%」。
# 跨过这条线就从研究变成了指令，法律与信任成本都变量级上升。
STANCES = ("逢低做多", "高抛低吸", "反弹做空", "观望", "持有", "降敞口")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClaimEvidence(_Model):
    """一条论断的一个证据锚点。series_id 指向事实，playbook_item_id 指向研究员的思路。"""

    series_id: str | None = None
    value: float | None = None
    as_of: str | None = None
    playbook_item_id: int | None = None
    signal_id: int | None = None

    @model_validator(mode="after")
    def _not_empty(self):
        if self.series_id is None and self.playbook_item_id is None and self.signal_id is None:
            raise ValueError("证据必须至少锚定一个 series_id / playbook_item_id / signal_id")
        return self

    # 注意：「有 value 必须有 series_id / as_of」刻意**不**放在这里，而是放在
    # llm/guard.py 的闸门里。pydantic 校验是整份成败——LLM 只要有一条论断漏了 as_of，
    # 整个 BriefDraft 就解析失败，重试再失败就是当天没有简报。闸门按条降级：
    # 坏的那条标灰进 unverified，其余照常出。就绪率 ≥90% 与幻觉率 =0 只有这样才能同时拿到。


class Claim(_Model):
    """简报里的一条论断。空口白话进不来——evidence 至少一条。"""

    text: str
    evidence: list[ClaimEvidence] = Field(min_length=1)
    side: str | None = None  # bull / bear / neutral，用于在页面上分栏呈现

    @model_validator(mode="after")
    def _side(self):
        if self.side is not None and self.side not in ("bull", "bear", "neutral"):
            raise ValueError(f"论断立场只能是 bull/bear/neutral，收到 {self.side!r}")
        return self


class PriceRange(_Model):
    """参考运行区间。是区间不是点位——点位预测在这行不是交付物。"""

    low: float
    high: float
    series_id: str  # 区间挂在哪条序列上，避免「40-41万」说不清是现货还是期货

    @model_validator(mode="after")
    def _ordered(self):
        if self.low >= self.high:
            raise ValueError(f"参考区间下沿必须小于上沿，收到 [{self.low}, {self.high}]")
        return self


class BriefDraft(_Model):
    """LLM 的结构化输出。走 tool use / json_schema 强约束，绝不解析自由文本。"""

    tone: str
    tone_note: str | None = None
    price_range: PriceRange | None = None
    stance: str | None = None
    summary: str
    claims: list[Claim] = Field(default_factory=list)
    # LLM 自报用到了哪些思路条目。后端会与实际喂进 prompt 的集合取交集，
    # 不信任 LLM 自己报的数——它可能引用没给它看过的条目。
    used_playbook_item_ids: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def _vocab(self):
        if self.tone not in TONES:
            raise ValueError(f"基调必须取自固定词表：{'/'.join(TONES)}，收到 {self.tone!r}")
        if self.stance is not None and self.stance not in STANCES:
            raise ValueError(f"操作倾向必须取自固定词表：{'/'.join(STANCES)}，收到 {self.stance!r}")
        return self
