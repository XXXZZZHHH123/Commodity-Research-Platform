"""首屏工作台（F9）的数据组装：待办 / 今日简报 / 昨夜变化 / 全景数据墙。

北极星是「我每天真的会打开它」（SPEC §1.3），所以首屏不是数据墙，是「今天我该干什么」。
数据墙做得再全也不会让人第二天想打开——它在 `/sn` 已经有了，这里默认折叠。

这一层只取数与拼装，**不新增任何业务规则**：
- 待办来自 `compute/signals.py` 已落库的 `review_tasks` 与 `signals`，不在这里重算；
- 简报只读 `daily_briefs`，落库与评审一律走 `brief/store.py`（唯一入口）；
- 数据墙与异动直接用 `export/board.py` 的现成组装，页面与快照永远同源。

所有查询按 `researcher_id` 过滤。一期只有一个人，但维度从第一天就带上——
M3 多人时不用回来重写这一屏。
"""

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from tin.compute.engine import day_end, latest_obs
from tin.compute.signals import TRIGGER_EXPIRED, TRIGGER_FALSIFIED
from tin.config import SHANGHAI
from tin.export.board import core_cards, macro_featured
from tin.models import DailyBrief, Indicator, Judgment, LlmCall, Observation, PlaybookItem, ReviewTask, Signal

# 第三类待办：指标断供。前两类是 ReviewTask.trigger_type 的取值，这一类没有对应的
# 待办行——它来自 `signals.state == '数据缺失'`，是「阈值和证伪条件白写」的当日证据。
TODO_GAP = "指标断供"
TODO_ORDER = {TRIGGER_FALSIFIED: 0, TRIGGER_EXPIRED: 1, TODO_GAP: 2}


def _local(dt: datetime | None) -> str | None:
    return dt.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M") if dt else None


# ---------------------------------------------------------------- 待办


def _rules(j: Judgment | None) -> dict[str, dict]:
    """rule_id → 规则的人话描述。signals 只存 rule_id，页面要显示名字得回判断里找。"""
    if j is None:
        return {}
    out: dict[str, dict] = {}
    for t in j.thresholds or []:
        out[t["id"]] = {"name": t["name"], "series_id": t["series_id"], "kind": "阈值"}
    for f in j.falsifiers or []:
        out[f["id"]] = {"name": f["desc"], "series_id": f["series_id"], "kind": "证伪条件",
                        "expression": f["expression"], "periods": f.get("consecutive_periods") or 1}
    return out


def todos(session: Session, variety: str, d: date | None) -> list[dict]:
    """待办：少而紧急。证伪触发 / 判断到期 取自 review_tasks，指标断供取自当日 signals。

    按 variety 关联而不是按「当前判断」：研究员换了新版本判断之后，旧版本上还没处理完的
    待办不能凭空消失——那是真的没做完，不是做完了。
    """
    juds: dict[int, Judgment | None] = {}
    rules: dict[int, dict] = {}

    def judgment(jid: int) -> Judgment | None:
        if jid not in juds:
            juds[jid] = session.get(Judgment, jid)
            rules[jid] = _rules(juds[jid])
        return juds[jid]

    out: list[dict] = []
    today = datetime.now(SHANGHAI).date()

    tasks = session.scalars(
        select(ReviewTask).join(Judgment, ReviewTask.judgment_id == Judgment.id)
        .where(Judgment.variety == variety, ReviewTask.state == "待处理")
        .order_by(ReviewTask.created_at.desc(), ReviewTask.id.desc())
    ).all()
    for t in tasks:
        j = judgment(t.judgment_id)
        if j is None:
            continue
        item = {"kind": t.trigger_type, "assignee": t.assignee, "series_id": None,
                "created_at": _local(t.created_at), "href": "/sn/judgment", "cta": "去复盘",
                "title": t.trigger_type, "detail": ""}
        if t.trigger_type == TRIGGER_FALSIFIED and t.signal_id:
            s = session.get(Signal, t.signal_id)
            rule = rules[t.judgment_id].get(s.rule_id, {}) if s else {}
            item["title"] = rule.get("name") or (s.rule_id if s else "证伪条件成立")
            item["series_id"] = rule.get("series_id")
            note = (f"{rule['expression']}，连续 {rule['periods']} 期成立"
                    if rule.get("expression") else None)
            last = (s.evidence or [])[-1] if s and s.evidence else None
            item["detail"] = "；".join(x for x in (note, (last or {}).get("desc")) if x)
        elif t.trigger_type == TRIGGER_EXPIRED:
            overdue = (today - j.review_due).days
            item["title"] = f"判断 v{j.version} 已到复盘期"
            item["detail"] = (f"复盘期 {j.review_due.isoformat()}"
                              + (f"，已超期 {overdue} 天" if overdue > 0 else ""))
        out.append(item)

    if d is not None:
        seen: set[str] = set()
        missing = session.scalars(
            select(Signal).join(Judgment, Signal.judgment_id == Judgment.id)
            .where(Judgment.variety == variety, Signal.trade_date == d.isoformat(),
                   Signal.state == "数据缺失")
            .order_by(Signal.rule_type, Signal.rule_id)
        ).all()
        for s in missing:
            judgment(s.judgment_id)
            rule = rules.get(s.judgment_id, {}).get(s.rule_id, {})
            sid = rule.get("series_id")
            key = sid or s.rule_id
            # 同一条序列断供会让好几条规则一起算不出来，那仍然只是一件事：这个数没来。
            # 不去重的话首屏待办会被同一个缺口刷屏，「少而紧急」就废了。
            if key in seen:
                continue
            seen.add(key)
            ind = session.get(Indicator, sid) if sid else None
            manual = ind is not None and ind.fetch_mode == "manual"
            name = ind.name if ind else (sid or s.rule_id)
            out.append({
                "kind": TODO_GAP, "title": f"{name} 今日无可用观测", "series_id": sid,
                "detail": s.gap_note or f"{rule.get('kind') or '规则'}「{rule.get('name') or s.rule_id}」无法比对",
                "assignee": None, "created_at": None,
                "href": f"/sn/entry?series_id={sid}" if manual else "/sn/indicators",
                "cta": "去补录" if manual else "看指标登记"})

    return sorted(out, key=lambda x: TODO_ORDER.get(x["kind"], 9))


