"""comment_attempts 表 — 每次评论的详细记录（成功/失败 + 失败阶段）

这是修复 Phase 1 "评论没有实际发出"问题的核心表。
每次手机点了发送都要记录，把失败定位到具体阶段：
  find_input / type_text / find_send / click_send / verify / shadow_ban
便于事后分析 selector 失效、UI 变化、风控拦截等不同原因。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CommentAttempt(Base):
    __tablename__ = "comment_attempts"
    __table_args__ = (
        Index("idx_attempts_device_time", "device_id", "attempt_at"),
        Index("idx_attempts_failures", "success", "failure_stage"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # 关联
    raw_post_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("raw_posts.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    device_id: Mapped[str] = mapped_column(String(50), nullable=False)
    
    # Hotfix 3.14: 二级评论回复时指向父评论
    parent_comment_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("raw_comments.id", ondelete="SET NULL")
    )
    platform: Mapped[str] = mapped_column(String(20), default="facebook", nullable=False)

    # 时间和耗时
    attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # 结果
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)

    # 失败阶段（仅 success=False 时填）
    # find_input  — 找不到评论输入框
    # type_text   — setText 后内容没真正进去
    # find_send   — 找不到发送按钮
    # click_send  — 点了但按钮 disabled
    # verify      — 点完发送但评论没出现在评论区（最可疑：可能是 shadow ban）
    # shadow_ban  — 检测到风控（消息发出但只对自己可见）
    # other       — 其他
    failure_stage: Mapped[str | None] = mapped_column(String(30), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 失败时回传截图（base64 或 URL）
    screenshot_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # 评论文本（保留以便事后核对）
    reply_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        flag = "✓" if self.success else f"✗({self.failure_stage})"
        return f"<CommentAttempt {self.id} {flag}>"
