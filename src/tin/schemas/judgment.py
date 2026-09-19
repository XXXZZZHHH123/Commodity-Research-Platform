"""判断单元字段规格（04 §1.4 / §4）。结构校验在这里；“能否生效”的硬规则在 judgments/validate.py。"""

import re
from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TONES = ("偏多", "偏空", "观望", "区间震荡", "回调买入", "反弹做空", "等信号")
PARADIGMS = ("总线合成", "对抗称重", "编排层")
EXPRESSION = re.compile(r"^(value|change)\s*(>=|<=|>|<)\s*(-?\d+(?:\.\d+)?)$")


class ThresholdType(StrEnum):
    风控线 = "风控线"
    介入位 = "介入位"
    观察位 = "观察位"


class Horizon(StrEnum):
    短期 = "短期"
    中长期 = "中长期"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(_Model):
    desc: str
    series_id: str | None = None
    value: float | None = None
    as_of: str | None = None
    source: str | None = None
    note: str | None = None


class Side(_Model):
    claim: str = ""
    evidence: list[Evidence] = Field(default_factory=list)


class Window(_Model):
    start: str | None = Field(None, alias="from")
    to: str | None = None
    note: str | None = None

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Contradiction(_Model):
    bull: Side = Field(default_factory=Side)
    bear: Side = Field(default_factory=Side)
    window: Window | None = None
    text: str | None = None


class MarginalItem(_Model):
    desc: str
    series_id: str | None = None


class CostItem(_Model):
    desc: str
    series_id: str | None = None
    value: float | None = None


class CostLine(_Model):
    type: Literal["cash", "full"] | None = None
    value: float | None = None
    source: str | None = None
    items: list[CostItem] = Field(default_factory=list)
    note: str | None = None


class PricingPower(_Model):
    category: Literal["有LME外盘", "纯宏观", "无外盘"] | None = None
    short: list[str] = Field(default_factory=list)
    long: list[str] = Field(default_factory=list)
    key: str | None = None
    sources: list[str] = Field(default_factory=list)


class TimeDimension(_Model):
    short: str | None = None
    long: str | None = None


class Threshold(_Model):
    id: str
    name: str
    series_id: str
    operator: Literal[">", ">=", "<", "<=", "between"]
    value: float | list[float]
    type: ThresholdType
    horizon: Horizon
    action_hint: str | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _value_shape(self):
        if self.operator == "between":
            if not (isinstance(self.value, list) and len(self.value) == 2 and self.value[0] < self.value[1]):
                raise ValueError(f"阈值 {self.id}：区间必须是 [下限, 上限] 且下限小于上限")
        elif isinstance(self.value, list):
            raise ValueError(f"阈值 {self.id}：运算符 {self.operator} 只接受单个数值")
        return self


class Falsifier(_Model):
    id: str
    desc: str
    series_id: str
    expression: str
    unit_confirmed: str
    consecutive_periods: int = Field(1, ge=1)
    side: Literal["bull", "bear"]
    note: str | None = None

    @field_validator("expression")
    @classmethod
    def _expr(cls, v: str) -> str:
        if not EXPRESSION.match(v.strip()):
            raise ValueError(f"判定表达式无法自动验证：「{v}」，请写成 value/change 比较一个数，如 value >= 10000")
        return v.strip()


class PriorityRule(_Model):
    order: list[str]
    note: str | None = None


class UnstructuredItem(_Model):
    section: str
    text: str
    reason: str


class JudgmentIn(_Model):
    variety: str
    author: str
    written_at: date
    review_period_days: int = Field(30, ge=1)
    contradiction: Contradiction
    marginal_focus: list[MarginalItem] = Field(default_factory=list)
    cost_line: CostLine | None = None
    pricing_power: PricingPower = Field(default_factory=PricingPower)
    time_dimension: TimeDimension | None = None
    survey_notes: str | None = None
    whitelist: list[str] = Field(default_factory=list)
    paradigm: str | None = None
    tone: str | None = None
    tone_note: str | None = None
    thresholds: list[Threshold] = Field(default_factory=list)
    priority_rule: PriorityRule | None = None
    falsifiers: list[Falsifier] = Field(default_factory=list)
    unstructured: list[UnstructuredItem] = Field(default_factory=list)
    source_note: str | None = None

    @field_validator("tone")
    @classmethod
    def _tone(cls, v):
        if v is not None and v not in TONES:
            raise ValueError(f"基调必须取自固定词表：{'/'.join(TONES)}")
        return v

    @field_validator("paradigm")
    @classmethod
    def _paradigm(cls, v):
        if v is not None and v not in PARADIGMS:
            raise ValueError(f"编排范式必须是：{'/'.join(PARADIGMS)}")
        return v

    @model_validator(mode="after")
    def _ids_unique(self):
        ids = [t.id for t in self.thresholds] + [f.id for f in self.falsifiers]
        dup = {i for i in ids if ids.count(i) > 1}
        if dup:
            raise ValueError(f"阈值/证伪条件 id 重复：{sorted(dup)}")
        if self.priority_rule:
            unknown = set(self.priority_rule.order) - {t.id for t in self.thresholds}
            if unknown:
                raise ValueError(f"冲突优先级引用了不存在的阈值：{sorted(unknown)}")
        return self
