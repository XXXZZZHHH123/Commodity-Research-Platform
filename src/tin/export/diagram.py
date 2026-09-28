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


def judgment_roles(session: Session, variety: str = "SN") -> tuple[dict[str, list[str]], dict]:
    """每个指标在**当前这一版判断**里扮演什么角色。

    回答研究员每天都要问的问题：我这个判断，靠的是产业链上哪几个环节？

    **必须按判断版本过滤。** 判断改版时旧版本只是置为「已归档」，它的引用行仍留在
    `judgment_series_refs` 里——取全表等于把历年所有版本的引用并起来，重点关注只增不减，
    研究员换了判断，图上还亮着上一版关心的环节。这与 P1「判断是一份带时间的快照」直接冲突。
    """
    from tin.judgments import service as judgment_service

    j = judgment_service.current(session, variety)
    if j is None:
        return {}, {}
    rows = session.execute(
        text("select series_id, ref_type from judgment_series_refs where judgment_id = :jid"),
        {"jid": j.id}).all()
    out: dict[str, list[str]] = {}
    for series_id, ref_type in rows:
        label = _ROLE_LABEL.get(ref_type, ref_type)
        out.setdefault(series_id, [])
        if label not in out[series_id]:
            out[series_id].append(label)
    meta = {"version": j.version, "status": j.status, "author": j.author,
            "written_at": j.written_at.isoformat() if j.written_at else None,
            "review_due": j.review_due.isoformat() if j.review_due else None}
    return out, meta


def resolve(session: Session, layout: dict, through: date) -> list[dict]:
    """按布局取出每个节点当前该显示什么。"""
    roles, _ = judgment_roles(session)
    out = []
    by_id: dict[str, dict] = {}
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
        by_id[node["id"]] = item
        out.append(item)

    # 分组的合计放在最后算：它要用到刚算出来的子节点取值
    for node in layout.get("nodes", []):
        binding = node.get("binding") or {}
        if binding.get("kind") == "agg":
            by_id[node["id"]].update(
                _aggregate(node, binding.get("op", "sum"), layout, by_id))
    return out


# ---------- 分组合计（方案 08 §5.2 的声明式组合） ----------

_AGG_LABEL = {"sum": "合计", "share": "占比"}


