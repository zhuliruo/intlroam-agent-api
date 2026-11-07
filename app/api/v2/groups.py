"""群组分配端点：GET /api/v2/next_groups"""
from __future__ import annotations

import redis.asyncio as redis
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models.group import Group
from app.models.device_group_blacklist import DeviceGroupBlacklist
from app.deps import get_db, get_device_id, get_redis
from app.services.alerting import pop_device_notice
from app.services.device_guard import (
    comment_attempts_today, daily_comment_limit, is_dispatch_paused,
)
from app.services.group_manager import get_next_groups, release_group_lock
from app.utils.logger import get_logger

router = APIRouter()
log = get_logger(__name__)

# 重复派发幂等窗口（秒）：实测双触发间隔 <1s，取 30s 留余量
NEXT_GROUPS_DEDUP_TTL_SECONDS = 30


class GroupItem(BaseModel):
    id: int
    external_id: str
    url: str
    name: str | None = None
    member_count: int | None = None
    last_crawl_at: str | None = None


class DeviceQuickConfig(BaseModel):
    """设备配置的简化版本，跟群组一起返回避免手机端再调一次 device_config"""
    daily_limit_comments: int
    cooldown_seconds: list[int]   # [min, max] 评论之间的冷却随机区间
    group_lock_ttl_seconds: int   # 告诉手机：必须在这个时间内完成


class NextGroupsResponse(BaseModel):
    groups: list[GroupItem] = Field(default_factory=list)
    config: DeviceQuickConfig

    # 2026-09-14: 服务端为什么没给群。空列表以前是完全沉默的, 三种原因
    # (没群可派 / 熔断中 / 到日限) 在手机端和日志里长得一模一样。
    # 取值: null | paused:<reason> | daily_limit | no_groups
    hold_reason: str | None = None

    # 给手机端显示用的一句话 (通知栏/弹窗)。APK 支持之前它只是被忽略的多余字段。
    notice: str | None = None


