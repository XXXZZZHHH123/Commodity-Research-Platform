"""L1 事实与 L2 判断之间的确定性比对：阈值测距 + 证伪判定，产出 signals / review_tasks。

这一层刻意不经过 LLM（SPEC §4.3）：**阈值有没有触发是算出来的，不是问出来的。**
LLM 只在下游 compose_brief 出现，而它看到的 signals 已经是算好的事实。

同理，Signal 只有「未触发 / 触发 / 数据缺失」三态、没有「建议」字段：信号陈述事实，
建议由简报层给出。取不到数写「数据缺失」而不是跳过——静默跳过会让研究员误以为该条
规则今天安全，这正是 L1「不写 NULL 行、但把缺口摆上台面」的同一条纪律。
"""

import operator
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from tin.compute.engine import day_end, latest_obs
from tin.config import SHANGHAI
from tin.export.board import threshold_radar
from tin.judgments.service import current as current_judgment
from tin.models import Indicator, Judgment, Observation, ReviewTask, Signal
from tin.schemas.judgment import EXPRESSION

# rule_type 与 JudgmentSeriesRef.ref_type 用同一套取值，signals 才能按
# (judgment_id, rule_type, rule_id) 直接 join 回引用表拿 series_id，不必再解析判断 JSON。
THRESHOLD, FALSIFIER = "threshold", "falsifier"

TRIGGER_FALSIFIED, TRIGGER_EXPIRED = "证伪触发", "判断到期"

# threshold_radar 的词表比 signals 宽（多一个「在区间内」）。落库口径必须收敛到三态，
# 否则下游（简报、首屏待办）要认两套词；区间命中同样是「触发」。
RADAR_STATE = {"已触发": "触发", "在区间内": "触发", "未触发": "未触发", "数据缺失": "数据缺失"}

OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le}


def _iso(dt: datetime) -> str:
    return dt.astimezone(SHANGHAI).isoformat()


def _evidence(o: Observation, desc: str, value: float, note: str | None = None) -> dict:
    """一条证据 = 一个真实数据点。

    字段沿用 schemas.judgment.Evidence 的形状（desc/series_id/value/as_of/source/note），
    另带 obs_id 与 source_url：验收要求简报里 100% 的数字可点开看到取数时间与来源（§6.1），
    而 signals 是简报唯一被允许引用的事实入口，溯源信息必须在这里就齐。
    """
    return {"desc": desc, "series_id": o.series_id, "value": value, "as_of": _iso(o.as_of),
            "source": o.source, "source_url": o.source_url, "obs_id": o.id, "note": note}


def recent_points(session: Session, series_id: str, d: date, need: int) -> list[Observation]:
    """截至 d 的最近 need 个数据点，按时间升序；同一时点只保留最高修订号。

    连续期判定按「期」算而不是按行算：一个数据点被修订三次仍然只是一期，
    不去重会把修订历史误当成三期连续成立。
    """
    end = day_end(d)
    stamps = session.scalars(
        select(Observation.as_of).distinct()
        .where(Observation.series_id == series_id, Observation.as_of < end)
        .order_by(Observation.as_of.desc()).limit(need)
    ).all()
    if not stamps:
        return []
    rows = session.scalars(
        select(Observation)
        .where(Observation.series_id == series_id, Observation.as_of.in_(stamps))
        .order_by(Observation.as_of.asc(), Observation.revision.desc())
    ).all()
    newest: dict[datetime, Observation] = {}
    for r in rows:
        newest.setdefault(r.as_of, r)
    return sorted(newest.values(), key=lambda x: x.as_of)


def _upsert(session: Session, judgment_id: int, rule_id: str, rule_type: str, d: date,
            state: str, current_value: float | None, evidence: list, gap_note: str | None) -> Signal:
    """同一 judgment + rule_id + trade_date 只留一行，重跑覆盖。

    signals 表上没有这个组合的唯一索引（建表在一期，改索引要动 alembic），所以幂等在代码里做：
    每天两班取数都会重算同一天，插入式写法会让同一条规则一天堆出两三行互相矛盾的记录。
    """
    row = session.scalars(
        select(Signal).where(Signal.judgment_id == judgment_id, Signal.rule_id == rule_id,
                             Signal.trade_date == d.isoformat())
    ).first()
    if row is None:
        row = Signal(judgment_id=judgment_id, rule_id=rule_id, trade_date=d.isoformat())
        session.add(row)
    row.rule_type = rule_type
    row.state = state
    row.current_value = current_value
    row.evidence = evidence
    row.gap_note = gap_note
    row.generated_at = datetime.now(timezone.utc)
    return row


