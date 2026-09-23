"""每日简报的 prompt 组装：稳定前缀进 system，当日事实进 messages。

**缓存纪律是这个文件的头等大事**（SPEC §4.1）。prompt caching 是本项目最主要的成本杠杆，
而它按「前缀逐字节匹配」生效：前缀里任何一个字节变了，后面全部作废。因此这里的分层
不是审美，是账单：

    system  = 固定人设 + L1 硬骨架 + L2 思路条目   ← 一个判断版本内每天一模一样
    messages = 日期 + 今日事实 + 今日信号          ← 每天都变

断点打在 **最后一个 system 块之后**，也就是 playbook 之后：断点之前是稳定区，
之后追加多少对话都不会让它失效——打回重试之所以便宜就是靠这条。

`LlmRequest` 会在构造时拒绝 system 里出现日期 / 时刻 / UUID。那条防线是故意的，
所以这里不开 `allow_volatile_system`，而是：当日的东西一律不往 system 放；
L1 自由文本里那种「写死在判断里、并不随天变」的日期（调研日期、验证窗口）由
`_stable()` 改写成中文写法——语义不变，前缀照样逐字节稳定。

与 compute/signals.py 的分工不能混：**阈值有没有触发是算出来的，不是问出来的。**
这里只负责把算好的 signals 摆给 LLM 看，一个字都不重新判定。
"""

import json
import re
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from tin.compute.engine import day_end, latest_obs
from tin.compute.signals import OPS, recent_points  # 比较算子与信号层共用一张表
from tin.config import SHANGHAI
from tin.export.board import contract_curve, core_cards, macro_featured, previous_value
from tin.ingest.fred import FEATURED_SERIES
from tin.llm.base import Message, SystemBlock
from tin.models import Indicator, Judgment, PlaybookItem, Signal
from tin.schemas.brief import STANCES, BriefDraft
from tin.schemas.judgment import EXPRESSION, TONES

# 输出契约。schemas/brief.py 是唯一的真源，这里只是把它翻成 json_schema，
# 绝不另写一份——两份规格迟早会分叉，而分叉的那一天没人会发现。
OUTPUT_SCHEMA = BriefDraft.model_json_schema()
SCHEMA_NAME = "brief_draft"
PURPOSE = "compose_brief"

# scope 里被支持的键。**不认识的键一律判为「不适用」而不是「始终适用」**：
# 条目是研究员写的，键写错了却照样每天进 prompt，等于用一条谁也没审过的规则污染简报。
# 这跟 signals 把「数据缺失」摆上台面、而不是静默当成「未触发」是同一条纪律。
SCOPE_KEYS = {"series_id", "when", "months"}

# 固定人设。这一段永远不变——它是整条缓存前缀的第一个字节，改一个字今天全表作废。
PERSONA = f"""你是一名有色金属期货研究员的分析助手。你的工作是**替研究员先想一遍**，
产出一份当日简报草稿交给他改；你不是在替他拍板，也不是在写行情播报。

交付物是行业标准三件套，缺一不可：
1. 基调 —— 只能取自固定词表：{'/'.join(TONES)}；
2. 参考运行区间 —— **是区间不是点位**，并且必须说明挂在哪条序列上（range 的 series_id）。
   点位预测在这一行不是交付物，做了反而毁信任；
3. 操作倾向 —— 只能取自固定词表：{'/'.join(STANCES)}。是倾向，不是仓位、不是下单指令。

证据纪律（硬性，违反的论断会被后端打回或标灰）：
- 每一条论断都必须带至少一个证据锚点：series_id、思路条目编号或信号编号；
- 引用数值时，`series_id`、`value`、`as_of` 三者必须同时给出，且 **value 与 as_of
  逐位照抄**下文给你的数字，不要换算单位、不要取整、不要凑整数；
- 只能引用下文**实际出现过**的 series_id、信号编号 #、思路条目编号 #。
  凭印象补一个看起来合理的编号，比少说一句话严重得多；
- 举不出证据的判断就不要写。宁可少写两条，也不要写"正确的废话"。

写法要求：
- 先说结论再说依据，一条论断一句话，不要堆排比；
- 多空两侧都要照顾到，用 side 字段标出 bull / bear / neutral；
- 只讲今天的边际变化，不复述已经写在硬骨架里的长期叙事；
- 全部用中文。"""