def _aggregate(group: dict, op: str, layout: dict, values: dict[str, dict]) -> dict:
    """分组对自己的**直接子节点**求和 / 算占比。

    这是 §5.2 说的"声明式组合"：用户选的是一个运算符，不是一段自由公式，
    所以守卫可以自动推出来，而不是指望写公式的人自己声明。

    **单位、频率、重量口径不一致一律阻断。** 元/吨 和 美元/吨 相加、实物吨和金属吨
    （差 2–3 倍）相加，算出来的数看起来完全正常——这正是 FR-5.2 要防的东西。
    合计一个"错得很合理"的数，比留空危险得多。
    """
    kids = [n for n in layout.get("nodes", [])
            if n.get("parent") == group["id"] and n.get("kind") != "group"]

    # **排除被同组别的节点包含的那些。** 缅甸矿进口 ⊂ 进口锡精矿，单位、频率完全一样，
    # 守卫拦不住，求和就把它重复算了一遍——而多出来的那部分看不出来。
    # 包含关系在数据里是显式的：同组内一条 A→B 的连线就是"B 是 A 的一部分"。
    ids = {k["id"] for k in kids}
    contained = {e["to"] for e in (layout.get("edges") or [])
                 if e.get("from") in ids and e.get("to") in ids}
    kids = [k for k in kids if k["id"] not in contained]
    nested = len(contained)

    picked = [values[k["id"]] for k in kids if k["id"] in values]
    usable = [v for v in picked if v.get("value") is not None]
    label = _AGG_LABEL.get(op, op)

    if not picked:
        return {"state": UNBOUND, "agg": op,
                "note": f"这个分组里没有直接子节点，无法{label}"}
    if not usable:
        return {"state": NO_DATA, "agg": op,
                "note": f"{len(picked)} 个子节点都还没有数据"}

    units = {v.get("unit") for v in usable}
    freqs = {v.get("frequency") for v in usable}
    if len(units) > 1:
        return {"state": BLOCKED, "agg": op, "value": None,
                "note": f"单位不一致（{'、'.join(sorted(u or '—' for u in units))}），不能{label}"}
    if op == "sum" and len(freqs) > 1:
        return {"state": BLOCKED, "agg": op, "value": None,
                "note": f"频率不一致（{'、'.join(sorted(f or '—' for f in freqs))}），"
                        f"合计的是不同期的数"}

    skipped = len(picked) - len(usable)
    unit = usable[0].get("unit")
    total = sum(v["value"] for v in usable)
    if op == "share":
        # 占比：每个子节点占合计的百分之多少。合计为 0 时不给数，别造一个 ∞
        if total == 0:
            return {"state": BLOCKED, "agg": op, "value": None,
                    "note": "合计为 0，占比无意义"}
        parts = sorted(({"label": k["label"],
                         "pct": values[k["id"]]["value"] / total * 100}
                        for k in kids
                        if k["id"] in values and values[k["id"]].get("value") is not None),
                       key=lambda p: -p["pct"])
        return {"state": OK, "agg": op, "value": total, "unit": unit,
                "parts": parts, "as_of": max(v.get("as_of") or "" for v in usable),
                "note": (f"{len(usable)} 项占比"
                         + (f"；{nested} 项是其他项的一部分，已排除" if nested else "")
                         + (f"；{skipped} 项无数据未计入" if skipped else ""))}

    # 合计的时点取最早的那个：里面只要有一项是旧的，整个合计就只到那一天
    stalest = min((v.get("as_of") or "" for v in usable))
    return {"state": OK if not skipped else STALE, "agg": op, "value": total, "unit": unit,
            "frequency": usable[0].get("frequency"),
            "as_of": stalest, "count": len(usable),
            "note": (f"{len(usable)} 项合计"
                     + (f"；{nested} 项是其他项的一部分，已排除以免重复计" if nested else "")
                     + (f"；{skipped} 项无数据**未计入**，这不是完整的合计" if skipped else ""))}


# ---------- 模板增删改查 ----------

class DiagramError(ValueError):
    pass


def _clean(layout: dict, *, allow_empty: bool = False) -> dict:
    """校验布局。

    空布局本身不是错误——新建一张图当然是从零个节点开始的。要拦的是**把已有的图存成空的**：
    那通常是误删或前端状态丢了，存下去就把研究员摆了半天的结构抹掉了。所以这条限制只对
    「更新一张本来有内容的布局」生效。
    """
    nodes = layout.get("nodes")
    if not isinstance(nodes, list):
        raise DiagramError("布局格式不对：nodes 必须是列表")
    if not nodes and not allow_empty:
        raise DiagramError("这张布局本来是有内容的，不能存成空的。"
                           "要清空请先删除全部节点再逐个确认，或直接删除整张布局")
    ids = [n.get("id") for n in nodes]
    if len(set(ids)) != len(ids) or not all(ids):
        raise DiagramError("节点 id 必须存在且唯一")
    return {"nodes": nodes, "edges": layout.get("edges") or []}


def to_dict(row) -> dict:
    return {"id": row.id, "name": row.name, "is_default": row.is_default,
            "layout": row.layout, "updated_at": row.updated_at.isoformat()}


class NeedsMigration(DiagramError):
    """库里还没有产业图的表。这不是坏数据，是少跑了一次迁移。"""


