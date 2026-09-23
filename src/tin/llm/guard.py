"""证据强制校验闸门（防幻觉硬闸门，SPEC §4.1 / §6.1 幻觉率 = 0）。

这不是新发明，是把 `JudgmentSeriesRef` 那条「数据库强制只能引用已登记指标」的思路
原样搬到 LLM 输出上——一期已经证明这招管用。

三条判定规则，按严厉程度排：
1. `series_id` 必须在 indicators 里真实存在；
2. 带了 `value` 的，必须与 observations 里那个时点的真实值一致（容差见 EvidenceContext）；
3. `playbook_item_id` / `signal_id` 必须在「本次实际喂进 prompt 的集合」里——
   **不信 LLM 自报的 id**。它完全可能引用一条没给它看过的思路条目，而那条条目恰好存在，
   查库照样能查到；所以判定基准只能是喂进去的那个集合，不是库。

不过的处理：打回重试一次 → 仍不过则该条降级进 `unverified` 并标灰，
**不是整份丢弃**。整份丢弃等于今天没有简报，而一条论断没证据不代表另外五条也没有。
"""

import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from tin.config import SHANGHAI
from tin.llm import audit
from tin.llm.base import STATUS_REFUSAL, LlmError, LlmProvider, LlmRefused, LlmRequest
from tin.models import Indicator, Observation
from tin.schemas.brief import BriefDraft, Claim, ClaimEvidence

# 数值比对容差。浮点往返（入库 / JSON / 模型复述）会丢末位，卡死等于把真话也拦下来；
# 但放到 1e-3 又会让「40.65 万」冒充「40.6 万」蒙混过关。1e-6 相对容差在锡的量级
# （40 万左右）上是 0.4 元，既容得下浮点噪声，又拦得住任何有意义的编造。
DEFAULT_REL_TOL = 1e-6
DEFAULT_ABS_TOL = 1e-6


@dataclass
class EvidenceContext:
    """本次实际喂进 prompt 的东西。校验只认这里的集合。"""

    playbook_item_ids: set[int] = field(default_factory=set)
    signal_ids: set[int] = field(default_factory=set)
    rel_tol: float = DEFAULT_REL_TOL
    abs_tol: float = DEFAULT_ABS_TOL

    def __post_init__(self):
        self.playbook_item_ids = {int(i) for i in self.playbook_item_ids}
        self.signal_ids = {int(i) for i in self.signal_ids}


@dataclass
class GuardResult:
    """校验闸门的产出。`unverified` 非空即为质量警报（SPEC §6.3）。"""

    draft: BriefDraft            # 只含通过校验的论断，可直接进 DailyBrief
    unverified: list[dict]       # 降级标灰的论断，进 DailyBrief.unverified
    used_playbook_item_ids: list[int]
    attempts: int                # 一共发了几次调用（1 = 一次就过）
    call_ids: list[int]          # 对应的 llm_calls.id，页面上「为什么这么说」点进去看这个

    @property
    def clean(self) -> bool:
        return not self.unverified


# ---------------------------------------------------------------- 单条证据的判定

def check_evidence(session: Session, evidence: ClaimEvidence, ctx: EvidenceContext) -> list[str]:
    """校验一个证据锚点，返回问题列表；空列表 = 通过。"""
    problems: list[str] = []

    if evidence.series_id is not None:
        if session.get(Indicator, evidence.series_id) is None:
            # 指标压根不存在，后面的数值比对没有意义，直接返回
            return [f"引用了未登记的指标 {evidence.series_id}"]
    elif evidence.value is not None:
        problems.append(f"给出了数值 {evidence.value} 却没有 series_id，无从核对")

    if evidence.series_id is not None and evidence.value is not None:
        problems.extend(_check_value(session, evidence, ctx))

    if evidence.playbook_item_id is not None and evidence.playbook_item_id not in ctx.playbook_item_ids:
        problems.append(
            f"引用了本次没有喂进 prompt 的思路条目 #{evidence.playbook_item_id}")

    if evidence.signal_id is not None and evidence.signal_id not in ctx.signal_ids:
        problems.append(f"引用了本次没有喂进 prompt 的信号 #{evidence.signal_id}")

    return problems


