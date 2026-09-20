"""Excel 批量导入：解析、口径防呆、四类分类与入库守卫。"""

import io
from datetime import datetime, time, timedelta, timezone

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from tin.config import SHANGHAI
from tin.ingest.excel_importer import (
    ERROR,
    READY,
    REVISION,
    SKIP,
    ImportError_,
    build_preview,
    commit_preview,
    match_header,
    parse_as_of,
    parse_value,
    read_table,
)
from tin.models import AuditLog, ImportPreview, Indicator, Observation


def xlsx(rows) -> bytes:
    wb = Workbook()
    for row in rows:
        wb.active.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def indicators(session):
    return list(session.scalars(select(Indicator)))


# ---------- 单元格解析 ----------

@pytest.mark.parametrize("raw, expected", [
    (406000, 406000.0), ("406,000", 406000.0), ("¥406,000 元", 406000.0),
    ("17500", 17500.0), ("4,979 吨", 4979.0), (15.44, 15.44),
])
def test_parse_value_cleans_separators_and_units(raw, expected):
    assert parse_value(raw) == expected


def test_parse_value_percent_respects_registered_unit():
    assert parse_value("15.5%", unit="%") == 15.5      # 指标单位就是 %，保留原数
    assert parse_value("15.5%", unit="吨") == 0.155     # 否则按小数制还原


@pytest.mark.parametrize("raw", ["暂无报价", "停牌", "-", "", None])
def test_parse_value_rejects_placeholders(raw):
    with pytest.raises(ValueError):
        parse_value(raw)


def test_parse_as_of_fills_time_only_when_date_has_none():
    filled, was_filled = parse_as_of("2026-09-18", time(11, 30))
    assert filled == datetime(2026, 9, 18, 11, 30, tzinfo=SHANGHAI) and was_filled is True

    exact, was_filled = parse_as_of("2026-09-18 14:05", time(11, 30))
    assert exact == datetime(2026, 9, 18, 14, 5, tzinfo=SHANGHAI) and was_filled is False


@pytest.mark.parametrize("raw", ["2026/09/18", "20260918", datetime(2026, 9, 18), 46283.0])
def test_parse_as_of_accepts_common_formats(raw):
    assert parse_as_of(raw, time(11, 30))[0].date().isoformat() == "2026-09-18"


def test_parse_as_of_rejects_impossible_date():
    with pytest.raises(ValueError, match="无法识别"):
        parse_as_of("2026-02-31", time(11, 30))


# ---------- 读表 ----------

def test_read_table_handles_gbk_csv():
    data = "日期,SMM 1# 锡现货\n2026-09-18,406000\n".encode("gb18030")
    assert read_table(data, "报价.csv")[0][1] == "SMM 1# 锡现货"


def test_read_table_rejects_unsupported_format():
    with pytest.raises(ImportError_, match="不支持的文件格式"):
        read_table(b"x", "report.pdf")


def test_read_table_rejects_oversized_file():
    rows = [[f"c{c}" for c in range(50)] for _ in range(1100)]  # 55,000 单元格
    with pytest.raises(ImportError_, match="规模限制"):
        read_table(xlsx(rows), "big.xlsx")


# ---------- 表头匹配与口径防呆 ----------

def test_code_tag_matches_exactly_without_variant_warning(session):
    spec = match_header("云南 40% 锡精矿加工费 [MYSTEEL.SN.TC.YN40]", indicators(session))
    assert (spec.series_id, spec.match_kind) == ("MYSTEEL.SN.TC.YN40", "code")
    assert spec.needs_choice is False, "显式代码属精确绑定，不该再要求人工确认"
    assert spec.error is None


def test_name_match_on_variant_prone_indicator_requires_confirmation(session):
    """库里只登记了 40% TC；表头写 60% 时按名称匹配会静默绑错，必须人工确认。"""
    spec = match_header("云南40%锡精矿加工费", indicators(session))
    assert spec.series_id == "MYSTEEL.SN.TC.YN40"
    assert spec.needs_choice is True
    assert "易混口径" in spec.error


def test_auto_fetched_indicator_is_refused(session):
    spec = match_header("沪锡主力合约结算价 [SHFE.SN.main.settle]", indicators(session))
    assert "自动采集指标不接受人工导入" in spec.error


def test_unregistered_header_is_reported(session):
    assert match_header("印尼精矿升水", indicators(session)).error == "系统未登记该指标"


