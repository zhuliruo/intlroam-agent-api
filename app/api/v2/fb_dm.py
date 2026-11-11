"""fb_dm — FB 群里旅行从业者的帖子: 除了评论, 点发帖人 → Message → Messenger 私信招分销 (2026-11-11)

用户 11-11: "在 Facebook 群组遇到旅行从业者的帖子除了发评论留言以外, 也可以尝试点击发帖人账号,
然后再点击发消息调 messenger 发私信消息推销。"

谁算旅行从业者: analyze_post 判 warm (FB 的 warm 专指"这个人在卖旅游服务") 或被广告规则直接判 cold
(reasoning=advertiser_marketing_pattern —— 卖团的广告帖, 评论不发, 但正是要找的分销商)。手机端判断后调这里。

流程: 手机 POST /fb_dm/next (作者名 + 帖子正文) → 服务端查闸门、生成开场白 (复用 WhatsApp/Zalo 招呼的
生成器, 同一套分销事实和例子价) 并发一个 token → 手机点作者 → Message → 发 → POST /fb_dm/result。
发成功后在 fb_engagements 建一条 platform=facebook 的 dm 会话 (history[0] 带 outreach=true),
对方在 Messenger 回复时走原来的 FB 私信回复链路, engagement 认出 outreach 标记后用招分销的话术。

闸门:
  - 越南时间 0-8 点不发 (用户 11-11: 凌晨只回消息)
  - 每台每天 FB_DM_DAILY 条 (默认 5): 陌生人私信进对方的"消息请求", FB 对这个很敏感, 先保守
  - 同一个作者全车队只发一次 (Redis 180 天 + fb_engagements 里已有同名 FB 私信会话也跳过)
"""
from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

import redis.asyncio as redis
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db, get_device_id, get_redis
from app.utils.logger import get_logger

router = APIRouter()
log = get_logger(__name__)

VNT = timezone(timedelta(hours=7))
PENDING_TTL = 3600
AUTHOR_TTL = 180 * 86400
_VI_RE = re.compile(r"[ăâđêôơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹ]", re.I)


def daily_cap() -> int:
    try:
        return int(os.getenv("FB_DM_DAILY", "5"))
    except ValueError:
        return 5


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _cap_key(device_id: str) -> str:
    return f"fb_dm_cap:{device_id}:{datetime.now(VNT).strftime('%Y%m%d')}"


def _author_key(name: str) -> str:
    return f"fb_dm_author:{_norm(name)}"


class NextRequest(BaseModel):
    author_name: str = Field(min_length=2, max_length=200)
    content: str = Field(default="", max_length=4000)
    group_id: int | None = None
    post_id: int | None = None


class NextResponse(BaseModel):
    ok: bool = False
    token: str | None = None
    text: str | None = None
    reason: str | None = None


class ResultRequest(BaseModel):
    token: str
    success: bool
    failure_reason: str | None = Field(default=None, max_length=200)


@router.post("/fb_dm/next", response_model=NextResponse)
async def fb_dm_next(body: NextRequest, device_id: str = Depends(get_device_id),
                     db: AsyncSession = Depends(get_db), rds: redis.Redis = Depends(get_redis)):
    from app.api.v2.outreach import _gen_greeting

    if 0 <= datetime.now(VNT).hour < 8:
        return NextResponse(reason="night")
    cap = daily_cap()
    try:
        used = int(await rds.get(_cap_key(device_id)) or 0)
    except Exception:
        used = cap
    if used >= cap:
        return NextResponse(reason="daily_cap")
    name = body.author_name.strip()
    seen = (await db.execute(text(
        "SELECT 1 FROM fb_engagements WHERE platform='facebook' AND kind='dm' AND lower(counterpart)=lower(:n) LIMIT 1"),
        {"n": name})).first()
    if seen:
        return NextResponse(reason="already_contacted")
    # 占住这个作者 (车队里别的手机同时刷到同一帖不会重复发); 失败时 result 里释放
    if not await rds.set(_author_key(name), f"pending:{device_id}", ex=PENDING_TTL, nx=True):
        return NextResponse(reason="already_contacted")
    region = "VN" if _VI_RE.search(body.content or "") else None
    msg = await _gen_greeting("facebook", region, body.content, source_platform="facebook_group")
    token = secrets.token_urlsafe(12)
    await rds.set(f"fb_dm_pending:{token}", json.dumps({
        "device": device_id, "author": name, "text": msg, "region": region,
        "group_id": body.group_id, "post_id": body.post_id, "content": (body.content or "")[:500],
    }, ensure_ascii=False), ex=PENDING_TTL)
    log.info("fb_dm_next", device_id=device_id, author=name[:60], used=used, cap=cap, region=region)
    return NextResponse(ok=True, token=token, text=msg)


@router.post("/fb_dm/result")
async def fb_dm_result(body: ResultRequest, device_id: str = Depends(get_device_id),
                       db: AsyncSession = Depends(get_db), rds: redis.Redis = Depends(get_redis)):
    from app.api.v2.engagement import thread_key

    raw = await rds.get(f"fb_dm_pending:{body.token}")
    if not raw:
        return {"ok": False, "reason": "unknown_token"}
    p = json.loads(raw)
    if p.get("device") != device_id:
        return {"ok": False, "reason": "wrong_device"}
    await rds.delete(f"fb_dm_pending:{body.token}")
    name = p["author"]
    if not body.success:
        reason = (body.failure_reason or "unknown")[:200]
        # 主页上没有 Message 按钮 / 对方关了陌生人私信: 以后也发不了, 记 30 天别再试; 其他失败 (没点到/网络) 放开
        if reason.startswith(("no_message_btn", "not_allowed")):
            await rds.set(_author_key(name), f"failed:{reason}", ex=30 * 86400)
        else:
            await rds.delete(_author_key(name))
        log.info("fb_dm_result", device_id=device_id, author=name[:60], success=False, reason=reason)
        return {"ok": True}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    history = [{"r": "us", "t": p["text"], "at": now, "outreach": True}]
    if p.get("region") == "VN":
        history[0]["region"] = "VN"
    rid = (await db.execute(text(
        "INSERT INTO fb_engagements (device_id, kind, thread_key, counterpart, context_text, our_reply, brand, status, "
        "history, turns, handover, human_status, platform, lead_type, sent_at) "
        "VALUES (:d, 'dm', :t, :c, :cx, :r, true, 'sent', CAST(:h AS jsonb), 1, false, 'auto', 'facebook', NULL, now()) "
        "ON CONFLICT (device_id, kind, thread_key) DO NOTHING RETURNING id"),
        {"d": device_id, "t": thread_key("dm", name, None, "facebook"), "c": name[:200], "cx": p.get("content"),
         "r": p["text"], "h": json.dumps(history, ensure_ascii=False)})).scalar()
    await db.commit()
    await rds.set(_author_key(name), f"sent:{device_id}", ex=AUTHOR_TTL)
    try:
        await rds.incr(_cap_key(device_id))
        await rds.expire(_cap_key(device_id), 2 * 86400)
    except Exception:
        pass
    log.info("fb_dm_result", device_id=device_id, author=name[:60], success=True, engagement_id=rid)
    return {"ok": True, "id": rid}