# ---------- 阈值 ----------

def _threshold_signals(session: Session, j: Judgment, variety: str, d: date) -> list[Signal]:
    """阈值求值直接吃 board.threshold_radar 的结果：当前值、触发条件、距离、between 区间
    的处理已经在那边一次写对了，这里再实现一遍只会出现「页面说未触发、信号说触发」。"""
    radar = {row["id"]: row for row in threshold_radar(session, variety, d)}
    out = []
    for t in j.thresholds:
        row = radar.get(t["id"])
        if row is None:  # 理论上不会发生：radar 与判断读的是同一份 thresholds
            continue
        state = RADAR_STATE[row["state"]]
        gap_note, evidence = None, []
        if state == "数据缺失":
            # 分清两种缺失：未登记是配置错误（判断引用了不存在的指标），
            # 无观测是数据断供。混成一句话，研究员不知道该去改判断还是去催数据。
            registered = session.get(Indicator, t["series_id"]) is not None
            gap_note = (f"指标 {t['series_id']} 截至 {d.isoformat()} 无可用观测"
                        if registered else f"指标 {t['series_id']} 未登记，无法比对")
        else:
            o = latest_obs(session, t["series_id"], day_end(d))
            unit = f"（{row['unit']}）" if row["unit"] else ""
            evidence.append(_evidence(
                o, f"{row['series_name']}{unit}：触发条件 {row['condition']}，{row['distance']}",
                row["value"], note=f"{row['type']} · {row['horizon']}"))
        out.append(_upsert(session, j.id, t["id"], THRESHOLD, d, state, row["value"], evidence, gap_note))
    return out


# ---------- 证伪条件 ----------

def _quantities(kind: str, points: list[Observation]) -> list[tuple[Observation, float]]:
    """把观测序列折成被比较的量。change 的语义是与上一个数据点的差值，
    因此 n 期 change 要 n+1 个点，首个点只当基期不参与判定。"""
    if kind == "change":
        return [(cur, cur.value - prev.value) for prev, cur in zip(points, points[1:], strict=False)]
    return [(p, p.value) for p in points]


def _falsifier_signal(session: Session, j: Judgment, f: dict, d: date) -> Signal:
    """证伪判定：expression 解析 + consecutive_periods 连续期。

    连续期必须**全部**成立才算触发——「社库连续两周累库」写的就是这个意思，
    中间断一期就要从头数，否则一周噪音会被当成趋势。
    """
    m = EXPRESSION.match(f["expression"].strip())
    if m is None:
        # 保存时 schemas.judgment.Falsifier 已经把表达式限死成这个形状；走到这里说明有人
        # 绕过服务层直接写库。宁可让流水线响，也不能把「算不了」悄悄渲染成「未触发」。
        raise ValueError(f"证伪条件 {f['id']} 的表达式无法解析：{f['expression']!r}")
    kind, op, target = m.group(1), m.group(2), float(m.group(3))
    need = int(f.get("consecutive_periods") or 1)
    want = need + 1 if kind == "change" else need
    points = recent_points(session, f["series_id"], d, want)

    if len(points) < want:
        # 数据不够不等于不成立：这里只能说「算不出来」。当成未触发就是替数据断供背书。
        registered = session.get(Indicator, f["series_id"]) is not None
        gap = (f"指标 {f['series_id']} 未登记，无法比对" if not registered else
               f"判定「{f['expression']}」连续 {need} 期需要 {want} 个数据点，"
               f"{f['series_id']} 截至 {d.isoformat()} 只有 {len(points)} 个")
        evidence = [_evidence(p, f"已有数据点：{p.value}", p.value, note="不足以判定") for p in points]
        return _upsert(session, j.id, f["id"], FALSIFIER, d, "数据缺失", None, evidence, gap)

    quantities = _quantities(kind, points)
    hits = [OPS[op](q, target) for _p, q in quantities]
    evidence = []
    if kind == "change":
        evidence.append(_evidence(points[0], f"基期 {_iso(points[0].as_of)[:10]}：{points[0].value}",
                                  points[0].value, note="仅用于计算变化量，不参与判定"))
    for (p, q), hit in zip(quantities, hits, strict=True):
        moved = f"较上一数据点变化 {q:+g}" if kind == "change" else f"实际值 {q:g}"
        evidence.append(_evidence(p, f"{_iso(p.as_of)[:10]} {moved}", p.value,
                                  note=f"{f['expression']} {'成立' if hit else '不成立'}"))
    state = "触发" if all(hits) else "未触发"
    return _upsert(session, j.id, f["id"], FALSIFIER, d, state, quantities[-1][1], evidence, None)