def test_column_carries_registered_default_entry_time(session):
    assert match_header("SMM 1# 锡现货均价", indicators(session)).as_of_time == "11:30"


# ---------- 四类清单 ----------

def preview_spot(session, rows, filename="现货周报.xlsx"):
    return build_preview(session, xlsx(rows), filename, actor="测试员", variety="SN")


def test_preview_classifies_four_lists(loaded):
    from tests.conftest import enter_spot
    enter_spot(loaded, 401000, datetime(2026, 9, 16, 11, 30, tzinfo=SHANGHAI))

    summary = preview_spot(loaded, [
        ["日期", "SMM 1# 锡现货均价 [SMM.SN.spot.1]"],
        ["2026-09-18", 406000],        # 新数据 → 可导入
        ["2026-09-17", 404500],        # 新数据 → 可导入
        ["2026-09-16", 401000],        # 与库内完全一致 → 相同忽略
        ["2026-09-15", "暂无报价"],     # 非数值 → 异常
    ])
    assert (summary["ready_count"], summary["skipped_count"], summary["error_count"]) == (2, 1, 1)

    loaded.query = None  # noqa: B018  仅为强调下面读取的是新会话状态
    enter_spot(loaded, 399000, datetime(2026, 9, 14, 11, 30, tzinfo=SHANGHAI))
    summary = preview_spot(loaded, [["日期", "SMM 1# 锡现货均价 [SMM.SN.spot.1]"],
                                    ["2026-09-14", 400000]])
    assert summary["revision_count"] == 1
    assert summary["rows"][0]["old_value"] == 399000


def test_preview_flags_time_fill_for_date_only_cells(loaded):
    summary = preview_spot(loaded, [["日期", "SMM 1# 锡现货均价 [SMM.SN.spot.1]"], ["2026-09-18", 406000]])
    row = summary["rows"][0]
    assert row["time_filled"] is True
    assert row["as_of"].startswith("2026-09-18T11:30"), "只有日期时按指标登记的发布时刻补齐"


def test_preview_warns_on_magnitude_outlier(loaded):
    from tests.conftest import enter_spot
    for day in range(10, 16):
        enter_spot(loaded, 400000 + day, datetime(2026, 9, day, 11, 30, tzinfo=SHANGHAI))
    summary = preview_spot(loaded, [["日期", "SMM 1# 锡现货均价 [SMM.SN.spot.1]"], ["2026-09-18", 40.6]])
    assert "数量级" in summary["rows"][0]["warn"]


# ---------- 入库守卫 ----------

def test_commit_requires_live_preview(loaded):
    with pytest.raises(ImportError_, match="预览不存在"):
        commit_preview(loaded, "no-such-id", [], {}, actor="测试员", default_note="x")


def test_commit_rejects_expired_preview(loaded):
    summary = preview_spot(loaded, [["日期", "SMM 1# 锡现货均价 [SMM.SN.spot.1]"], ["2026-09-18", 406000]])
    row = loaded.get(ImportPreview, summary["preview_id"])
    row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    loaded.commit()
    with pytest.raises(ImportError_, match="已过期"):
        commit_preview(loaded, summary["preview_id"], [], {}, actor="测试员", default_note="x")


def test_commit_writes_through_record_gate_with_traceable_note(loaded):
    summary = preview_spot(loaded, [["日期", "SMM 1# 锡现货均价 [SMM.SN.spot.1]"], ["2026-09-18", 406000]])
    result = commit_preview(loaded, summary["preview_id"], [summary["rows"][0]["row_key"]], {},
                            actor="张三", default_note="SMM 官网定盘价")
    assert result["committed"] == 1
    obs = loaded.scalars(select(Observation).where(Observation.series_id == "SMM.SN.spot.1")).one()
    assert obs.entered_by == "张三", "录入人取上传操作者，不取表内文本"
    assert obs.note == "[截点补齐: 11:30] SMM 官网定盘价", "补齐时点必须留痕"
    assert result["recalculated_dates"] == ["2026-09-18"]


def test_commit_detects_concurrent_change_and_refuses_overwrite(loaded):
    """预览与确认之间库内被写入不同数值，必须拦截而不是悄悄改写。"""
    from tests.conftest import enter_spot
    summary = preview_spot(loaded, [["日期", "SMM 1# 锡现货均价 [SMM.SN.spot.1]"], ["2026-09-18", 406000]])
    enter_spot(loaded, 405000, datetime(2026, 9, 18, 11, 30, tzinfo=SHANGHAI))

    result = commit_preview(loaded, summary["preview_id"], [summary["rows"][0]["row_key"]], {},
                            actor="张三", default_note="SMM")
    assert result["committed"] == 0
    assert "已拦截改写" in result["stale_rows"][0]["reason"]
    obs = loaded.scalars(select(Observation).where(Observation.series_id == "SMM.SN.spot.1")).one()
    assert obs.value == 405000, "库内既有数值不得被覆盖"


