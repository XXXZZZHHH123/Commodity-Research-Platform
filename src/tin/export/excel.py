"""Excel 生成：导入模板与自定义数据导出（方案 05 §3.8 / §4）。

导出的核心约束是把系统内的"不出数保护"带到离线文件里：
阻断、未计算、数据缺失三种空单元格必须能被区分，低频数据不得前向填充。
"""

import io
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill
from sqlalchemy import select
from sqlalchemy.orm import Session

from tin.compute.formulas import REGISTRY
from tin.config import SHANGHAI
from tin.models import Derived, Indicator, Judgment, Observation, TradingDay

HEADER_FILL = PatternFill("solid", fgColor="F4F6F5")
HEADER_FONT = Font(bold=True, size=11)
HINT_FONT = Font(size=9, color="5E6F66")
WARN_FONT = Font(bold=True, size=11, color="B45309")
WARN_FILL = PatternFill("solid", fgColor="FEF3C7")

# 商业授权来源：导出含这些来源的数据时写入审计日志并加合规标识（04 §8）
COMMERCIAL_SOURCES = ("SMM", "Mysteel", "LME", "ICDX")
COMPLIANCE_TEXT = ("【内部投研参考 严禁外发】本表含 SMM / Mysteel 等商业授权数据，"
                   "未经许可严禁以任何形式对外复制与传播。")

# 单位 → Excel 数字格式
NUMBER_FORMATS = {"元/吨": "#,##0", "吨": "#,##0", "实物吨": "#,##0", "手": "#,##0",
                  "元": "0.0000", "%": "0.00", "点": "#,##0.00", "指数": "#,##0.00"}

DATE_RANGES = {"recent_30_trade_days": 30, "recent_60_trade_days": 60, "recent_120_trade_days": 120}


@dataclass
class Column:
    field: str
    label: str
    kind: str  # meta / observation / derived / judgment


# ---------- 可选字段目录 ----------

def field_catalog(session: Session, variety: str) -> list[dict]:
    """按指标登记表动态生成，避免硬编码目录与指标表脱节。"""
    groups: dict[str, list[dict]] = {"交易日历": [
        {"field": "trade_date", "label": "交易日", "kind": "meta", "unit": "", "note": "每行一个交易日"}]}

    per_contract = re.compile(r"^SHFE\.[A-Z]+\.\d{4}\.")
    for ind in session.scalars(select(Indicator)
                               .where(Indicator.status == "可用", Indicator.variety.in_([variety, "COMMON"]))
                               .order_by(Indicator.category, Indicator.series_id)):
        if per_contract.match(ind.series_id):
            continue  # 逐合约行情按月换约，不适合放进长时序宽表
        groups.setdefault(ind.category, []).append({
            "field": ind.series_id, "label": ind.name, "kind": "observation", "unit": ind.unit,
            "note": f"{ind.source} · {ind.frequency}频 · {'人工' if ind.fetch_mode == 'manual' else '自动'}"})

    groups["派生指标"] = [{"field": spec.formula_id, "label": spec.name, "kind": "derived",
                       "unit": spec.unit, "note": spec.expression} for spec in REGISTRY.values()]
    groups["研判"] = [
        {"field": "judgment_tone", "label": "当日基调（含版本与过期）", "kind": "judgment", "unit": "",
         "note": "取该交易日当时生效的判断版本，并附过期天数"},
        {"field": "judgment_version", "label": "判断版本号", "kind": "judgment", "unit": "", "note": ""},
    ]
    return [{"name": name, "fields": fields} for name, fields in groups.items() if fields]


# ---------- 取数 ----------

def _trade_dates(session: Session, date_range: str, start: str | None, end: str | None) -> list[date]:
    rows = [date.fromisoformat(d) for d in session.scalars(
        select(TradingDay.trade_date).order_by(TradingDay.trade_date))]
    if date_range == "custom" and start and end:
        lo, hi = date.fromisoformat(start), date.fromisoformat(end)
        return [d for d in rows if lo <= d <= hi]
    return rows[-DATE_RANGES.get(date_range, 30):]