# ---------- 对外入口 ----------

def evaluate(session: Session, variety: str, d: date) -> list[Signal]:
    """把 d 日的事实与当前判断逐条对上，写入 signals；同一 judgment+rule+trade_date 幂等。

    取判断用 judgments.service.current（生效优先、否则最新草稿），与看板 threshold_radar
    读的是同一条：两边各取各的，页面和信号就会对不上。
    """
    j = current_judgment(session, variety)
    if j is None:
        return []
    out = _threshold_signals(session, j, variety, d)
    out += [_falsifier_signal(session, j, f, d) for f in j.falsifiers]
    session.commit()
    return out


def create_review_tasks(session: Session, signals: list[Signal], *,
                        today: date | None = None) -> list[ReviewTask]:
    """证伪触发、判断到期 → 待办。today 省略时取 signals 里最大的交易日。

    幂等的粒度是「同一次触发」而不是「同一天」：一条证伪连续成立十天是同一件事，
    只要上一条待办还没处理就不再堆新的，否则首屏待办会被一条一直成立的规则刷屏。
    """
    session.flush()  # 待办要挂 signal_id，先确保信号已有主键
    out: list[ReviewTask] = []
    now = datetime.now(timezone.utc)
    cache: dict[int, Judgment] = {}

    def author(jid: int) -> str:
        if jid not in cache:
            cache[jid] = session.get(Judgment, jid)
        return cache[jid].author

    for s in signals:
        if s.rule_type != FALSIFIER or s.state != "触发":
            continue
        open_task = session.scalars(
            select(ReviewTask).join(Signal, ReviewTask.signal_id == Signal.id)
            .where(ReviewTask.judgment_id == s.judgment_id, Signal.rule_id == s.rule_id,
                   ReviewTask.state == "待处理")
        ).first()
        if open_task is not None:
            continue
        out.append(ReviewTask(judgment_id=s.judgment_id, trigger_type=TRIGGER_FALSIFIED,
                              signal_id=s.id, assignee=author(s.judgment_id), created_at=now))
        session.add(out[-1])
        session.flush()  # 同一批里同一规则的第二条信号要能查到刚建的待办

    ref = today or max((date.fromisoformat(s.trade_date) for s in signals), default=None)
    for jid in dict.fromkeys(s.judgment_id for s in signals):
        if ref is None or cache.setdefault(jid, session.get(Judgment, jid)).review_due > ref:
            continue
        # 到期只发生一次：review_due 是这个版本的固定日期，一个 judgment_id 只建一条，
        # 处理过也不重建。研究员复盘后会存新版本、拿到新 judgment_id，届时自然是新待办。
        done = session.scalars(select(ReviewTask).where(
            ReviewTask.judgment_id == jid, ReviewTask.trigger_type == TRIGGER_EXPIRED)).first()
        if done is not None:
            continue
        out.append(ReviewTask(judgment_id=jid, trigger_type=TRIGGER_EXPIRED, signal_id=None,
                              assignee=cache[jid].author, created_at=now))
        session.add(out[-1])
    session.commit()
    return out


def resolve_review_task(session: Session, task_id: int, *, actor: str, note: str) -> ReviewTask:
    """关闭一条复盘待办。

    没有这条路径，待办就只生不死——`create_review_tasks` 只建不关，判断页换版本也不关。
    几天之内「少而紧急」就变成第二面数据墙，而首屏待办区的全部价值就在于它短。
    这直接打在北极星（「我每天真的会打开它」）上，所以关闭是必备能力，不是锦上添花。

    要求写 `note`：待办被关掉意味着研究员对那条证伪或到期做出了处置，
    处置理由本身就是复盘记录；空着等于把一件事悄悄划掉。
    """
    task = session.get(ReviewTask, task_id)
    if task is None:
        raise ValueError(f"复盘待办 {task_id} 不存在")
    if task.state != "待处理":
        raise ValueError(f"复盘待办 {task_id} 已经是「{task.state}」，不重复处理")
    if not (note or "").strip():
        raise ValueError("处理复盘待办必须写明处置说明——它就是这次复盘的记录")
    task.state = "已处理"
    task.resolution_note = note
    task.resolved_at = datetime.now(timezone.utc)
    session.commit()
    return task


def evaluate_signals(session: Session, variety: str, d: date,
                     *, today: date | None = None) -> tuple[list[Signal], list[ReviewTask]]:
    """每日流水线里 fetch → compute 之后的那一步（SPEC §4.3）。"""
    signals = evaluate(session, variety, d)
    return signals, create_review_tasks(session, signals, today=today)
