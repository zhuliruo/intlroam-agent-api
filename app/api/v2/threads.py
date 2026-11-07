"""
app/api/v2/threads.py
Threads 专属端点 (方案与 tiktok 一致: 服务端下发配置 + 关键词, 设备自主执行)

端点:
  GET  /api/v2/threads_config    - 单次 session 配置 (含搜索关键词 + 7天新鲜度阈值)
  POST /api/v2/threads_keywords  - [DEPRECATED] 老 APK 的关键词提交入口, 见 keywords.py

回复生成复用现有 POST /api/v2/analyze_post (platform="threads")。
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db
from app.services.keywords import (
    THREADS_SAMPLE_SIZE,
    KeywordSubmitError,
    get_target_keywords,
    map_legacy_category,
    pick_search_keywords,
    submit_keyword,
)

router = APIRouter()

DEFAULT_SAMPLE_SIZE = THREADS_SAMPLE_SIZE


@router.get("/threads_config")
async def get_threads_config(
    device_id: str = Query(..., min_length=4, max_length=80),
    sample_size: int = Query(DEFAULT_SAMPLE_SIZE, ge=4, le=30),
    db: AsyncSession = Depends(get_db),
):
    """语言由 devices.languages 决定; 没配则跨库里所有语言均衡抽样 (原有行为)。"""
    search = await pick_search_keywords(db, device_id, "threads", sample_size=sample_size)
    # 2026-07-31: 评论区扫描用的意图词以前只下发给 TikTok, Threads 从来没收到过 ——
    # 客户端只能退回 ThreadsWorkflow 里硬编码的 TRAVEL_HINT_KEYWORDS, 库里加的词
    # 对 Threads 完全不生效。这里补上, 客户端有则用之、无则仍走硬编码兜底。
    targets = await get_target_keywords(db, "threads", device_id=device_id)
    return {
        "version": "threads_config_v1",
        "device_id": device_id,
        "session_minutes": 60,
        "max_post_age_days": 7,            # 只评论 7 天以内 (相对时间) 的帖子
        # 每个关键词向下浏览, 累计遇到 N 个 >7天(绝对时间) 的旧帖 → 切换下一个关键词
        "max_stale_posts_per_keyword": 20,
        # ── 评论频率 ──
        # 2026-07-26 一降: 0.6/每词6条/无 session 上限 → 0.35/2/5。实测一小时只出 3-4 条,
        #            砍过头了 (真正卡住的是 session 上限 5, 不是概率)。
        # 2026-07-26 二调: 概率和每词上限提回来一点, session 上限同步放宽,
        #            目标 ~6-8 条/小时, 仍低于改版前的 15-20 条/天密度。
        "comment_probability": 0.75,        # 命中新鲜帖时的评论概率
        "comment_cooldown_ms_min": 45000,  # 两条评论间至少 45 秒 (2026-10-25 提量)
        "comment_cooldown_ms_max": 110000, # 最多约 1.8 分钟
        "max_comments_per_session": 18,     # 一次 session 全部关键词加起来的硬上限
        "max_comments_per_keyword": 5,     # 单个关键词最多 5 条
        "like_probability": 0.2,
        "search_keywords": search,         # [{id, keyword, language, category}]
        "target_keywords": targets,        # [{keyword, intent}] 评论区筛选用
        "reply_prompt_version": "threads_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ════════════════════════════════════════════════════════════════
# [DEPRECATED] 老 APK 的 Threads 关键词提交入口
# 新 APK 走 POST /api/v2/keywords。这里保留老的字段与默认值。
# ════════════════════════════════════════════════════════════════
class SubmitThreadsKeywordRequest(BaseModel):
    keyword: str = Field(..., min_length=3, max_length=80)
    category: str = Field(default="generic", max_length=40)
    language: str = Field(default="vi", max_length=10)
    device_id: str = Field(..., min_length=4, max_length=80)


@router.post("/threads_keywords", deprecated=True)
async def submit_threads_keyword(
    body: SubmitThreadsKeywordRequest,
    db: AsyncSession = Depends(get_db),
):
    try:
        return await submit_keyword(
            db,
            keyword=body.keyword,
            submitted_by=body.device_id,
            platforms=["threads"],
            role="search",
            category=map_legacy_category(body.category),
            language=body.language,
        )
    except KeywordSubmitError as e:
        raise HTTPException(status_code=400, detail=str(e))
