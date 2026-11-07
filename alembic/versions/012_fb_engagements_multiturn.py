"""fb_engagements 多轮私信: 会话历史 / 目的地 / 转人工

Revision ID: 012
Revises: 011
Create Date: 2026-11-03

用户 11-03: 私信从"只首回"改成多轮自动回复 (上限见 engagement.MAX_DM_TURNS),
有明确目的地时推荐具体产品 (产品页链接 + 网站实时价), 不再每次都发首页。
"""
from alembic import op

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE fb_engagements
        ADD COLUMN turns        INT     NOT NULL DEFAULT 0,      -- 我们已发出的条数
        ADD COLUMN history      JSONB   NOT NULL DEFAULT '[]',   -- [{"r":"them|us","t":"...","at":"iso"}]
        ADD COLUMN destination  VARCHAR(60),
        ADD COLUMN handover     BOOLEAN NOT NULL DEFAULT FALSE;  -- 转人工后不再自动回
    UPDATE fb_engagements SET turns = 1 WHERE status = 'sent';
    -- 已有的私信会话: 把首轮补进历史, 下一轮回复才有上下文; 不再算"待人工"
    UPDATE fb_engagements
       SET history = jsonb_build_array(
               jsonb_build_object('r','them','t',coalesce(their_text,''),'at',to_char(created_at,'YYYY-MM-DD"T"HH24:MI:SSOF')),
               jsonb_build_object('r','us','t',coalesce(our_reply,''),'at',to_char(coalesce(sent_at,created_at),'YYYY-MM-DD"T"HH24:MI:SSOF'))),
           human_status = 'auto'
     WHERE kind = 'dm' AND status = 'sent';
    """)


def downgrade() -> None:
    op.execute("""
    ALTER TABLE fb_engagements DROP COLUMN turns, DROP COLUMN history,
        DROP COLUMN destination, DROP COLUMN handover;
    """)
