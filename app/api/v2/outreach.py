"""WhatsApp / Zalo 打招呼 —— 找兼职 eSIM 分销商 (2026-11-07)

POST /api/v2/leads/phones     手机上报帖子全文里的号码 (服务端解析 + 按群国家补区号 + 入库)
POST /api/v2/outreach/next    手机要一批打招呼对象 {channel, count} → [{lead_id, phone, greeting}]
POST /api/v2/outreach/result  手机发完回填 (greeted / not_on_app / failed + 对方显示名)

打招呼成功后在 fb_engagements 建一行 (platform = whatsapp|zalo, kind=dm, history 里放我们的招呼),
对方回复时走 /engagement/claim 的多轮逻辑 (见 engagement.py 的 OUTREACH 分支)。
"""
from __future__ import annotations

import asyncio
import json
import random
import re
from datetime import datetime, timedelta, timezone

import redis.asyncio as redis
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db, get_device_id, get_redis
from app.services.llm_client import call_llm
from app.services.partner_offer import price_lines, prices_ok
from app.services.phone_leads import TABLE, group_region, upsert_phones
from app.utils.logger import get_logger

router = APIRouter()
log = get_logger(__name__)

HKT = timezone(timedelta(hours=8))
CHANNELS = tuple(TABLE)                    # zalo | whatsapp
# 每台每天最多给多少个陌生号码打招呼。WhatsApp/Zalo 对"短时间大量给陌生人发消息"封号很快,
# 首页选 12-20 人/轮 + 循环跑几轮也不会超过这里。
DAILY_GREET_CAP = {"whatsapp": 40, "zalo": 40}
MAX_BATCH = 20
STALE_ASSIGN = timedelta(hours=3)          # 派出去 3 小时没回填 → 放回池子
# 11-11: 真机 (Pixel 4 XL / WA Business) 账号被 WhatsApp 限制 "You can't start new chats" —— 会话页没有输入框,
# 招呼全部停在 Draft, 手机每条报 no_input, 每条线索白烧一次 attempts (3 次就永久 failed)。
# 手机现在认得出限制提示 (failure_reason=account_restricted…) → 线索原样放回池子, 这台机该渠道暂停一段时间
RESTRICT_PAUSE = timedelta(hours=12)


def outreach_thread_key(platform: str, phone: str) -> str:
    """WhatsApp/Zalo 会话按电话号码认人 (与名字无关)。engagement.py 也用它。"""
    import hashlib
    return hashlib.sha256(f"dm|{platform}|tel|{re.sub(r'[^0-9]', '', phone)}".encode()).hexdigest()[:32]


# ─────────────────────────── 号码上报 ───────────────────────────

class PhoneItem(BaseModel):
    text: str = Field(min_length=6, max_length=6000)
    platform: str = "facebook"
    group_id: int | None = None
    author: str | None = Field(default=None, max_length=200)


class PhonesRequest(BaseModel):
    items: list[PhoneItem] = Field(max_length=50)


@router.post("/leads/phones")
async def leads_phones(body: PhonesRequest, device_id: str = Depends(get_device_id),
                       db: AsyncSession = Depends(get_db)):
    new = 0
    for it in body.items:
        region = await group_region(db, it.group_id)
        new += await upsert_phones(db, it.text, default_region=region, platform=it.platform,
                                   device_id=device_id, group_id=it.group_id, raw_post_id=None,
                                   author=it.author)
    await db.commit()
    return {"ok": True, "new": new}


# ─────────────────────────── 打招呼 ───────────────────────────

class NextRequest(BaseModel):
    channel: str
    count: int = Field(ge=1, le=MAX_BATCH)


class Lead(BaseModel):
    lead_id: int
    phone: str
    greeting: str


class NextResponse(BaseModel):
    channel: str
    leads: list[Lead] = []
    reason: str | None = None


