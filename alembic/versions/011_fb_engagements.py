"""fb_engagements — 主动回复: Messenger 私信 / 别人回复我们的评论 / 评论我们的帖子

Revision ID: 011
Revises: 010
Create Date: 2026-11-03

手机端 NotificationListener 抓到 FB/Messenger 推送 → /engagement/claim 判重+生成回复
→ 手机发出去 → /engagement/result 回填。
- 评论类: 每个 (设备, 对方, 帖子) 只回一次 (thread_key 唯一)
- 私信: 每个 (设备, 对方) 只自动回第一条, 之后进日报交给人工 (human_status)
"""
from alembic import op

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE fb_engagements (
        id              SERIAL PRIMARY KEY,
        device_id       VARCHAR(64)  NOT NULL,
        kind            VARCHAR(20)  NOT NULL,          -- dm | comment_reply | post_comment
        counterpart     VARCHAR(200) NOT NULL,
        thread_key      VARCHAR(64)  NOT NULL,
        their_text      TEXT,
        context_text    TEXT,                           -- 通知原文 (含帖子/评论摘要)
        our_reply       TEXT,
        brand           BOOLEAN,
        status          VARCHAR(20)  NOT NULL DEFAULT 'claimed',  -- claimed|sent|failed|skipped
        failure_reason  VARCHAR(200),
        attempts        INT          NOT NULL DEFAULT 1,
        human_status    VARCHAR(20),                    -- dm: pending → done (人工跟进)
        created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
        updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
        sent_at         TIMESTAMPTZ,
        UNIQUE (device_id, kind, thread_key)
    );
    CREATE INDEX ix_fb_engagements_created ON fb_engagements (created_at);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE fb_engagements")