@router.get("/next_groups", response_model=NextGroupsResponse)
async def next_groups(
    platform: str = Query(default="facebook", pattern="^(facebook|instagram|threads|tiktok)$"),
    count: int = Query(default=5, ge=1, le=20),
    joined_only: bool = Query(default=False),  # 2026-11-03: 手机关了自动加群时带 1
    device_id: str = Depends(get_device_id),
    db: AsyncSession = Depends(get_db),
    rds: redis.Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
):
    """
    给手机派发要采集的群组。

    - 自动排除：is_active=False、verified_at IS NULL 的群组
    - 加 30 分钟 Redis 锁防止重复采集
    - 同时返回精简的配置（限额、冷却时间），减少手机端 RPC
    """
    daily_limit = daily_comment_limit(settings, platform)

    def _hold(reason: str, notice: str | None = None) -> NextGroupsResponse:
        """不派群时的统一返回 —— 空 groups 但带上原因。"""
        return NextGroupsResponse(
            groups=[],
            config=DeviceQuickConfig(
                daily_limit_comments=daily_limit,
                cooldown_seconds=[60, 150],
                group_lock_ttl_seconds=settings.group_lock_ttl_seconds,
            ),
            hold_reason=reason,
            notice=notice,
        )

    # ── 闸门 1: 空跑熔断 (2026-09-14) ──
    # 连续 5 次打不开 App 的机器停派 30 分钟, 别再往里灌任务。
    paused = await is_dispatch_paused(rds, device_id, platform)
    if paused:
        log.info("next_groups_paused", device_id=device_id,
                 platform=platform, reason=paused)
        return _hold(
            f"paused:{paused}",
            notice=f"已暂停派发：连续打不开 {platform}（{paused}）。请检查 App Links 开关或账号登录状态。",
        )

    # ── 闸门 2: 日评论上限 (2026-09-14) ──
    # 以前 daily_limit 只是"下发给手机的建议值", 服务端从不拦截 ——
    # 实测单机单日跑到 87 次 (配置 25), 同期 shadow_ban 率翻倍。
    used_today = await comment_attempts_today(db, device_id, platform)
    if used_today >= daily_limit:
        log.info("next_groups_daily_limit", device_id=device_id,
                 platform=platform, used=used_today, limit=daily_limit)
        return _hold(
            "daily_limit",
            notice=f"今日 {platform} 评论已达上限 {daily_limit} 条，明日自动恢复。",
        )

    # 同一台设备在极短时间内重复调用（WorkManager + AlarmManager 双触发），
    # 会各自锁走一批群 30 分钟，其中一批必然被丢弃。这里做一次幂等窗口：
    # 窗口内重复调用直接返回上一次派发的同一批群，不再消耗新的群锁。
    dedup_key = f"next_groups_recent:{device_id}:{platform}"
    cached = await rds.get(dedup_key)
    groups = []
    if cached:
        ids = [int(x) for x in cached.split(",") if x]
        if ids:
            rows = (await db.execute(select(Group).where(Group.id.in_(ids)))).scalars().all()
            order = {gid: i for i, gid in enumerate(ids)}
            groups = sorted(rows, key=lambda g: order.get(g.id, 10**6))
            log.info(
                "next_groups_dedup_hit",
                device_id=device_id, platform=platform, count=len(groups),
            )
    if not groups:
        groups = await get_next_groups(
            db=db, rds=rds, device_id=device_id,
            platform=platform, count=count, joined_only=joined_only,
        )
        if groups:
            await rds.setex(
                dedup_key, NEXT_GROUPS_DEDUP_TTL_SECONDS,
                ",".join(str(g.id) for g in groups),
            )

    # 限额已在函数开头按平台算好 (daily_limit), 那里还要用它做闸门。

    if not groups:
        return _hold("no_groups")

    return NextGroupsResponse(
        groups=[
            GroupItem(
                id=g.id,
                external_id=g.external_id,
                url=g.url,
                name=g.name,
                member_count=g.member_count,
                last_crawl_at=g.last_crawl_at.isoformat() if g.last_crawl_at else None,
            )
            for g in groups
        ],
        config=DeviceQuickConfig(
            daily_limit_comments=daily_limit,
            cooldown_seconds=[60, 150],
            group_lock_ttl_seconds=settings.group_lock_ttl_seconds,
        ),
        notice=await pop_device_notice(rds, device_id),
    )


class ReportGroupBlockedRequest(BaseModel):
    group_id: int
    platform: str = "facebook"
    reason: str = "access_blocked"


@router.post("/report_group_blocked")
async def report_group_blocked(
    req: ReportGroupBlockedRequest,
    device_id: str = Depends(get_device_id),
    db: AsyncSession = Depends(get_db),
    rds: redis.Redis = Depends(get_redis),
):
    """工作机被某群组拉黑 → 按 (device_id, group_id) 加黑名单, 以后不再分配给本机。"""
    stmt = (
        pg_insert(DeviceGroupBlacklist)
        .values(device_id=device_id, group_id=req.group_id, reason=req.reason)
        .on_conflict_do_nothing(index_elements=["device_id", "group_id"])
    )
    await db.execute(stmt)
    await db.commit()
    try:
        await release_group_lock(rds, req.group_id, device_id)
    except Exception:
        pass
    log.info("group_blacklisted device=%s group=%s reason=%s", device_id, req.group_id, req.reason)
    return {"ok": True, "device_id": device_id, "group_id": req.group_id}



# ════════════════════════════════════════════════════════════
# Submit Groups (Phase 2 Posting + 群组管理)
# ════════════════════════════════════════════════════════════

import re
from typing import Optional


