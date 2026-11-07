"""二级评论端点 - Hotfix 3.14

POST /api/v2/analyze_comment   - 分析评论 → LLM → 返回 reply
POST /api/v2/report_comment    - 上报单条评论尝试结果（评论帖子 or 回复评论）
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Literal

import redis.asyncio as redis
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db, get_device_id, get_redis
from app.models.comment_attempt import CommentAttempt
from app.models.raw_comment import RawComment
from app.models.raw_post import RawPost
from app.services.keyword_filter import filter_post
from app.services.llm_client import call_llm
from app.services.post_analyzer import generate_reply
from app.utils.logger import get_logger

router = APIRouter()
log = get_logger(__name__)


# ============= /analyze_comment =============

class AnalyzeCommentRequest(BaseModel):
    raw_post_id: int                         # 父帖子 ID
    comment_content: str
    comment_author: str | None = None
    comment_author_user_id: str | None = None
    post_content: str | None = None          # 给 LLM 上下文
    platform: str = "facebook"


class AnalyzeCommentResponse(BaseModel):
    comment_id: int                          # raw_comments.id
    intent: str
    confidence: float
    reasoning: str | None = None
    reply: str | None = None
    cache_hit: bool = False


def _comment_hash(content: str) -> str:
    """评论内容的指纹（用于去重）"""
    normalized = content.strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _load_classifier_prompt(language: str = "vi") -> str:
    from pathlib import Path
    name = "en_comment_classifier.txt" if language == "en" else "vi_comment_classifier.txt"
    return Path(f"/opt/intlroam_api/prompts/{name}").read_text()


@router.post("/analyze_comment", response_model=AnalyzeCommentResponse)
async def analyze_comment(
    req: AnalyzeCommentRequest,
    device_id: str = Depends(get_device_id),
    db: AsyncSession = Depends(get_db),
    rds: redis.Redis = Depends(get_redis),
):
    """分析评论 → LLM → 返回 reply（hot/warm 才生成 reply）"""
    from app.services.post_analyzer import detect_language
    # 评论没有群组上下文, 只能从内容认语言。越南语声调符号是极强的信号。
    cmt_lang = detect_language(req.comment_content or "")
    
    # 1. 关键词过滤
    if not filter_post(req.comment_content):
        log.info("h314_comment_skip_no_keyword", 
                 post_id=req.raw_post_id,
                 content_preview=req.comment_content[:50])
        # 仍然写入 raw_comments 但 intent=ignore
        chash = _comment_hash(req.comment_content)
        c = RawComment(
            raw_post_id=req.raw_post_id,
            device_id=device_id,
            platform=req.platform,
            comment_author=req.comment_author,
            comment_author_user_id=req.comment_author_user_id,
            comment_content=req.comment_content,
            comment_hash=chash,
            intent="ignore",
            intent_confidence=1.0,
            llm_reasoning="no_keyword",
            action_status="skipped",
            captured_at=datetime.now(timezone.utc),
        )
        db.add(c)
        await db.commit()
        await db.refresh(c)
        return AnalyzeCommentResponse(
            comment_id=c.id,
            intent="ignore",
            confidence=1.0,
            reasoning="no_keyword",
        )
    
    chash = _comment_hash(req.comment_content)
    
    # 2. 数据库去重 - 查同 hash
    stmt = select(RawComment).where(
        RawComment.comment_hash == chash,
        RawComment.raw_post_id == req.raw_post_id,
    ).limit(1)
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing:
        log.info("h314_comment_cache_hit_db", 
                 comment_id=existing.id,
                 post_id=req.raw_post_id)
        return AnalyzeCommentResponse(
            comment_id=existing.id,
            intent=existing.intent or "ignore",
            confidence=existing.intent_confidence or 0.0,
            reasoning=existing.llm_reasoning,
            reply=existing.suggested_reply,
            cache_hit=True,
        )
    
    # 3. 调 LLM
    system_prompt = _load_classifier_prompt(cmt_lang)
    user_message = f"Comment to classify:\n{req.comment_content}"
    if req.post_content:
        user_message = f"Parent post:\n{req.post_content[:300]}\n\nComment to classify:\n{req.comment_content}"
    
    llm_result = await call_llm(
        system_instruction=system_prompt,
        user_message=user_message,
        temperature=0.2,
        json_mode=True,
    )
    
    if not llm_result.success:
        log.warning("h314_comment_llm_failed", error=str(llm_result.error))
        raise HTTPException(500, "LLM classify failed")
    
    intent = llm_result.data.get("intent", "ignore")
    confidence = float(llm_result.data.get("confidence", 0.5))
    reasoning = llm_result.data.get("reasoning", "")
    destination = llm_result.data.get("destination_country")
    
    # 4. 写入 raw_comments
    c = RawComment(
        raw_post_id=req.raw_post_id,
        device_id=device_id,
        platform=req.platform,
        comment_author=req.comment_author,
        comment_author_user_id=req.comment_author_user_id,
        comment_content=req.comment_content,
        comment_hash=chash,
        intent=intent,
        intent_confidence=confidence,
        llm_reasoning=reasoning,
        llm_provider=llm_result.provider,
        llm_model=llm_result.model,
        llm_latency_ms=llm_result.latency_ms,
        analyzed_at=datetime.now(timezone.utc),
        action_status="pending",
        captured_at=datetime.now(timezone.utc),
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    
    log.info("h314_comment_analyzed", 
             comment_id=c.id,
             post_id=req.raw_post_id,
             intent=intent,
             confidence=confidence)
    
    # 5. 如果 hot/warm → 生成 reply
    reply = None
    if intent in ("hot", "warm") and confidence >= 0.7:
        reply_result = await generate_reply(
            content=req.comment_content,
            intent=intent,
            destination_country=destination,
            redis=rds,
            language=cmt_lang,
        )
        if reply_result:
            reply = reply_result.reply
            # 更新 raw_comments
            c.suggested_reply = reply
            await db.commit()
    
    return AnalyzeCommentResponse(
        comment_id=c.id,
        intent=intent,
        confidence=confidence,
        reasoning=reasoning,
        reply=reply,
    )


# ============= /report_comment =============

class ReportCommentRequest(BaseModel):
    """单条评论尝试结果上报（评论帖子 or 回复评论）"""
    raw_post_id: int | None = None           # 评论的帖子 (评论帖子时填)
    parent_comment_id: int | None = None     # 父评论 (回复评论时填)
    success: bool
    failure_stage: Literal[
        "find_button", "find_input", "type_text", "find_send",
        "click_send", "verify", "shadow_ban", "fb_rejected", "other", "already_commented"
    ] | None = None
    failure_reason: str | None = None
    elapsed_ms: int | None = None
    reply_text: str | None = None
    screenshot_url: str | None = None


class ReportCommentResponse(BaseModel):
    ok: bool = True
    attempt_id: int


@router.post("/report_comment", response_model=ReportCommentResponse)
async def report_comment(
    req: ReportCommentRequest,
    device_id: str = Depends(get_device_id),
    db: AsyncSession = Depends(get_db),
    rds: redis.Redis = Depends(get_redis),
):
    """单条评论尝试上报 - 立即入库 + 更新对应 raw_posts/raw_comments 状态"""
    
    if not req.raw_post_id and not req.parent_comment_id:
        raise HTTPException(400, "must provide raw_post_id or parent_comment_id")
    
    # 1. 写 comment_attempts
    attempt = CommentAttempt(
        raw_post_id=req.raw_post_id,
        parent_comment_id=req.parent_comment_id,
        device_id=device_id,
        platform="facebook",
        success=req.success,
        failure_stage=req.failure_stage,
        failure_reason=req.failure_reason,
        elapsed_ms=req.elapsed_ms,
        reply_text=req.reply_text,
        screenshot_url=req.screenshot_url,
        attempt_at=datetime.now(timezone.utc),
    )
    db.add(attempt)
    await db.flush()  # 拿 attempt.id
    
    # 2. 同步更新 raw_posts.action_status 或 raw_comments.action_status
    if req.raw_post_id and req.success:
        # 评论帖子成功
        await db.execute(
            update(RawPost)
            .where(RawPost.id == req.raw_post_id)
            .values(
                action_status="replied",
                actual_reply_text=req.reply_text,
                replied_at=datetime.now(timezone.utc),
            )
        )
        # Redis 标记 24h 不再评论同帖
        try:
            await rds.set(f"commented:{req.raw_post_id}", "1", ex=86400)
            # Hotfix 3.15.1: 删除 hourly counter (limit 已删)
        except Exception as e:
            log.warning("redis_set_commented_failed", error=str(e))
    
    elif req.raw_post_id and not req.success:
        await db.execute(
            update(RawPost)
            .where(RawPost.id == req.raw_post_id)
            .values(action_status="failed")
        )
    
    if req.parent_comment_id and req.success:
        # 回复评论成功
        await db.execute(
            update(RawComment)
            .where(RawComment.id == req.parent_comment_id)
            .values(
                action_status="replied",
                actual_reply_text=req.reply_text,
                replied_at=datetime.now(timezone.utc),
            )
        )
    
    elif req.parent_comment_id and not req.success:
        await db.execute(
            update(RawComment)
            .where(RawComment.id == req.parent_comment_id)
            .values(action_status="failed")
        )
    
    await db.commit()
    
    log.info("h314_comment_reported",
             attempt_id=attempt.id,
             raw_post_id=req.raw_post_id,
             parent_comment_id=req.parent_comment_id,
             success=req.success,
             elapsed_ms=req.elapsed_ms)
    
    return ReportCommentResponse(attempt_id=attempt.id)
