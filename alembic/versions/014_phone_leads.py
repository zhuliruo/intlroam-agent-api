"""phone_leads_vn / phone_leads_intl — 采集到的电话号码 + WhatsApp/Zalo 打招呼

Revision ID: 014
Revises: 013
Create Date: 2026-11-07

用户 11-07: agent 运行中采集帖子里的电话号码 (必须能确定国家区号, 存 E.164),
越南号码与其他国家分开保存 —— 越南走 Zalo 联系, 其他国家走 WhatsApp。
群组和关键词全是旅行相关, 这些号码大概率是旅行从业者 → 打招呼找兼职 eSIM 分销商。

两张表结构完全相同, 只是按渠道分开:
  phone_leads_vn   (+84, Zalo)
  phone_leads_intl (其他, WhatsApp)

status: new → assigned (派给某台手机) → greeted / not_on_app / failed
        greeted 之后的对话在 fb_engagements (platform = zalo|whatsapp, lead_id 关联):
        replied / handover (转人工, 已发 Chatwoot) / opted_out (对方拒绝, 永不再联系)

fb_engagements 新增:
  lead_id                  打招呼对象 (WhatsApp/Zalo)
  lead_type                reseller | customer (LLM 判定, 转人工时带上)
  chatwoot_conversation_id 转人工时在 Chatwoot 开的会话; 之后对方再发消息直接转进去
"""
from alembic import op

revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None

_COLS = """
    id                SERIAL PRIMARY KEY,
    phone_e164        VARCHAR(20)  NOT NULL UNIQUE,     -- +84901234567
    country_code      VARCHAR(5)   NOT NULL,            -- 84 (国际区号, 不带 +)
    region            VARCHAR(2),                       -- VN (ISO 3166)
    raw_match         VARCHAR(60),                      -- 帖子里的原始写法
    source_platform   VARCHAR(20),                      -- facebook | tiktok | threads
    source_device     VARCHAR(64),
    source_group_id   BIGINT,
    source_raw_post_id BIGINT,
    source_author     VARCHAR(200),
    source_text       TEXT,                             -- 号码所在帖子片段 (打招呼时给 LLM 做个性化)
    seen_count        INT          NOT NULL DEFAULT 1,
    last_seen_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    status            VARCHAR(20)  NOT NULL DEFAULT 'new',
    assigned_device   VARCHAR(64),
    assigned_at       TIMESTAMPTZ,
    greeting          TEXT,
    greeted_at        TIMESTAMPTZ,
    display_name      VARCHAR(200),                     -- 对方在 WhatsApp/Zalo 上的名字 (回消息时按它认人)
    attempts          INT          NOT NULL DEFAULT 0,
    failure_reason    VARCHAR(200),
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now()
"""


def upgrade() -> None:
    for t in ("phone_leads_vn", "phone_leads_intl"):
        op.execute(f"CREATE TABLE {t} ({_COLS});")
        op.execute(f"CREATE INDEX ix_{t}_status ON {t} (status, created_at);")
        op.execute(f"CREATE INDEX ix_{t}_assigned ON {t} (assigned_device, status);")
    op.execute("""
    ALTER TABLE fb_engagements ADD COLUMN lead_id INT;
    ALTER TABLE fb_engagements ADD COLUMN lead_type VARCHAR(20);
    ALTER TABLE fb_engagements ADD COLUMN chatwoot_conversation_id INT;
    """)


def downgrade() -> None:
    op.execute("""
    ALTER TABLE fb_engagements DROP COLUMN chatwoot_conversation_id;
    ALTER TABLE fb_engagements DROP COLUMN lead_type;
    ALTER TABLE fb_engagements DROP COLUMN lead_id;
    DROP TABLE phone_leads_intl;
    DROP TABLE phone_leads_vn;
    """)
