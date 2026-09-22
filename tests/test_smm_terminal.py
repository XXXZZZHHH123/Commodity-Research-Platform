"""SMM 终端导出工作簿的整体导入（docs/SMM终端整体导入方案.md）。"""

import io
from datetime import date, datetime

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from tin.caliber.dictionary import ContractKind, PriceType, SpotSource, StockScope, WeightBasis
from tin.config import SHANGHAI
from tin.ingest import smm_terminal as smm
from tin.ingest.record import latest_index, record
from tin.models import Indicator, Observation
from tin.schemas.caliber import Caliber
from tin.schemas.observation import ObservationIn


def make_workbook(sheets: dict[str, list[list]]) -> io.BytesIO:
    """按 SMM 终端的四行表头布局造一本工作簿。"""
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


SAMPLE = {
    "Sheet1": [
        ["指标名称", "SHFE: 锡: 主力合约: 收盘价: 日度", "SHFE: 锡: 连二合约: 结算价: 日度",
         "（停）SHFE: 锡: 仓单日报分仓库_旧库: 期货: 日度"],
        ["指标Id", "a1001", "a1002", "a1003"],
        ["单位", "元/吨", "元/吨", "吨"],
        ["频率", "日", "日", "日"],
        [datetime(2026, 9, 18), 407490, 405000, 12],
        [datetime(2026, 9, 21), 409000, 408000, 13],
    ],
    "Sheet2": [
        ["指标名称", "SMM: 1#锡-平均价: 日度", "SMM: 锡矿矿端平衡: 缅甸矿进口量: 月度",
         "SMM: 锡锭社会库存: 总库存: 周度", "SMM: 锡现货买卖强度指数: 日度"],
        ["指标Id", "s20002960", "a10005168", "a10020744", "a10021649"],
        ["单位", "元/吨", "实物吨", "吨", "-"],
        ["频率", "日", "月", "周", "日"],
        [datetime(2026, 9, 18), 408200, 15000, 9100, 51.2],
        [datetime(2026, 9, 21), 409350, None, 8800, "-"],
    ],
    "说明": [["本表为说明页"], ["无数据"]],
}


@pytest.fixture
def series():
    return {s.vendor_code: s for s in smm.read_workbook(make_workbook(SAMPLE))}


def test_reads_the_four_row_header_layout(series):
    """指标Id 是对齐主键：改了显示名也不会错位，这是这条通道不做名称猜测的底气。"""
    assert set(series) == {"a1001", "a1002", "a1003", "s20002960", "a10005168", "a10020744", "a10021649"}
    spot = series["s20002960"]
    assert spot.unit == "元/吨" and spot.frequency == "日"
    assert spot.points == {date(2026, 9, 18): 408200.0, date(2026, 9, 21): 409350.0}


def test_sheets_without_the_header_are_skipped(series):
    """「说明」这类目录页没有四行表头，整张跳过而不是硬解析出一堆空列。"""
    assert all(s.sheet != "说明" for s in series.values())


def test_blank_and_dash_cells_are_not_zero(series):
    """SMM 用空单元格和「-」表示无值，绝不能当成 0 写进事实层。"""
    assert series["a10005168"].points == {date(2026, 9, 18): 15000.0}
    assert series["a10021649"].points == {date(2026, 9, 18): 51.2}


def test_duplicate_vendor_ids_are_taken_once():
    """同一 Id 在表里既有短名又有全名，只能留一条，否则同一序列会被登记两次。"""
    sheets = {"S": [
        ["指标名称", "SHFE: 锡锭库存总计: 周度", "SHFE: 锡: 库存周报: 本周库存小计: 周度"],
        ["指标Id", "a10024418", "a10024418"],
        ["单位", "吨", "吨"],
        ["频率", "周", "周"],
        [datetime(2026, 9, 18), 7059, 7059],
    ]}
    got = smm.read_workbook(make_workbook(sheets))
    assert [s.vendor_code for s in got] == ["a10024418"]


def test_discontinued_columns_are_flagged(series):
    assert series["a1003"].discontinued
    assert not series["a1001"].discontinued


def test_override_binds_to_the_already_registered_series(series):
    """已登记的现货价不另建一份，否则 BASIS 会指着一条空序列算不出数。"""
    assert series["s20002960"].series_id == "SMM.SN.spot.1"
    assert series["a1001"].series_id == "SMM.a1001"


def test_caliber_is_derived_from_the_name_hierarchy(series):
    cal = smm.derive_caliber(series["a1001"]).dump()
    assert cal["price_type"] == PriceType.收盘价
    assert cal["contract_kind"] == ContractKind.主力合约
    assert cal["time_type"] == "交易时点"

    assert smm.derive_caliber(series["a1002"]).dump()["contract_kind"] == ContractKind.连二连续
    assert smm.derive_caliber(series["a10020744"]).dump()["stock_scope"] == StockScope.社会库存
    assert smm.derive_caliber(series["a10005168"]).dump()["weight_basis"] == WeightBasis.实物吨
    assert smm.derive_caliber(series["s20002960"]).dump()["spot_source"] == SpotSource.SMM
    # SMM 自采数据是发布时点，不是交易时点
    assert smm.derive_caliber(series["a10020744"]).dump()["time_type"] == "发布时点"


