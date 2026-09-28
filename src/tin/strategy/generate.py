"""Generate draft strategies using a frozen whitelist of real observations.

Market-date cutoff and knowledge cutoff are separate: requesting an old market date
does not pretend that observations imported today were known at that old date.
No candidate is persisted until every candidate passes the same evidence gate.
"""

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from tin.compute.engine import day_end
from tin.config import SHANGHAI
from tin.judgments.service import to_payload
from tin.llm import build_provider
from tin.llm.audit import invoke
from tin.llm.base import LlmError, LlmProvider
from tin.models import Indicator, Judgment, Observation
from tin.schemas.strategy import StrategyPlan
from tin.strategy.prompt import build_request
from tin.strategy.service import create_strategy

CONTRACT = re.compile(r"^SHFE\.SN\.(\d{4})\.(close|settle)$")
MAX_AGE_DAYS = {"日": 7, "周": 21, "月": 75, "季": 150, "年": 400}


class CandidateBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidates: list[StrategyPlan] = Field(default_factory=list, max_length=3)
    gaps: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def no_silent_abstention(self):
        if not self.candidates and not any(x.strip() for x in self.gaps):
            raise ValueError("无策略时必须说明数据或研究缺口")
        return self


@dataclass
class GenerationResult:
    status: Literal["generated", "no_candidate", "error"]
    strategies: list = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    call_id: int | None = None


def build_context(session: Session, variety: str, d: date, *, researcher_id: int,
                  research_question: str = "", knowledge_cutoff: datetime | None = None
                  ) -> tuple[dict, list[str]]:
    """Return exactly the context exposed to the model, and blocking gaps."""
    captured = datetime.now(timezone.utc)
    known = knowledge_cutoff or captured
    if known.tzinfo is None or known > captured:
        raise ValueError("知识截止时点必须带时区且不能晚于当前时间")
    cutoff = min(day_end(d), known)
    context = {
        "schema_version": 1, "variety": variety, "trade_date": d.isoformat(),
        "researcher_id": researcher_id, "research_question": research_question[:4000],
        "captured_at": captured.isoformat(), "knowledge_cutoff": known.isoformat(),
        "market_cutoff_exclusive": cutoff.isoformat(),
        "replay_note": "以知识截止时点已入库的数据研究指定行情日期，不是历史当时可知回测",
        "observations": [], "available_contracts": [], "research": None,
        "market_as_of": None, "data_gaps": [],
    }
    if variety != "SN":
        return context, ["首版策略生成仅支持 SN"]
    judgment = session.scalars(select(Judgment).where(
        Judgment.variety == variety, Judgment.status == "生效",
        Judgment.written_at <= d, Judgment.created_at <= known,
    ).order_by(Judgment.version.desc())).first()
    blockers = []
    if judgment is None:
        blockers.append("缺少截至研究日已生效的研究判断，请先建立并生效判断")
    elif judgment.review_due < d:
        blockers.append("研究判断已超过复核日期，请先更新研究依据")
    else:
        context["research"] = {"id": judgment.id, "version": judgment.version,
                               "payload": to_payload(judgment).model_dump(mode="json", by_alias=True)}

    indicators = list(session.scalars(select(Indicator).where(
        Indicator.variety.in_([variety, "COMMON"]))))
    for indicator in indicators:
        observations = list(session.scalars(select(Observation).where(
            Observation.series_id == indicator.series_id, Observation.as_of < cutoff,
            Observation.fetched_at <= known, Observation.status == "正常",
        ).order_by(Observation.as_of.desc(), Observation.revision.desc()).limit(12)))
        seen_times = set()
        limit = MAX_AGE_DAYS.get(indicator.frequency, 7)
        for observation in observations:
            if observation.as_of in seen_times:
                continue
            seen_times.add(observation.as_of)
            if len(seen_times) > 3:
                break
            if cutoff - observation.as_of > timedelta(days=limit):
                continue
            if not math.isfinite(observation.value):
                continue
            context["observations"].append({
                "observation_id": observation.id, "series_id": observation.series_id,
                "value": observation.value, "as_of": observation.as_of.isoformat(),
                "fetched_at": observation.fetched_at.isoformat(), "source": observation.source,
                "source_url": observation.source_url, "revision": observation.revision,
                "unit": indicator.unit, "caliber": observation.caliber_snapshot,
            })
        if not any(o["series_id"] == indicator.series_id for o in context["observations"]):
            context["data_gaps"].append(f"{indicator.series_id} 缺少近期可用观测")

    quotes = {}
    for observation in context["observations"]:
        match = CONTRACT.fullmatch(observation["series_id"])
        if not match:
            continue
        month = match[1]
        if not 1 <= int(month[2:]) <= 12 or int(month) < int(d.strftime("%y%m")):
            continue
        quote_time = datetime.fromisoformat(observation["as_of"])
        if quote_time.astimezone(SHANGHAI).date() != d or observation["value"] <= 0:
            continue
        key = f"SN{month}"
        existing = quotes.get(key)
        if existing is None or (match[2] == "close" and existing["series_id"].endswith("settle")):
            quotes[key] = observation
    context["available_contracts"] = [
        {"contract": name, "quote_series_id": obs["series_id"],
         "observation_id": obs["observation_id"]}
        for name, obs in sorted(quotes.items())
    ]
    if not quotes:
        blockers.append("指定行情日缺少具体 SN 合约报价，不能用主力连续代替可交易合约")
    else:
        context["market_as_of"] = max(o["as_of"] for o in quotes.values())
    if not any(not o["series_id"].startswith("SHFE.SN.") or
               o["series_id"] in {"SHFE.SN.warrant", "SHFE.SN.stock.weekly"}
               for o in context["observations"]):
        blockers.append("只有合约行情，缺少近期产业或宏观证据，不能据此编造目标价格")
    return context, blockers


