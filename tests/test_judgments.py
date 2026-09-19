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
    assert "锡_研究逻辑.txt" in j.source_note, "原文全文随判断保存，保证导入无丢失"


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


def test_first_import_prefills_text_fields_and_matches_whitelist_by_vendor_code(session):
    j, rep = import_text(TIN_LOGIC.read_text(encoding="utf-8"), variety="SN", author="锡研究员",
                         base=None, vendor_codes=_vendor_codes(session))
    assert str(j.written_at) == "2026-08-16"
    assert set(j.whitelist) == set(SEED["whitelist"])
    assert (j.tone, j.paradigm) == ("区间震荡", "总线合成")
    assert len(j.marginal_focus) == 4 and "40万补库线" in [m.desc for m in j.marginal_focus]
    assert any("关联" in u for u in rep.unmatched)


def test_new_version_import_keeps_structured_rules_from_previous(session):
    base = to_payload(seed_judgment(session))
    new_text = TIN_LOGIC.read_text(encoding="utf-8").replace("高位宽幅震荡", "高位宽幅震荡偏强")
    j, rep = import_text(new_text, variety="SN", author="锡研究员", base=base, vendor_codes=_vendor_codes(session))
    assert [t.id for t in j.thresholds] == ["th_risk_vix", "th_entry_40w"]
    assert [f.id for f in j.falsifiers] == ["fl_myanmar", "fl_social_stock_2w"]
    assert "偏强" in j.tone_note
    assert all(m.series_id for m in j.marginal_focus), "排版差异（全角括号、空格）不得让边际条目丢失指标绑定"
    assert rep.carried and "逐条核对" in rep.carried[0]
    row = save_version(session, j, actor="t")
    assert row.version == 2 and row.status == "草稿"
