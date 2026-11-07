"""005 add post metadata

Revision ID: 005_post_metadata
Revises: 004_partner_potential  # 可能是 003_group_join_status，按需改
Create Date: 2026-05-11

加 author_user_id 和 post_created_at 字段。
author_user_id: 用于 24h 反刷屏检测（同一作者发太多帖）
post_created_at: 用于真实发布时间过滤（>7 天不评论）
"""
from alembic import op
import sqlalchemy as sa


revision = "005_post_metadata"
# 注意: 如果你的最后一个 revision 不是 004，请改这里
# 跑 alembic history 看实际 head
down_revision = "003_group_join_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "raw_posts",
        sa.Column("author_user_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "raw_posts",
        sa.Column("post_created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "idx_raw_posts_author_user_id",
        "raw_posts",
        ["author_user_id"],
    )
    op.create_index(
        "idx_raw_posts_post_created_at",
        "raw_posts",
        ["post_created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_raw_posts_post_created_at", table_name="raw_posts")
    op.drop_index("idx_raw_posts_author_user_id", table_name="raw_posts")
    op.drop_column("raw_posts", "post_created_at")
    op.drop_column("raw_posts", "author_user_id")
