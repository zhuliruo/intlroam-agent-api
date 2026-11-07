"""devices 表 — 工作机配置"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # 设备配置（覆盖默认值）
    # 结构：{"work_hours": {"facebook": [[8,12],[14,22]]}, ...}
    persona_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False,
    )

    # 每日上限（覆盖全局设置）
    # 结构：{"facebook": 15, "instagram": 15}
    daily_limits: Mapped[dict[str, int]] = mapped_column(
        JSONB, default=dict, nullable=False,
    )

    # 启用平台
    # 结构：["facebook"]
    platforms: Mapped[list[str]] = mapped_column(
        JSONB, default=lambda: ["facebook"], nullable=False,
    )

    # 抽关键词时用哪些语言; [] = 用平台默认 (tiktok=vi, threads=库里全部语言)
    languages: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False,
    )

    # 状态
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 设备上报的 APK 版本 (2026-09-26)。心跳从 User-Agent 解析后写入。
    # 以前只存 Redis (dev_app_ver:*, 7 天 TTL) —— 掉线超 7 天的机器会从版本
    # 列表里直接消失, 而那恰恰是最该被发现的一台。
    # app_version_code 才是判新旧的依据: versionName 按字符串比大小是错的
    # ("1.9" > "1.10")。老客户端 UA 里没带 code, 这列留 NULL。
    app_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    app_version_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    app_version_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # 简易认证（如果 DEVICE_TOKEN_REQUIRED=true 才用）
    token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
        onupdate=func.now(), nullable=False,
    )

    def __repr__(self) -> str:
        return f"<Device {self.device_id}>"
