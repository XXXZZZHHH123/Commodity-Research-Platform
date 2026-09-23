import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from tin.config import ROOT
from tin.judgments.importer import import_text
from tin.judgments.service import JudgmentError, save_version, to_payload
from tin.jobs.seed import TIN_LOGIC, seed_judgment
from tin.models import Indicator, Judgment, JudgmentSeriesRef
from tin.schemas.judgment import JudgmentIn, PriorityRule

SEED = json.loads((ROOT / "seeds" / "judgment_sn_v1.json").read_text(encoding="utf-8"))


def payload(**over) -> JudgmentIn:
    return JudgmentIn.model_validate({**SEED, **over})


def test_seed_imports_as_draft_blocked_only_by_missing_priority(session):
    j = seed_judgment(session)
    assert (j.version, j.status) == (1, "草稿")
    from tin.judgments.validate import activation_blockers
    blockers = activation_blockers(to_payload(j))
    assert len(blockers) == 1 and "冲突优先级" in blockers[0]
    assert TIN_LOGIC.name in j.source_note, "来源说明必须指名原文出处"
    if TIN_LOGIC.exists():
        # 本机有原文（PRD/ 不随仓库分发，CI 上没有）才能断言全文留证
        assert TIN_LOGIC.read_text(encoding="utf-8") in j.source_note, "原文全文随判断保存，保证导入无丢失"
    else:
        assert "未随判断留存" in j.source_note, "缺原文时必须显式声明，不能让人误以为留证完整"


def test_seed_keeps_unstructurable_items_visible(session):
    j = seed_judgment(session)
    texts = [u["text"] for u in j.unstructured]
    assert any("SPX 破 20 日线" in t for t in texts)
    assert any("40 万附近成交放量" in t for t in texts)


def test_seed_refs_are_recorded(session):
    j = seed_judgment(session)
    refs = session.scalars(select(JudgmentSeriesRef).where(JudgmentSeriesRef.judgment_id == j.id)).all()
    by_type = {r.ref_type for r in refs}
    assert {"whitelist", "marginal_focus", "threshold", "falsifier", "evidence"} <= by_type
    assert any(r.ref_id == "fl_myanmar" and r.series_id == "CUSTOMS.2609.import.MM" for r in refs)


def test_cannot_activate_without_priority_when_two_thresholds(session):
    with pytest.raises(JudgmentError, match="冲突优先级"):
        save_version(session, payload(), actor="t", activate=True)


def test_activation_archives_previous_active_version(session):
    pr = PriorityRule(order=["th_risk_vix", "th_entry_40w"], note="风控线优先").model_dump()
    v1 = save_version(session, payload(priority_rule=pr), actor="t", activate=True)
    v2 = save_version(session, payload(priority_rule=pr), actor="t", activate=True)
    session.refresh(v1)
    assert (v1.status, v2.status, v2.version) == ("已归档", "生效", 2)


def test_falsifier_must_bind_registered_series(session):
    bad = [{**SEED["falsifiers"][0], "series_id": "NOT.REGISTERED"}]
    with pytest.raises(JudgmentError, match="未登记的指标：NOT.REGISTERED"):
        save_version(session, payload(falsifiers=bad), actor="t")


def test_falsifier_expression_must_be_machine_checkable():
    bad = [{**SEED["falsifiers"][0], "expression": "缅甸进口明显放量"}]
    with pytest.raises(ValidationError, match="无法自动验证"):
        payload(falsifiers=bad)


def test_one_sided_contradiction_is_blocked(session):
    c = {**SEED["contradiction"], "bear": {"claim": "", "evidence": []}}
    pr = PriorityRule(order=["th_risk_vix", "th_entry_40w"]).model_dump()
    with pytest.raises(JudgmentError, match="主逻辑空方缺失"):
        save_version(session, payload(contradiction=c, priority_rule=pr), actor="t", activate=True)


# ---------- 导入器（预留的上传入口） ----------

def _vendor_codes(session):
    return {i.vendor_code: i.series_id for i in session.scalars(select(Indicator)) if i.vendor_code}


# 真实研究逻辑原文属机密，PRD/ 不随仓库分发——CI 上没有它。
# 解析器的结构契约改用 fixtures/research_logic_sample.txt（合成内容）保证覆盖，
# 下面两条针对真实原文的断言只在本机有文件时才跑。
SAMPLE_LOGIC = Path(__file__).parent / "fixtures" / "research_logic_sample.txt"
needs_real_logic = pytest.mark.skipif(not TIN_LOGIC.exists(),
                                      reason="真实研究逻辑原文在 PRD/ 下，不随仓库分发")