def validate_evidence(plan: StrategyPlan, context: dict) -> list[str]:
    """Validate the exact values sent to the model, not merely registered IDs."""
    errors = []
    whitelist = {o["observation_id"]: o for o in context["observations"]}
    quoted = set()
    for evidence in plan.evidence:
        observation = whitelist.get(evidence.observation_id)
        if observation is None:
            errors.append(f"证据 #{evidence.observation_id} 不在输入观测白名单中")
            continue
        quoted.add(evidence.observation_id)
        if evidence.series_id != observation["series_id"]:
            errors.append("证据指标与实际输入不一致")
        if evidence.value is None or evidence.value != observation["value"]:
            errors.append(f"{observation['series_id']} 引用数值与输入观测不一致")
        if evidence.as_of != datetime.fromisoformat(observation["as_of"]):
            errors.append(f"{observation['series_id']} 引用时点与输入观测不一致")
        if evidence.source != observation["source"]:
            errors.append(f"{observation['series_id']} 引用来源与输入观测不一致")
    contracts = {c["contract"]: c for c in context["available_contracts"]}
    for leg in plan.legs:
        actual = contracts.get(leg.contract)
        if actual is None or actual["quote_series_id"] != leg.quote_series_id:
            errors.append(f"{leg.contract} 未引用输入中具体合约的报价")
        elif actual["observation_id"] not in quoted:
            errors.append(f"{leg.contract} 缺少对应报价的完整证据引用")
        if leg.multiplier != 1:
            errors.append("SN 合约乘数必须为 1 吨/手")
    if plan.as_of != datetime.fromisoformat(context["market_as_of"]):
        errors.append("策略行情时点必须等于输入 market_as_of")
    if plan.mode != "simulation":
        errors.append("AI 只能生成模拟草稿")
    if plan.max_quote_age_hours > 72:
        errors.append("不得放宽报价时效至 72 小时以上")
    if not getattr(plan, "alternative_explanation", "").strip():
        errors.append("缺少竞争性解释")
    return errors


def generate_candidates(session: Session, variety: str, d: date, *, researcher_id: int,
                        provider: LlmProvider | None = None, research_question: str = "",
                        knowledge_cutoff: datetime | None = None) -> GenerationResult:
    context, blockers = build_context(session, variety, d, researcher_id=researcher_id,
                                     research_question=research_question,
                                     knowledge_cutoff=knowledge_cutoff)
    if blockers:
        return GenerationResult("no_candidate", gaps=blockers)
    request = build_request(context, CandidateBatch.model_json_schema())
    try:
        response = invoke(session, provider or build_provider(), request)
    except LlmError as exc:
        return GenerationResult("error", gaps=[f"策略生成失败：{exc}"])
    if not response.ok:
        return GenerationResult("no_candidate", gaps=["模型拒绝生成策略，请补充研究依据"],
                                call_id=response.call_id)
    try:
        batch = CandidateBatch.model_validate(response.data)
    except ValidationError as exc:
        return GenerationResult("no_candidate", gaps=[f"模型策略未通过结构校验：{exc}"],
                                call_id=response.call_id)
    errors = [error for candidate in batch.candidates
              for error in validate_evidence(candidate, context)]
    if errors:
        return GenerationResult("no_candidate", gaps=errors, call_id=response.call_id)
    rows = []
    for candidate in batch.candidates:
        # Provenance is supplied by the server, never entrusted to model output.
        candidate = candidate.model_copy(update={
            "source_context": context, "llm_call_id": response.call_id,
        })
        rows.append(create_strategy(session, candidate, actor=f"researcher:{researcher_id}",
                                    reason="AI 生成待审查模拟策略；未发布、未成交"))
    return GenerationResult("generated" if rows else "no_candidate", strategies=rows,
                            gaps=batch.gaps, call_id=response.call_id)
