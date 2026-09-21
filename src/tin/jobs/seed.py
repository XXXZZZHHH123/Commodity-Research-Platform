import json
from datetime import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from tin.config import ROOT, settings
from tin.judgments.service import save_version
from tin.models import Indicator, Judgment
from tin.schemas.caliber import Caliber
from tin.schemas.judgment import JudgmentIn

SEEDS = ROOT / "seeds"
TIN_LOGIC = ROOT / "PRD" / "参考资料" / "锡_研究逻辑.txt"


def seed_indicators(session: Session, path: Path = SEEDS / "indicators_sn.json") -> int:
    """只补登记缺失的指标，不覆盖已有登记（研究员在页面上的修改优先）。"""
    added = 0
    for row in json.loads(path.read_text(encoding="utf-8")):
        if session.get(Indicator, row["series_id"]) is not None:
            continue
        Caliber.model_validate(row["caliber"])
        at = row.pop("default_entry_time", None)
        if at:
            row["default_entry_time"] = time.fromisoformat(at)
        session.add(Indicator(variety=row.pop("variety", settings.variety), phase="P1", status="可用", **row))
        added += 1
    session.commit()
    return added


def seed_judgment(session: Session, path: Path = SEEDS / "judgment_sn_v1.json") -> Judgment | None:
    data = json.loads(path.read_text(encoding="utf-8"))
    if session.scalars(select(Judgment).where(Judgment.variety == data["variety"])).first():
        return None
    data["source_note"] = f"导入自 PRD/参考资料/{TIN_LOGIC.name}\n\n" + TIN_LOGIC.read_text(encoding="utf-8")
    row = save_version(session, JudgmentIn.model_validate(data), actor="系统导入")
    session.commit()
    return row
