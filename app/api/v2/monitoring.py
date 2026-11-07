"""监控端点：/health 和 /api/v2/heartbeat"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as redis
from fastapi import APIRouter, Depends, status, Request
from pydantic import BaseModel
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import Device
from app.deps import get_db, get_device_id, get_redis
from app.services.alerting import send_alert
from app.utils.logger import get_logger

router = APIRouter()
log = get_logger(__name__)


_UA_RE = re.compile(r"^IntlRoamAgent/(\S+)(?:\s+\((\d+)\))?")


def _parse_agent_ua(ua: str) -> tuple[str, int | None]:
    """从 User-Agent 里取 (versionName, versionCode)。

    1.61 之前的客户端只发 "IntlRoamAgent/1.61" —— 没有括号那段, code 返回 None。
    1.62 起是 "IntlRoamAgent/1.62 (63)"。两种都要认, 车队不可能同时升完。
    """
    m = _UA_RE.match(ua or "")
    if not m:
        return "", None
    name = m.group(1)[:32]
    code = int(m.group(2)) if m.group(2) else None
    return name, code


class HealthResponse(BaseModel):
    status: str
    service: str = "intlroam-api"
    components: dict[str, str]


class HeartbeatRequest(BaseModel):
    battery: int | None = None      # 0-100
    status: str = "idle"            # idle / busy / error / sleeping
    today_stats: dict[str, Any] | None = None


class HeartbeatResponse(BaseModel):
    ok: bool = True
    server_time: datetime


@router.get("/health", response_model=HealthResponse)
async def health(
    db: AsyncSession = Depends(get_db),
    rds: redis.Redis = Depends(get_redis),
):
    """健康检查 — 验证 DB 和 Redis 都通"""
    components = {}

    try:
        await db.execute(text("SELECT 1"))
        components["postgres"] = "ok"
    except Exception as e:
        components["postgres"] = f"fail: {type(e).__name__}"

    try:
        await rds.ping()
        components["redis"] = "ok"
    except Exception as e:
        components["redis"] = f"fail: {type(e).__name__}"

    overall = "ok" if all(v == "ok" for v in components.values()) else "degraded"
    return HealthResponse(status=overall, components=components)


@router.post(
    "/api/v2/heartbeat",
    response_model=HeartbeatResponse,
    status_code=status.HTTP_200_OK,
)
async def heartbeat(
    request: Request,
    body: HeartbeatRequest,
    device_id: str = Depends(get_device_id),
    rds: redis.Redis = Depends(get_redis),
    db: AsyncSession = Depends(get_db),
):
    """手机心跳，每 5 分钟一次。写 Redis（监控面板）+ devices.last_seen_at（离线判断）"""
    # 2026-09-15: 记录设备上报的 APK 版本。
    # 客户端在 User-Agent 里带 "IntlRoamAgent/<version>" —— 但那个版本号一直是
    # AgentApp 里**写死的 "2.0.0"**, 每台机都一样, 于是"哪台机装了新包"这个问题
    # 在服务端根本无法回答 (排查 5a5cf 的 TikTok 发送失败时就卡在这里)。
    # 客户端已改成读 BuildConfig 的真实版本, 这里把它落下来。
    # 存 Redis 不建表: 这是运维可见性不是业务数据, 7 天 TTL 够用。
    #
    # 2026-09-26: 除了 Redis 也落 devices 表 (见下面那次 update)。Redis 这份保留 ——
    # 监控面板读它, 且 7 天 TTL 天然等于"最近活着的机器", 两个口径各有用处。
    # 同时开始解析 versionCode: OTA 判新旧用的是 code, versionName 按字符串
    # 比大小是错的 ("1.9" > "1.10")。
    _ua = request.headers.get("user-agent", "")
    _ver, _ver_code = _parse_agent_ua(_ua)
    if _ver:
        try:
            await rds.set(f"dev_app_ver:{device_id}", _ver, ex=7 * 86400)
        except Exception:
            pass
    payload = {
        "battery": body.battery,
        "status": body.status,
        "last_seen": datetime.utcnow().isoformat(),
        "today_stats": body.today_stats or {},
    }
    # Redis hash + 5 分钟过期
    await rds.hset(f"hb:{device_id}", mapping={
        k: str(v) if not isinstance(v, str) else v
        for k, v in payload.items()
    })
    await rds.expire(f"hb:{device_id}", 600)

    # devices.last_seen_at 以前只有 /device_config 会更新，导致一台机在跑
    # 但几小时不拉配置时看起来像掉线。心跳一并更新，别让判活指标骗人。
    try:
        _vals: dict[str, Any] = {"last_seen_at": datetime.now(timezone.utc)}
        if _ver:
            _vals["app_version"] = _ver
            _vals["app_version_at"] = datetime.now(timezone.utc)
            # 老客户端不带 code —— 那就别拿 None 去覆盖上一次记到的真值,
            # 否则一台机在新旧包之间来回重启就会把 code 抹掉。
            if _ver_code is not None:
                _vals["app_version_code"] = _ver_code
        await db.execute(
            update(Device)
            .where(Device.device_id == device_id)
            .values(**_vals)
        )
    except Exception as e:  # 心跳绝不能因为写库失败而挂掉
        log.warning("heartbeat_last_seen_update_failed", device_id=device_id, error=str(e))

    log.info("heartbeat", device_id=device_id, status=body.status,
             battery=body.battery, app_version=_ver or "unknown",
             app_version_code=_ver_code)

    # ── "在线但瞎了" 告警 (2026-09-21) ──────────────────────────────
    # status=no_a11y 意味着心跳正常、进程活着, 但无障碍服务是关的 —— 这台机
    # 什么都干不了, 而在任何"最后心跳"口径下它都显示为在线。9ed4 就这么空转了
    # 大半天才被人发现 (真因: 无障碍悬浮按钮被 agent 自己误触关掉, 以及 Android 13
    # 侧载应用装新包时被系统清掉)。客户端 1.60 起会自愈, 但没授 WRITE_SECURE_SETTINGS
    # 的机器修不了, 必须有人去处理 —— 所以这里必须叫人。
    #
    # 连续 3 次 (约 15 分钟) 才告警: 装新包/重启的瞬间会有一两次 no_a11y, 那是正常的。
    if body.status == "no_a11y":
        try:
            streak = await rds.incr(f"noa11y_streak:{device_id}")
            await rds.expire(f"noa11y_streak:{device_id}", 3600)
            if streak >= 3:
                await send_alert(
                    rds,
                    alert_key=f"no_a11y:{device_id}",
                    subject=f"[Agent] {device_id[:8]} 无障碍是关的, 这台机在空转",
                    body=(
                        f"设备      : {device_id}\n"
                        f"APK 版本  : {_ver or 'unknown'}\n"
                        f"连续次数  : {streak} 次心跳 (约 {streak * 5} 分钟)\n\n"
                        "含义: 心跳正常 = 进程活着、网络通, 但**无障碍服务是关的**, "
                        "所以它不会浏览、不会评论、不会发帖 —— 在监控面板上却显示在线。\n\n"
                        "常见原因:\n"
                        "  1. 无障碍悬浮按钮绑在了本服务上, agent 自动点击时误触把自己关了\n"
                        "     (9ed4 实测; 1.60 起客户端会自动解绑)\n"
                        "  2. Android 13 侧载应用装新包后系统清掉了无障碍授权\n\n"
                        "处理:\n"
                        "  - 已装 1.60 且授过 WRITE_SECURE_SETTINGS 的机器会自愈, 等一轮轮询即可\n"
                        "  - 否则接上 USB 跑一次:\n"
                        "      adb shell pm grant com.intlroam.agent android.permission.WRITE_SECURE_SETTINGS\n"
                        "      adb shell settings delete secure accessibility_button_targets\n"
                        "  - 没有 USB 的话, 手机上: 设置 → 无障碍 → IntlRoam Agent → 打开, "
                        "并把该服务的\"快捷方式/悬浮按钮\"关掉\n"
                    ),
                )
        except Exception as e:
            log.warning("no_a11y_alert_failed", device_id=device_id, error=str(e))
    else:
        try:
            await rds.delete(f"noa11y_streak:{device_id}")
        except Exception:
            pass

    return HeartbeatResponse(server_time=datetime.utcnow())
