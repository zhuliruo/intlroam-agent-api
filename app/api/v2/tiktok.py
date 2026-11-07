"""
app/api/v2/tiktok.py
TikTok 专属 API 端点 (方案 B: 手机端自主, 服务端只下发配置 + 接收回报)

端点:
  GET  /api/v2/tiktok_config       - 单次 session 配置 (含抽样关键词)
  POST /api/v2/tiktok_keywords     - [DEPRECATED] 老 APK 的关键词提交入口, 见 keywords.py
  POST /api/v2/tiktok_session      - 上报 session 结束指标
"""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db
from app.models.tiktok import TikTokSession
from app.services.keywords import (
    TIKTOK_SAMPLE_SIZE,
    KeywordSubmitError,
    get_target_keywords,
    map_legacy_category,
    pick_search_keywords,
    submit_keyword,
)

router = APIRouter()

DEFAULT_SAMPLE_SIZE = TIKTOK_SAMPLE_SIZE


# ════════════════════════════════════════════════════════════════
# 1. /tiktok_config — session 启动时拉一次
# ════════════════════════════════════════════════════════════════
@router.get("/tiktok_config")
async def get_tiktok_config(
    device_id: str = Query(..., min_length=4, max_length=80),
    sample_size: int = Query(DEFAULT_SAMPLE_SIZE, ge=8, le=40),
    db: AsyncSession = Depends(get_db),
):
    """
    返回单次 session 所需的全部配置:
    - search_keywords: 本次随机抽的搜索词
    - target_keywords: 评论区扫描时用于识别目标提问的关键词
    - session 参数 (时长 / 概率 / cooldown 等)

    语言由 devices.languages 决定, 没配则用平台默认 (vi)。
    """
    search = await pick_search_keywords(db, device_id, "tiktok", sample_size=sample_size)
    targets = await get_target_keywords(db, "tiktok", device_id=device_id)

    return {
        "version": "tiktok_config_v1",
        "device_id": device_id,
        "session_minutes": 90,
        "post_interval_hours": 12,                   # W1 用
        "warmup_video_count": 4,
        "watch_seconds_min": 3,
        "watch_seconds_max": 12,
        # 2026-07-14 降频: 30-60s -> 90-180s。均值从 45s 拉到 135s。
        # 2026-07-26 再降: 90-180s -> 150-300s。实测 07-26 晚 28 分钟内出了 10 条
        # (≈21 条/小时), 对新号太密; 均值 135s -> 225s, 一个 90 分钟 session 的
        # 理论上限从 ~40 条降到 ~24 条, 叠加下面的概率后大致 8-12 条。
        "comment_cooldown_ms_min": 110000,
        "comment_cooldown_ms_max": 220000,
        # 一个关键词下少扫一个视频, 让搜索面更宽、单个话题下的评论更分散
        "max_videos_per_keyword": 4,
        # 评论区扫不到目标时的兜底"直接评论"概率。0.7 太高 —— 意味着大部分评论
        # 都是没有对象的硬广。2026-07-14 降到 0.4, 07-26 再降到 0.25:
        # 没有明确提问对象的评论最像广告, 优先把额度留给评论区真有意图的人。
        "direct_comment_probability": 0.35,
        "search_keywords": search,                   # [{id, keyword, language, category}]
        "target_keywords": targets,                  # [{keyword, intent}]
        "reply_prompt_version": "tiktok_v1",
        "workflow_time_budget_pct": {                # Step 2/3/4 时间比例
            "w2_search":    0.35,
            "w3_following": 0.20,
            "w4_for_you":   0.45,
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ════════════════════════════════════════════════════════════════
# 2. /tiktok_keywords — [DEPRECATED] 老 APK 提交入口
#    新 APK 走 POST /api/v2/keywords (可指定 language / role / platforms)。
#    老 APK 不发 language, 这里沿用它一贯的 vi 默认值, 免得旧机器提交的词被打错语言标签。
# ════════════════════════════════════════════════════════════════
class SubmitKeywordRequest(BaseModel):
    keyword: str = Field(..., min_length=3, max_length=80)
    category: str = Field(default="misc", max_length=40)
    device_id: str = Field(..., min_length=4, max_length=80)


@router.post("/tiktok_keywords", deprecated=True)
async def submit_tiktok_keyword(
    body: SubmitKeywordRequest,
    db: AsyncSession = Depends(get_db),
):
    try:
        return await submit_keyword(
            db,
            keyword=body.keyword,
            submitted_by=body.device_id,
            platforms=["tiktok"],
            role="search",
            category=map_legacy_category(body.category),
            language="vi",
        )
    except KeywordSubmitError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ════════════════════════════════════════════════════════════════
# 3. /tiktok_session — session 结束上报指标
# ════════════════════════════════════════════════════════════════
class SessionReportRequest(BaseModel):
    device_id: str
    started_at: datetime
    ended_at: Optional[datetime] = None
    workflow_breakdown: Optional[dict] = None
    keywords_used: list[str] = Field(default_factory=list)
    videos_visited: int = 0
    comments_scanned: int = 0
    comments_posted: int = 0
    replies_posted: int = 0
    errors: int = 0
    exit_reason: str = "normal"


@router.post("/tiktok_session")
async def report_tiktok_session(
    body: SessionReportRequest,
    db: AsyncSession = Depends(get_db),
):
    new_row = TikTokSession(
        device_id=body.device_id,
        started_at=body.started_at,
        ended_at=body.ended_at or datetime.now(timezone.utc),
        workflow_breakdown=body.workflow_breakdown,
        keywords_used=body.keywords_used,
        videos_visited=body.videos_visited,
        comments_scanned=body.comments_scanned,
        comments_posted=body.comments_posted,
        replies_posted=body.replies_posted,
        errors=body.errors,
        exit_reason=body.exit_reason,
    )
    db.add(new_row)
    await db.commit()
    await db.refresh(new_row)
    return {"ok": True, "session_id": new_row.id}
