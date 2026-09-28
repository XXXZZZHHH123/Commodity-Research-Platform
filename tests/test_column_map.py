"""终端导出文件的列映射学习缓存（SPEC §4.4 第 3 段）。

这块要守住的承诺只有一句：**把「每次导出都要拿给 AI 认列」变成「只有新格式才要人看」**。
所以测试围绕三件事：换日期不能失效、新格式必须吵、确认过一次之后不能再问第二次。
"""

import io
from datetime import date, datetime

import pytest
from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from tin.ingest import column_map as cm
from tin.ingest import vendor_terminal as vt
from tin.models import Indicator, Observation, VendorColumnMap


def make_workbook(sheets: dict[str, list[list]]) -> io.BytesIO:
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


# 一张「新格式」的钢联报表：两列编码库里都没登记过，正是过去每次都要拿给 AI 认的那种。
COL_A = "锡锭：社会库存：中国（周）"
COL_B = "锡：焊料企业开工率：中国（月）"


def sheet(rows: list[list], names=(COL_A, COL_B), codes=("ID07000001", "ID07000002")) -> dict:
    return {"Sheet1": [
        ["钢联数据"],
        ["指标名称", *names],
        ["单位", "吨", "%"],
        ["指标编码", *codes],
        ["频度", "周", "月"],
        ["指标描述", "·", "·"],
        *rows,
    ]}


DAY1 = sheet([[datetime(2026, 9, 18), 8968, 62.5]])
DAY2 = sheet([[datetime(2026, 9, 21), 8800, 63.1], [datetime(2026, 9, 22), 8750, 63.4]])


def headers_of(book) -> list[str]:
    return [s.name for s in vt.read_workbook(book)]


# ---------- 指纹 ----------

def test_same_report_on_another_day_keeps_the_same_fingerprint():
    """换日期、换数据行数——指纹必须纹丝不动，否则缓存每天失效一次，等于没做。"""
    assert cm.fingerprint(headers_of(make_workbook(DAY1))) == \
           cm.fingerprint(headers_of(make_workbook(DAY2)))


def test_fingerprint_ignores_punctuation_and_width_noise():
    """全角半角、冒号括号的差异不该算改版：终端不同版本导出的标点并不稳定。"""
    assert cm.fingerprint(["锡锭：库存：中国（周）"]) == cm.fingerprint(["锡锭: 库存: 中国(周)"])


def test_a_new_column_changes_the_fingerprint():
    """加了一列就是另一张报表，必须重新认一次。"""
    assert cm.fingerprint([COL_A, COL_B]) != cm.fingerprint([COL_A, COL_B, "新增的一列"])


def test_reordered_columns_are_deliberately_a_different_fingerprint():
    """列顺序参与指纹，是刻意取的保守一侧。

    映射按列名存，顺序本身不影响命中；顺序变了最可能的解释是报表改版，而改版之后
    同名列的口径未必还是原来那个。宁可多问人一次，也不要让一条口径已变的列悄悄
    套用旧映射写进事实层——错误方向必须是「多问一次」，不是「默默写错」。
    """
    assert cm.fingerprint([COL_A, COL_B]) != cm.fingerprint([COL_B, COL_A])


# ---------- 未命中要上报 ----------

def test_a_new_format_is_reported_instead_of_silently_guessed(session):
    """第一次遇到的格式：一条都不许猜，原样上报等人确认。"""
    fp = cm.fingerprint([COL_A, COL_B])
    mapped, unmapped = cm.lookup(session, fp, [COL_A, COL_B])
    assert mapped == {}
    assert unmapped == [COL_A, COL_B]


def test_unattended_import_of_a_new_format_reports_the_columns(session):
    """无人值守导入遇到新格式：不新建指标、不写数，把待确认的列摆出来。"""
    report = vt.load(session, make_workbook(DAY1), entered_by="定时任务", register_new=False)
    assert report.written == 0 and report.registered == 0
    assert report.column_cache_hits == 0
    got = {u["column_name"]: u for u in report.unmapped_columns}
    assert set(got) == {COL_A, COL_B}
    # 上报的每一项都带齐了确认所需的信息，调用方不必回头重算指纹
    assert got[COL_A]["fingerprint"] == cm.fingerprint([COL_A, COL_B])
    assert got[COL_A]["vendor_code"] == "ID07000001" and got[COL_A]["sheet"] == "Sheet1"
    assert "列映射" in report.line()