def _check_value(session: Session, evidence: ClaimEvidence, ctx: EvidenceContext) -> list[str]:
    series_id, value, as_of = evidence.series_id, evidence.value, evidence.as_of

    if as_of is None:
        # 不带时点的数值只能退化成「比最新观测」：今天核得上，明天数据一更新就核不上，
        # 溯源随之失真。§6.1 要求简报里 100% 的数字可点开看到取数时间——那就必须要到。
        # 这条放在闸门而不是 schemas/brief.py：闸门只废掉这一条论断，schema 会废掉整份简报。
        return [f"{series_id} 给了数值 {value} 却没有 as_of，无法锁定是哪一个数据点"]

    moment = _parse_moment(as_of)
    if moment is None:
        return [f"{series_id} 的 as_of「{as_of}」无法解析成时点"]
    obs = _at(session, series_id, moment)
    if obs is None:
        return [f"{series_id} 在 {as_of} 没有观测，无从核对 {value}"]

    if not math.isclose(obs.value, value, rel_tol=ctx.rel_tol, abs_tol=ctx.abs_tol):
        when = obs.as_of.astimezone(SHANGHAI).isoformat()
        return [f"{series_id} 在 {when} 的库内值是 {obs.value}，LLM 报的是 {value}"]
    return []


def _at(session: Session, series_id: str, moment: datetime | date) -> Observation | None:
    """先按精确时点找；找不到再退到「同一自然日」。

    退一步是有意的：口径对得上、数值对得上，只是模型把 15:00 写成了 15:00:00 或者
    丢了时区，不该判成幻觉。真正要拦的是「库里根本没有这个数」。
    """
    base = select(Observation).where(Observation.series_id == series_id)
    if isinstance(moment, datetime):
        exact = session.scalars(
            base.where(Observation.as_of == moment).order_by(Observation.revision.desc()).limit(1)
        ).first()
        if exact is not None:
            return exact
        day = moment.astimezone(SHANGHAI).date()
    else:
        day = moment

    start = datetime.combine(day, datetime.min.time(), tzinfo=SHANGHAI)
    return session.scalars(
        base.where(Observation.as_of >= start, Observation.as_of < start + timedelta(days=1))
        .order_by(Observation.as_of.desc(), Observation.revision.desc()).limit(1)
    ).first()


def _parse_moment(text: str) -> datetime | date | None:
    raw = text.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        # 库里全是带时区的 UTC；不带时区的一律按上海时间理解（取数与录入都在这个时区）
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed


# ---------------------------------------------------------------- 整份草稿的判定

def check_claim(session: Session, claim: Claim, ctx: EvidenceContext) -> list[str]:
    problems: list[str] = []
    for idx, evidence in enumerate(claim.evidence, 1):
        problems.extend(f"证据 {idx}：{p}" for p in check_evidence(session, evidence, ctx))
    return problems


def verify_draft(session: Session, draft: BriefDraft,
                 ctx: EvidenceContext) -> tuple[list[Claim], list[dict]]:
    """逐条校验。返回（通过的论断, 降级条目）。"""
    kept: list[Claim] = []
    rejected: list[dict] = []

    for claim in draft.claims:
        problems = check_claim(session, claim, ctx)
        if problems:
            rejected.append({
                "field": "claim",
                "claim": claim.text,
                "side": claim.side,
                "evidence": [e.model_dump() for e in claim.evidence],
                "reasons": problems,
            })
        else:
            kept.append(claim)

    if draft.price_range is not None:
        if session.get(Indicator, draft.price_range.series_id) is None:
            rejected.append({
                "field": "price_range",
                "claim": f"参考运行区间 [{draft.price_range.low}, {draft.price_range.high}]",
                "side": None,
                "evidence": [{"series_id": draft.price_range.series_id}],
                "reasons": [f"区间挂在未登记的指标 {draft.price_range.series_id} 上"],
            })

    return kept, rejected


