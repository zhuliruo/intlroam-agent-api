"""initial schema

Revision ID: 001_initial
Revises:
Create Date: 2026-05-06

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---------- groups ----------
    op.create_table(
        "groups",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("platform", sa.String(20), nullable=False, server_default="facebook"),
        sa.Column("external_id", sa.String(100), nullable=False),
        sa.Column("url", sa.String(500), nullable=False),
        sa.Column("name", sa.String(300), nullable=True),
        sa.Column("member_count", sa.Integer(), nullable=True),
        sa.Column("language_hint", sa.String(10), nullable=True, server_default="vi"),
        sa.Column("last_crawl_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("crawl_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verify_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notes", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("platform", "external_id", name="uq_groups_platform_external_id"),
    )
    op.create_index("ix_groups_is_active", "groups", ["is_active"])

    # ---------- devices ----------
    op.create_table(
        "devices",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("device_id", sa.String(50), nullable=False, unique=True),
        sa.Column("name", sa.String(100), nullable=True),
        sa.Column("persona_config", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default="{}"),
        sa.Column("daily_limits", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default="{}"),
        sa.Column("platforms", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default='["facebook"]'),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("token_hash", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_devices_device_id", "devices", ["device_id"], unique=True)

    # ---------- raw_posts ----------
    op.create_table(
        "raw_posts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("group_id", sa.BigInteger(),
                  sa.ForeignKey("groups.id", ondelete="SET NULL"), nullable=True),
        sa.Column("device_id", sa.String(50), nullable=False),
        sa.Column("platform", sa.String(20), nullable=False, server_default="facebook"),
        sa.Column("fb_post_url", sa.String(500), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("author_name", sa.String(200), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("intent", sa.String(20), nullable=True),
        sa.Column("intent_confidence", sa.Float(), nullable=True),
        sa.Column("suggested_reply", sa.Text(), nullable=True),
        sa.Column("llm_reasoning", sa.Text(), nullable=True),
        sa.Column("llm_provider", sa.String(20), nullable=True),
        sa.Column("llm_model", sa.String(50), nullable=True),
        sa.Column("llm_latency_ms", sa.BigInteger(), nullable=True),
        sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("action_status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("replied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_reply_text", sa.Text(), nullable=True),
    )
    op.create_index("ix_raw_posts_group_id", "raw_posts", ["group_id"])
    op.create_index("ix_raw_posts_device_id", "raw_posts", ["device_id"])
    op.create_index("ix_raw_posts_content_hash", "raw_posts", ["content_hash"])
    op.create_index("ix_raw_posts_intent", "raw_posts", ["intent"])
    op.create_index("idx_raw_posts_group_time", "raw_posts", ["group_id", "captured_at"])
    op.create_index("idx_raw_posts_intent", "raw_posts", ["intent", "action_status"])

    # ---------- comment_attempts ----------
    op.create_table(
        "comment_attempts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("raw_post_id", sa.BigInteger(),
                  sa.ForeignKey("raw_posts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("device_id", sa.String(50), nullable=False),
        sa.Column("platform", sa.String(20), nullable=False, server_default="facebook"),
        sa.Column("attempt_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("elapsed_ms", sa.Integer(), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("failure_stage", sa.String(30), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("screenshot_url", sa.String(500), nullable=True),
        sa.Column("reply_text", sa.Text(), nullable=True),
    )
    op.create_index("ix_comment_attempts_raw_post_id", "comment_attempts", ["raw_post_id"])
    op.create_index("idx_attempts_device_time", "comment_attempts", ["device_id", "attempt_at"])
    op.create_index("idx_attempts_failures", "comment_attempts", ["success", "failure_stage"])

    # ---------- daily_device_stats ----------
    op.create_table(
        "daily_device_stats",
        sa.Column("date", sa.Date(), primary_key=True),
        sa.Column("device_id", sa.String(50), primary_key=True),
        sa.Column("platform", sa.String(20), primary_key=True, server_default="facebook"),
        sa.Column("posts_scanned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("posts_analyzed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("comments_attempted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("comments_succeeded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("likes_given", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("groups_visited", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("daily_device_stats")
    op.drop_table("comment_attempts")
    op.drop_table("raw_posts")
    op.drop_table("devices")
    op.drop_table("groups")