_GREET_SYSTEM = """You write the FIRST {app} message to a travel business contact. {found} —
they are most likely a tour operator, travel agent, visa service, guide, driver, homestay/hotel
or a phone/SIM shop.

You work with IntlRoam (intlroam.com), a travel eSIM: mobile data for 180+ countries, delivered
instantly as a QR code, no physical SIM, no roaming bill. IntlRoam is looking for PART-TIME
RESELLERS: they offer eSIMs to their own travellers/customers at their own price and keep the
difference, with no stock and no upfront cost.

PARTNER PRICE EXAMPLES (what the partner pays us; the ONLY prices you may use, copy exactly):
{prices}

Write the message:
- Language: {language}.
- 2-4 short sentences, friendly and personal, not a sales blast.
- You may say {how}, but do NOT quote SOURCE and do NOT invent details about their business
  you can't see in it.
- Mention IntlRoam once. Give ONE or TWO of the price examples above (pick the destinations
  most relevant to SOURCE; otherwise Japan and Thailand) as "partner price", so they see the
  margin. End with ONE simple yes/no question: would they be interested in earning extra by
  offering eSIMs to their customers?
- Under 380 characters. No links, no other prices or numbers, no percentages/commission, no
  hashtags, at most one emoji. Never claim to be a bot; no name, nationality or personal story.
Return JSON only: {{"message": "..."}}"""

_FALLBACK = {
    "vi": ("Chào anh/chị, em thấy thông tin liên hệ của anh/chị trong một nhóm du lịch. Bên em là "
           "IntlRoam eSIM du lịch (180+ quốc gia), đang tìm cộng tác viên bán eSIM cho khách, không cần "
           "vốn hay ôm hàng. Ví dụ giá đối tác: Nhật 7 ngày 30GB/ngày 233.000đ, Thái Lan 10 ngày 50GB "
           "145.000đ. Anh/chị có muốn tìm hiểu thêm không ạ?"),
    "en": ("Hi! I came across your contact in a travel group. I work with IntlRoam travel eSIM (data in "
           "180+ countries) and we're looking for part-time partners - no stock, no upfront cost. Partner "
           "price e.g. Japan 7 days 30GB/day $8.95, Thailand 10 days 50GB $5.57. Would you be interested?"),
}


def _greet_language(channel: str, region: str | None) -> str:
    if channel == "zalo" or region == "VN":
        return "Vietnamese (natural, polite: use anh/chị and em)"
    return ("the language their SOURCE post is written in if that is clear; otherwise English")


_FOUND = {
    "google_maps": ("Their business and phone number are listed on Google Maps (SOURCE below)",
                    "you found their business on Google Maps"),
}
_FOUND_DEFAULT = ("Their phone number was publicly posted in a travel post (SOURCE below)",
                  "you came across their post/contact in a travel group")


async def _gen_greeting(channel: str, region: str | None, source: str | None,
                        source_platform: str | None = None) -> str:
    app = "Zalo" if channel == "zalo" else "WhatsApp"
    vi = channel == "zalo" or region == "VN"
    found, how = _FOUND.get(source_platform or "", _FOUND_DEFAULT)
    system = _GREET_SYSTEM.format(app=app, language=_greet_language(channel, region), prices=price_lines(vi),
                                  found=found, how=how)
    user = f"SOURCE (where we found them, for context only):\n{(source or '(none)')[:800]}"
    try:
        res = await call_llm(system_instruction=system, user_message=user, temperature=0.9, json_mode=True)
        msg = str((res.data or {}).get("message") or "").strip() if res.success and isinstance(res.data, dict) else ""
    except Exception as e:
        log.warning("greet_llm_failed", err=str(e)[:120])
        msg = ""
    # 防御: 不许带链接; 金额只能是给定的例子价 (编价/佣金比例 → 换成模板)
    if msg and (re.search(r"https?://|www\.|\.com\b", msg, re.I) or not prices_ok(msg)):
        log.info("greet_rejected", msg=msg[:200])
        msg = ""
    if not msg or len(msg) > 450:
        msg = _FALLBACK["vi" if (channel == "zalo" or region == "VN") else "en"]
    return msg


# 用户 11-07: WhatsApp 的招呼一部分只发简单的 "Hello" / "Hello, is this <店名>?" —— 陌生号码第一条
# 发长推销最容易被举报/风控, 短招呼回复率也更高。对方回了再介绍 (engagement._OUTREACH_TASK 处理)。
SHORT_GREET_RATE = {"whatsapp": 0.6}
_HELLO = ["Hello", "Hello 👋", "Hi", "Hi there", "Hello, good day", "Hi, good morning", "Hello!"]
_HELLO_AR = ["السلام عليكم", "مرحبا", "Hello", "Hi"]
_ARABIC_REGIONS = {"OM", "AE", "SA", "QA", "BH", "KW", "JO", "EG", "MA"}
_IS_THIS = ["Hello, is this {n}?", "Hi, is this {n}?", "Hello, is this {n}? 🙂", "Hi! Is this {n}?"]