# ---------------------------------------------------------------- 今日简报


def _observation(session: Session, series_id: str, as_of: str | None, d: date) -> Observation | None:
    """找出证据里那个数对应的那一行观测——溯源要的是当时那一条，不是今天最新那一条。"""
    if as_of:
        try:
            when = datetime.fromisoformat(as_of)
        except ValueError:
            when = None
        if when is not None:
            if when.tzinfo is None:
                when = when.replace(tzinfo=SHANGHAI)
            row = session.scalars(
                select(Observation).where(Observation.series_id == series_id,
                                          Observation.as_of == when)
                .order_by(Observation.revision.desc()).limit(1)).first()
            if row is not None:
                return row
    return latest_obs(session, series_id, day_end(d))


def _evidence_view(session: Session, ev: dict, d: date, playbook: dict[int, str]) -> dict:
    """一个证据锚点摊成页面能点开的形状。

    §6.1 要求 100% 的数字可点开看到 series_id、取数时间、来源 URL，所以缺什么就写什么，
    不做「查不到就不显示」——查不到本身就是要让人看见的信息。
    """
    sid = ev.get("series_id")
    item = {"series_id": sid, "value": ev.get("value"), "as_of": ev.get("as_of"),
            "name": None, "unit": "", "source": None, "source_url": None, "revision": None,
            "playbook_item_id": ev.get("playbook_item_id"), "playbook_text": None,
            "signal_id": ev.get("signal_id"), "note": None}
    if ev.get("playbook_item_id"):
        item["playbook_text"] = playbook.get(ev["playbook_item_id"], "该思路条目已被删除")
    if not sid:
        return item
    ind = session.get(Indicator, sid)
    if ind is not None:
        item["name"], item["unit"] = ind.name, ind.unit
    o = _observation(session, sid, ev.get("as_of"), d)
    if o is None:
        item["note"] = "库内暂时查不到这个时点的观测"
        return item
    item.update({"source": o.source, "source_url": o.source_url, "revision": o.revision})
    if item["as_of"] is None:
        item["as_of"] = o.as_of.astimezone(SHANGHAI).isoformat()
    if item["value"] is None:
        item["value"] = o.value
    return item


def _claim_view(session: Session, claim: dict, d: date, playbook: dict[int, str]) -> dict:
    return {"text": claim.get("text") or claim.get("claim") or "",
            "side": claim.get("side"),
            "evidence": [_evidence_view(session, e, d, playbook)
                         for e in (claim.get("evidence") or [])]}


