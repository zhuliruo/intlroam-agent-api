"""
app/models/keyword.py
统一关键词表 — 取代 tiktok_search_keywords / tiktok_target_keywords / threads_search_keywords

三张老表 80% 字段重合, 且各平台实际用的词高度雷同, 拆表只带来重复录入。
这里用两个维度收敛:
  role      : search (拿去搜) | target (拿去扫评论区判断意图)
  platforms : 一条词可同时供 tiktok / threads / facebook 使用

唯一键是 (lower(keyword), language, role) —— 见 alembic 008 的表达式唯一索引。
"""
from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.sql import func

from app.database import Base

# 平台 / 角色 / 统一 category 词表 —— service 与 admin CRUD 共用, 别各写一份
PLATFORMS = ("tiktok", "threads", "facebook")
ROLES = ("search", "target")
INTENTS = ("hot", "warm")

# 统一后的 category (老表 11 种 tiktok + 5 种 threads 归并而来, 映射见 alembic 008)
CATEGORIES = (
    "destination",    # 国家/城市/目的地 SIM
    "connectivity",   # eSIM / 漫游 / 流量 这类通用连接词
    "activity",       # 旅行场景与活动
    "pain_points",    # 痛点 / 抱怨
    "reseller",       # 2026-09-15 转型: 找代理/分销商的搜索词
    "hashtag",        # # 标签
    "generic",        # 兜底
)

DEFAULT_PLATFORMS = ["tiktok", "threads"]


class Keyword(Base):
    __tablename__ = "keywords"

    id           = Column(Integer, primary_key=True)
    keyword      = Column(String(120), nullable=False)
    language     = Column(String(10), nullable=False, default="en")
    role         = Column(String(10), nullable=False, default="search")
    category     = Column(String(40), nullable=False, default="generic")
    intent       = Column(String(20))          # 仅 role=target 有意义 (hot/warm)
    platforms    = Column(ARRAY(Text), nullable=False, default=lambda: list(DEFAULT_PLATFORMS))
    enabled      = Column(Boolean, nullable=False, default=True)
    weight       = Column(Integer, nullable=False, default=10)
    submitted_by = Column(String(80))          # device_id, NULL = seed
    use_count    = Column(Integer, nullable=False, default=0)
    last_used_at = Column(DateTime(timezone=True))
    created_at   = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self) -> str:
        return f"<Keyword {self.role}/{self.language}/{self.keyword}>"