def _biz_name(author: str | None) -> str | None:
    """Google Maps 店名 → 可以直接问 "is this X?" 的短名; 太长/像地址就不用。"""
    n = re.split(r"\s[|\-–—•]\s|[|•(]", (author or "").strip())[0].strip(" ,.")
    if not (2 <= len(n) <= 40) or re.search(r"\d{3,}|https?://|@", n):
        return None
    return n


def _short_greeting(channel: str, region: str | None, source_platform: str | None,
                    author: str | None) -> str | None:
    if random.random() >= SHORT_GREET_RATE.get(channel, 0.0):
        return None
    name = _biz_name(author) if source_platform == "google_maps" else None
    if name and random.random() < 0.6:
        return random.choice(_IS_THIS).format(n=name)
    return random.choice(_HELLO_AR if region in _ARABIC_REGIONS else _HELLO)


def _greet_cap_key(device_id: str, channel: str) -> str:
    return f"outreach_cap:{device_id}:{channel}:{datetime.now(HKT).strftime('%Y%m%d')}"


def _pause_key(device_id: str, channel: str) -> str:
    return f"outreach_pause:{device_id}:{channel}"


@router.post("/outreach/next", response_model=NextResponse)
async def outreach_next(body: NextRequest, device_id: str = Depends(get_device_id),
                        db: AsyncSession = Depends(get_db), rds: redis.Redis = Depends(get_redis)):
    ch = body.channel
    if ch not in CHANNELS:
        return NextResponse(channel=ch, reason="bad_channel")
    t = TABLE[ch]
    try:
        used = int(await rds.get(_greet_cap_key(device_id, ch)) or 0)
    except Exception:
        used = 0
    room = DAILY_GREET_CAP[ch] - used
    if room <= 0:
        return NextResponse(channel=ch, reason="daily_cap")
    try:
        paused = await rds.get(_pause_key(device_id, ch))
    except Exception:
        paused = None
    if paused:
        log.info("outreach_paused", device_id=device_id, channel=ch, why=str(paused)[:120])
        return NextResponse(channel=ch, reason="paused")
    n = min(body.count, room)

    # 超时没回填的派单放回池子 (手机中途被杀/装包)
    await db.execute(text(
        f"UPDATE {t} SET status='new', assigned_device=NULL, updated_at=now() "
        f"WHERE status='assigned' AND assigned_at < now() - make_interval(secs => :s)"),
        {"s": int(STALE_ASSIGN.total_seconds())})
    rows = (await db.execute(text(
        f"SELECT id, phone_e164, region, source_text, source_platform, source_author FROM {t} WHERE status='new' "
        f"ORDER BY last_seen_at DESC LIMIT :n FOR UPDATE SKIP LOCKED"), {"n": n})).all()
    if not rows:
        await db.commit()
        return NextResponse(channel=ch, reason="pool_empty")
    await db.execute(text(
        f"UPDATE {t} SET status='assigned', assigned_device=:d, assigned_at=now(), "
        f"attempts=attempts+1, updated_at=now() WHERE id = ANY(:ids)"),
        {"d": device_id, "ids": [r.id for r in rows]})
    await db.commit()

    async def _greet(r):
        return (_short_greeting(ch, r.region, r.source_platform, r.source_author)
                or await _gen_greeting(ch, r.region, r.source_text, r.source_platform))
    greetings = await asyncio.gather(*[_greet(r) for r in rows])
    for r, g in zip(rows, greetings):
        await db.execute(text(f"UPDATE {t} SET greeting=:g WHERE id=:id"), {"g": g, "id": r.id})
    await db.commit()
    log.info("outreach_next", device_id=device_id, channel=ch, n=len(rows), cap_used=used)
    return NextResponse(channel=ch, leads=[Lead(lead_id=r.id, phone=r.phone_e164, greeting=g)
                                           for r, g in zip(rows, greetings)])


class ResultRequest(BaseModel):
    channel: str
    lead_id: int
    success: bool
    failure_reason: str | None = Field(default=None, max_length=200)   # not_on_app | open_failed | ...
    display_name: str | None = Field(default=None, max_length=200)
    sent_text: str | None = Field(default=None, max_length=2000)       # 实际发出的文字 (可能被手机截断)


