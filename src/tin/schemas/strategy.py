"""Explicit, reviewable SN trading plans. Prices are CNY/tonne, PnL is CNY."""
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


class PriceBand(_Model):
    low: float
    high: float
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def ordered(self):
        if self.low > self.high:
            raise ValueError("区间下限不得大于上限")
        if any(abs(value / 10 - round(value / 10)) > 1e-8 for value in (self.low, self.high)):
            raise ValueError("沪锡价格区间须按 10 元/吨最小变动价位报价")
        return self


class StrategyLeg(_Model):
    contract: str = Field(pattern=r"^SN\d{2}(0[1-9]|1[0-2])$")
    side: Literal["buy", "sell"]
    ratio: int = Field(default=1, ge=1, le=10000, strict=True)
    multiplier: float = Field(default=1, gt=0)
    quote_series_id: str

    @model_validator(mode="after")
    def specific_quote(self):
        if self.multiplier != 1:
            raise ValueError("SN 合约乘数必须为 1 吨/手")
        if self.quote_series_id not in {
            f"SHFE.SN.{self.contract[2:]}.close", f"SHFE.SN.{self.contract[2:]}.settle"
        }:
            raise ValueError("行情必须是该具体合约的 close 或 settle；不能使用主连")
        return self


class StrategyEvidence(_Model):
    series_id: str | None = None
    as_of: AwareDatetime
    source: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    value: float | None = None
    observation_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def numeric_reference(self):
        if self.series_id is not None or self.value is not None or self.observation_id is not None:
            if not self.series_id or self.value is None or self.observation_id is None:
                raise ValueError("定量证据必须同时提供指标、数值和观测编号")
        return self


class StrategyPlan(_Model):
    title: str = Field(min_length=1, max_length=200)
    variety: Literal["SN"] = "SN"
    kind: Literal["outright", "calendar_spread"]
    mode: Literal["simulation", "actual"] = "simulation"
    legs: list[StrategyLeg] = Field(min_length=1, max_length=2)
    thesis: str = Field(min_length=1)
    alternative_explanation: str = Field(min_length=1)
    evidence: list[StrategyEvidence] = Field(min_length=1)
    market_expectation: str = Field(min_length=1)
    invalidation: str = Field(min_length=1)
    catalyst: str = Field(min_length=1)
    horizon_days: int = Field(ge=1, le=366)
    as_of: AwareDatetime
    review_at: AwareDatetime
    entry: PriceBand
    target: PriceBand
    stop: PriceBand
    entry_condition: str = Field(min_length=1)
    risk_budget: float = Field(gt=0)
    max_quote_age_hours: float = Field(default=72, gt=0, le=168)
    max_leg_skew_minutes: float = Field(default=60, ge=0, le=1440)
    source_context: dict = Field(default_factory=dict)
    llm_call_id: int | None = None

    @model_validator(mode="after")
    def coherent(self):
        if self.review_at <= self.as_of:
            raise ValueError("复核时间必须晚于研究数据截止时间")
        if any(e.as_of > self.as_of for e in self.evidence):
            raise ValueError("证据时间不得晚于研究数据截止时间")
        if self.kind == "outright":
            if len(self.legs) != 1:
                raise ValueError("单边策略必须只有一条腿")
            if min(self.entry.low, self.target.low, self.stop.low) <= 0:
                raise ValueError("单边价格必须为正")
        else:
            if len(self.legs) != 2:
                raise ValueError("跨期策略必须有两条腿")
            near, far = self.legs
            if near.contract >= far.contract:
                raise ValueError("跨期腿必须按近月、远月顺序且合约不同")
            if near.side == far.side or near.ratio != far.ratio:
                raise ValueError("一期跨期必须等手数、相反方向")
            if near.quote_series_id.rsplit('.', 1)[1] != far.quote_series_id.rsplit('.', 1)[1]:
                raise ValueError("跨期两腿须使用相同价格口径")
        if self.legs[0].side == "buy":
            valid = self.stop.high < self.entry.low and self.entry.high < self.target.low
        else:
            valid = self.target.high < self.entry.low and self.entry.high < self.stop.low
        if not valid:
            raise ValueError("目标、入场和止损区间必须与策略方向一致且不重叠")
        return self


class LegFill(_Model):
    contract: str = Field(pattern=r"^SN\d{2}(0[1-9]|1[0-2])$")
    price: float = Field(gt=0)
    quantity: int = Field(gt=0, strict=True)
    filled_at: AwareDatetime
    costs: float = Field(default=0, ge=0)

    @model_validator(mode="after")
    def tick_size(self):
        if abs(self.price / 10 - round(self.price / 10)) > 1e-8:
            raise ValueError("沪锡成交价须按 10 元/吨最小变动价位记录")
        return self


class ExecutionIn(_Model):
    fills: list[LegFill] = Field(min_length=1, max_length=2)
    reason: str = Field(min_length=1)
