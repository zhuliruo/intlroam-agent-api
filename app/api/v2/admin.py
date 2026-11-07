"""运营管理端点 (Hotfix 3.4)

提供运营手动恢复 quarantine 群组的能力 + 关键词/设备语言管理。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Header, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete as sa_delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.device import Device
from app.models.group import Group
from app.models.keyword import CATEGORIES, DEFAULT_PLATFORMS, Keyword
from app.services.keywords import VALID_LANGUAGES, find_duplicate


log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin")

Platform = Literal["tiktok", "threads", "facebook"]
Role = Literal["search", "target"]
Intent = Literal["hot", "warm"]


# 简单的 admin token 校验（环境变量 ADMIN_TOKEN）
def _check_admin_token(x_admin_token: Annotated[str | None, Header()] = None):
    import os
    expected = os.environ.get("ADMIN_TOKEN", "")
    if not expected or x_admin_token != expected:
        raise HTTPException(status_code=403, detail="invalid admin token")
    return True


@router.post(
    "/groups/{group_id}/unquarantine",
    summary="手动取消群组 quarantine（运营恢复用）",
)
async def unquarantine_group(
    group_id: int,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(_check_admin_token),
):
    """手动恢复一个被自动 quarantine 的群组"""
    grp = (await db.execute(
        select(Group).where(Group.id == group_id)
    )).scalar_one_or_none()

    if grp is None:
        raise HTTPException(status_code=404, detail="group not found")

    if grp.quarantined_at is None:
        return {"ok": True, "message": "group not in quarantine", "group_id": group_id}

    await db.execute(
        update(Group)
        .where(Group.id == group_id)
        .values(
            quarantined_at=None,
            quarantine_reason=None,
            is_active=True,
            last_health_check_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()

    log.info(f"admin manually unquarantined group {group_id} ({grp.external_id})")
    return {
        "ok": True,
        "group_id": group_id,
        "external_id": grp.external_id,
        "previous_reason": grp.quarantine_reason,
    }


@router.get(
    "/groups/health",
    summary="查看群组健康度排名（运营查看用）",
)
async def list_groups_health(
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(_check_admin_token),
):
    """列出所有群组的健康度评分（含 quarantine 状态）"""
    groups = (await db.execute(
        select(Group)
        .order_by(Group.health_score.asc().nulls_last(), Group.id.asc())
    )).scalars().all()

    return {
        "groups": [
            {
                "id": g.id,
                "external_id": g.external_id,
                "name": g.name,
                "health_score": g.health_score,
                "is_active": g.is_active,
                "quarantined": g.quarantined_at is not None,
                "quarantine_reason": g.quarantine_reason,
                "last_check": g.last_health_check_at.isoformat() if g.last_health_check_at else None,
                "joined_at": g.joined_at.isoformat() if g.joined_at else None,
            }
            for g in groups
        ]
    }


# ═══════════════════ 关键词管理 (统一 CRUD) ═══════════════════
#
# 三张老表已合并成 keywords 一张 (alembic 008):
#   role=search : 拿去搜的词    (category + weight + 使用统计)
#   role=target : 扫评论区判断意图的词 (intent = hot/warm)
#   platforms   : 一条词可同时供 tiktok / threads / facebook 使用
#
# Facebook 目前没有关键词消费方 —— 帖子相关性由服务端 LLM (/analyze_post) 判定,
# 但 platforms 里允许写 facebook, 为以后留口子。

def _serialize(row: Keyword) -> dict:
    return {
        "id": row.id,
        "keyword": row.keyword,
        "language": row.language,
        "role": row.role,
        "category": row.category,
        "intent": row.intent,
        "platforms": list(row.platforms or []),
        "enabled": row.enabled,
        "weight": row.weight,
        "submitted_by": row.submitted_by,
        "use_count": row.use_count,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _validate(role: str, *, category: str | None, intent: str | None, language: str | None) -> None:
    """把字段用错 role 的请求挡在入库前, 而不是让它静默丢失。"""
    if role == "search" and intent is not None:
        raise HTTPException(422, "role=search 没有 intent 字段（那是 role=target 的）")
    if role == "target" and category is not None:
        raise HTTPException(422, "role=target 没有 category 字段")
    if category is not None and category not in CATEGORIES:
        raise HTTPException(422, f"category 必须是 {list(CATEGORIES)} 之一")
    if language is not None and language not in VALID_LANGUAGES:
        raise HTTPException(422, f"language 必须是 {sorted(VALID_LANGUAGES)} 之一")


class KeywordCreate(BaseModel):
    keyword: str = Field(min_length=1, max_length=120)
    language: str = Field(default="en", max_length=10)
    role: Role = "search"
    category: str | None = Field(default=None, max_length=40)
    intent: Intent | None = None
    platforms: list[Platform] = Field(default_factory=lambda: list(DEFAULT_PLATFORMS))
    weight: int | None = Field(default=None, ge=0, le=100)
    enabled: bool = True


class KeywordBulkCreate(BaseModel):
    keywords: list[str] = Field(min_length=1, max_length=500)
    language: str = Field(default="en", max_length=10)
    role: Role = "search"
    category: str | None = Field(default=None, max_length=40)
    intent: Intent | None = None
    platforms: list[Platform] = Field(default_factory=lambda: list(DEFAULT_PLATFORMS))
    weight: int | None = Field(default=None, ge=0, le=100)
    enabled: bool = True


class KeywordUpdate(BaseModel):
    keyword: str | None = Field(default=None, min_length=1, max_length=120)
    language: str | None = Field(default=None, max_length=10)
    category: str | None = Field(default=None, max_length=40)
    intent: Intent | None = None
    platforms: list[Platform] | None = None
    weight: int | None = Field(default=None, ge=0, le=100)
    enabled: bool | None = None


class PlatformsPatch(BaseModel):
    """给一批已有的词批量加/减平台 —— 合并后最常用的操作, 别让人一条条 PATCH。"""
    add: list[Platform] = Field(default_factory=list)
    remove: list[Platform] = Field(default_factory=list)
    role: Role | None = None
    language: str | None = None
    platform: Platform | None = Field(default=None, description="只改当前含此平台的词")
    ids: list[int] | None = None


def _build_values(body, role: str) -> dict:
    """keyword 由调用方填 —— 单条来自 body.keyword, 批量来自列表逐项。"""
    values: dict = {
        "language": body.language,
        "role": role,
        "platforms": sorted(set(body.platforms)),
        "enabled": body.enabled,
    }
    if role == "search":
        values["category"] = body.category or "generic"
        values["intent"] = None
    else:
        values["category"] = "generic"
        values["intent"] = body.intent or "hot"
    if body.weight is not None:
        values["weight"] = body.weight
    return values


@router.get("/keywords", summary="列出关键词（可按 role/平台/语言/启用状态过滤）")
async def list_keywords(
    role: Role | None = None,
    platform: Platform | None = None,
    language: str | None = None,
    enabled: bool | None = None,
    q: str | None = Query(default=None, description="关键词模糊搜索"),
    limit: int = Query(default=500, ge=1, le=2000),
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(_check_admin_token),
):
    stmt = select(Keyword)
    if role is not None:
        stmt = stmt.where(Keyword.role == role)
    if platform is not None:
        stmt = stmt.where(Keyword.platforms.any(platform))
    if language is not None:
        stmt = stmt.where(Keyword.language == language)
    if enabled is not None:
        stmt = stmt.where(Keyword.enabled.is_(enabled))
    if q:
        stmt = stmt.where(Keyword.keyword.ilike(f"%{q}%"))

    rows = (await db.execute(stmt.order_by(Keyword.id.asc()).limit(limit))).scalars().all()
    return {"total": len(rows), "keywords": [_serialize(r) for r in rows]}


@router.post("/keywords", status_code=201, summary="新增单个关键词")
async def create_keyword(
    body: KeywordCreate,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(_check_admin_token),
):
    _validate(body.role, category=body.category, intent=body.intent, language=body.language)
    keyword = body.keyword.strip()
    if not keyword:
        raise HTTPException(422, "keyword 不能是空白")
    if not body.platforms:
        raise HTTPException(422, "platforms 不能为空")

    if await find_duplicate(db, keyword, body.language, body.role):
        raise HTTPException(
            409, f"已存在同 (keyword, language, role): {keyword}/{body.language}/{body.role}"
        )

    values = _build_values(body, body.role)
    values["keyword"] = keyword
    row = Keyword(**values)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    log.info(f"admin created keyword {keyword} ({body.language}/{body.role})")
    return _serialize(row)


@router.post("/keywords/bulk", summary="批量导入关键词（跳过已存在的）")
async def bulk_create_keywords(
    body: KeywordBulkCreate,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(_check_admin_token),
):
    _validate(body.role, category=body.category, intent=body.intent, language=body.language)
    if not body.platforms:
        raise HTTPException(422, "platforms 不能为空")

    created: list[dict] = []
    skipped: list[str] = []
    seen: set[str] = set()

    for raw in body.keywords:
        keyword = raw.strip()
        # 同一批次内部也要去重, 否则会撞 DB 唯一约束整批回滚
        if not keyword or keyword.lower() in seen:
            skipped.append(raw)
            continue
        seen.add(keyword.lower())
        if await find_duplicate(db, keyword, body.language, body.role):
            skipped.append(keyword)
            continue

        values = _build_values(body, body.role)
        values["keyword"] = keyword
        row = Keyword(**values)
        db.add(row)
        await db.flush()
        created.append(_serialize(row))

    await db.commit()
    log.info(f"admin bulk keywords ({body.language}/{body.role}): "
             f"created={len(created)} skipped={len(skipped)}")
    return {"ok": True, "created_count": len(created), "skipped_count": len(skipped),
            "created": created, "skipped": skipped}


@router.post("/keywords/platforms", summary="批量给关键词加/减平台")
async def patch_keyword_platforms(
    body: PlatformsPatch,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(_check_admin_token),
):
    if not body.add and not body.remove:
        raise HTTPException(422, "add / remove 至少给一个")

    stmt = select(Keyword)
    if body.ids:
        stmt = stmt.where(Keyword.id.in_(body.ids))
    if body.role is not None:
        stmt = stmt.where(Keyword.role == body.role)
    if body.language is not None:
        stmt = stmt.where(Keyword.language == body.language)
    if body.platform is not None:
        stmt = stmt.where(Keyword.platforms.any(body.platform))

    rows = (await db.execute(stmt)).scalars().all()
    changed = 0
    emptied: list[int] = []
    for row in rows:
        new_platforms = (set(row.platforms or []) | set(body.add)) - set(body.remove)
        if not new_platforms:
            # 平台清空 = 这条词谁也不会用, 等于静默失效。宁可拒绝, 让人显式 disable。
            emptied.append(row.id)
            continue
        if new_platforms != set(row.platforms or []):
            row.platforms = sorted(new_platforms)
            changed += 1

    if emptied:
        await db.rollback()
        raise HTTPException(
            422, f"这些词会被清空 platforms（等于静默失效, 请改用 enabled=false）: {emptied[:20]}"
        )

    await db.commit()
    log.info(f"admin platforms patch add={body.add} remove={body.remove}: "
             f"matched={len(rows)} changed={changed}")
    return {"ok": True, "matched": len(rows), "changed": changed}


@router.patch("/keywords/{keyword_id}", summary="修改关键词（含启用/停用）")
async def update_keyword(
    keyword_id: int,
    body: KeywordUpdate,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(_check_admin_token),
):
    row = (await db.execute(select(Keyword).where(Keyword.id == keyword_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "keyword not found")

    _validate(row.role, category=body.category, intent=body.intent, language=body.language)

    patch = body.model_dump(exclude_unset=True)
    if "keyword" in patch:
        patch["keyword"] = patch["keyword"].strip()
        if not patch["keyword"]:
            raise HTTPException(422, "keyword 不能是空白")
    if "platforms" in patch:
        if not patch["platforms"]:
            raise HTTPException(422, "platforms 不能为空（要停用请用 enabled=false）")
        patch["platforms"] = sorted(set(patch["platforms"]))

    # 改动 keyword/language 会挪动唯一键, 先确认不会撞上别人
    new_kw = patch.get("keyword", row.keyword)
    new_lang = patch.get("language", row.language)
    if (new_kw.lower(), new_lang) != (row.keyword.lower(), row.language):
        if await find_duplicate(db, new_kw, new_lang, row.role, exclude_id=row.id):
            raise HTTPException(
                409, f"已存在同 (keyword, language, role): {new_kw}/{new_lang}/{row.role}"
            )

    for field, value in patch.items():
        setattr(row, field, value)
    await db.commit()
    await db.refresh(row)
    log.info(f"admin updated keyword {keyword_id}: {sorted(patch)}")
    return _serialize(row)


@router.delete("/keywords/{keyword_id}", summary="删除关键词")
async def delete_keyword(
    keyword_id: int,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(_check_admin_token),
):
    row = (await db.execute(select(Keyword).where(Keyword.id == keyword_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "keyword not found")
    await db.execute(sa_delete(Keyword).where(Keyword.id == keyword_id))
    await db.commit()
    log.info(f"admin deleted keyword {keyword_id} ({row.keyword})")
    return {"ok": True, "id": keyword_id, "keyword": row.keyword}


# ═══════════════════ 设备语言 ═══════════════════
# 抽词时用哪些语言由这里决定; 空数组 = 用平台默认 (tiktok=vi, threads=全语言)。
# 转英语市场就是把目标机器设成 ["en"], 不用改 APK。

class DeviceLanguages(BaseModel):
    languages: list[str] = Field(default_factory=list)


@router.get("/devices/languages", summary="查看各设备的抽词语言")
async def list_device_languages(
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(_check_admin_token),
):
    rows = (await db.execute(select(Device).order_by(Device.id.asc()))).scalars().all()
    return {
        "devices": [
            {
                "device_id": d.device_id,
                "name": d.name,
                "is_active": d.is_active,
                "languages": list(d.languages or []),   # [] = 平台默认
            }
            for d in rows
        ]
    }


@router.put("/devices/{device_id}/languages", summary="设置某设备的抽词语言（[] = 平台默认）")
async def set_device_languages(
    device_id: str,
    body: DeviceLanguages,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(_check_admin_token),
):
    bad = [l for l in body.languages if l not in VALID_LANGUAGES]
    if bad:
        raise HTTPException(422, f"不支持的语言: {bad}; 可选 {sorted(VALID_LANGUAGES)}")

    row = (await db.execute(
        select(Device).where(Device.device_id == device_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "device not found")

    row.languages = list(dict.fromkeys(body.languages))
    await db.commit()
    await db.refresh(row)
    log.info(f"admin set device {device_id} languages={row.languages}")
    return {"ok": True, "device_id": device_id, "languages": list(row.languages)}
