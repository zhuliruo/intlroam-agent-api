"""device_group_blacklist 表 — 工作机被某群组拉黑后的黑名单 (device_id + group_id)"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class DeviceGroupBlacklist(Base):
    __tablename__ = "device_group_blacklist"
    __table_args__ = (
        UniqueConstraint("device_id", "group_id", name="uq_device_group_blacklist"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    group_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String(64), default="access_blocked", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