def test_unknown_spot_market_stays_blank_rather_than_inventing_a_value():
    """个旧不在口径字典里。塞一个字典外的值会让同时点守卫失效，宁可留空并标注。"""
    sheets = {"S": [
        ["指标名称", "SMM: 个旧1#锡 - 平均价: 日度", "SMM: 长江 1#锡-平均价: 日度"],
        ["指标Id", "s22795417", "s20123078"],
        ["单位", "元/吨", "元/吨"],
        ["频率", "日", "日"],
        [datetime(2026, 9, 21), 408000, 409000],
    ]}
    by_code = {s.vendor_code: s for s in smm.read_workbook(make_workbook(sheets))}
    gejiu = smm.derive_caliber(by_code["s22795417"]).dump()
    assert "spot_source" not in gejiu or gejiu["spot_source"] is None
    assert "个旧" in gejiu["note"]
    assert smm.derive_caliber(by_code["s20123078"]).dump()["spot_source"] == SpotSource.长江有色


def test_load_registers_indicators_and_writes_observations(session):
    report = smm.load(session, make_workbook(SAMPLE), entered_by="测试")
    assert report.skipped_series == 1  # 停用列
    assert report.rejected == []
    assert report.written == 10  # 两表各列的有效点之和，停用列不计

    ind = session.get(Indicator, "SMM.a1001")
    assert ind is not None and ind.vendor_code == "a1001" and ind.fetch_mode == "manual"
    assert ind.source == smm.SOURCE

    rows = session.scalars(select(Observation).where(Observation.series_id == "SMM.a1001")).all()
    assert {r.value for r in rows} == {407490.0, 409000.0}
    # 日期单元格没有时刻，按发布方的常规发布时刻补齐，并在 note 里说明补齐规则
    assert {r.as_of.astimezone(SHANGHAI).strftime("%H:%M") for r in rows} == {"15:00"}
    assert "补齐" in rows[0].note


def test_load_is_idempotent(session):
    first = smm.load(session, make_workbook(SAMPLE), entered_by="测试")
    second = smm.load(session, make_workbook(SAMPLE), entered_by="测试")
    assert first.written > 0
    assert second.written == 0
    assert second.unchanged == first.written
    assert second.registered == 0


def test_load_still_goes_through_the_record_gate(session):
    """口径与登记不符时必须被拒，批量通道不是绕过闸门的后门。"""
    session.add(Indicator(
        series_id="SMM.a1001", name="占位", variety="SN", category="价格",
        caliber={"time_type": "交易时点", "price_type": "结算价"},  # 与表里的「收盘价」不符
        unit="元/吨", source="SMM 终端", frequency="日", fetch_mode="manual", phase="P1"))
    session.flush()

    report = smm.load(session, make_workbook(SAMPLE), entered_by="测试")
    assert any("口径不符" in r for r in report.rejected)
    assert session.scalars(select(Observation).where(Observation.series_id == "SMM.a1001")).all() == []


def test_batch_cache_behaves_exactly_like_the_uncached_gate(session):
    """`record()` 的缓存参数只为省查询，判重与修订号必须与逐条查询完全一致。"""
    session.add(Indicator(
        series_id="T.X", name="测试", variety="SN", category="价格", caliber={"time_type": "发布时点"},
        unit="元/吨", source="测试", frequency="日", fetch_mode="auto", phase="P1"))
    session.flush()
    at = datetime(2026, 9, 21, 15, tzinfo=SHANGHAI)

    def obs(value):
        return ObservationIn(series_id="T.X", value=value, as_of=at,
                             caliber=Caliber(time_type="发布时点"), source="测试")

    assert record(session, obs(1.0)).revision == 0
    assert record(session, obs(1.0)) is None          # 值未变
    assert record(session, obs(2.0)).revision == 1    # 值变了，追加修订

    cache = latest_index(session, ["T.X"])
    assert record(session, obs(2.0), cache=cache) is None
    bumped = record(session, obs(3.0), cache=cache)
    assert bumped.revision == 2
    # 缓存要跟着更新，否则同一批里的后续点会拿到过期的修订号
    assert record(session, obs(3.0), cache=cache) is None
    session.flush()
    assert len(session.scalars(select(Observation).where(Observation.series_id == "T.X")).all()) == 3


def test_a_workbook_without_the_smm_layout_is_rejected_loudly():
    """格式不符要明确报错，不能静默返回空——静默返回空正是网页导入那个「四个 0」的病根。"""
    sheets = {"S": [["日期", "随便一列"], [datetime(2026, 9, 21), 1]]}
    with pytest.raises(smm.SmmFormatError):
        smm.read_workbook(make_workbook(sheets))


