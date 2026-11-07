"""FastAPI 依赖注入"""
from __future__ import annotations

from collections.abc import AsyncGenerator

import redis.asyncio as redis
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.database import get_db

# Redis 单例
_redis_client: redis.Redis | None = None


async def get_redis() -> redis.Redis:
    """Redis 客户端依赖"""
    global _redis_client
    if _redis_client is None:
        settings = get_settings()
        _redis_client = redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
    return _redis_client


async def close_redis() -> None:
    """应用关闭时调用"""
    global _redis_client
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None


async def get_device_id(
    x_device_id: str | None = Header(default=None, alias="X-Device-Id"),
    settings: Settings = Depends(get_settings),
) -> str:
    """从请求头取 device_id（手机端必带）"""
    if not x_device_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Device-Id header required",
        )
    return x_device_id


async def verify_device_token(
    x_device_id: str = Depends(get_device_id),
    x_device_token: str | None = Header(default=None, alias="X-Device-Token"),
    settings: Settings = Depends(get_settings),
) -> str:
    """简易 token 校验（可选，DEVICE_TOKEN_REQUIRED=false 时跳过）"""
    if not settings.device_token_required:
        return x_device_id

    if not x_device_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-Device-Token required",
        )

    # TODO: 真实校验逻辑（查 devices 表，比对 token hash）
    # 此处占位通过
    return x_device_id


# 把常用依赖打包出去
__all__ = [
    "get_db",
    "get_redis",
    "close_redis",
    "get_device_id",
    "verify_device_token",
]