def _observation_series(session: Session, series_id: str, dates: list[date]) -> dict[date, dict]:
    """把观测落到交易日行上。非发布日留空（禁止 ffill）；as_of 非交易日则顺延到下一个交易日。"""
    if not dates:
        return {}
    lo = datetime.combine(dates[0] - timedelta(days=95), datetime.min.time()).replace(tzinfo=SHANGHAI)
    hi = datetime.combine(dates[-1] + timedelta(days=1), datetime.min.time()).replace(tzinfo=SHANGHAI)
    rows = session.scalars(
        select(Observation).where(Observation.series_id == series_id,
                                  Observation.as_of >= lo, Observation.as_of < hi)
        .order_by(Observation.as_of, Observation.revision)).all()

    out: dict[date, dict] = {}
    for obs in rows:
        on = obs.as_of.astimezone(SHANGHAI).date()
        if on in set(dates):
            out[on] = {"value": obs.value, "note": None}
            continue
        later = [d for d in dates if d > on]
        if later:  # 月度数据 as_of 常落在非交易日的月末
            out[later[0]] = {"value": obs.value,
                             "note": f"[顺延对齐] 原始数据时点 as_of 为 {on}（非交易日），顺延对齐至本交易日"}
    return out


def _derived_series(session: Session, variety: str, formula_id: str, dates: list[date]) -> dict[date, dict]:
    rows = session.scalars(
        select(Derived).where(Derived.variety == variety, Derived.formula_id == formula_id,
                              Derived.trade_date.in_([d.isoformat() for d in dates]))
        .order_by(Derived.id)).all()
    out: dict[date, dict] = {}
    for row in rows:  # 同日多次重算取最后一次
        if row.status == "ok":
            out[date.fromisoformat(row.trade_date)] = {"value": row.value, "note": None}
        elif row.status.startswith("blocked"):
            out[date.fromisoformat(row.trade_date)] = {
                "value": None, "note": f"[阻断不出数: 口径/时点不一致] {row.note or ''}".strip()}
        else:
            # 缺输入 ≠ 阻断：前者是没数可算，后者是算得出但口径对不上，两种含义不能混
            out[date.fromisoformat(row.trade_date)] = {
                "value": None, "note": f"[缺少输入] {row.note or ''}".strip()}
    return out


def _judgment_series(session: Session, variety: str, dates: list[date], field: str) -> dict[date, dict]:
    """取该交易日当时最新的判断版本；基调必须复合输出，防止过期研判脱离系统后被当成当下结论。"""
    versions = session.scalars(select(Judgment).where(Judgment.variety == variety)
                               .order_by(Judgment.written_at, Judgment.version)).all()
    out: dict[date, dict] = {}
    for d in dates:
        live = [j for j in versions if j.written_at <= d]
        if not live:
            continue
        j = live[-1]
        if field == "judgment_version":
            out[d] = {"value": f"v{j.version}（{j.status}）", "note": None}
            continue
        expired = (d - j.review_due).days
        tail = f"已过期 {expired} 天" if expired > 0 else "生效中"
        out[d] = {"value": f"{j.tone or '未定基调'}（v{j.version}，{tail}）", "note": None}
    return out


# ---------- 生成 ----------

def _resolve(session: Session, variety: str, columns: list[Column], dates: list[date]) -> tuple[dict, list[str]]:
    known = {i.series_id: i for i in session.scalars(select(Indicator))}
    data, skipped = {}, []
    for col in columns:
        if col.kind == "meta":
            continue
        if col.kind == "observation":
            ind = known.get(col.field)
            if ind is None or ind.status != "可用":
                skipped.append(col.field)  # 模板引用了已停用/已删除的指标：跳过而不是报错
                continue
            data[col.field] = _observation_series(session, col.field, dates)
        elif col.kind == "derived":
            if col.field not in REGISTRY:
                skipped.append(col.field)
                continue
            data[col.field] = _derived_series(session, variety, col.field, dates)
        else:
            data[col.field] = _judgment_series(session, variety, dates, col.field)
    return data, skipped


def _header_label(col: Column, known: dict) -> str:
    if col.kind == "observation":
        return f"{known[col.field].name} [{col.field}]"
    if col.kind == "derived":
        return f"{REGISTRY[col.field].name} [{col.field}]"
    return col.label


