"""设备端点：GET /api/v2/device_config"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.deps import get_db, get_device_id
from app.models.device import Device
from app.services.device_guard import daily_comment_limit
from app.utils.logger import get_logger

router = APIRouter()
log = get_logger(__name__)


class DeviceConfigResponse(BaseModel):
    """设备远程配置 — 不需要重装 APK 就能调"""
    device_id: str

    # 工作时段（24h 制，香港/越南时间）
    # 结构：{"facebook": [[8, 12], [14, 22]], ...}
    work_hours: dict[str, list[list[int]]] = Field(default_factory=dict)

    # 每日上限
    daily_limits: dict[str, int] = Field(default_factory=dict)

    # 评论间隔（秒）— [min, max] 拟人化
    comment_cooldown_seconds: list[int] = Field(default_factory=lambda: [60, 150])

    # 是否启用
    is_active: bool = True

    # 平台分配
    platforms: list[str] = Field(default_factory=lambda: ["facebook"])

    # 拟人化参数（每台设备首次注册时随机生成）
    persona: dict[str, Any] = Field(default_factory=dict)


@router.get("/device_config", response_model=DeviceConfigResponse)
async def device_config(
    device_id: str = Depends(get_device_id),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """
    设备拉取自己的配置。如果 devices 表里还没有，自动建一条用默认值。
    """
    result = await db.execute(
        select(Device).where(Device.device_id == device_id)
    )
    dev = result.scalar_one_or_none()

    if dev is None:
        # 首次见到，自动注册（默认配置）
        dev = Device(
            device_id=device_id,
            name=device_id,
            persona_config={},
            # facebook/instagram 上限只认 settings (见下方 daily_comment_limit), 不落库,
            # 否则注册当时的值会被写死、之后改 .env 时与库里的旧值互相矛盾。
            daily_limits={},
            platforms=["facebook"],
            is_active=True,
            last_seen_at=datetime.now(timezone.utc),
        )
        db.add(dev)
        await db.flush()
        log.info("device_auto_registered", device_id=device_id)
    else:
        # 更新最后见到时间
        dev.last_seen_at = datetime.now(timezone.utc)

    # 2026-09-14: facebook / instagram 两个限额只认 settings, 不再让 DB 覆盖。
    #
    # 原来是 daily_limits.update(dev.daily_limits) —— devices 表里留着 08-09 之前
    # 写死的 {"facebook": 15}, 于是 /device_config 告诉手机 15, 而 /next_groups 那边
    # 用的是 settings (当时 25)。**两个端点对同一台手机下发两个不同的上限**, 谁也说不清
    # 哪个在起作用; 实测单机单日跑到 87 次, 两个都没拦住。
    # 现在服务端会真的按 settings 拦截 (见 services/device_guard.daily_comment_limit),
    # 配置就必须只有一个来源。DB 里其它自定义键仍然保留。
    daily_limits = {k: v for k, v in (dev.daily_limits or {}).items()
                    if k not in ("facebook", "instagram")}
    daily_limits["facebook"] = daily_comment_limit(settings, "facebook")
    daily_limits["instagram"] = daily_comment_limit(settings, "instagram")

    work_hours = (dev.persona_config or {}).get("work_hours") or {
        "facebook": [[8, 12], [14, 22]],
    }

    cooldown = (dev.persona_config or {}).get("comment_cooldown") or [60, 150]

    return DeviceConfigResponse(
        device_id=device_id,
        work_hours=work_hours,
        daily_limits=daily_limits,
        comment_cooldown_seconds=cooldown,
        is_active=dev.is_active,
        platforms=dev.platforms or ["facebook"],
        persona=dev.persona_config or {},
    )
