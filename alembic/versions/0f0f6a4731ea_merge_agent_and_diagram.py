"""合流：二期 Agent 层与产业图

两条分支都从 6c0c0443dc71 分出去，各自建了自己的表（Agent 层的简报/策略、
产业图的 diagram_templates）。互不相干，但 Alembic 会因为**两个 head** 而拒绝
`upgrade head`——不合流的话谁都升不了级。

这个修订不改任何表结构，只是把两条链接回一条。

Revision ID: 0f0f6a4731ea
Revises: 97707b0ebd62, aa91f2283c14
Create Date: 2026-09-28

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import tin.db


# revision identifiers, used by Alembic.
revision: str = '0f0f6a4731ea'
down_revision: Union[str, Sequence[str], None] = ('97707b0ebd62', 'aa91f2283c14')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