def _degrade(draft: BriefDraft, kept: list[Claim], rejected: list[dict],
             ctx: EvidenceContext) -> BriefDraft:
    """把没过校验的部分从草稿里摘掉，剩下的照常出。"""
    update: dict = {
        "claims": kept,
        # LLM 自报用了哪些条目一律与实际喂进去的集合取交集，多报的直接丢
        "used_playbook_item_ids": sorted(set(draft.used_playbook_item_ids) & ctx.playbook_item_ids),
    }
    if any(r["field"] == "price_range" for r in rejected):
        update["price_range"] = None
    return draft.model_copy(update=update)


# ---------------------------------------------------------------- 调用 + 闸门

def compose(session: Session, provider: LlmProvider, request: LlmRequest,
            ctx: EvidenceContext, *, retries: int = 1) -> GuardResult:
    """走一次「调用 → 校验 → 打回重试 → 降级」的完整闸门。

    `retries=1` 就是 SPEC 说的「打回重试一次」。重试用同一段 system 前缀追加对话，
    所以缓存照样命中，重试的成本远低于重开一轮。
    """
    call_ids: list[int] = []
    attempts = 0
    draft: BriefDraft | None = None
    kept: list[Claim] = []
    rejected: list[dict] = []
    current = request

    while True:
        attempts += 1
        response = audit.invoke(session, provider, current)
        if response.call_id is not None:
            call_ids.append(response.call_id)

        if response.status == STATUS_REFUSAL:
            # 拒答重试同一段 prompt 没有意义，交给上层降级为「只做确定性信号，不出简报」
            raise LlmRefused(f"{provider.name} 拒绝了本次 {request.purpose} 调用")

        try:
            draft = BriefDraft.model_validate(response.data or {})
        except ValidationError as exc:
            problems = [f"输出不符合简报契约：{exc.error_count()} 处，首个：{exc.errors()[0]['msg']}"]
            if attempts > retries:
                raise LlmError(f"{provider.name} 连续 {attempts} 次返回不符合契约的简报：{exc}") from exc
            current = current.followup(_echo(response.text, response.data), _feedback_text(problems))
            continue

        kept, rejected = verify_draft(session, draft, ctx)
        if not rejected or attempts > retries:
            break
        current = current.followup(
            _echo(response.text, response.data),
            _feedback_text([f"「{r['claim']}」：{'；'.join(r['reasons'])}" for r in rejected]),
        )

    return GuardResult(
        draft=_degrade(draft, kept, rejected, ctx),
        unverified=rejected,
        used_playbook_item_ids=sorted(set(draft.used_playbook_item_ids) & ctx.playbook_item_ids),
        attempts=attempts,
        call_ids=call_ids,
    )


def _echo(text: str | None, data: dict | None) -> str:
    if text:
        return text
    return json.dumps(data or {}, ensure_ascii=False)


def _feedback_text(problems: list[str]) -> str:
    """打回时说清楚哪一条为什么不过。含糊的「请重试」换不回更好的结果。"""
    lines = "\n".join(f"- {p}" for p in problems)
    return (
        "上一版草稿没有通过证据校验，以下条目被打回：\n"
        f"{lines}\n\n"
        "请重新输出完整的简报草稿，并遵守：\n"
        "1. 只引用上文实际给出的 series_id、思路条目编号和信号编号，不要凭印象补；\n"
        "2. 引用数值时必须与上文给出的数值逐位一致，不要换算、不要取整；\n"
        "3. 举不出证据的论断直接删掉，不要保留也不要换个说法。"
    )