def test_summary_surfaces_unmapped_columns_without_counting_hits(session):
    """确认页要看得到「哪几列还没认」，但它只是预览，不该计入命中数。"""
    s = vt.summarize(session, make_workbook(DAY1))
    assert s["column_cache_hits"] == 0
    assert [u["column_name"] for u in s["unmapped_columns"]] == [COL_A, COL_B]
    assert s["column_fingerprints"]["Sheet1"] == cm.fingerprint([COL_A, COL_B])


def test_codes_already_bound_in_the_db_never_reach_the_cache(session):
    """编码通道能对上的列不进兜底通道：列映射是兜底，不是替代。"""
    book = make_workbook(sheet(
        [[datetime(2026, 9, 18), 8968, 62.5]],
        codes=("ID01517441", "a10083975")))  # 两个编码库里都已绑定
    s = vt.summarize(session, book)
    assert s["unmapped_columns"] == [] and s["column_cache_hits"] == 0


def test_background_job_carries_the_columns_that_need_a_human(tmp_path, monkeypatch, session):
    """网页那条通道也要看得到待确认的列，否则「等人确认」这句话落不了地。"""
    from sqlalchemy.orm import sessionmaker

    from tin.config import settings
    from tin.ingest import import_jobs

    monkeypatch.setattr(settings, "staging_dir", tmp_path)
    monkeypatch.setattr("tin.db.SessionLocal",
                        sessionmaker(session.get_bind(), expire_on_commit=False))
    token = import_jobs.stage(make_workbook(DAY1).getvalue())
    job_id = import_jobs.create_terminal_job(token, "测试员", "tin.xlsx")
    import_jobs.execute(job_id)

    state = import_jobs.snapshot(job_id)
    assert state["state"] == "done", state.get("error")
    assert [u["column_name"] for u in state["unmapped_columns"]] == [COL_A, COL_B]
    assert state["column_cache_hits"] == 0


# ---------- 确认一次，之后自动命中 ----------

def test_confirmed_mapping_makes_the_next_import_land_automatically(session):
    """这条是整块功能的价值所在：人只认一次，之后同格式的导出自动落到对的序列上。"""
    fp = cm.fingerprint([COL_A, COL_B])
    assert cm.confirm(session, "MYSTEEL", fp,
                      {COL_A: "MYSTEEL.SN.stock.social", COL_B: "SMM.SN.solder.oprate"},
                      actor="研究员") == 2
    session.commit()

    # 换一天导出，同一张报表：无人值守也能直接入库，不再需要人
    report = vt.load(session, make_workbook(DAY2), entered_by="定时任务", register_new=False)
    assert report.unmapped_columns == []
    assert report.column_cache_hits == 2
    assert report.rejected == []
    assert report.written == 4

    rows = session.scalars(select(Observation).where(
        Observation.series_id == "MYSTEEL.SN.stock.social")).all()
    assert {r.value for r in rows} == {8800.0, 8750.0}
    # 命中走的是已登记指标，口径以登记为准，绝不另起一条 MYSTEEL.ID07000001
    assert session.get(Indicator, "MYSTEEL.ID07000001") is None
    assert rows[0].caliber_snapshot["stock_scope"] == "社会库存"


def test_lookup_matches_across_punctuation_noise(session):
    """确认时存的是原名，命中时按归一化比对：标点变了不该让人再认一遍。"""
    fp = cm.fingerprint([COL_A])
    cm.confirm(session, "MYSTEEL", fp, {COL_A: "MYSTEEL.SN.stock.social"}, actor="研究员")
    mapped, unmapped = cm.lookup(session, fp, ["锡锭: 社会库存: 中国(周)"])
    assert unmapped == [] and list(mapped.values()) == ["MYSTEEL.SN.stock.social"]


# ---------- 幂等与命中计数 ----------

def test_confirming_twice_updates_in_place_instead_of_failing(session):
    """人在确认页改主意重点一次是常态：同一 (指纹, 列名) 重复确认要就地更新，不报错、不写重。"""
    fp = cm.fingerprint([COL_A])
    cm.confirm(session, "MYSTEEL", fp, {COL_A: "MYSTEEL.SN.stock.social"}, actor="甲")
    cm.confirm(session, "MYSTEEL", fp, {COL_A: "MYSTEEL.SN.stock.social"}, actor="甲")
    # 改绑到另一个指标也走同一条路
    cm.confirm(session, "MYSTEEL", fp, {COL_A: "MYSTEEL.SN.TC.YN40"}, actor="乙",
               source=cm.SOURCE_LLM)
    session.commit()

    rows = session.scalars(select(VendorColumnMap).where(
        VendorColumnMap.fingerprint == fp)).all()
    assert len(rows) == 1
    assert rows[0].series_id == "MYSTEEL.SN.TC.YN40"
    assert (rows[0].confirmed_by, rows[0].source) == ("乙", cm.SOURCE_LLM)