@router.post("/outreach/result")
async def outreach_result(body: ResultRequest, device_id: str = Depends(get_device_id),
                          db: AsyncSession = Depends(get_db), rds: redis.Redis = Depends(get_redis)):
    if body.channel not in CHANNELS:
        return {"ok": False, "reason": "bad_channel"}
    t = TABLE[body.channel]
    row = (await db.execute(text(
        f"SELECT id, phone_e164, greeting, status FROM {t} WHERE id=:id AND assigned_device=:d FOR UPDATE"),
        {"id": body.lead_id, "d": device_id})).first()
    if not row:
        await db.rollback()
        return {"ok": False, "reason": "not_assigned"}
    if not body.success:
        fr = (body.failure_reason or "")[:200]
        if fr.startswith("account_restricted"):
            # 这台机的账号被限制, 不是号码的问题: 线索原样放回池子 (不算一次尝试), 这台机该渠道暂停 RESTRICT_PAUSE
            await db.execute(text(
                f"UPDATE {t} SET status='new', assigned_device=NULL, attempts=GREATEST(attempts-1, 0), "
                f"failure_reason=:fr, updated_at=now() WHERE id=:id"), {"fr": fr, "id": row.id})
            await db.commit()
            try:
                await rds.set(_pause_key(device_id, body.channel), fr, ex=int(RESTRICT_PAUSE.total_seconds()))
            except Exception:
                pass
            log.warning("outreach_account_restricted", device_id=device_id, channel=body.channel,
                        lead_id=row.id, reason=fr[:120])
            return {"ok": True}
        # not_on_app 是号码本身的属性, 永久; already_chatted = 这台机以前 (人工) 聊过, 永久不再打招呼;
        # 其他失败 (打不开/找不到按钮) 放回池子给下一轮 (最多 3 次)。手机 11-11 起在 no_input 后面带屏幕文字 (no_input|screen:…)
        st = {"not_on_app": "not_on_app", "already_chatted": "already_contacted"}.get(fr.split("|")[0], "failed")
        await db.execute(text(
            f"UPDATE {t} SET status = CASE WHEN :st='failed' AND attempts < 3 THEN 'new' ELSE :st END, "
            f"assigned_device = CASE WHEN :st='failed' AND attempts < 3 THEN NULL ELSE assigned_device END, "
            f"failure_reason=:fr, updated_at=now() WHERE id=:id"),
            {"st": st, "fr": body.failure_reason, "id": row.id})
        await db.commit()
        log.info("outreach_result", device_id=device_id, channel=body.channel, lead_id=row.id,
                 status=st, reason=body.failure_reason)
        return {"ok": True}

    sent = (body.sent_text or row.greeting or "").strip()
    name = (body.display_name or "").strip() or row.phone_e164
    await db.execute(text(
        f"UPDATE {t} SET status='greeted', greeted_at=now(), display_name=:n, failure_reason=NULL, "
        f"updated_at=now() WHERE id=:id"), {"n": body.display_name, "id": row.id})
    hist = [{"r": "us", "t": sent, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}]
    await db.execute(text(
        "INSERT INTO fb_engagements (device_id, kind, thread_key, counterpart, our_reply, brand, status, "
        "history, turns, human_status, platform, lead_id, sent_at) "
        "VALUES (:d, 'dm', :tk, :c, :r, true, 'sent', CAST(:h AS jsonb), 1, 'auto', :pf, :lid, now()) "
        "ON CONFLICT (device_id, kind, thread_key) DO UPDATE SET "
        "history = COALESCE(fb_engagements.history, CAST('[]' AS jsonb)) || CAST(:h AS jsonb), "
        "turns = fb_engagements.turns + 1, "
        "lead_id = :lid, updated_at = now()"),
        {"d": device_id, "tk": outreach_thread_key(body.channel, row.phone_e164), "c": name[:200],
         "r": sent, "h": json.dumps(hist, ensure_ascii=False), "pf": body.channel, "lid": row.id})
    await db.commit()
    try:
        k = _greet_cap_key(device_id, body.channel)
        await rds.incr(k)
        await rds.expire(k, 2 * 86400)
    except Exception:
        pass
    log.info("outreach_result", device_id=device_id, channel=body.channel, lead_id=row.id, status="greeted")
    return {"ok": True}
