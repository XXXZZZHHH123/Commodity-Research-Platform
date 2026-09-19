"""研究逻辑文本 → 判断草稿（FR-1.7）。

只求减少二次录入的打字量，不求解析准确：按“## 标题”分段预填文字类字段；
阈值、证伪条件、优先级等结构化内容沿用上一版本，由研究员对照新文本修订。
"""

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date

from tin.schemas.judgment import (
    PARADIGMS,
    CostItem,
    CostLine,
    JudgmentIn,
    MarginalItem,
    PricingPower,
    TimeDimension,
    UnstructuredItem,
)

TONE_KEYWORDS = (("区间", "区间震荡"), ("回调买入", "回调买入"), ("反弹做空", "反弹做空"),
                 ("偏多", "偏多"), ("偏空", "偏空"), ("观望", "观望"), ("等信号", "等信号"))


@dataclass
class ImportReport:
    mapped: list[str] = field(default_factory=list)
    carried: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)


def _norm(s: str) -> str:
    """全角转半角并去空白，让排版差异（括号、空格）不影响条目对应。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s))


def split_sections(text: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    heading = None
    for line in text.splitlines():
        if line.startswith("## "):
            heading = line[3:].strip()
            sections[heading] = ""
        elif heading is not None:
            sections[heading] += line + "\n"
    return {k: v.strip() for k, v in sections.items()}


def _find(sections: dict[str, str], *keys: str) -> tuple[str, str] | None:
    for heading, body in sections.items():
        if any(k in heading for k in keys):
            return heading, body
    return None


def _bullets(body: str) -> list[str]:
    return [re.sub(r"\*\*", "", ln.lstrip("- ").strip()) for ln in body.splitlines() if ln.strip().startswith("-")]


def _labelled(lines: list[str], label: str) -> list[str]:
    return [ln.split(":", 1)[1].strip() if ":" in ln else ln for ln in lines if ln.startswith(label)]


def import_text(text: str, *, variety: str, author: str, base: JudgmentIn | None,
                vendor_codes: dict[str, str]) -> tuple[JudgmentIn, ImportReport]:
    """vendor_codes: 数据商编码 → series_id，用于把白名单表格行对上已登记指标。"""
    rep = ImportReport()
    sec = split_sections(text)
    data = (base.model_dump(by_alias=True) if base else
            {"variety": variety, "author": author, "written_at": date.today(), "contradiction": {}})
    data["variety"], data["author"], data["source_note"] = variety, author, text

    m = re.search(r"(\d{4}-\d{2}-\d{2})", text[:300])
    if m:
        data["written_at"] = date.fromisoformat(m.group(1))

    if hit := _find(sec, "主逻辑"):
        data["contradiction"] = {**data.get("contradiction", {}), "text": hit[1]}
        rep.mapped.append(f"「{hit[0]}」→ 主逻辑原文（多空两方的论点与证据沿用上一版，请对照修订）")

    if hit := _find(sec, "短线边际"):
        raw = hit[1].strip("`\n ")
        old = {_norm(mi["desc"]): mi.get("series_id") for mi in data.get("marginal_focus", [])}
        items = [MarginalItem(desc=d.strip(), series_id=old.get(_norm(d))) for d in raw.split("|") if d.strip()]
        data["marginal_focus"] = [i.model_dump() for i in items]
        rep.mapped.append(f"「{hit[0]}」→ 短线边际 {len(items)} 条"
                          + ("" if all(i.series_id for i in items) else "（部分条目需重新关联指标）"))

    if hit := _find(sec, "成本线"):
        prev = data.get("cost_line") or {}
        data["cost_line"] = CostLine(type=prev.get("type"), value=prev.get("value"), source=prev.get("source"),
                                     items=[CostItem(desc=b) for b in _bullets(hit[1])],
                                     note=prev.get("note")).model_dump()
        rep.mapped.append(f"「{hit[0]}」→ 成本线 {len(data['cost_line']['items'])} 条")

    if hit := _find(sec, "定价权"):
        lines = _bullets(hit[1])
        prev = data.get("pricing_power") or {}
        data["pricing_power"] = PricingPower(
            category=prev.get("category"), sources=prev.get("sources", []),
            short=_labelled(lines, "短期"), long=_labelled(lines, "中长期"),
            key=next(iter(_labelled(lines, "关键")), prev.get("key"))).model_dump()
        rep.mapped.append(f"「{hit[0]}」→ 边际定价权")

    if hit := _find(sec, "时间维度"):
        lines = _bullets(hit[1])
        data["time_dimension"] = TimeDimension(short=next(iter(_labelled(lines, "短期")), None),
                                               long=next(iter(_labelled(lines, "中长期")), None)).model_dump()
        rep.mapped.append(f"「{hit[0]}」→ 时间维度")

    if hit := _find(sec, "调研信息差"):
        data["survey_notes"] = f"{hit[0]}\n{hit[1]}"
        rep.mapped.append(f"「{hit[0]}」→ 调研信息差（一期为自由文本）")

    if hit := _find(sec, "白名单"):
        found, missed = [], []
        for row in hit[1].splitlines():
            if not row.startswith("|") or "---" in row or "指标" in row.split("|")[1]:
                continue
            sid = next((s for code, s in vendor_codes.items() if code in row), None)
            (found if sid else missed).append(sid or row.split("|")[1].strip())
        data["whitelist"] = found
        rep.mapped.append(f"「{hit[0]}」→ 白名单 {len(found)} 个（按数据商编码匹配已登记指标）")
        rep.unmatched += [f"白名单“{m}”未匹配到已登记指标" for m in missed]

    if hit := _find(sec, "编排范式"):
        data["paradigm"] = next((p for p in PARADIGMS if p in hit[0] + hit[1]), data.get("paradigm"))
        rep.mapped.append(f"「{hit[0]}」→ 编排范式：{data['paradigm']}")

    if hit := _find(sec, "基调"):
        data["tone"] = next((t for k, t in TONE_KEYWORDS if k in hit[1]), data.get("tone"))
        data["tone_note"] = hit[1]
        rep.mapped.append(f"「{hit[0]}」→ 基调：{data['tone']}（原文存入基调附注）")

    known = ("主逻辑", "短线边际", "成本线", "定价权", "时间维度", "调研信息差", "白名单", "编排范式", "基调")
    rep.unmatched += [f"「{h}」不属于九字段模板，原文保留在导入来源中" for h in sec if not any(k in h for k in known)]

    if base:
        rep.carried.append(f"阈值 {len(base.thresholds)} 条、证伪条件 {len(base.falsifiers)} 条、"
                           f"冲突优先级、多空论点与证据沿用上一版本——请对照新文本逐条核对")
    else:
        data.setdefault("unstructured", [])
        data["unstructured"] = data["unstructured"] + [
            UnstructuredItem(section="全文", text="阈值、证伪条件、冲突优先级",
                             reason="首次导入不自动结构化，请在判断页逐条填写").model_dump()]
    return JudgmentIn.model_validate(data), rep
