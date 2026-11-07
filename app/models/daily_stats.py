"""daily_device_stats 表 — 每天每设备的聚合统计（Dashboard 用）"""
from __future__ import annotations

from datetime import date as date_type

from sqlalchemy import Date, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class DailyDeviceStats(Base):
    __tablename__ = "daily_device_stats"

    date: Mapped[date_type] = mapped_column(Date, primary_key=True)
    device_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    platform: Mapped[str] = mapped_column(String(20), primary_key=True, default="facebook")

    posts_scanned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    posts_analyzed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    comments_attempted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    comments_succeeded: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    likes_given: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    groups_visited: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    @property
    def comment_success_rate(self) -> float:
        if self.comments_attempted == 0:
            return 0.0
        return self.comments_succeeded / self.comments_attempted