def build_export(session: Session, variety: str, columns: list[Column], *, date_range: str = "recent_30_trade_days",
                 start_date: str | None = None, end_date: str | None = None,
                 sort_order: str = "desc", actor: str = "") -> tuple[bytes, dict]:
    dates = _trade_dates(session, date_range, start_date, end_date)
    data, skipped = _resolve(session, variety, columns, dates)
    known = {i.series_id: i for i in session.scalars(select(Indicator))}
    columns = [c for c in columns if c.kind == "meta" or c.field not in skipped]

    wb = Workbook()
    ws = wb.active
    ws.title = f"{variety}_研究数据"
    ws.append([_header_label(c, known) for c in columns])
    for cell in ws[1]:
        cell.fill, cell.font = HEADER_FILL, HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.freeze_panes = "B2"
    ws.oddHeader.center.text = COMPLIANCE_TEXT

    ordered = sorted(dates, reverse=(sort_order != "asc"))
    for d in ordered:
        ws.append([None] * len(columns))
        row = ws.max_row
        for idx, col in enumerate(columns, start=1):
            cell = ws.cell(row=row, column=idx)
            if col.kind == "meta":
                cell.value, cell.number_format = d.isoformat(), "@"
                cell.alignment = Alignment(horizontal="center")
                continue
            point = data.get(col.field, {}).get(d)
            if point is None:
                cell.comment = Comment(_absent_note(session, col, known, d), "系统")
                continue
            cell.value = point["value"]
            if isinstance(point["value"], (int, float)):
                unit = known[col.field].unit if col.kind == "observation" else REGISTRY[col.field].unit
                cell.number_format = NUMBER_FORMATS.get(unit, "#,##0.00")
                cell.alignment = Alignment(horizontal="right")
            if point["note"]:
                cell.comment = Comment(point["note"], "系统")

    ws.column_dimensions["A"].width = 13
    for idx, col in enumerate(columns[1:], start=2):
        ws.column_dimensions[ws.cell(row=1, column=idx).column_letter].width = max(
            14, min(30, len(_header_label(col, known)) + 6))

    _write_dictionary(wb, session, variety, columns, known, skipped, dates, actor)
    buf = io.BytesIO()
    wb.save(buf)

    sources = sorted({known[c.field].source for c in columns
                      if c.kind == "observation" and known[c.field].source in COMMERCIAL_SOURCES})
    return buf.getvalue(), {"rows": len(ordered), "columns": len(columns), "skipped": skipped,
                            "commercial_sources": sources,
                            "range": [ordered[-1].isoformat(), ordered[0].isoformat()] if ordered else []}


def _absent_note(session: Session, col: Column, known: dict, d: date) -> str:
    """空单元格必须说明它为什么空：阻断、未计算、还是没取到数——三者含义完全不同。"""
    if col.kind == "derived":
        return "[未计算] 该交易日尚未执行派生计算"
    if col.kind == "judgment":
        return "[无判断] 该交易日尚无已写定的判断版本"
    ind = known.get(col.field)
    freq = ind.frequency if ind else ""
    if freq in ("周", "月", "事件"):
        return f"[非发布日] 该指标为{freq}频，当日无发布值（不沿用前值）"
    return "[数据缺失] 该交易日未取到数据"


