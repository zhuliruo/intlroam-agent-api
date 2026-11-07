"""
device_guard — 派发侧的两道闸门

背景 (2026-09-14 两周数据审计):

1. **空跑熔断**: 1285 次群访问里 393 次 (30.6%) 是
   `failure_reason='facebook_not_open' / action_taken='aborted'`, posts_scanned=0。
   而且不是均匀噪音, 是单机单日雪崩 —— 4824e1fb 在 09-04 一天空跑 159 次,
   服务端全程照派不误。连续几次开不了 FB 就该停派 + 告警, 而不是把当天的
   派发额度全烧在一台打不开 App 的机器上。

2. **日评论上限形同虚设**: `.env` 的 MAX_DAILY_COMMENTS_FB 只是"下发给手机的建议值",
   服务端从不拦截。实测 4824e1fb 09-12 做了 87 次 FB 评论尝试, 是当时配置 (25) 的
   3.5 倍; 同期 shadow_ban 率从 3~5% 升到 8%。限额必须在派发侧强制执行。
"""
from __future__ import annotations

from datetime import datetime, time, timezone

import redis.asyncio as redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.comment_attempt import CommentAttempt
from app.services.alerting import push_device_notice, send_alert
from app.utils.logger import get_logger

log = get_logger(__name__)


def daily_comment_limit(settings: Settings, platform: str) -> int:
    """该平台的单机单日评论上限。唯一取值口径, 端点和闸门都走这里。"""
    return (
        settings.max_daily_comments_fb if platform == "facebook"
        else settings.max_daily_comments_ig
    )

# ── 空跑熔断 ──
# 连续多少次"开不了 App"就停派。取 5: 单次失败很常见 (冷启动慢/系统杀后台),
# 连续 5 次基本只剩两种可能 —— App Links 开关被关, 或账号被登出。
NOT_OPEN_THRESHOLD = 5
# 熔断时长。比 30 分钟群锁略长, 让在途任务自然结束。
PAUSE_SECONDS = 1800
# 计数器自己的 TTL: 超过这么久没再失败就当作已恢复, 不累计到下一次。
COUNTER_TTL = 3600

# 这些 failure_reason 都属于"人没到场就修不好"的空跑
ABORT_REASONS = {
    "facebook_not_open",
    "facebook_not_in_foreground",
    "unviewable_page_after_retry",
}


def _counter_key(device_id: str, platform: str) -> str:
    return f"abort_streak:{platform}:{device_id}"


def _pause_key(device_id: str, platform: str) -> str:
    return f"dispatch_paused:{platform}:{device_id}"


async def record_visit_outcome(
    rds: redis.Redis,
    device_id: str,
    platform: str,
    verified: bool,
    failure_reason: str | None,
) -> None:
    """
    report_result 每次调用。连续空跑到阈值就熔断 + 告警;
    任何一次正常访问都把连击数清零。
    """
    reason = (failure_reason or "").strip()
    is_abort = (not verified) and reason in ABORT_REASONS

    try:
        if not is_abort:
            await rds.delete(_counter_key(device_id, platform))
            return

        key = _counter_key(device_id, platform)
        streak = await rds.incr(key)
        await rds.expire(key, COUNTER_TTL)
    except Exception as e:
        log.warning("abort_streak_track_failed", device_id=device_id, err=str(e))
        return

    log.info("abort_streak", device_id=device_id, platform=platform,
             reason=reason, streak=streak)

    if streak < NOT_OPEN_THRESHOLD:
        return

    try:
        await rds.set(_pause_key(device_id, platform), reason, ex=PAUSE_SECONDS)
    except Exception as e:
        log.warning("dispatch_pause_set_failed", device_id=device_id, err=str(e))
        return

    log.warning("dispatch_paused", device_id=device_id, platform=platform,
                reason=reason, streak=streak, seconds=PAUSE_SECONDS)

    # 手机端通知 (APK 支持后会在通知栏/弹窗显示; 现在先存着不丢)
    await push_device_notice(
        rds, device_id,
        f"连续 {streak} 次打不开 {platform}，已暂停派发 "
        f"{PAUSE_SECONDS // 60} 分钟。请检查 App Links 开关或账号是否被登出。",
    )

    await send_alert(
        rds,
        alert_key=f"abort_streak:{platform}:{device_id}",
        subject=f"[Agent] {device_id[:8]} 连续 {streak} 次打不开 {platform}，已暂停派发",
        body=(
            f"设备      : {device_id}\n"
            f"平台      : {platform}\n"
            f"失败原因  : {reason}\n"
            f"连续次数  : {streak}\n"
            f"处理      : 已暂停派发 {PAUSE_SECONDS // 60} 分钟，到期自动恢复\n\n"
            f"最常见的真因是设备端 App Links 开关被关（见 memory: "
            f"intlroam-agent-fb-applinks），其次是账号被登出。\n"
            f"排查：adb shell pm get-app-links com.facebook.katana\n"
        ),
    )


async def is_dispatch_paused(
    rds: redis.Redis, device_id: str, platform: str
) -> str | None:
    """熔断中返回原因字符串, 否则 None。Redis 不通时一律放行。"""
    try:
        v = await rds.get(_pause_key(device_id, platform))
    except Exception:
        return None
    if not v:
        return None
    return v.decode() if isinstance(v, bytes) else str(v)


async def comment_attempts_today(
    db: AsyncSession, device_id: str, platform: str
) -> int:
    """
    今天这台设备在该平台上的评论尝试数。

    用 attempts 而不是 successes: 平台看到的是"你试了多少次",
    失败的那些 (shadow_ban / 找不到输入框) 一样消耗了账号的行为额度。

    按 UTC 自然日算, 与 daily_device_stats 的口径一致。00:00 UTC = 08:00 HKT,
    正好是 work_hours 的开工点, 所以计数器是在开工时清零, 不会把一天劈成两半。
    (不用 func.date(): 那个走 DB 会话时区, 和上面这个口径对不上, 而且吃不到
     idx_attempts_device_time 索引。)
    """
    now = datetime.now(timezone.utc)
    day_start = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
    return (await db.execute(
        select(func.count(CommentAttempt.id)).where(
            CommentAttempt.device_id == device_id,
            CommentAttempt.platform == platform,
            CommentAttempt.attempt_at >= day_start,
        )
    )).scalar_one() or 0