# 打在 messages 末尾的当日任务说明。放 messages 而不是 system 是因为它要点名当天的日期。
TASK_TEMPLATE = """## 今天要你做的事
请依据上面的硬骨架、思路条目与 {trade_date} 的事实，输出当日简报草稿（结构化 JSON）。

再强调三条，后端会逐条核对：
1. `value` 必须与上文「可引用事实」表或「今日信号」里给出的数字逐位一致；
2. `as_of` 照抄上文给出的时点字符串；
3. 用到了哪几条思路条目，填进 `used_playbook_item_ids`（只填上文出现过的编号）。"""


# ---------------------------------------------------------------- 文本工具

_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_CLOCK = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})")


def _stable(text: str) -> str:
    """把自由文本里的 ISO 日期 / 时刻改写成中文写法。

    `LlmRequest` 按**形状**拦 system 里的日期，拦的是「今天是 2026-09-18」这种每天都变的
    内容。但 L1 硬骨架里也会出现写死的日期（调研月份、原文时点），它在一个判断版本内
    是常量，对缓存无害却会被同一条规则误伤。与其开 `allow_volatile_system`
    （那等于整份 prompt 放弃缓存），不如在这里做一次确定性改写：语义不变，
    前缀依旧逐字节稳定，那条防线也仍然守着「今天」不许进 system。
    """
    text = _DATE.sub(lambda m: f"{m[1]}年{int(m[2])}月{int(m[3])}日", text)
    return _CLOCK.sub(lambda m: f"{int(m[1])}时{m[2]}分{m[3]}秒", text)


def _num(v: float | None) -> str:
    """数值的文本形式。

    用 10 位有效数字而不是千分位或 %g 的默认 6 位：这些数字是要被 LLM 原样抄回来、
    再由闸门按 1e-6 相对容差比对的。加了千分位逗号它会抄错，位数不够它会抄得"差不多"——
    两种都会变成一条被标灰的论断。
    """
    return "—" if v is None else f"{v:.10g}"


def _lines(*parts: str) -> str:
    return "\n".join(p for p in parts if p)


# ---------------------------------------------------------------- scope 筛选

def _series_value(session: Session, series_id: str, kind: str, d: date) -> float | None:
    """取 scope 条件要比的量。kind=value 取最新观测；kind=change 取与上一个数据点的差。"""
    if kind == "change":
        points = recent_points(session, series_id, d, 2)
        return None if len(points) < 2 else points[-1].value - points[-2].value
    o = latest_obs(session, series_id, day_end(d))
    return None if o is None else o.value