# ---------- 网页整体导入通道 ----------

def test_detects_terminal_layout_without_reading_everything():
    """探测必须发生在单元格上限之前，所以只看前四行的首列标签。"""
    assert smm.looks_like_terminal_export(make_workbook(SAMPLE).getvalue())
    plain = {"S": [["日期", "SMM 1# 锡现货均价 [SMM.SN.spot.1]"], [datetime(2026, 9, 21), 409000]]}
    assert not smm.looks_like_terminal_export(make_workbook(plain).getvalue())
    assert not smm.looks_like_terminal_export(b"not an xlsx at all")


def test_summary_answers_what_a_human_can_actually_judge(session):
    """几十万行没人逐行看得完，确认页给的是批次级信息。"""
    s = smm.summarize(session, make_workbook(SAMPLE))
    assert s["mode"] == "smm_terminal"
    assert s["new_indicators"] == 5
    assert [r["series_id"] for r in s["reused_indicators"]] == ["SMM.SN.spot.1"]
    assert s["skipped_discontinued"] == 1
    assert s["points"] == 10
    assert (s["first_date"], s["last_date"]) == ("2026-09-18", "2026-09-21")
    assert s["caliber_conflicts"] == []
    assert sum(s["categories"].values()) == s["new_indicators"]


def test_summary_pre_checks_caliber_conflicts(session):
    """口径冲突只会出在已登记的那几条上，预检一次，别等跑完三分钟才发现。"""
    session.add(Indicator(
        series_id="SMM.a1001", name="占位", variety="SN", category="价格",
        caliber={"time_type": "交易时点", "price_type": "结算价"},
        unit="元/吨", source="SMM 终端", frequency="日", fetch_mode="manual", phase="P1"))
    session.flush()
    s = smm.summarize(session, make_workbook(SAMPLE))
    assert any("口径不符" in c for c in s["caliber_conflicts"])


def test_staging_token_cannot_escape_the_staging_directory(tmp_path, monkeypatch):
    """令牌直接参与拼文件名，必须挡住路径穿越。"""
    from tin.config import settings
    from tin.ingest import import_jobs

    monkeypatch.setattr(settings, "staging_dir", tmp_path)
    for bad in ("../../etc/passwd", "a" * 31, "../" + "a" * 29, "ABCDEF" + "0" * 26):
        with pytest.raises(ValueError, match="令牌"):
            import_jobs.staging_path(bad)

    token = import_jobs.stage(b"x")
    assert import_jobs.staging_path(token).parent == tmp_path


def test_job_status_never_leaks_the_staging_token(tmp_path, monkeypatch):
    from tin.config import settings
    from tin.ingest import import_jobs

    monkeypatch.setattr(settings, "staging_dir", tmp_path)
    token = import_jobs.stage(make_workbook(SAMPLE).getvalue())
    job_id = import_jobs.create_smm_job(token, "测试员", "tin.xlsx")
    state = import_jobs.snapshot(job_id)
    assert "token" not in state
    assert state["state"] == "running" and state["percent"] == 0


def test_background_job_imports_and_reports(tmp_path, monkeypatch, session):
    """后台任务跑完要把结果留在状态里，并写一条审计。"""
    from sqlalchemy.orm import sessionmaker

    from tin.config import settings
    from tin.ingest import import_jobs
    from tin.models import AuditLog

    monkeypatch.setattr(settings, "staging_dir", tmp_path)
    monkeypatch.setattr("tin.db.SessionLocal", sessionmaker(session.get_bind(), expire_on_commit=False))

    token = import_jobs.stage(make_workbook(SAMPLE).getvalue())
    job_id = import_jobs.create_smm_job(token, "测试员", "tin.xlsx")
    import_jobs.execute(job_id)

    state = import_jobs.snapshot(job_id)
    assert state["state"] == "done", state.get("error")
    assert state["registered"] == 5 and state["written"] == 10
    assert state["percent"] == 100
    # 暂存文件跑完即删，不留副本
    assert not import_jobs.staging_path(token).exists()

    log = session.scalars(select(AuditLog).where(AuditLog.action == "导入")).all()
    assert len(log) == 1 and log[0].detail["written"] == 10


def test_background_job_records_failure_instead_of_swallowing_it(tmp_path, monkeypatch):
    from tin.config import settings
    from tin.ingest import import_jobs

    monkeypatch.setattr(settings, "staging_dir", tmp_path)
    token = import_jobs.stage("这不是一个 xlsx".encode())
    job_id = import_jobs.create_smm_job(token, "测试员", "坏文件.xlsx")
    import_jobs.execute(job_id)
    state = import_jobs.snapshot(job_id)
    assert state["state"] == "failed" and state["error"]
