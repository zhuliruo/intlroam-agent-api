"""groups.country — 群组所属国家 (用于优先群与按国家定制发帖/评论)

Revision ID: 010
Revises: 009
Create Date: 2026-10-31

国家原来只写在 notes 里 ('seed 20260809 / Italy'), 大小写和写法都不统一
(French/CANADA/Hawaii), 跨洲的群 (Europe/Caribbean/others) 也混在一起。
单独一列: 标准英文国名, 识别不出来的 (多国群) 留 NULL。
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("groups", sa.Column("country", sa.String(60), nullable=True))
    op.create_index("ix_groups_country", "groups", ["country"])


def downgrade() -> None:
    op.drop_index("ix_groups_country", table_name="groups")
    op.drop_column("groups", "country")