def test_commit_revalidates_column_rebinding(loaded):
    summary = preview_spot(loaded, [["日期", "云南40%锡精矿加工费"], ["2026-09-18", 17500]])
    col_key = summary["columns"][0]["col_key"]
    assert summary["columns"][0]["needs_choice"] is True

    with pytest.raises(ImportError_, match="自动采集指标"):
        commit_preview(loaded, summary["preview_id"], [r["row_key"] for r in summary["rows"]],
                       {col_key: {"series_id": "SHFE.SN.main.settle"}}, actor="张三", default_note="x")

    with pytest.raises(ImportError_, match="未登记"):
        commit_preview(loaded, summary["preview_id"], [r["row_key"] for r in summary["rows"]],
                       {col_key: {"series_id": "NOT.REGISTERED"}}, actor="张三", default_note="x")


def test_unconfirmed_variant_column_cannot_slip_through(loaded):
    """未完成口径确认的列，即便勾选了行也不得入库。"""
    summary = preview_spot(loaded, [["日期", "云南40%锡精矿加工费"], ["2026-09-18", 17500]])
    result = commit_preview(loaded, summary["preview_id"], [r["row_key"] for r in summary["rows"]],
                            {}, actor="张三", default_note="x")
    assert result["committed"] == 0
    assert loaded.scalars(select(Observation).where(
        Observation.series_id == "MYSTEEL.SN.TC.YN40")).first() is None


def test_commit_after_confirming_variant_succeeds(loaded):
    summary = preview_spot(loaded, [["日期", "云南40%锡精矿加工费"], ["2026-09-18", 17500]])
    col_key = summary["columns"][0]["col_key"]
    result = commit_preview(loaded, summary["preview_id"], [r["row_key"] for r in summary["rows"]],
                            {col_key: {"series_id": "MYSTEEL.SN.TC.YN40", "note": "Mysteel 周报"}},
                            actor="张三", default_note="批量导入")
    assert result["committed"] == 1
    obs = loaded.scalars(select(Observation).where(
        Observation.series_id == "MYSTEEL.SN.TC.YN40")).one()
    assert obs.value == 17500 and obs.note.endswith("Mysteel 周报")


def test_reimporting_same_file_is_idempotent(loaded):
    rows = [["日期", "SMM 1# 锡现货均价 [SMM.SN.spot.1]"], ["2026-09-18", 406000]]
    first = preview_spot(loaded, rows)
    commit_preview(loaded, first["preview_id"], [first["rows"][0]["row_key"]], {},
                   actor="张三", default_note="SMM")
    second = preview_spot(loaded, rows)
    assert (second["skipped_count"], second["ready_count"], second["revision_count"]) == (1, 0, 0)


# ---------- 往返无损（方案 §6.2 验收项） ----------

def test_generated_template_reimports_without_any_fuzzy_matching(session):
    """模板表头带 [series_id]，回到导入器必须精确对齐，且不触发任何口径确认。"""
    from tin.export.excel import build_import_template

    header = read_table(build_import_template(session, "SN"), "模板.xlsx")[0]
    assert header[0] == "日期"
    specs = [match_header(h, indicators(session)) for h in header[1:]]
    assert specs, "模板里应至少有一个人工录入指标"
    assert all(s.match_kind == "code" for s in specs), "全部应走显式代码精确对齐"
    assert not any(s.needs_choice for s in specs), "精确对齐不得触发品位防呆"
    assert not any(s.error for s in specs)
    assert {s.series_id for s in specs} == {
        i.series_id for i in indicators(session)
        if i.fetch_mode == "manual" and i.status == "可用"}


def test_template_only_offers_manual_indicators(session):
    from tin.export.excel import build_import_template

    header = read_table(build_import_template(session, "SN"), "模板.xlsx")[0]
    codes = " ".join(str(h) for h in header)
    assert "SMM.SN.spot.1" in codes
    assert "SHFE.SN.main.settle" not in codes, "自动采集指标不应出现在人工导入模板里"
