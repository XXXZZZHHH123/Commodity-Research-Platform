import json
from datetime import datetime, time, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from tin.config import ROOT, settings
from tin.judgments.service import save_version
from tin.models import Indicator, Judgment, Researcher
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


def seed_researcher(session: Session, username: str = "me", display_name: str = "研究员") -> Researcher:
    """建一个默认研究员。简报、playbook、纠错日志全都挂在 researcher_id 上，
    一期只有一个人，但维度从第一天就留着——M3 多人时不用迁移数据。"""
    row = session.scalars(select(Researcher).where(Researcher.username == username)).first()
    if row is None:
        row = Researcher(username=username, display_name=display_name,
                         varieties=[settings.variety], status="在职",
                         created_at=datetime.now(timezone.utc))
        session.add(row)
        session.commit()
    return row


def _source_note() -> str:
    """原文是机密，`PRD/` 不随仓库分发（00 §7.1）——所以这里必须能在缺文件时活下去。

    本机有原文就连全文一起留证；没有就只记来源出处。降级后判断依然完整可用：
    结构化字段全部来自 seeds/judgment_sn_v1.json，缺的只是「导入无丢失」那条留证。
    CI 的托管 runner 和新装的服务器走的都是这条降级路径——早先这里无条件读文件，
    结果 `jobs init` 在新机器上直接崩，流水线的 test 环节也必红。
    """
    head = f"导入自 PRD/参考资料/{TIN_LOGIC.name}"
    if not TIN_LOGIC.exists():
        return f"{head}\n\n（本机无此原文：PRD/ 不随仓库分发，原文全文未随判断留存）"
    return f"{head}\n\n" + TIN_LOGIC.read_text(encoding="utf-8")


def seed_judgment(session: Session, path: Path = SEEDS / "judgment_sn_v1.json") -> Judgment | None:
    data = json.loads(path.read_text(encoding="utf-8"))
    if session.scalars(select(Judgment).where(Judgment.variety == data["variety"])).first():
        return None
    data["source_note"] = _source_note()
    row = save_version(session, JudgmentIn.model_validate(data), actor="系统导入")
    session.commit()
    return row
