"""chatwoot_outbox — 管理员在 Chatwoot "Agent Leads" 收件箱里的回复 → 由对应手机发到 WhatsApp/Zalo

Revision ID: 015
Revises: 014
Create Date: 2026-11-10

用户 11-10: 转人工后的线索要能直接在 Chatwoot 里回复, 不用再去手机上开 WhatsApp/Zalo。
流程: Chatwoot API 收件箱 webhook (message_created, outgoing, 非私有备注)
      → POST /api/v2/chatwoot/webhook 写一行 pending
      → 手机 (fb_engagements.device_id) 轮询 /chatwoot/outbox/next 领走 (claimed)
      → 在 App 里发出 → /chatwoot/outbox/result (sent | failed)
      → 回写 Chatwoot 消息状态 (delivered / failed + 原因), 并把这条记进 fb_engagements.history

status: pending → claimed → sent | failed
        claimed 超过 CLAIM_TTL 没回报 → 重新 pending (attempts+1, 到上限 failed)
        pending 超过 24h (手机一直没上线) → failed
"""
from alembic import op

revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE chatwoot_outbox (
            id                  SERIAL PRIMARY KEY,
            chatwoot_message_id BIGINT       NOT NULL UNIQUE,
            conversation_id     BIGINT       NOT NULL,       -- Chatwoot display_id
            engagement_id       BIGINT       NOT NULL,       -- fb_engagements.id
            device_id           VARCHAR(64)  NOT NULL,
            platform            VARCHAR(20)  NOT NULL,       -- whatsapp | zalo
            phone               VARCHAR(20),                 -- E.164
            counterpart         VARCHAR(255),
            content             TEXT         NOT NULL,
            sender_name         VARCHAR(255),
            status              VARCHAR(16)  NOT NULL DEFAULT 'pending',
            attempts            INT          NOT NULL DEFAULT 0,
            failure_reason      VARCHAR(255),
            created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
            claimed_at          TIMESTAMPTZ,
            done_at             TIMESTAMPTZ
        );
    """)
    op.execute("CREATE INDEX ix_chatwoot_outbox_device ON chatwoot_outbox (device_id, status, id);")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS chatwoot_outbox;")
