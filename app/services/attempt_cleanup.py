"""
attempt_cleanup — 后台任务

每小时跑一次, 清理超时 pending attempt:
  - 找 failure_stage='pending' 且 attempt_at > 1 小时前的记录
  - 标记为 failure_stage='lost_confirmation', success=False
  - 释放对应 raw_post 的 Redis 锁

防止 APP 拿到 reply 后崩溃/关开关/断网导致的"永久 pending"脏数据.
"""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone, timedelta

import redis.asyncio as redis
from sqlalchemy import select

from app.database import db_context
from app.models.comment_attempt import CommentAttempt
from app.utils.logger import get_logger

log = get_logger(__name__)

INTERVAL_SECONDS = 3600   # 每小时
PENDING_TIMEOUT_HOURS = 1   # pending 超过 1 小时 → 视为丢失


async def cleanup_pending_attempts(rds: redis.Redis):
    """
    无限循环, 每小时扫一次.
    在 main.py lifespan 里以 asyncio.create_task() 启动.
    """
    log.info("attempt_cleanup_started", interval=INTERVAL_SECONDS)
    while True:
        try:
            await asyncio.sleep(INTERVAL_SECONDS)

            async with db_context() as db:
                cutoff = datetime.now(timezone.utc) - timedelta(hours=PENDING_TIMEOUT_HOURS)
                result = await db.execute(
                    select(CommentAttempt)
                    .where(CommentAttempt.failure_stage == "pending")
                    .where(CommentAttempt.attempt_at < cutoff)
                    .limit(500)   # 防止单次清理过多
                )
                pending_list = result.scalars().all()

                if not pending_list:
                    log.debug("attempt_cleanup_no_pending")
                    continue

                released = 0
                for att in pending_list:
                    att.failure_stage = "lost_confirmation"
                    att.success = False

                    if att.raw_post_id:
                        try:
                            n = await rds.delete(f"post_intent_locked:{att.raw_post_id}")
                            if n > 0:
                                released += 1
                        except Exception as e:
                            log.warn("cleanup_lock_release_failed",
                                     post_id=att.raw_post_id, err=str(e))

                # db_context 自动 commit
                log.info("attempt_cleanup_done",
                         marked=len(pending_list), locks_released=released)

        except asyncio.CancelledError:
            log.info("attempt_cleanup_cancelled")
            raise
        except Exception as e:
            log.error("attempt_cleanup_failed", err=str(e))
            # 失败不退出, 等下一轮