def list_templates(session: Session, variety: str) -> list[dict]:
    from sqlalchemy.exc import OperationalError, ProgrammingError

    from tin.models import DiagramTemplate

    try:
        rows = session.scalars(select(DiagramTemplate).where(DiagramTemplate.variety == variety)
                               .order_by(DiagramTemplate.is_default.desc(),
                                         DiagramTemplate.updated_at.desc())).all()
    except (OperationalError, ProgrammingError) as e:
        # 缺表甩一个 SQL 堆栈的 500 出来，等于让人自己去猜要跑迁移。
        # 这个分支只认「表不存在」，其余数据库错误照常抛出，不掩盖真问题。
        text_of = str(e.orig if getattr(e, "orig", None) else e).lower()
        if "diagram_templates" in text_of and (
                "no such table" in text_of or "does not exist" in text_of
                or "doesn't exist" in text_of):
            session.rollback()
            raise NeedsMigration(
                "产业图的数据表还没建 —— 这个库停在旧版本，少跑了一次迁移。\n"
                "在项目目录下执行：conda activate tin && alembic upgrade head\n"
                "（只新增一张空表 diagram_templates，不动任何已有数据）") from None
        raise
    return [to_dict(r) for r in rows]


def save_template(session: Session, variety: str, payload: dict) -> dict:
    from tin.models import DiagramTemplate

    name = str(payload.get("name", "")).strip()
    if not name:
        raise DiagramError("请填写布局名称")
    now = datetime.now(tz=SHANGHAI).astimezone()

    row = session.get(DiagramTemplate, payload["id"]) if payload.get("id") else None
    if row is not None and row.variety != variety:
        raise DiagramError("布局不属于当前品种")
    # 新建可以是空的；把一张已经有内容的图存成空的才是要拦的事
    existing = (row.layout or {}).get("nodes") if row is not None else None
    layout = _clean(payload.get("layout") or {}, allow_empty=not existing)
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

def _crosscheck_specs(variety: str) -> list[dict]:
    import json

    from tin.config import ROOT

    path = ROOT / "seeds" / f"crosscheck_{variety.lower()}.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("checks", [])


def crosscheck(session: Session, variety: str, through: date, periods: int = 6) -> list[dict]:
    """同一个事实由两家分别给出时，对不对得上。

    **不做 SMM 平衡表内部的恒等式校验**：表观消费本就是「产量+进口−出口−库存变化」
    倒算出来的，校验它恒等于 0，是同义反复，永远抓不到任何问题。有价值的是跨来源——
    实测精锡月产量两家差 −5.3% ~ +8.8%，而锡矿进口两家完全一致（说明转载同一份海关数据）。

    差异本身不是错误：两家口径不同很正常。这个功能要回答的是「差多少、是不是一直这么差、
    最近有没有突然变化」——突然变化才是信号。
    """
    checks = _crosscheck_specs(variety)
    if not checks:
        return []

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


# ---------- 这个指标被谁在用 ----------

def usage(session: Session, series_id: str, variety: str = "SN") -> dict:
    """一个指标被判断和产业图怎么用着。

    指标列表页回答不了「这条指标要不要维护」——444 行里每一行看起来都一样重要。
    真正的分辨依据是**有没有人在用**：被当前判断的阈值/证伪条件引用的指标断更是事故，
    没人引用的指标断更只是噪音。
    """
    from tin.judgments import service as judgment_service

    out: dict = {"judgment": [], "nodes": [], "crosscheck": []}

    j = judgment_service.current(session, variety)
    if j is not None:
        rows = session.execute(
            text("select ref_type, ref_id from judgment_series_refs "
                 "where judgment_id = :jid and series_id = :sid"),
            {"jid": j.id, "sid": series_id}).all()
        out["judgment"] = [{"role": _ROLE_LABEL.get(t, t), "ref_id": r} for t, r in rows]
        out["judgment_version"] = j.version
        out["judgment_status"] = j.status

    for tpl in list_templates(session, variety):
        for node in (tpl["layout"] or {}).get("nodes", []):
            b = node.get("binding") or {}
            if b.get("series_id") == series_id or b.get("formula_id") == series_id:
                out["nodes"].append({"template_id": tpl["id"], "template": tpl["name"],
                                     "node": node.get("label", node.get("id")),
                                     "proxy": bool(b.get("proxy"))})

    for spec in _crosscheck_specs(variety):
        if series_id in (spec.get("a"), spec.get("b")):
            other = spec["b"] if series_id == spec["a"] else spec["a"]
            out["crosscheck"].append({"label": spec.get("label", ""), "against": other})

    return out
