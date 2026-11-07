"""
app/api/v2/keywords.py
手机端统一关键词提交入口 (取代 POST /tiktok_keywords + POST /threads_keywords)

  POST /api/v2/keywords  - 提交一个关键词, 可一次指定多个平台

老的两个端点保留在 tiktok.py / threads.py 里当别名, 供还没升级的 APK 使用。
"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db
from app.models.keyword import DEFAULT_PLATFORMS
from app.services.keywords import KeywordSubmitError, submit_keyword

router = APIRouter()


class SubmitKeywordRequest(BaseModel):
    keyword: str = Field(..., min_length=3, max_length=80)
    device_id: str = Field(..., min_length=4, max_length=80)
    language: str = Field(default="en", max_length=10)
    role: Literal["search", "target"] = "search"
    category: str = Field(default="generic", max_length=40)
    platforms: list[Literal["tiktok", "threads", "facebook"]] = Field(
        default_factory=lambda: list(DEFAULT_PLATFORMS)
    )
    intent: Literal["hot", "warm"] | None = None


@router.post("/keywords")
async def submit_keyword_unified(
    body: SubmitKeywordRequest,
    db: AsyncSession = Depends(get_db),
):
    try:
        return await submit_keyword(
            db,
            keyword=body.keyword,
            submitted_by=body.device_id,
            platforms=list(body.platforms),
            role=body.role,
            category=body.category,
            language=body.language,
            intent=body.intent,
        )
    except KeywordSubmitError as e:
        raise HTTPException(status_code=400, detail=str(e))