def scope_applies(session: Session, scope: dict | None, d: date) -> tuple[bool, str | None]:
    """这条 scope 今天适不适用。返回（适用?, 不适用的原因）。

    判定必须确定性、可测：同样的库 + 同样的 d 永远得到同样的结果，不经过 LLM。
    支持的写法（多个键之间是「且」）：
      `{}`                                          始终适用
      `{"months": [9, 10]}`                         只在这些自然月适用
      `{"series_id": "MACRO.VIX"}`                  该序列截至 d 有观测才适用
      `{"series_id": "MACRO.VIX", "when": "value > 19"}`   该序列满足条件才适用

    条件算不出来（无观测、表达式写坏、键不认识）一律**不适用**。理由与 signals 相同：
    与其让一条判不了的规则照常进 prompt，不如把它摘出来、把原因摆上台面。
    """
    scope = scope or {}
    if not isinstance(scope, dict):
        return False, f"scope 不是对象：{scope!r}"
    if unknown := sorted(set(scope) - SCOPE_KEYS):
        return False, f"scope 含无法判定的键 {unknown}（支持：{sorted(SCOPE_KEYS)}）"
    if not scope:
        return True, None

    if (months := scope.get("months")) is not None:
        if not isinstance(months, list) or not all(isinstance(m, int) for m in months):
            return False, f"months 必须是整数月份列表，收到 {months!r}"
        if d.month not in months:
            return False, f"仅适用于 {months} 月，今天是 {d.month} 月"

    series_id, when = scope.get("series_id"), scope.get("when")
    if when is not None and series_id is None:
        return False, f"写了条件「{when}」却没给 series_id，无从取数"
    if series_id is None:
        return True, None
    if session.get(Indicator, series_id) is None:
        return False, f"条件挂在未登记的指标 {series_id} 上"

    if when is None:
        # 只给序列不给条件 = 「有这个数的时候这条思路才有用」
        if latest_obs(session, series_id, day_end(d)) is None:
            return False, f"{series_id} 截至 {d.isoformat()} 无可用观测"
        return True, None

    m = EXPRESSION.match(str(when).strip())
    if m is None:
        # 与 schemas.judgment.Falsifier 用同一个表达式词法：L2 的条件不该比 L1 更自由，
        # 否则「可自动判定」这条底线就从软思路这边漏掉了。
        return False, f"条件「{when}」无法解析（写成 value/change 比较一个数，如 value > 19）"
    kind, op, target = m.group(1), m.group(2), float(m.group(3))
    actual = _series_value(session, series_id, kind, d)
    if actual is None:
        return False, f"{series_id} 截至 {d.isoformat()} 数据不足，条件「{when}」无法判定"

    if not OPS[op](actual, target):
        return False, f"{series_id} 当前 {_num(actual)}，不满足「{when}」"
    return True, None


def select_playbook(session: Session, *, researcher_id: int, variety: str,
                    d: date) -> tuple[list[PlaybookItem], list[dict]]:
    """筛出今日适用的 L2 条目。返回（进 prompt 的, 被筛掉的及原因）。

    排序固定为 `(priority, id)`：priority 是研究员表达「先看哪条」的唯一手段，
    id 兜底让顺序与插入先后无关。顺序一旦随查询结果飘，缓存前缀就每天都不一样。
    """
    rows = session.scalars(
        select(PlaybookItem)
        .where(PlaybookItem.researcher_id == researcher_id,
               PlaybookItem.variety == variety,
               PlaybookItem.status == "生效")
    ).all()
    rows = sorted(rows, key=lambda i: (i.priority, i.id))

    kept, skipped = [], []
    for item in rows:
        applies, why = scope_applies(session, item.scope, d)
        if applies:
            kept.append(item)
        else:
            skipped.append({"id": item.id, "text": item.text, "reason": why})
    return kept, skipped


# ---------------------------------------------------------------- L1 骨架渲染

def _evidence_lines(side: dict) -> str:
    out = []
    for e in side.get("evidence") or []:
        tail = [x for x in (e.get("series_id"), e.get("source"), e.get("note")) if x]
        out.append(f"    · {e.get('desc', '')}" + (f"（{' · '.join(map(str, tail))}）" if tail else ""))
    return "\n".join(out)


def _threshold_line(t: dict) -> str:
    value = t["value"]
    cond = (f"落在 [{_num(value[0])}, {_num(value[1])}] 区间内"
            if t["operator"] == "between" else f"{t['operator']} {_num(value)}")
    hint = f" → {t['action_hint']}" if t.get("action_hint") else ""
    return f"  - [{t['id']}] {t['name']}：{t['series_id']} {cond}（{t['type']} · {t['horizon']}）{hint}"


def _falsifier_line(f: dict) -> str:
    periods = f.get("consecutive_periods") or 1
    side = {"bull": "多头", "bear": "空头"}.get(f.get("side"), f.get("side"))
    return (f"  - [{f['id']}] {f['desc']}：{f['series_id']} 满足「{f['expression']}」"
            f"连续 {periods} 期即证伪{side}逻辑")


