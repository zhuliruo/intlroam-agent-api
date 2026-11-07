"""
stranded_replies — 后台任务: 回收"文案已生成但没发出去"的帖子

背景 (2026-09-14 两周数据审计):
  raw_posts 里有 398 条 action_status='pending' 且 suggested_reply 非空 ——
  服务端已经付费调 LLM 生成了回复, 但手机那次 session 结束前没轮到它发,
  之后**再也没有任何机制**把它捡回来 (action_status 只在 report_result 里被改写)。
  其中 142 条超过 3 天, 等于白买。

这个任务每 30 分钟跑一次, 做两件事:

  1. 过期: 滞留超过 RETRY_MAX_AGE_HOURS 的标成 'expired'。
     帖子沉底之后再去评论既没效果, 又是"在陈年老帖刷评论"的反作弊红旗。
     注意 analyze_post 里配套加了 expired 短路, 否则手机再遇到它还会照发。

  2. 补发: 把还在窗口内的滞留帖所属的群塞进 Redis 集合,
     get_next_groups 看到就优先派发这些群。手机重新进群扫到同一条帖子时,
     analyze_post 的 content_hash 命中分支会**免费**返回已存的 suggested_reply
     (不再调 LLM), 于是这条回复就补上了。

  只有 Facebook 走得通 —— Threads/TikTok 没有"群"这个可以重新导航过去的锚点,
  它们的滞留帖只会被 ① 过期掉。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import redis.asyncio as redis
from sqlalchemy import func, select, update

from app.database import db_context
from app.models.raw_post import RawPost
from app.utils.logger import get_logger

log = get_logger(__name__)

INTERVAL_SECONDS = 1800          # 每 30 分钟

# 太新的不碰: 手机当次 session 可能正要发它, 现在插队会两台机撞同一条帖子。
RETRY_MIN_AGE_MINUTES = 45
# 超过这个岁数就不值得补了 (帖子沉底 + 老帖刷评论是反作弊红旗)
RETRY_MAX_AGE_HOURS = 24

# 单轮最多标记多少条, 防止历史积压一次性打爆一个事务
EXPIRE_BATCH_LIMIT = 2000

RETRY_GROUPS_KEY = "retry_groups:{platform}"
# TTL 比 INTERVAL 长一点, 保证两轮之间集合不会空窗
RETRY_GROUPS_TTL = INTERVAL_SECONDS * 3


def _has_reply():
    """滞留判定: pending + 有非空回复文案。"""
    return (
        (RawPost.action_status == "pending")
        & RawPost.suggested_reply.is_not(None)
        & (RawPost.suggested_reply != "")
    )


async def _expire_old(db, now: datetime) -> int:
    cutoff = now - timedelta(hours=RETRY_MAX_AGE_HOURS)
    ids = (await db.execute(
        select(RawPost.id)
        .where(_has_reply(), RawPost.captured_at < cutoff)
        .limit(EXPIRE_BATCH_LIMIT)
    )).scalars().all()
    if not ids:
        return 0
    await db.execute(
        update(RawPost).where(RawPost.id.in_(ids)).values(action_status="expired")
    )
    return len(ids)


async def _collect_retry_groups(db, now: datetime) -> dict[str, list[int]]:
    """还在窗口内的滞留帖 → {platform: [group_id, ...]}"""
    rows = (await db.execute(
        select(RawPost.platform, RawPost.group_id, func.count().label("n"))
        .where(
            _has_reply(),
            RawPost.group_id.is_not(None),
            RawPost.captured_at < now - timedelta(minutes=RETRY_MIN_AGE_MINUTES),
            RawPost.captured_at >= now - timedelta(hours=RETRY_MAX_AGE_HOURS),
        )
        .group_by(RawPost.platform, RawPost.group_id)
        .order_by(func.count().desc())
    )).all()

    by_platform: dict[str, list[int]] = {}
    for platform, group_id, _n in rows:
        by_platform.setdefault(platform or "facebook", []).append(group_id)
    return by_platform


async def sweep_stranded_replies(rds: redis.Redis):
    """无限循环。在 main.py lifespan 里以 asyncio.create_task() 启动。"""
    log.info("stranded_sweep_started",
             interval=INTERVAL_SECONDS,
             min_age_min=RETRY_MIN_AGE_MINUTES,
             max_age_h=RETRY_MAX_AGE_HOURS)
    while True:
        try:
            await asyncio.sleep(INTERVAL_SECONDS)
            now = datetime.now(timezone.utc)

            async with db_context() as db:
                expired = await _expire_old(db, now)
                by_platform = await _collect_retry_groups(db, now)

            total_groups = 0
            for platform, group_ids in by_platform.items():
                key = RETRY_GROUPS_KEY.format(platform=platform)
                try:
                    async with rds.pipeline(transaction=True) as pipe:
                        pipe.delete(key)
                        pipe.sadd(key, *group_ids)
                        pipe.expire(key, RETRY_GROUPS_TTL)
                        await pipe.execute()
                    total_groups += len(group_ids)
                except Exception as e:
                    log.warning("stranded_retry_queue_failed",
                                platform=platform, err=str(e))

            # 没有滞留的平台要把上一轮的集合清掉, 否则会一直优先派发老群
            for platform in ("facebook", "instagram", "threads", "tiktok"):
                if platform not in by_platform:
                    try:
                        await rds.delete(RETRY_GROUPS_KEY.format(platform=platform))
                    except Exception:
                        pass

            log.info("stranded_sweep_done",
                     expired=expired, retry_groups=total_groups)

        except asyncio.CancelledError:
            log.info("stranded_sweep_cancelled")
            raise
        except Exception as e:
            log.error("stranded_sweep_failed", err=str(e))
            # 失败不退出, 等下一轮


async def get_retry_group_ids(rds: redis.Redis, platform: str) -> set[int]:
    """给 get_next_groups 用: 本平台待补发的群 id。Redis 不通就返回空集。"""
    try:
        members = await rds.smembers(RETRY_GROUPS_KEY.format(platform=platform))
    except Exception as e:
        log.warning("retry_groups_read_failed", platform=platform, err=str(e))
        return set()

    out: set[int] = set()
    for m in members:
        try:
            out.add(int(m.decode() if isinstance(m, bytes) else m))
        except (ValueError, AttributeError):
            continue
    return out
