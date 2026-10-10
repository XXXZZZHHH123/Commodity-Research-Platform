"""把仍停在「宏观」的五条序列归入与指标页相同的分类。

已改成其他分类的不动，避免盖掉登记之后的人工调整。

Revision ID: b7e4c1a09d55
Revises: 0f0f6a4731ea
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b7e4c1a09d55"
down_revision: Union[str, Sequence[str], None] = "0f0f6a4731ea"
branch_labels = None
depends_on = None

# 只搬迁仍使用旧档「宏观」的行。
MOVES = {
    "FX.USDCNY.mid": "美元与流动性",
    "MACRO.VIX": "风险偏好",
    "MACRO.SOX": "风险偏好",
    "MACRO.SPX": "风险偏好",
    "MACRO.FEDPROB": "利率与通胀",
}


def upgrade() -> None:
    conn = op.get_bind()
    for series_id, category in MOVES.items():
        conn.execute(sa.text(
            "UPDATE indicators SET category = :category "
            "WHERE series_id = :series_id AND category = '宏观'"
        ), {"category": category, "series_id": series_id})


def downgrade() -> None:
    conn = op.get_bind()
    for series_id, category in MOVES.items():
        conn.execute(sa.text(
            "UPDATE indicators SET category = '宏观' "
            "WHERE series_id = :series_id AND category = :category"
        ), {"category": category, "series_id": series_id})