def test_hit_count_accumulates_across_imports(session):
    """命中计数回答的是「这张报表还在导吗」，长期为 0 的映射可以清理掉。"""
    fp = cm.fingerprint([COL_A, COL_B])
    cm.confirm(session, "MYSTEEL", fp,
               {COL_A: "MYSTEEL.SN.stock.social", COL_B: "SMM.SN.solder.oprate"}, actor="研究员")
    session.commit()
    row = session.scalars(select(VendorColumnMap).where(
        VendorColumnMap.column_name == COL_A)).one()
    assert row.hit_count == 0 and row.last_hit_at is None

    vt.load(session, make_workbook(DAY1), entered_by="定时任务", register_new=False)
    session.refresh(row)
    assert row.hit_count == 1 and row.last_hit_at is not None
    first_hit = row.last_hit_at

    vt.load(session, make_workbook(DAY2), entered_by="定时任务", register_new=False)
    session.refresh(row)
    assert row.hit_count == 2
    assert row.last_hit_at >= first_hit

    # 试算与确认页预览只是看看，不该把计数推上去
    vt.load(session, make_workbook(DAY2), entered_by="定时任务", dry_run=True)
    vt.summarize(session, make_workbook(DAY2))
    session.refresh(row)
    assert row.hit_count == 2


def test_source_outside_the_domain_is_refused(session):
    with pytest.raises(ValueError, match="映射来源"):
        cm.confirm(session, "MYSTEEL", cm.fingerprint([COL_A]),
                   {COL_A: "MYSTEEL.SN.stock.social"}, actor="研究员", source="随便编一个")


# ---------- 外键：数据库层面真的拦得住 ----------

def test_foreign_key_refuses_to_map_onto_a_series_that_does_not_exist(session):
    """缓存里不可能出现一条指向空气的映射——这一层由数据库兜底，不靠调用方自觉。

    与 `judgment_series_refs`「证伪条件只能引用已登记指标」是同一招。
    """
    assert session.get(Indicator, "MYSTEEL.SN.查无此项") is None
    with pytest.raises(IntegrityError):
        cm.confirm(session, "MYSTEEL", cm.fingerprint([COL_A]),
                   {COL_A: "MYSTEEL.SN.查无此项"}, actor="研究员")
    session.rollback()
    assert session.scalars(select(VendorColumnMap)).all() == []


# ---------- 候选建议（现阶段纯确定性，不调 LLM） ----------

def test_suggest_matches_on_the_registered_name(session):
    got = cm.suggest(session, "MYSTEEL", [COL_A], [COL_A])
    assert len(got) == 1
    item = got[0]
    assert item["column_name"] == COL_A
    assert item["source"] == cm.SUGGEST_RULE  # 将来 LLM 那批建议的 source 取 SUGGEST_LLM
    top = item["candidates"][0]
    assert top["series_id"] == "MYSTEEL.SN.stock.social"
    assert 0 < top["confidence"] <= 99 and top["reason"]


def test_suggest_matches_on_the_vendor_code_in_the_column_name(session):
    """列名里直接带着供应商编码时，那是最硬的证据，排第一。"""
    got = cm.suggest(session, "MYSTEEL", [], ["锡锭库存 [ID01517441]"])
    assert got[0]["candidates"][0]["series_id"] == "MYSTEEL.SN.stock.social"
    assert got[0]["candidates"][0]["confidence"] == 99


def test_suggest_returns_nothing_rather_than_a_wild_guess(session):
    """认不出来就给空候选。给一个离谱的候选比不给更危险——人会顺手点确认。"""
    got = cm.suggest(session, "MYSTEEL", [], ["某个与任何已登记指标都不沾边的列 ZZZQQQ"])
    assert got[0]["candidates"] == []


def test_suggest_does_not_offer_retired_indicators(session):
    ind = session.get(Indicator, "MYSTEEL.SN.stock.social")
    ind.status = "停用"
    session.flush()
    got = cm.suggest(session, "MYSTEEL", [COL_A], [COL_A])
    assert all(c["series_id"] != "MYSTEEL.SN.stock.social" for c in got[0]["candidates"])