def _write_dictionary(wb: Workbook, session: Session, variety: str, columns: list[Column],
                      known: dict, skipped: list[str], dates: list[date], actor: str) -> None:
    ws = wb.create_sheet("口径与合规说明")
    ws.append([COMPLIANCE_TEXT])
    ws["A1"].font, ws["A1"].fill = WARN_FONT, WARN_FILL
    ws.append([f"导出人：{actor or '—'}　导出时间：{datetime.now(SHANGHAI):%Y-%m-%d %H:%M}　"
               f"数据区间：{dates[0] if dates else '—'} 至 {dates[-1] if dates else '—'}　共 {len(dates)} 个交易日"])
    ws.append([])
    ws.append(["列名", "代码", "口径", "单位", "来源", "频率", "取数方式"])
    for cell in ws[4]:
        cell.fill, cell.font = HEADER_FILL, HEADER_FONT

    for col in columns:
        if col.kind == "observation":
            ind = known[col.field]
            caliber = " · ".join(str(v) for k, v in ind.caliber.items() if k != "note")
            ws.append([ind.name, col.field, caliber, ind.unit, ind.source, ind.frequency,
                       "人工录入" if ind.fetch_mode == "manual" else "自动采集"])
        elif col.kind == "derived":
            spec = REGISTRY[col.field]
            ws.append([spec.name, col.field, f"{spec.expression}；容差：{spec.tolerance}",
                       spec.unit, "派生计算", "日", "系统计算"])
        elif col.kind == "judgment":
            ws.append([col.label, col.field, "取该交易日当时最新的判断版本", "", "研究判断", "事件", "人工写定"])
        else:
            ws.append([col.label, col.field, "交易日历", "", "上期所", "日", "自动采集"])

    ws.append([])
    ws.append(["空单元格的三种含义（导出时已逐格写入批注）"])
    ws.append(["[阻断不出数]", "口径或时点不一致，系统拒绝给出看似正常的数值"])
    ws.append(["[未计算]", "该交易日尚未执行派生计算"])
    ws.append(["[数据缺失] / [非发布日]", "未取到数据，或该指标为低频、当日本就无发布值——一律不沿用前值"])
    if skipped:
        ws.append([])
        ws.append([f"以下指标已停用或不存在，本次导出已略过：{'、'.join(skipped)}"])
    for width, letter in ((30, "A"), (26, "B"), (52, "C"), (10, "D"), (14, "E"), (8, "F"), (12, "G")):
        ws.column_dimensions[letter].width = width


# ---------- 导入模板 ----------

def build_import_template(session: Session, variety: str) -> bytes:
    """导入模板：表头写成「指标名 [series_id]」，让回传文件精确对齐，绕开名称模糊匹配。"""
    rows = session.scalars(
        select(Indicator).where(Indicator.fetch_mode == "manual", Indicator.status == "可用",
                                Indicator.variety.in_([variety, "COMMON"]))
        .order_by(Indicator.category, Indicator.series_id)).all()

    wb = Workbook()
    ws = wb.active
    ws.title = f"{variety}_批量导入"
    ws.append(["日期"] + [f"{i.name} [{i.series_id}]" for i in rows])
    for cell in ws[1]:
        cell.fill, cell.font = HEADER_FILL, HEADER_FONT
        cell.alignment = Alignment(horizontal="center")
    ws.freeze_panes = "B2"

    ws.append(["口径与单位 →"] + [
        f"{i.unit}｜{'·'.join(str(v) for k, v in i.caliber.items() if k != 'note')}" for i in rows])
    ws.append(["发布时刻 →"] + [
        (i.default_entry_time.strftime("%H:%M") if i.default_entry_time else "15:00") for i in rows])
    for cell in list(ws[2]) + list(ws[3]):
        cell.font = HINT_FONT

    today = date.today()
    for offset in (2, 1):
        ws.append([(today - timedelta(days=offset)).isoformat()] + [None] * len(rows))

    ws.column_dimensions["A"].width = 16
    for idx, indicator in enumerate(rows, start=2):
        ws.column_dimensions[ws.cell(row=1, column=idx).column_letter].width = max(
            16, min(34, len(indicator.name) * 2 + 12))

    notes = wb.create_sheet("填写说明")
    for line in (
        ["填写说明"],
        ["1. 第 1 行表头请勿改动：方括号中的代码是系统对齐指标的依据，改动后将退回名称模糊匹配。"],
        ["2. 第 2、3 行为口径与发布时刻提示，导入时会被忽略，可以删除。"],
        ["3. 日期列只填日期时，系统按该指标登记的发布时刻补齐，并在来源说明中留痕。"],
        ["4. 单元格留空表示当日无数据；请勿填 0 或「暂无报价」等占位符。"],
        ["5. 仅人工录入类指标可以导入；交易所行情、汇率、VIX 等自动采集指标不接受人工写入。"],
        ["6. 单个文件最多 50,000 个单元格，超出请按时间分批。"],
    ):
        notes.append(line)
    notes["A1"].font = HEADER_FONT
    notes.column_dimensions["A"].width = 96

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
