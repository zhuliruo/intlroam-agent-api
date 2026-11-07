"""groups 表 — FB 群组主数据 (Hotfix 3.4 加群组健康度字段)"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Integer, String, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Group(Base):
    __tablename__ = "groups"
    __table_args__ = (
        UniqueConstraint("platform", "external_id", name="uq_groups_platform_external_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(String(20), default="facebook", nullable=False)
    external_id: Mapped[str] = mapped_column(String(100), nullable=False)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    member_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    language_hint: Mapped[str | None] = mapped_column(String(10), default="vi", nullable=True)
    # 2026-10-31 (alembic 010): 标准英文国名, 多国群 (Europe/Caribbean/others) 为 NULL。
    # 用于优先群打标 + 按国家主推产品 (services/country_promo.py)。
    country: Mapped[str | None] = mapped_column(String(60), nullable=True, index=True)

    # 采集状态
    last_crawl_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Hotfix 3.16 H1: 软标记群组旧帖太多 (连续 5 篇 >7 天)
    last_stale_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    crawl_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # 真实性校验
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verify_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # ━━━ Hotfix 3.4 加群组健康度（生命周期）字段 ━━━

    # 工作机加入该群组的时间（FB 限制：加群 < 3 天通常被禁止评论）
    # 用于计算"加群成熟度"——加群 < 3 天的拒绝率不计入统计
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 自动 quarantine 时间（NULL = 正常，非 NULL = 被自动隔离）
    # quarantined_at 30 天后自动重置（FB 限制可能解除）
    quarantined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    # quarantine 原因（fb_rejection_high / hotwarm_low / 其他）
    quarantine_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # 上次评分时间
    last_health_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 健康度评分 0-100（运营查看用）
    health_score: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Hotfix 3.9: 加群状态跟踪
    join_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="unknown",
        server_default="unknown", index=True,
    )
    join_attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    last_join_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    # 淘汰开关
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)

    # 元数据
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
        onupdate=func.now(), nullable=False,
    )

    def __repr__(self) -> str:
        return f"<Group {self.platform}/{self.external_id} ({self.name})>"