def l1_block(j: Judgment) -> str:
    """L1 硬骨架。**LLM 只能读，不得改写**（SPEC §2）。

    刻意只渲染「定义」不渲染「当前值」：阈值今天有没有触发是 signals 算好的事实，
    属于当日信息，必须走 messages。把当前值写进来就等于每天换一次缓存前缀。
    """
    c = j.contradiction or {}
    window = c.get("window") or {}
    cost = j.cost_line or {}
    power = j.pricing_power or {}
    time_dim = j.time_dimension or {}

    parts = [
        "# 硬骨架（L1，研究员写定的判断，你只能读，不得改写）",
        f"品种：{j.variety}　判断版本：v{j.version}　作者：{j.author}",
        f"基调基线：{j.tone or '未填'}" + (f"（{j.tone_note}）" if j.tone_note else ""),
        "",
        "## 核心矛盾",
        c.get("text") or "（未填）",
    ]
    for key, label in (("bull", "多头"), ("bear", "空头")):
        side = c.get(key) or {}
        if side.get("claim") or side.get("evidence"):
            parts += [f"{label}：{side.get('claim', '')}", _evidence_lines(side)]
    if window:
        parts.append(f"验证窗口：{window.get('from', '')} — {window.get('to', '')}"
                     + (f"（{window['note']}）" if window.get("note") else ""))

    if cost:
        parts += ["", "## 成本线"]
        parts += [f"  - {i.get('desc', '')}"
                  + (f"：{_num(i.get('value'))}" if i.get("value") is not None else "")
                  for i in cost.get("items") or []]
        if cost.get("note"):
            parts.append(f"  （{cost['note']}）")

    if power:
        parts += ["", "## 定价权",
                  f"类别：{power.get('category') or '未填'}",
                  f"关键：{power.get('key') or '未填'}"]
        for key, label in (("short", "短期驱动"), ("long", "中长期驱动")):
            if power.get(key):
                parts.append(f"{label}：{'、'.join(power[key])}")

    if time_dim:
        parts += ["", "## 时间维度",
                  f"短期：{time_dim.get('short') or '未填'}",
                  f"中长期：{time_dim.get('long') or '未填'}"]

    parts += ["", "## 阈值定义（当前值与是否触发见消息里的「今日信号」，不要自己判定）"]
    parts += [_threshold_line(t) for t in j.thresholds or []] or ["  （无）"]

    parts += ["", "## 证伪条件定义（同上，判定结果见「今日信号」）"]
    parts += [_falsifier_line(f) for f in j.falsifiers or []] or ["  （无）"]

    if j.survey_notes:
        parts += ["", "## 调研信息差（研究员的一手信息，公开数据里没有）", j.survey_notes]

    if j.unstructured:
        parts += ["", "## 说不清、暂时绑不上数据的事项（只作背景，不能当证据）"]
        parts += [f"  - 〔{u.get('section', '')}〕{u.get('text', '')}" for u in j.unstructured]

    return _lines(*parts)


def playbook_block(items: list[PlaybookItem]) -> str:
    """L2 思路条目。条目化而不是一大坨 prompt——一大坨出了问题你不知道是哪句话导致的。"""
    head = ("# 思路条目（L2，这位研究员自己的分析思路）\n"
            "用到哪条就在证据里写它的编号（playbook_item_id），并填进 used_playbook_item_ids。")
    if not items:
        # 冷启动不是边界情况，是新研究员的必经之路（SPEC §2.1）。这里必须还能出简报，
        # 但要明说「没有个人思路」，免得模型拿通用套话冒充这个人的判断。
        return (f"{head}\n\n（这位研究员目前还没有生效的思路条目。今天只依据上面的硬骨架"
                f"与消息里的事实作答，不要自行脑补他的偏好与习惯说法。）")
    # scope 用 sort_keys 序列化而不是直接 repr：同一份 scope 存进库时键序可能不同，
    # 直接 repr 会让「内容没变但键序变了」也把整段缓存前缀作废。
    lines = [f"  #{i.id}（优先级 {i.priority}"
             + (f" · 适用条件 {json.dumps(i.scope, ensure_ascii=False, sort_keys=True)}"
                if i.scope else "")
             + f"）{i.text}" for i in items]
    return _lines(head, "", *lines)


