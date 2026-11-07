"""设备 × 群组维度的两张表。

为什么需要:
  groups.join_status / joined_at / join_attempt_count 是**群级别**的, 但一批群会被
  多台设备 (= 多个 FB 账号) 共用。A 账号加过 → 全局标 joined, B 设备再被派到同一个群时
  它自己并不是成员, 只能重新点 Join。于是日志里出现 success=false 而 new_status=joined
  这种自相矛盾, 而 DB 根本回答不了"这台手机到底加没加"。

  device_group_membership — 每台设备对每个群的真实成员状态 (当前值)
  device_group_visits     — 每次 report_result 的流水 (为什么这次什么都没干)
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Integer, String, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class DeviceGroupMembership(Base):
    """(device_id, group_id) → 这台设备的账号在这个群里的成员状态。"""

    __tablename__ = "device_group_membership"
    __table_args__ = (
        UniqueConstraint("device_id", "group_id", name="uq_device_group_membership"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    group_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    # unknown / joined / pending_approval / failed
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="unknown", server_default="unknown",
    )
    joined_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # 最近一次加群失败的原因 (客户端上报, 见 ReportGroupJoinRequest.reason)
    last_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
        onupdate=func.now(), nullable=False,
    )


class DeviceGroupVisit(Base):
    """每次 report_result 落一行 —— 回答"这台设备这次进群做了什么/为什么没做"。

    以前这些信息只体现为 groups.verify_failures += 1, 原因字符串直接丢弃,
    于是"设备在空转"和"设备卡死"在服务端长得一模一样。
    """

    __tablename__ = "device_group_visits"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    group_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    platform: Mapped[str] = mapped_column(
        String(20), nullable=False, default="facebook", server_default="facebook",
    )

    verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true",
    )
    # 客户端 sendReport 的 failureReason: screen_wake_failed / facebook_not_open /
    # group_navigation_failed / facebook_not_in_foreground / unviewable_page_after_retry ...
    failure_reason: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # 客户端 TaskResult 里的 action_taken: join_group_attempt /
    # skipped_not_joined_public_by_toggle / browse_and_like_no_comments / commented ...
    action_taken: Mapped[str | None] = mapped_column(String(60), nullable=True)

    posts_scanned: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    posts_analyzed: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    likes_given: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    comment_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    joined_group: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True,
    )