def test_first_import_prefills_all_nine_fields_and_matches_whitelist_by_vendor_code(session):
    """九字段模板的解析契约。用合成样本，保证 CI 上 importer.py 不是零覆盖。"""
    j, rep = import_text(SAMPLE_LOGIC.read_text(encoding="utf-8"), variety="SN", author="测试研究员",
                         base=None, vendor_codes=_vendor_codes(session))
    assert str(j.written_at) == "2026-09-18", "正文前 300 字内的日期应被识别为成文日"
    assert (j.tone, j.paradigm) == ("区间震荡", "总线合成")
    assert len(j.marginal_focus) == 4
    # 白名单按数据商编码匹配已登记指标；编造的 ZZ99999999 匹配不上，必须进 unmatched 而不是静默丢弃
    assert set(j.whitelist) == {"MYSTEEL.SN.stock.social", "MYSTEEL.SN.TC.YN40", "SMM.SN.solder.oprate"}
    assert any("未匹配到已登记指标" in u for u in rep.unmatched)
    assert any("不属于九字段模板" in u for u in rep.unmatched), "模板外的小节要上报，不能静默吞掉"
    assert j.cost_line and len(j.cost_line.items) == 3
    assert j.pricing_power.short and j.pricing_power.long and j.pricing_power.key
    assert j.time_dimension.short and j.time_dimension.long
    assert j.survey_notes and "华东调研" in j.survey_notes
    assert j.unstructured, "首次导入不自动结构化阈值/证伪，必须留可见待办"


def test_new_version_import_keeps_structured_rules_from_previous(session):
    """新版本导入沿用上一版的结构化规则，且排版噪音不得让边际条目丢失指标绑定。"""
    base = to_payload(seed_judgment(session))
    # 先用样本导一版并手工补上指标绑定，模拟研究员已关联过的状态
    first, _ = import_text(SAMPLE_LOGIC.read_text(encoding="utf-8"), variety="SN", author="测试研究员",
                           base=base, vendor_codes=_vendor_codes(session))
    for mi, sid in zip(first.marginal_focus, ["MYSTEEL.SN.stock.social", "MYSTEEL.SN.TC.YN40",
                                              "SMM.SN.solder.oprate", "LME.SN.stock"]):
        mi.series_id = sid
    # 再导一版，正文加上全角括号与多余空格——归一化必须让条目仍对得上
    noisy = SAMPLE_LOGIC.read_text(encoding="utf-8").replace(
        "社库斜率 | 加工费走势", "社库斜率（含隐形） |  加工费走势 ").replace(
        "高位区间震荡", "高位区间震荡偏强")
    j, rep = import_text(noisy, variety="SN", author="测试研究员", base=first,
                         vendor_codes=_vendor_codes(session))
    assert [t.id for t in j.thresholds] == ["th_risk_vix", "th_entry_40w"], "阈值沿用上一版"
    assert [f.id for f in j.falsifiers] == ["fl_myanmar", "fl_social_stock_2w"], "证伪条件沿用上一版"
    assert "偏强" in j.tone_note
    bound = {m.desc: m.series_id for m in j.marginal_focus}
    assert bound["加工费走势"] == "MYSTEEL.SN.TC.YN40", "多余空格不得让条目丢失指标绑定"
    assert bound["焊料开工率"] == "SMM.SN.solder.oprate"
    assert rep.carried and "逐条核对" in rep.carried[0]
    row = save_version(session, j, actor="t")
    assert row.version == 2 and row.status == "草稿"


@needs_real_logic
def test_real_logic_text_still_parses_on_machines_that_have_it(session):
    """本机有真实原文时，额外守住它特有的解析结果（CI 上跳过）。"""
    j, rep = import_text(TIN_LOGIC.read_text(encoding="utf-8"), variety="SN", author="锡研究员",
                         base=None, vendor_codes=_vendor_codes(session))
    assert str(j.written_at) == "2026-08-16"
    assert set(j.whitelist) == set(SEED["whitelist"])
    assert (j.tone, j.paradigm) == ("区间震荡", "总线合成")
    assert len(j.marginal_focus) == 4 and "40万补库线" in [m.desc for m in j.marginal_focus]
    assert any("关联" in u for u in rep.unmatched)
