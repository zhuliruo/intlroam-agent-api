"""Hotfix 3.14: raw_comments 表 + 单条评论上报支持

Revision ID: 006
Revises: 005
Create Date: 2026-05-12

变更：
1. 新建 raw_comments 表（二级评论分析）
2. comment_attempts 加 parent_comment_id 字段（指向 raw_comments）
"""
from alembic import op
import sqlalchemy as sa

revision = "006"
down_revision = "005_post_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. 新表 raw_comments
    op.create_table(
        "raw_comments",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("raw_post_id", sa.BigInteger, sa.ForeignKey("raw_posts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("device_id", sa.String(50), nullable=False),
        sa.Column("platform", sa.String(20), nullable=False, server_default="facebook"),
        sa.Column("comment_author", sa.String(200)),
        sa.Column("comment_author_user_id", sa.String(64)),
        sa.Column("comment_content", sa.Text, nullable=False),
        sa.Column("comment_hash", sa.String(64), nullable=False),
        sa.Column("intent", sa.String(20)),
        sa.Column("intent_confidence", sa.Float),
        sa.Column("suggested_reply", sa.Text),
        sa.Column("llm_reasoning", sa.Text),
        sa.Column("llm_provider", sa.String(20)),
        sa.Column("llm_model", sa.String(50)),
        sa.Column("llm_latency_ms", sa.BigInteger),
        sa.Column("analyzed_at", sa.DateTime(timezone=True)),
        sa.Column("action_status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("replied_at", sa.DateTime(timezone=True)),
        sa.Column("actual_reply_text", sa.Text),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index("idx_raw_comments_post_id", "raw_comments", ["raw_post_id"])
    op.create_index("idx_raw_comments_hash", "raw_comments", ["comment_hash"])
    op.create_index("idx_raw_comments_intent", "raw_comments", ["intent", "action_status"])
    
    # 2. comment_attempts 加 parent_comment_id
    op.add_column("comment_attempts", 
        sa.Column("parent_comment_id", sa.BigInteger, 
                  sa.ForeignKey("raw_comments.id", ondelete="SET NULL")))


def downgrade() -> None:
    op.drop_column("comment_attempts", "parent_comment_id")
    op.drop_index("idx_raw_comments_intent")
    op.drop_index("idx_raw_comments_hash")
    op.drop_index("idx_raw_comments_post_id")
    op.drop_table("raw_comments")
