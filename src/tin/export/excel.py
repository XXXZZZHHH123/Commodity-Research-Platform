"""Excel 生成。本阶段先提供导入模板；完整的自定义导出在 M-EXCEL-3 接入。"""

import io
from datetime import date, timedelta

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from sqlalchemy import select
from sqlalchemy.orm import Session

from tin.models import Indicator

HEADER_FILL = PatternFill("solid", fgColor="F4F6F5")
HEADER_FONT = Font(bold=True, size=11)
HINT_FONT = Font(size=9, color="5E6F66")


def _header(ws, labels: list[str]) -> None:
    ws.append(labels)
    for cell in ws[1]:
        cell.fill, cell.font = HEADER_FILL, HEADER_FONT
        cell.alignment = Alignment(horizontal="center")
    ws.freeze_panes = "B2"


def build_import_template(session: Session, variety: str) -> bytes:
    """导入模板：表头写成「指标名 [series_id]」，让回传文件精确对齐，绕开名称模糊匹配。"""
    rows = session.scalars(
        select(Indicator).where(Indicator.fetch_mode == "manual", Indicator.status == "可用",
                                Indicator.variety.in_([variety, "COMMON"]))
        .order_by(Indicator.category, Indicator.series_id)).all()

    wb = Workbook()
    ws = wb.active
    ws.title = f"{variety}_批量导入"
    _header(ws, ["日期"] + [f"{i.name} [{i.series_id}]" for i in rows])

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