def brief_view(session: Session, brief: DailyBrief, d: date) -> dict:
    """把 DailyBrief 摊成页面要的形状。只读，不写——评审一律走 `store.review`。"""
    ids = list(brief.playbook_item_ids or [])
    for c in (brief.claims or []) + (brief.unverified or []):
        for e in c.get("evidence") or []:
            if e.get("playbook_item_id"):
                ids.append(e["playbook_item_id"])
    playbook = {}
    if ids:
        playbook = {p.id: p.text for p in
                    session.scalars(select(PlaybookItem).where(PlaybookItem.id.in_(set(ids))))}

    range_name = None
    if brief.range_series_id:
        ind = session.get(Indicator, brief.range_series_id)
        range_name = ind.name if ind else None

    return {
        "id": brief.id,
        "status": brief.status,
        # 「AI 草稿，未经研究员确认」的开关。研究员一动作就摘掉——摘不掉的标注等于没标注。
        "is_draft": brief.status == "草稿",
        "trade_date": brief.trade_date,
        "tone": brief.tone,
        "stance": brief.stance,
        "summary": brief.summary,
        "range_low": brief.range_low,
        "range_high": brief.range_high,
        "range_series_id": brief.range_series_id,
        "range_name": range_name,
        "claims": [_claim_view(session, c, d, playbook) for c in (brief.claims or [])],
        # 「改」要回传完整 claims，回传的必须是库里原样这份：页面那份补过指标名与来源，
        # 写回去就把渲染用的装饰混进了证据链。
        "claims_raw": list(brief.claims or []),
        # 非空即为质量警报，必须标灰摆在台面上，不能藏（SPEC §4.1 证据强制校验）
        "unverified": [{
            "claim": u.get("claim") or u.get("text") or "",
            "side": u.get("side"),
            "field": u.get("field") or "claim",
            "reasons": u.get("reasons") or u.get("problems") or [],
            "evidence": [_evidence_view(session, e, d, playbook) for e in (u.get("evidence") or [])],
        } for u in (brief.unverified or [])],
        "playbook": [{"id": i, "text": playbook.get(i, "（条目已删除）")}
                     for i in (brief.playbook_item_ids or [])],
        "signal_ids": list(brief.signal_ids or []),
        "model": brief.model,
        "generated_at": _local(brief.generated_at),
        "reviewed_at": _local(brief.reviewed_at),
        "reviewed_by": brief.reviewed_by,
    }


def brief_gap(session: Session, researcher, d: date | None) -> str:
    """没有今日简报时说明原因，而不是让首屏整页崩。

    §7 的风险应对写得很清楚：外部 API 断连时降级为「只做确定性信号，不出简报」，
    而不是出一份差的。所以这里只解释，不兜底生成——生成是 B1 的活。
    """
    if researcher is None:
        return "尚未初始化研究员档案：先跑一次 python -m tin.jobs init，简报才有归属。"
    if d is None:
        return "库里还没有任何交易日数据：先跑 python -m tin.jobs daily 取一次数。"
    # 最近一次组装调用。不在 SQL 里按日期切片：created_at 是 UTC，按上海日切片要绕一圈，
    # 而「最后一次跑成什么样」本来就是最近一条说了算。
    last = session.scalars(
        select(LlmCall).where(LlmCall.purpose == "compose_brief")
        .order_by(LlmCall.id.desc()).limit(1)).first()
    if last is not None and last.status != "ok":
        return (f"{d.isoformat()} 的简报组装没跑通：{last.error or last.status}"
                f"（llm_calls #{last.id}）。确定性信号与异动不受影响，照常可用。")
    return (f"{d.isoformat()} 还没有简报：LLM 未接通或组装任务尚未执行。"
            "待办与异动照常可用——简报缺席不影响你今天该干的事。")


# ---------------------------------------------------------------- 昨夜到今早什么变了


def overnight(session: Session, variety: str, d: date) -> dict:
    """异动归因：核心数字与上一数据点的变化，按幅度从大到小。

    用 board.core_cards 的现成结果而不是另查一遍：另查一遍必然会出现
    「首屏说涨 2%、总览说涨 1.8%」，那比不显示还糟。
    """
    core = []
    for c in core_cards(session, variety, d):
        if c.status != "ok" or not c.change:
            continue
        core.append({"label": c.label, "unit": c.unit, "value": c.value, "change": c.change,
                     "change_pct": c.change_pct, "as_of": c.as_of, "source": c.source,
                     "series_id": c.series_id or (c.detail or {}).get("formula"),
                     "sub": c.sub, "key": c.key})
    core.sort(key=lambda r: abs(r["change_pct"] or 0), reverse=True)
    macro = [m for m in macro_featured(session, d) if m.get("change")]
    return {"core": core, "macro": macro}
