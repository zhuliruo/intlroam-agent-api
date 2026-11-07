"""fb_engagements 加 platform: TikTok 私信/评论回复复用同一张表

Revision ID: 013
Revises: 012
Create Date: 2026-11-03

用户 11-03: 给 TikTok 增加私信和评论回复。判重/限额/多轮逻辑与 FB 完全相同,
只是 platform 不同 (thread_key 里也带 platform, 两个平台同名用户互不干扰)。
"""
from alembic import op

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE fb_engagements ADD COLUMN platform VARCHAR(20) NOT NULL DEFAULT 'facebook';
    CREATE INDEX IF NOT EXISTS ix_fb_engagements_platform_created ON fb_engagements (platform, created_at);
    """)


def downgrade() -> None:
    op.execute("""
    DROP INDEX IF EXISTS ix_fb_engagements_platform_created;
    ALTER TABLE fb_engagements DROP COLUMN platform;
    """)