# ---------------------------------------------------------------- 当日事实渲染

def _wanted_series(cards, j: Judgment | None) -> list[str]:
    """今天值得摆给 LLM 看的序列：核心盘面 + 判断引用到的 + 首屏宏观。"""
    ids = [c.series_id for c in cards if c.series_id]
    if j is not None:
        ids += list(j.whitelist or [])
        ids += [t["series_id"] for t in j.thresholds or []]
        ids += [f["series_id"] for f in j.falsifiers or []]
        ids += [m["series_id"] for m in j.marginal_focus or [] if m.get("series_id")]
    ids += list(FEATURED_SERIES)
    return sorted(dict.fromkeys(ids))


def facts(session: Session, d: date, j: Judgment | None, cards) -> list[dict]:
    """可引用事实表：**只收已登记、且截至 d 确有观测的序列**。

    这条筛选与闸门的第一条规则（series_id 必须在 indicators 里）是同一条，故意的：
    prompt 里出现过的东西必须全都核得动，否则就是在诱导模型去引用一个过不了闸门的数。
    """
    out = []
    for sid in _wanted_series(cards, j):
        ind = session.get(Indicator, sid)
        if ind is None:
            continue
        o = latest_obs(session, sid, day_end(d))
        if o is None:
            continue
        prev = previous_value(session, sid, o.as_of)
        out.append({
            "series_id": sid, "name": ind.name, "unit": ind.unit, "value": o.value,
            "as_of": o.as_of.astimezone(SHANGHAI).isoformat(),
            "change": None if prev is None else o.value - prev,
        })
    return out


def _facts_section(rows: list[dict]) -> str:
    if not rows:
        return "## 可引用事实\n（今天一条可核对的观测都没有：不要编，宁可只给基调与区间。）"
    lines = ["## 可引用事实（引用数值只能用这张表里的 series_id / 数值 / 时点，逐位照抄）",
             "series_id | 名称 | 数值 | 单位 | 时点(as_of) | 较上一数据点"]
    for r in rows:
        lines.append(f"{r['series_id']} | {r['name']} | {_num(r['value'])} | {r['unit']} | "
                     f"{r['as_of']} | {_num(r['change'])}")
    lines.append("（最后一列是变化量，只能用来叙述，不能填进证据的 value——"
                 "value 必须等于「数值」列。）")
    return "\n".join(lines)


def _signals_section(signals: list[Signal]) -> str:
    """今日信号。**已经是算好的事实**，不要重新判定有没有触发。"""
    if not signals:
        return ("## 今日信号\n（今天没有算出任何信号：多半是还没跑 evaluate_signals，"
                "或该品种暂无生效判断。不要自行判定阈值是否触发。）")
    lines = ["## 今日信号（阈值与证伪的判定结果，由确定性代码算出，不要自己重判）"]
    for s in signals:
        kind = {"threshold": "阈值", "falsifier": "证伪"}.get(s.rule_type, s.rule_type)
        lines.append(f"  #{s.id} [{kind}] {s.rule_id}：{s.state}"
                     + (f"，当前值 {_num(s.current_value)}" if s.current_value is not None else ""))
        if s.gap_note:
            lines.append(f"      数据缺口：{s.gap_note}")
        for e in s.evidence or []:
            anchor = (f"{e['series_id']} = {_num(e.get('value'))} @ {e.get('as_of')}"
                      if e.get("series_id") else "")
            lines.append(f"      · {e.get('desc', '')}" + (f"（{anchor}）" if anchor else ""))
    return "\n".join(lines)