def extract_facebook_group_id(url: str) -> Optional[str]:
    """
    从 FB 群组 URL 提取 external_id (群组的数字 ID 或 vanity name).
    
    支持格式:
        https://www.facebook.com/groups/1234567890
        https://www.facebook.com/groups/1234567890/
        https://m.facebook.com/groups/1234567890
        https://www.facebook.com/groups/vanity_name
        https://www.facebook.com/groups/vanity_name/
    """
    url = url.strip().rstrip('/')
    m = re.search(r'facebook\.com/groups/([a-zA-Z0-9_.\-]+)', url)
    if m:
        return m.group(1)
    return None


def normalize_facebook_group_url(url: str, external_id: str) -> str:
    """规范化 URL 为 https://www.facebook.com/groups/<id>"""
    return f"https://www.facebook.com/groups/{external_id}"


class SubmitGroupItem(BaseModel):
    """单条群组提交"""
    url: str
    name: Optional[str] = None  # 可选: 提交者标注的名字


class SubmitGroupsRequest(BaseModel):
    """多群组批量提交"""
    items: list[SubmitGroupItem]


class SubmitGroupResultItem(BaseModel):
    """单条群组提交结果"""
    url: str
    external_id: Optional[str] = None
    group_id: Optional[int] = None
    status: str  # "added" | "already_exists" | "invalid_url"
    message: Optional[str] = None


class SubmitGroupsResponse(BaseModel):
    items: list[SubmitGroupResultItem]
    added_count: int
    already_exists_count: int
    invalid_count: int


@router.post("/submit_groups", response_model=SubmitGroupsResponse)
async def submit_groups_endpoint(
    body: SubmitGroupsRequest,
    device_id: str = Depends(get_device_id),
    db: AsyncSession = Depends(get_db),
):
    """
    APP 端批量提交 FB 群组 URL.
    
    设计:
        1. 解析 URL 提取 external_id
        2. 已存在的 (UNIQUE constraint platform+external_id) -> already_exists
        3. 新群组 -> INSERT, is_active=true, join_status='unknown'
        4. 下次 next_groups 自动分配设备去尝试加入
    """
    if not device_id:
        raise HTTPException(status_code=400, detail="device_id required")

    if not body.items or len(body.items) > 50:
        raise HTTPException(
            status_code=400, 
            detail="items count must be 1-50"
        )

    results: list[SubmitGroupResultItem] = []
    added = 0
    already_exists = 0
    invalid = 0

    for item in body.items:
        url = item.url.strip()
        external_id = extract_facebook_group_id(url)
        
        if not external_id:
            results.append(SubmitGroupResultItem(
                url=url, status="invalid_url",
                message="cannot parse external_id from URL",
            ))
            invalid += 1
            continue

        normalized_url = normalize_facebook_group_url(url, external_id)

        # 检查是否已存在
        existing_q = await db.execute(
            select(Group).where(
                Group.platform == "facebook",
                Group.external_id == external_id,
            )
        )
        existing = existing_q.scalar_one_or_none()
        
        if existing:
            results.append(SubmitGroupResultItem(
                url=normalized_url,
                external_id=external_id,
                group_id=existing.id,
                status="already_exists",
                message=f"id={existing.id}, is_active={existing.is_active}, join_status={existing.join_status}",
            ))
            already_exists += 1
            continue

        # 新群组 - INSERT
        new_group = Group(
            platform="facebook",
            external_id=external_id,
            url=normalized_url,
            name=item.name,
            is_active=True,
            join_status="unknown",
            crawl_count=0,
            verify_failures=0,
            join_attempt_count=0,
            notes=f"submitted_by_device:{device_id}",
        )
        db.add(new_group)
        await db.flush()  # 拿 id
        
        results.append(SubmitGroupResultItem(
            url=normalized_url,
            external_id=external_id,
            group_id=new_group.id,
            status="added",
            message=f"id={new_group.id}",
        ))
        added += 1

    if added > 0:
        await db.commit()
    
    log.info("submit_groups",
             device_id=device_id, 
             total=len(body.items),
             added=added, already_exists=already_exists, invalid=invalid)

    return SubmitGroupsResponse(
        items=results,
        added_count=added,
        already_exists_count=already_exists,
        invalid_count=invalid,
    )
