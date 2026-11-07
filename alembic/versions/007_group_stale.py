"""Hotfix 3.16 H1: groups.last_stale_at for soft-stale tracking

Revision ID: 007_group_stale
Revises: 006_raw_comments_and_endpoints
Create Date: 2026-05-16

软标记群组旧帖太多 (连续 5 篇 >7 天)。
不冻结群组, 只是排序时降低优先级让其他群组先采集。
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "groups",
        sa.Column("last_stale_at", sa.DateTime(timezone=True), nullable=True),
    )
    # 索引: 用于 next_groups 排序
    op.create_index(
        "ix_groups_last_stale_at",
        "groups",
        ["last_stale_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_groups_last_stale_at", table_name="groups")
    op.drop_column("groups", "last_stale_at")
