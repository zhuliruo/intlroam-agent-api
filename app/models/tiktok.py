"""
app/models/tiktok.py
TikTok 相关 ORM 模型

关键词已并入 app/models/keyword.py 的统一 Keyword 表 (alembic 008)。
"""
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, ForeignKey, Text, ARRAY
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from app.database import Base   # 沿用项目里已有的 Base



class TikTokSession(Base):
    __tablename__ = "tiktok_sessions"

    id                 = Column(Integer, primary_key=True)
    device_id          = Column(String(80), nullable=False)
    started_at         = Column(DateTime(timezone=True), nullable=False)
    ended_at           = Column(DateTime(timezone=True))
    workflow_breakdown = Column(JSONB)
    keywords_used      = Column(ARRAY(Text))
    videos_visited     = Column(Integer, nullable=False, default=0)
    comments_scanned   = Column(Integer, nullable=False, default=0)
    comments_posted    = Column(Integer, nullable=False, default=0)
    replies_posted     = Column(Integer, nullable=False, default=0)
    errors             = Column(Integer, nullable=False, default=0)
    exit_reason        = Column(String(40))
    created_at         = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
