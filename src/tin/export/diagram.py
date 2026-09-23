"""产业图的取值与状态判定（方案 08 §3 / §4）。

布局里只存「绑定谁」，数值每次渲染现取——否则图会变成又一份会过期的快照。

这里最重要的不是取值，是**状态**：一个节点有没有数、是不是断更了、是不是被口径
守卫阻断了、是不是只是个代理指标，必须分得清清楚楚。把「阻断」显示成「无数据」，
研究员就失去了"为什么不出数"的线索；把代理指标显示成实测值，就是把推断当事实。
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from tin.config import SHANGHAI
from tin.models import Derived, Indicator, Observation

# 节点状态。顺序即严重程度，渲染时按这个决定配色。
OK = "ok"                    # 有值且在预期更新周期内
STALE = "stale"              # 超过预期更新频率仍无新值
NO_DATA = "no_data"          # 指标已登记但一条观测都没有
BLOCKED = "blocked"          # 派生节点的守卫不通过——数据有，但口径/时点冲突，不能算
MISSING_INPUT = "missing_input"  # 派生节点缺输入——不是冲突，是还没取到
UNBOUND = "unbound"          # 节点还没绑任何指标
RETIRED = "retired"          # 绑的指标已停用或已被删除

# 各频率的预期更新间隔（天）。超过就算断更。
# 宽限是必要的：月频数据常在次月中旬才发布，按 30 天卡会天天误报。
_EXPECTED_DAYS = {"日": 5, "周": 12, "月": 55, "季": 130, "年": 430, "事件": 90}


def _norm(t: str) -> str:
    return re.sub(r"[\s（）()·\-_:：/]+", "", unicodedata.normalize("NFKC", str(t))).lower()


@dataclass(frozen=True)
class NodeValue:
    node_id: str
    state: str
    label: str
    value: float | None = None
    unit: str = ""
    frequency: str = ""
    as_of: str | None = None
    mom: float | None = None          # 环比百分比
    yoy: float | None = None          # 同比百分比
    delta: float | None = None        # 与上一期的差值（原单位）——比百分比更直观
    note: str | None = None
    proxy: bool = False               # 代理指标：不是本环节的实测值
    series_id: str | None = None
    roles: list[str] | None = None    # 在当前判断里扮演的角色（阈值 / 证伪 / 证据…）

    def dump(self) -> dict:
        d = {"node_id": self.node_id, "state": self.state, "label": self.label,
             "value": self.value, "unit": self.unit, "frequency": self.frequency,
             "as_of": self.as_of, "mom": self.mom, "yoy": self.yoy, "delta": self.delta,
             "note": self.note, "proxy": self.proxy,
             "series_id": self.series_id, "roles": self.roles or []}
        return d


def _points(session: Session, series_id: str, through: date, limit: int = 24) -> list[Observation]:
    """取该序列截至 through 的最近若干个观测，新的在前。

    只取最高修订号那一条：同一时点有多个修订时，历史修订不该参与环比计算。
    """
    rows = session.scalars(
        select(Observation)
        .where(Observation.series_id == series_id)
        .order_by(Observation.as_of.desc(), Observation.revision.desc())
    ).all()
    seen, out = set(), []
    for row in rows:
        if row.as_of.astimezone(SHANGHAI).date() > through:
            continue  # 年频按期末标注会出现晚于今天的点，渲染当日图时不该取用
        if row.as_of in seen:
            continue
        seen.add(row.as_of)
        out.append(row)
        if len(out) >= limit:
            break
    return out


def _change(latest: float, previous: float | None) -> float | None:
    if previous is None or previous == 0:
        return None
    return (latest - previous) / abs(previous) * 100


def _same_period_last_year(rows: list[Observation], frequency: str) -> float | None:
    """去年同期。按频率回退固定期数，不做插值——找不到就返回 None。"""
    steps = {"日": None, "周": 52, "月": 12, "季": 4, "年": 1}.get(frequency)
    if steps is None or len(rows) <= steps:
        return None
    return rows[steps].value


def _observation_node(session: Session, node: dict, series_id: str, through: date) -> NodeValue:
    label = node.get("label") or series_id
    ind = session.get(Indicator, series_id)
    if ind is None or ind.status != "可用":
        return NodeValue(node["id"], RETIRED, label, series_id=series_id,
                         note="指标已停用或已删除" if ind else "指标不存在")

    rows = _points(session, series_id, through)
    proxy = bool(node.get("binding", {}).get("proxy"))
    if not rows:
        return NodeValue(node["id"], NO_DATA, label, unit=ind.unit, frequency=ind.frequency,
                         series_id=series_id, proxy=proxy, note="尚无数据")

    latest = rows[0]
    day = latest.as_of.astimezone(SHANGHAI).date()
    gap_days = (through - day).days
    expected = _EXPECTED_DAYS.get(ind.frequency, 30)
    state = STALE if gap_days > expected else OK
    note = f"已 {gap_days} 天未更新（{ind.frequency}频预期 {expected} 天内）" if state == STALE else None

    return NodeValue(
        node["id"], state, label, value=latest.value, unit=ind.unit, frequency=ind.frequency,
        as_of=day.isoformat(),
        mom=_change(latest.value, rows[1].value if len(rows) > 1 else None),
        yoy=_change(latest.value, _same_period_last_year(rows, ind.frequency)),
        delta=(latest.value - rows[1].value) if len(rows) > 1 else None,
        note=note, proxy=proxy, series_id=series_id,
    )


def _derived_node(session: Session, node: dict, formula_id: str, through: date) -> NodeValue:
    label = node.get("label") or formula_id
    row = session.scalars(
        select(Derived).where(Derived.formula_id == formula_id,
                              Derived.trade_date <= through.isoformat())
        .order_by(Derived.trade_date.desc()).limit(1)).first()
    if row is None:
        return NodeValue(node["id"], NO_DATA, label, note="尚未计算", series_id=formula_id)
    if row.value is None:
        # 「缺输入」与「守卫阻断」不是一回事：前者是某个输入还没取到，补数据就能算；
        # 后者是数据都有但口径或时点对不上，补数据也没用，得先解决口径。
        # 混成一种，研究员就不知道该去录数还是该去查口径。
        state = MISSING_INPUT if row.status == "missing_input" else BLOCKED
        return NodeValue(node["id"], state, label, as_of=row.trade_date,
                         note=row.note or ("缺少输入" if state == MISSING_INPUT else "守卫阻断，不出数"),
                         series_id=formula_id)
    prev = session.scalars(
        select(Derived).where(Derived.formula_id == formula_id,
                              Derived.trade_date < row.trade_date, Derived.value.isnot(None))
        .order_by(Derived.trade_date.desc()).limit(1)).first()
    # derived 表不存单位，单位是公式的属性，取自公式登记表
    from tin.compute.formulas import REGISTRY

    spec = REGISTRY.get(formula_id)
    return NodeValue(node["id"], OK, label, value=row.value, unit=spec.unit if spec else "",
                     frequency="日", as_of=row.trade_date,
                     mom=_change(row.value, prev.value if prev else None),
                     delta=(row.value - prev.value) if prev else None,
                     note=row.note, series_id=formula_id)


_ROLE_LABEL = {"threshold": "阈值", "falsifier": "证伪条件", "evidence": "证据",
               "marginal_focus": "边际关注", "whitelist": "白名单"}


def judgment_roles(session: Session) -> dict[str, list[str]]:
    """每个指标在当前判断里扮演什么角色。

    回答研究员每天都要问的问题：我这个判断，靠的是产业链上哪几个环节？
    """
    rows = session.execute(text(
        "select series_id, ref_type from judgment_series_refs")).all()
    out: dict[str, list[str]] = {}
    for series_id, ref_type in rows:
        label = _ROLE_LABEL.get(ref_type, ref_type)
        out.setdefault(series_id, [])
        if label not in out[series_id]:
            out[series_id].append(label)
    return out


def resolve(session: Session, layout: dict, through: date) -> list[dict]:
    """按布局取出每个节点当前该显示什么。"""
    roles = judgment_roles(session)
    out = []
    for node in layout.get("nodes", []):
        binding = node.get("binding") or {}
        kind = binding.get("kind")
        if kind == "series" and binding.get("series_id"):
            value = _observation_node(session, node, binding["series_id"], through)
        elif kind == "derived" and binding.get("formula_id"):
            value = _derived_node(session, node, binding["formula_id"], through)
        else:
            # 没绑指标不是错误：结构分组框、纯静态标注框本来就不该有值
            value = NodeValue(node["id"], UNBOUND, node.get("label", ""))
        item = value.dump()
        item["roles"] = roles.get(item["series_id"] or "", [])
        out.append(item)
    return out


# ---------- 模板增删改查 ----------

class DiagramError(ValueError):
    pass


def _clean(layout: dict) -> dict:
    nodes = layout.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise DiagramError("布局至少要有一个节点")
    ids = [n.get("id") for n in nodes]
    if len(set(ids)) != len(ids) or not all(ids):
        raise DiagramError("节点 id 必须存在且唯一")
    return {"nodes": nodes, "edges": layout.get("edges") or []}


def to_dict(row) -> dict:
    return {"id": row.id, "name": row.name, "is_default": row.is_default,
            "layout": row.layout, "updated_at": row.updated_at.isoformat()}


def list_templates(session: Session, variety: str) -> list[dict]:
    from tin.models import DiagramTemplate

    rows = session.scalars(select(DiagramTemplate).where(DiagramTemplate.variety == variety)
                           .order_by(DiagramTemplate.is_default.desc(),
                                     DiagramTemplate.updated_at.desc())).all()
    return [to_dict(r) for r in rows]


def save_template(session: Session, variety: str, payload: dict) -> dict:
    from tin.models import DiagramTemplate

    name = str(payload.get("name", "")).strip()
    if not name:
        raise DiagramError("请填写布局名称")
    layout = _clean(payload.get("layout") or {})
    now = datetime.now(tz=SHANGHAI).astimezone()

    row = session.get(DiagramTemplate, payload["id"]) if payload.get("id") else None
    if row is not None and row.variety != variety:
        raise DiagramError("布局不属于当前品种")
    if row is None:
        row = DiagramTemplate(variety=variety, created_at=now,
                              is_default=not list_templates(session, variety))
        session.add(row)
    row.name, row.layout, row.updated_at = name, layout, now
    if payload.get("is_default"):
        for other in session.scalars(select(DiagramTemplate)
                                     .where(DiagramTemplate.variety == variety)):
            other.is_default = other is row
    session.flush()
    session.commit()
    return to_dict(row)


def delete_template(session: Session, variety: str, template_id: int) -> dict:
    from tin.models import DiagramTemplate

    row = session.get(DiagramTemplate, template_id)
    if row is None or row.variety != variety:
        raise DiagramError("布局不存在")
    was_default = row.is_default
    session.delete(row)
    session.flush()
    if was_default:  # 默认被删后顺位，避免「默认永远删不掉」的死角
        heir = session.scalars(select(DiagramTemplate).where(DiagramTemplate.variety == variety)
                               .order_by(DiagramTemplate.updated_at.desc()).limit(1)).first()
        if heir is not None:
            heir.is_default = True
    session.commit()
    return {"deleted": template_id, "templates": list_templates(session, variety)}


def ensure_default(session: Session, variety: str) -> dict:
    """第一次进页面时给一张按结构图摆好的默认布局，而不是一张白纸。"""
    existing = list_templates(session, variety)
    if existing:
        return existing[0]
    import json

    from tin.config import ROOT

    path = ROOT / "seeds" / f"diagram_{variety.lower()}.json"
    if not path.exists():
        raise DiagramError(f"没有 {variety} 的默认布局种子（{path.name}）")
    return save_template(session, variety, {"name": json.loads(path.read_text(encoding="utf-8"))["name"],
                                            "layout": json.loads(path.read_text(encoding="utf-8")),
                                            "is_default": True})


# ---------- 指标推荐（方案 08 §8.2） ----------

# 这些词片在指标名里满天飞，用来匹配只会把不相干的指标拉进来。
# 实测教训：「冶炼产量·中国」曾因为「中国」二字被推荐了「锡锭升贴水：中国」。
_STOPWORDS = frozenset((
    "中国", "全球", "日度", "周度", "月度", "年度", "季度", "合计", "总计", "指数",
    "价格", "数量", "金额", "平均", "累计", "小计", "当月", "当日", "国内", "海外",
))

_SPLIT = re.compile(r"[·\s（）()／/、,，:：\-—>＞]+")


def _tokens(label: str) -> list[str]:
    raw = re.sub(r"^[└├│─\s]+|◍", "", label)
    return [p for p in _SPLIT.split(raw) if len(p) >= 2 and p not in _STOPWORDS]


def suggest(session: Session, variety: str, label: str, bound: set[str] | None = None,
            limit: int = 6) -> list[dict]:
    """按节点名推荐候选指标。

    **必须有名称命中才推荐。** 实测过按「判断引用过 + 有数据」兜底的版本：没命中时
    十几个节点的 Top3 都是同样那三条，纯噪音。给不出推荐时明说「无匹配」，比给三条
    不相干的有用——而且给不出的节点往往正是数据本来就缺的那些，这本身就是信息。
    """
    parts = _tokens(label)
    if not parts:
        return []
    bound = bound or set()
    judged = {r[0] for r in session.execute(text(
        "select distinct series_id from judgment_series_refs")).all()}

    out = []
    for ind in session.scalars(select(Indicator).where(Indicator.status == "可用",
                                                       Indicator.variety.in_([variety, "COMMON"]))):
        name = _norm(ind.name)
        hits = sum(1 for p in parts if _norm(p) and _norm(p) in name)
        if not hits:
            continue
        count = session.scalar(select(func.count()).select_from(Observation)
                               .where(Observation.series_id == ind.series_id)) or 0
        score = hits * 30 + (15 if count else -40) + (20 if ind.series_id in judged else 0)
        score += min(count // 500, 6)
        out.append({"series_id": ind.series_id, "name": ind.name, "unit": ind.unit,
                    "frequency": ind.frequency, "points": count,
                    "already_bound": ind.series_id in bound, "score": score,
                    "why": f"名称命中 {hits} 处" + ("，判断引用过" if ind.series_id in judged else "")
                           + ("" if count else "，但尚无数据")})
    out.sort(key=lambda r: -r["score"])
    return out[:limit]


# ---------- 节点详情（方案 08 §10） ----------

def detail(session: Session, series_id: str, through: date, limit: int = 60) -> dict:
    """点开一个节点要看到什么。

    重点不是再报一遍数值——图上已经有了。重点是**这个数字凭什么可信**：
    口径的每一个维度、来源与凭证、有没有被修订过、离上次更新多久。
    这些散在指标页与导出批注里，看图的人不该为了查一个数在三个页面之间跳。
    """
    from tin.caliber.dictionary import DIMENSIONS
    from tin.compute.formulas import REGISTRY

    if series_id in REGISTRY:
        spec = REGISTRY[series_id]
        rows = session.scalars(
            select(Derived).where(Derived.formula_id == series_id,
                                  Derived.trade_date <= through.isoformat())
            .order_by(Derived.trade_date.desc()).limit(limit)).all()
        latest = rows[0] if rows else None
        return {
            "kind": "derived", "series_id": series_id, "name": spec.name,
            "unit": spec.unit, "frequency": "日",
            "expression": spec.expression, "tolerance": spec.tolerance,
            "caliber": [], "source": "本平台计算", "source_url": None,
            "history": [{"as_of": r.trade_date, "value": r.value, "status": r.status}
                        for r in reversed(rows) if r.value is not None],
            "inputs": latest.inputs if latest else [],
            "status_note": (latest.note if latest else None),
            "revisions": [],
        }

    ind = session.get(Indicator, series_id)
    if ind is None:
        raise DiagramError(f"指标不存在：{series_id}")

    obs = session.scalars(
        select(Observation).where(Observation.series_id == series_id)
        .order_by(Observation.as_of.desc(), Observation.revision.desc())).all()
    obs = [o for o in obs if o.as_of.astimezone(SHANGHAI).date() <= through]

    seen, history = set(), []
    for o in obs:
        if o.as_of in seen:
            continue
        seen.add(o.as_of)
        history.append(o)
        if len(history) >= limit:
            break

    labels = {d.key: d.label for d in DIMENSIONS}
    caliber = [{"key": k, "label": labels.get(k, k), "value": v}
               for k, v in (ind.caliber or {}).items() if v and k != "note"]
    if (ind.caliber or {}).get("note"):
        caliber.append({"key": "note", "label": "备注", "value": ind.caliber["note"]})

    # 修订过的时点：同一 as_of 有多个修订号，说明数据商回溯改过数
    revised = [o for o in obs if o.revision > 0][:10]

    return {
        "kind": "series", "series_id": series_id, "name": ind.name,
        "unit": ind.unit, "frequency": ind.frequency, "category": ind.category,
        "source": ind.source, "source_url": ind.source_url,
        "vendor_code": ind.vendor_code, "fetch_mode": ind.fetch_mode,
        "owner": ind.owner, "status": ind.status,
        "caliber": caliber,
        "history": [{"as_of": o.as_of.astimezone(SHANGHAI).date().isoformat(),
                     "value": o.value} for o in reversed(history)],
        "revisions": [{"as_of": o.as_of.astimezone(SHANGHAI).date().isoformat(),
                       "value": o.value, "revision": o.revision,
                       "entered_by": o.entered_by, "note": o.note} for o in revised],
        "latest_note": history[0].note if history else None,
        "entered_by": history[0].entered_by if history else None,
    }


# ---------- 跨来源交叉校验（方案 08 §11.5.2） ----------

def crosscheck(session: Session, variety: str, through: date, periods: int = 6) -> list[dict]:
    """同一个事实由两家分别给出时，对不对得上。

    **不做 SMM 平衡表内部的恒等式校验**：表观消费本就是「产量+进口−出口−库存变化」
    倒算出来的，校验它恒等于 0，是同义反复，永远抓不到任何问题。有价值的是跨来源——
    实测精锡月产量两家差 −5.3% ~ +8.8%，而锡矿进口两家完全一致（说明转载同一份海关数据）。

    差异本身不是错误：两家口径不同很正常。这个功能要回答的是「差多少、是不是一直这么差、
    最近有没有突然变化」——突然变化才是信号。
    """
    import json

    from tin.config import ROOT

    path = ROOT / "seeds" / f"crosscheck_{variety.lower()}.json"
    if not path.exists():
        return []
    checks = json.loads(path.read_text(encoding="utf-8")).get("checks", [])

    def series(series_id: str) -> dict[str, float]:
        rows = session.scalars(
            select(Observation).where(Observation.series_id == series_id)
            .order_by(Observation.as_of.desc(), Observation.revision.desc())).all()
        out: dict[str, float] = {}
        for r in rows:
            day = r.as_of.astimezone(SHANGHAI).date()
            if day > through:
                continue
            out.setdefault(day.isoformat(), r.value)
        return out

    results = []
    for chk in checks:
        a, b = series(chk["a"]), series(chk["b"])
        shared = sorted(set(a) & set(b), reverse=True)[:periods]
        if not shared:
            results.append({**chk, "state": NO_DATA, "points": [],
                            "summary": "两个序列没有重叠的时点，无法比较"})
            continue
        pts = []
        for day in shared:
            diff = a[day] - b[day]
            rel = (diff / b[day] * 100) if b[day] else None
            pts.append({"as_of": day, "a": a[day], "b": b[day], "diff": diff, "rel": rel})
        rels = [abs(p["rel"]) for p in pts if p["rel"] is not None]
        worst = max(rels) if rels else 0
        latest = pts[0]["rel"]
        tol = chk.get("tolerance_pct", 5)
        state = OK if worst <= tol else STALE
        if worst <= 0.01:
            summary = "两家完全一致——很可能转载同一来源，不必双边维护"
        elif state == OK:
            summary = f"最近 {len(pts)} 期差异都在 ±{tol}% 内（最大 {worst:.1f}%）"
        else:
            summary = (f"最近 {len(pts)} 期最大差异 {worst:.1f}%，超出容差 ±{tol}%；"
                       f"最新一期 {latest:+.1f}%")
        results.append({**chk, "state": state, "points": pts, "worst": worst,
                        "latest_rel": latest, "summary": summary})
    return results