def _background_section(session: Session, variety: str, d: date, cards,
                        citable: set[str]) -> str:
    """派生值与期限结构。**没有 series_id，所以不可作为证据引用**，只能当背景。"""
    lines = ["## 背景（派生值与结构，没有 series_id，不能作为证据引用，只能用于叙述）"]
    for c in cards:
        if c.series_id and c.status == "ok":
            continue  # 已经在可引用事实表里
        shown = _num(c.value) if c.status == "ok" else (c.note or "暂无")
        lines.append(f"  - {c.label}：{shown} {c.unit if c.status == 'ok' else ''}".rstrip())

    curve = contract_curve(session, variety, d)
    if curve["rows"]:
        main = next((r["contract"] for r in curve["rows"] if r["main"]), None)
        lines.append(f"  - 期限结构：{curve['shape'] or '不足以判定'}；"
                     f"合约 {len(curve['rows'])} 个，结算价 {_num(curve['min'])}–{_num(curve['max'])}"
                     + (f"；主力 {main}" if main else ""))

    # 已经在可引用事实表里的序列不再重复出现：macro 卡片给的是展示口径（换算过、
    # 时点按发布市场算），同一个 VIX 出现两次两个写法，模型抄哪个都可能被闸门打掉。
    macro = [m for m in macro_featured(session, d)
             if m["status"] == "ok" and m["series_id"] not in citable]
    if macro:
        lines.append("  - 宏观（展示口径，数值已按展示单位换算，不要当作证据数值引用）：")
        lines += [f"      {m['name']} {m['value']} {m['unit']}"
                  + (f"，较上一期 {m['change']}" if m.get("change") else "")
                  + f"（{m['as_of']}）" for m in macro]
    return "\n".join(lines)


# ---------------------------------------------------------------- 组装

@dataclass(frozen=True)
class PromptBundle:
    """一次简报调用的 prompt，外加「到底喂了什么」的清单。

    `playbook_item_ids` / `signal_ids` 必须与 system/messages 里实际出现的编号**完全一致**：
    闸门就是拿这两个集合去拦「引用了没给它看过的条目」，多喂一个就等于开了一个洞。
    """

    system: tuple[SystemBlock, ...]
    messages: tuple[Message, ...]
    playbook_item_ids: list[int] = field(default_factory=list)
    signal_ids: list[int] = field(default_factory=list)
    skipped_items: list[dict] = field(default_factory=list)


def build(session: Session, *, variety: str, d: date, judgment: Judgment,
          items: list[PlaybookItem], signals: list[Signal],
          skipped_items: list[dict] | None = None) -> PromptBundle:
    """拼 system 与 messages。

    system 三块：人设 → L1 骨架 → L2 条目，断点打在最后一块（playbook 之后）。
    只打一个断点：这三块在一个判断版本内一起稳定，拆成多个断点多花缓存写、换不回命中率。
    """
    system = (
        SystemBlock(_stable(PERSONA)),
        SystemBlock(_stable(l1_block(judgment))),
        SystemBlock(_stable(playbook_block(items)), cache_breakpoint=True),
    )

    cards = core_cards(session, variety, d)
    fact_rows = facts(session, d, judgment, cards)
    head = (f"# 今日事实（{variety}，交易日 {d.isoformat()}）\n"
            f"判断 v{judgment.version} 的复盘到期日是 {judgment.review_due.isoformat()}"
            + ("，**已过期**，措辞请保留余地。" if judgment.review_due < d else "。"))
    content = _lines(
        head, "",
        _facts_section(fact_rows), "",
        _signals_section(signals), "",
        _background_section(session, variety, d, cards,
                            {r["series_id"] for r in fact_rows}), "",
        TASK_TEMPLATE.format(trade_date=d.isoformat()),
    )

    return PromptBundle(
        system=system,
        messages=(Message("user", content),),
        playbook_item_ids=[i.id for i in items],
        signal_ids=[s.id for s in signals],
        skipped_items=list(skipped_items or []),
    )
