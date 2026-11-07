"""raw_posts 表 — 帖子采集原始数据 + AI 分析结果"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger, DateTime, Float, ForeignKey, Index, String, Text, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RawPost(Base):
    __tablename__ = "raw_posts"
    __table_args__ = (
        Index("idx_raw_posts_group_time", "group_id", "captured_at"),
        Index("idx_raw_posts_intent", "intent", "action_status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # 来源
    group_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("groups.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    device_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    platform: Mapped[str] = mapped_column(String(20), default="facebook", nullable=False)

    # 帖子内容
    fb_post_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    author_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    author_user_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True,
    )
    post_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True,
    )
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 唯一性 hash（content + author + group），避免重复入库
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    # ---------- AI 分析结果 ----------
    # intent: hot / warm / cold / competitor / ignore
    intent: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    intent_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    suggested_reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_provider: Mapped[str | None] = mapped_column(String(20), nullable=True)
    llm_model: Mapped[str | None] = mapped_column(String(50), nullable=True)
    llm_latency_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ---------- 处理状态 ----------
    # action_status: pending / replied / skipped / failed
    action_status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    actual_reply_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<RawPost {self.id} intent={self.intent} status={self.action_status}>"
