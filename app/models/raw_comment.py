"""Raw Comment Model - Hotfix 3.14
二级评论（帖子下面的用户评论）的分析 + 回复存储
"""
from __future__ import annotations

from datetime import datetime
from sqlalchemy import (
    BigInteger, Column, DateTime, Float, ForeignKey,
    Index, String, Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RawComment(Base):
    __tablename__ = "raw_comments"
    __table_args__ = (
        Index("idx_raw_comments_post_id", "raw_post_id"),
        Index("idx_raw_comments_hash", "comment_hash"),
        Index("idx_raw_comments_intent", "intent", "action_status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    raw_post_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("raw_posts.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[str] = mapped_column(String(50), nullable=False)
    platform: Mapped[str] = mapped_column(String(20), nullable=False, default="facebook")
    
    # 评论内容
    comment_author: Mapped[str | None] = mapped_column(String(200))
    comment_author_user_id: Mapped[str | None] = mapped_column(String(64))
    comment_content: Mapped[str] = mapped_column(Text, nullable=False)
    comment_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    
    # LLM 分析
    intent: Mapped[str | None] = mapped_column(String(20))
    intent_confidence: Mapped[float | None] = mapped_column(Float)
    suggested_reply: Mapped[str | None] = mapped_column(Text)
    llm_reasoning: Mapped[str | None] = mapped_column(Text)
    llm_provider: Mapped[str | None] = mapped_column(String(20))
    llm_model: Mapped[str | None] = mapped_column(String(50))
    llm_latency_ms: Mapped[int | None] = mapped_column(BigInteger)
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    
    # 回复状态
    # action_status: pending / replied / skipped / failed
    action_status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actual_reply_text: Mapped[str | None] = mapped_column(Text)
    
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    def __repr__(self) -> str:
        return f"<RawComment {self.id} post={self.raw_post_id} intent={self.intent}>"
