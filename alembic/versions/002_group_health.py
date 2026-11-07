"""hotfix 3.4: add group health & quarantine fields

Revision ID: 002_group_health
Revises: 001_initial_schema
Create Date: 2026-05-10 18:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = "002_group_health"
down_revision = "001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """加 5 个群组健康度字段"""
    op.add_column(
        "groups",
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "groups",
        sa.Column("quarantined_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "groups",
        sa.Column("quarantine_reason", sa.String(50), nullable=True),
    )
    op.add_column(
        "groups",
        sa.Column("last_health_check_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "groups",
        sa.Column("health_score", sa.Integer(), nullable=True),
    )
    
    # 加索引（quarantined_at 频繁查询）
    op.create_index(
        "ix_groups_quarantined_at",
        "groups",
        ["quarantined_at"],
    )
    
    # 给老群组设置 joined_at = created_at（避免新字段全是 NULL 影响逻辑）
    op.execute("UPDATE groups SET joined_at = created_at WHERE joined_at IS NULL")


def downgrade() -> None:
    op.drop_index("ix_groups_quarantined_at", table_name="groups")
    op.drop_column("groups", "health_score")
    op.drop_column("groups", "last_health_check_at")
    op.drop_column("groups", "quarantine_reason")
    op.drop_column("groups", "quarantined_at")
    op.drop_column("groups", "joined_at")
