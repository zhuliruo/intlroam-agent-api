"""统一关键词表: keywords 取代 tiktok_search/tiktok_target/threads_search 三张表

Revision ID: 008
Revises: 007
Create Date: 2026-07-14

三张老表 80% 字段重合, 各平台实际用词高度雷同 -> 合成一张:
  role      = search | target        (target 才有 intent)
  platforms = text[]                 (一条词可同时供多个平台使用)
唯一键是 (lower(keyword), language, role)。

老表不删, 改名成 *_legacy_20260714 留回滚窗口。
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


# 老表 category 归并到统一词表 (tiktok 11 种 + threads 5 种 -> 6 种)
_CATEGORY_MAP_SQL = """
    CASE category
        WHEN 'destination_sim' THEN 'destination'
        WHEN 'city_jp'         THEN 'destination'
        WHEN 'city_kr'         THEN 'destination'
        WHEN 'city_sea'        THEN 'destination'
        WHEN 'city_tw_cn'      THEN 'destination'
        WHEN 'city_west'       THEN 'destination'
        WHEN 'destination'     THEN 'destination'
        WHEN 'generic_esim'    THEN 'connectivity'
        WHEN 'connectivity'    THEN 'connectivity'
        WHEN 'activity'        THEN 'activity'
        WHEN 'activity_based'  THEN 'activity'
        WHEN 'travel'          THEN 'activity'
        WHEN 'pain_points'     THEN 'pain_points'
        WHEN 'hashtag'         THEN 'hashtag'
        ELSE 'generic'
    END
"""

# 同一个词已存在时: 并 platforms, 取较大 weight, 累加 use_count, 保留最近使用时间
_ON_CONFLICT = """
    ON CONFLICT (lower(keyword), language, role) DO UPDATE SET
        platforms    = (SELECT array_agg(DISTINCT p ORDER BY p)
                        FROM unnest(keywords.platforms || excluded.platforms) AS p),
        weight       = GREATEST(keywords.weight, excluded.weight),
        use_count    = keywords.use_count + excluded.use_count,
        enabled      = keywords.enabled OR excluded.enabled,
        last_used_at = GREATEST(keywords.last_used_at, excluded.last_used_at)
"""


def upgrade() -> None:
    # ── 设备级语言配置 (空数组 = 用平台默认) ──
    op.add_column(
        "devices",
        sa.Column(
            "languages",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )

    # ── 统一关键词表 ──
    op.create_table(
        "keywords",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("keyword", sa.String(120), nullable=False),
        sa.Column("language", sa.String(10), nullable=False, server_default="en"),
        sa.Column("role", sa.String(10), nullable=False, server_default="search"),
        sa.Column("category", sa.String(40), nullable=False, server_default="generic"),
        sa.Column("intent", sa.String(20), nullable=True),
        sa.Column(
            "platforms",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("ARRAY['tiktok','threads']::text[]"),
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("weight", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("submitted_by", sa.String(80), nullable=True),
        sa.Column("use_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("role IN ('search','target')", name="ck_keywords_role"),
        sa.CheckConstraint(
            "intent IS NULL OR intent IN ('hot','warm')", name="ck_keywords_intent"
        ),
    )
    # 唯一键用 lower(keyword): 老表的 UNIQUE(keyword,language) 是大小写敏感的,
    # 会放进 'eSIM' / 'esim' 两行。合并时顺手收敛掉。
    op.execute(
        "CREATE UNIQUE INDEX ux_keywords_kw_lang_role "
        "ON keywords (lower(keyword), language, role)"
    )
    op.execute("CREATE INDEX ix_keywords_platforms ON keywords USING GIN (platforms)")
    op.create_index("ix_keywords_role_enabled", "keywords", ["role", "enabled"])

    # ── 迁数据 ──
    # DISTINCT ON 是必需的: 同一条 INSERT 里若有两行冲突到同一个唯一键,
    # ON CONFLICT DO UPDATE 会报 "cannot affect row a second time"。
    op.execute(f"""
        INSERT INTO keywords (keyword, language, role, category, platforms,
                              enabled, weight, submitted_by, use_count, last_used_at, created_at)
        SELECT DISTINCT ON (lower(keyword), language)
               keyword, language, 'search', {_CATEGORY_MAP_SQL}, ARRAY['tiktok']::text[],
               enabled, weight, submitted_by, use_count, last_used_at, created_at
        FROM tiktok_search_keywords
        ORDER BY lower(keyword), language, weight DESC, id ASC
        {_ON_CONFLICT}
    """)
    op.execute(f"""
        INSERT INTO keywords (keyword, language, role, category, platforms,
                              enabled, weight, submitted_by, use_count, last_used_at, created_at)
        SELECT DISTINCT ON (lower(keyword), language)
               keyword, language, 'search', {_CATEGORY_MAP_SQL}, ARRAY['threads']::text[],
               enabled, weight, submitted_by, use_count, last_used_at, created_at
        FROM threads_search_keywords
        ORDER BY lower(keyword), language, weight DESC, id ASC
        {_ON_CONFLICT}
    """)
    # target 词 role 不同, 与上面的 search 词不会冲突
    op.execute("""
        INSERT INTO keywords (keyword, language, role, category, intent, platforms,
                              enabled, weight, use_count, created_at)
        SELECT DISTINCT ON (lower(keyword), language)
               keyword, language, 'target', 'generic', intent, ARRAY['tiktok']::text[],
               enabled, 10, 0, created_at
        FROM tiktok_target_keywords
        ORDER BY lower(keyword), language, id ASC
        ON CONFLICT (lower(keyword), language, role) DO UPDATE SET
            platforms = (SELECT array_agg(DISTINCT p ORDER BY p)
                         FROM unnest(keywords.platforms || excluded.platforms) AS p),
            enabled   = keywords.enabled OR excluded.enabled
    """)

    # ── 老表改名保留 (确认新表跑顺后再 DROP) ──
    op.execute("ALTER TABLE tiktok_search_keywords  RENAME TO tiktok_search_keywords_legacy_20260714")
    op.execute("ALTER TABLE threads_search_keywords RENAME TO threads_search_keywords_legacy_20260714")
    op.execute("ALTER TABLE tiktok_target_keywords  RENAME TO tiktok_target_keywords_legacy_20260714")


def downgrade() -> None:
    op.execute("ALTER TABLE tiktok_search_keywords_legacy_20260714  RENAME TO tiktok_search_keywords")
    op.execute("ALTER TABLE threads_search_keywords_legacy_20260714 RENAME TO threads_search_keywords")
    op.execute("ALTER TABLE tiktok_target_keywords_legacy_20260714  RENAME TO tiktok_target_keywords")
    op.drop_table("keywords")
    op.drop_column("devices", "languages")
