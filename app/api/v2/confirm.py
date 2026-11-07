"""确认评论结果端点: POST /api/v2/confirm_attempt"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Literal

import redis.asyncio as redis
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db, get_device_id, get_redis
from app.models.comment_attempt import CommentAttempt
from app.models.raw_post import RawPost
from app.services.post_analyzer import remember_own_reply
from app.utils.logger import get_logger

router = APIRouter()
log = get_logger(__name__)


class ConfirmAttemptRequest(BaseModel):
    attempt_id: int
    success: bool
    failure_stage: str | None = None       # APP 报告的失败阶段
    failure_reason: str | None = None
    elapsed_ms: int | None = None
    screenshot_url: str | None = None


class ConfirmAttemptResponse(BaseModel):
    ok: bool = True
    attempt_id: int
    final_status: str  # "success" | "failed" | "not_found"


@router.post("/confirm_attempt", response_model=ConfirmAttemptResponse)
async def confirm_attempt(
    body: ConfirmAttemptRequest,
    device_id: str = Depends(get_device_id),
    db: AsyncSession = Depends(get_db),
    rds: redis.Redis = Depends(get_redis),
):
    """
    APP 在评论完成 (无论成功/失败) 后调用.
    把 analyze_post 预写的 pending attempt 更新为最终状态.
    """
    attempt = await db.get(CommentAttempt, body.attempt_id)
    if not attempt:
        log.warn("confirm_attempt_not_found", attempt_id=body.attempt_id, device_id=device_id)
        return ConfirmAttemptResponse(ok=False, attempt_id=body.attempt_id, final_status="not_found")

    # 防串改: device_id 必须匹配
    if attempt.device_id != device_id:
        log.warn("confirm_attempt_device_mismatch",
                 attempt_id=body.attempt_id,
                 stored_device=attempt.device_id, current_device=device_id)
        raise HTTPException(403, "device mismatch")

    # 防重复 confirm
    if attempt.failure_stage != "pending":
        log.info("confirm_attempt_already_done",
                 attempt_id=body.attempt_id, current_stage=attempt.failure_stage)
        return ConfirmAttemptResponse(
            ok=True,
            attempt_id=attempt.id,
            final_status="success" if attempt.success else "failed",
        )

    # 更新 attempt
    attempt.success = body.success
    attempt.failure_stage = None if body.success else (body.failure_stage or "unknown")
    attempt.failure_reason = body.failure_reason
    attempt.elapsed_ms = body.elapsed_ms
    attempt.screenshot_url = body.screenshot_url

    # 成功 → 更新 raw_posts
    if body.success and attempt.raw_post_id:
        await db.execute(
            update(RawPost)
            .where(RawPost.id == attempt.raw_post_id)
            .values(
                action_status="replied",
                actual_reply_text=attempt.reply_text,
                replied_at=datetime.now(timezone.utc),
            )
        )

    # 成功 → 记下自发内容指纹 (2026-09-16)。
    # Threads 把评论当帖子抓, 我们发出去的评论会被自己重新抓回来分析;
    # 实测 35 小时里 50 条逐字回收、8 条真回复了自己。这里记一笔,
    # analyze 入口据此拦掉。记不上不该影响确认流程本身。
    if body.success and attempt.reply_text:
        await remember_own_reply(rds, attempt.reply_text)

    # 失败 → 释放 Redis 锁 (允许别的机重试或永久放弃)
    if not body.success and attempt.raw_post_id:
        try:
            await rds.delete(f"post_intent_locked:{attempt.raw_post_id}")
            log.info("confirm_attempt_released_lock", post_id=attempt.raw_post_id)
        except Exception as e:
            log.warn("confirm_attempt_lock_release_failed", err=str(e))

    await db.commit()
    log.info("confirm_attempt_done",
             attempt_id=attempt.id, success=body.success,
             failure_stage=attempt.failure_stage)

    return ConfirmAttemptResponse(
        ok=True,
        attempt_id=attempt.id,
        final_status="success" if body.success else "failed",
    )
