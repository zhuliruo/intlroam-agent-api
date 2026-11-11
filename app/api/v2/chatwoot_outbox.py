"""Chatwoot 人工回复 → 手机代发到 WhatsApp / Zalo (2026-11-10)

转人工的线索在 Chatwoot "Agent Leads" (API 收件箱) 里有会话 (services/chatwoot_notify.py)。
用户 11-10: 管理员直接在 Chatwoot 回复, 不用再去手机上开 WhatsApp/Zalo。

  POST /api/v2/chatwoot/webhook        Chatwoot API 收件箱的 webhook (只认 HMAC 签名)
  POST /api/v2/chatwoot/outbox/next    手机领自己名下待发的人工回复
  POST /api/v2/chatwoot/outbox/result  手机回报 sent / failed

WhatsApp/Zalo 账号登录在打招呼的那台测试机上, 没有官方接口 —— 只能由那台手机 (fb_engagements.device_id)
在 App 里发。手机离线时消息排队, 上线后补发; 24 小时没发出去就在 Chatwoot 标红失败。
发送结果回写到 Chatwoot 消息状态 (delivered / failed + 原因), 坐席在界面上就能看到发没发出去。
Messenger / TikTok 的转人工会话不支持 (手机端没有对应的发送流程) → 标失败并留备注。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import redis.asyncio as redis

from app.deps import get_db, get_device_id, get_redis
from app.services import chatwoot_notify
from app.services.phone_leads import TABLE as LEAD_TABLE
from app.utils.logger import get_logger

router = APIRouter()
log = get_logger(__name__)

SUPPORTED = ("whatsapp", "zalo")
CLAIM_TTL_MIN = 15        # 领走后这么久没回报 → 重新排队 (手机中途被杀/断网)
MAX_ATTEMPTS = 3
EXPIRE_HOURS = 24         # 手机一直不上线, 消息过期作废 (晚一天才到, 不如让坐席换个方式联系)
SIG_MAX_SKEW_S = 300

# 手机端失败原因 → 坐席看得懂的话 (显示在 Chatwoot 那条消息的红色失败提示里)
_REASON_ZH = {
    "open_failed": "手机打不开这个对话",
    "not_on_app": "对方号码没开通该 App",
    "no_input": "手机上找不到输入框",
    "no_send_btn": "手机上找不到发送键",
    "type_failed": "手机输入文字失败",
    "still_in_input": "点了发送但文字还在输入框里",
    "no_phone": "这个线索没有电话号码, 手机打不开对话",
    "app_not_installed": "手机上没装这个 App",
    "wrong_thread": "打开的不是这个人的对话",
    "a11y_off": "手机无障碍服务没开",
    "device_offline": f"手机 {EXPIRE_HOURS} 小时没上线",
    "no_result": "手机多次领取后没有回报结果",
    "unsupported_platform": "Messenger/TikTok 线索暂不支持在 Chatwoot 回复",
    "attachment_only": "只能发文字, 附件/图片发不出去",
    "no_thread": "找不到这个会话对应的手机和号码",
}


def _reason_zh(r: str | None) -> str:
    r = (r or "").strip()
    base = r.split(":", 1)[0]
    return _REASON_ZH.get(base, f"手机发送失败 ({r[:60] or 'unknown'})")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _secret() -> str:
    return os.getenv("CHATWOOT_LEADS_WEBHOOK_SECRET", "").strip().strip("'\"")


def _sig_ok(raw: bytes, ts: str | None, sig: str | None) -> bool:
    sec = _secret()
    if not sec or not ts or not sig:
        return False
    try:
        if abs(time.time() - int(ts)) > SIG_MAX_SKEW_S:
            return False
    except ValueError:
        return False
    want = "sha256=" + hmac.new(sec.encode(), ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, sig.strip())


async def _fail_in_chatwoot(conv: int, msg_id: int, reason: str, with_note: bool = True) -> None:
    zh = _reason_zh(reason)
    await chatwoot_notify.set_message_status(conv, msg_id, "failed", zh)
    if with_note:
        await chatwoot_notify.note(conv, f"⚠️ 上面这条没有发到客户那边: {zh}。")


# ─────────────────────────── webhook ───────────────────────────

@router.post("/chatwoot/webhook")
async def webhook(request: Request, db: AsyncSession = Depends(get_db), rds: redis.Redis = Depends(get_redis)):
    raw = await request.body()
    if not _sig_ok(raw, request.headers.get("X-Chatwoot-Timestamp"), request.headers.get("X-Chatwoot-Signature")):
        log.warning("chatwoot_webhook_bad_sig", has_ts=bool(request.headers.get("X-Chatwoot-Timestamp")))
        # 200 而不是 401: 非 2xx 会让 Chatwoot 把那条消息标成发送失败, 而伪造请求不该影响真实消息
        return {"ok": False}
    try:
        p = json.loads(raw or b"{}")
    except ValueError:
        return {"ok": False}
    if p.get("event") != "message_created":
        return {"ok": True, "skip": "event"}
    mtype = p.get("message_type")
    if mtype not in ("outgoing", 1) or p.get("private"):
        return {"ok": True, "skip": "not_outgoing"}
    sender = p.get("sender") or {}
    if (sender.get("type") or "").lower() not in ("user", ""):
        return {"ok": True, "skip": "sender"}          # 机器人 / 自动化发的不代发
    inbox_id = str((p.get("inbox") or {}).get("id") or (p.get("conversation") or {}).get("inbox_id") or "")
    if inbox_id != os.getenv("CHATWOOT_LEADS_INBOX_ID", "").strip():
        return {"ok": True, "skip": "inbox"}
    msg_id = p.get("id")
    conv = (p.get("conversation") or {}).get("id")
    if not msg_id or not conv:
        return {"ok": False}
    content = (p.get("content") or "").strip()
    # 11-11: 模型回复同步进 Chatwoot 也是 outgoing (发送人=token 的主人) —— 不是坐席写的, 不能让手机再发一遍
    if content.startswith(chatwoot_notify.BOT_PREFIX.strip()):
        return {"ok": True, "skip": "bot_echo"}
    try:
        if await rds.delete(chatwoot_notify.echo_key(conv, content)):
            return {"ok": True, "skip": "bot_echo"}
    except Exception:
        pass

    row = (await db.execute(text(
        "SELECT id, device_id, platform, counterpart, lead_id FROM fb_engagements "
        "WHERE chatwoot_conversation_id=:c ORDER BY id DESC LIMIT 1"), {"c": conv})).first()
    if not row:
        await _fail_in_chatwoot(conv, msg_id, "no_thread")
        return {"ok": True, "skip": "no_thread"}
    if row.platform not in SUPPORTED:
        await _fail_in_chatwoot(conv, msg_id, "unsupported_platform")
        return {"ok": True, "skip": "unsupported_platform"}
    if not content:
        await _fail_in_chatwoot(conv, msg_id, "attachment_only")
        return {"ok": True, "skip": "attachment_only"}
    if p.get("attachments"):
        await chatwoot_notify.note(conv, "⚠️ 附件发不出去, 只会发文字部分。")

    phone = None
    if row.lead_id:
        phone = (await db.execute(text(f"SELECT phone_e164 FROM {LEAD_TABLE[row.platform]} WHERE id=:id"),
                                  {"id": row.lead_id})).scalar()
    res = await db.execute(text(
        "INSERT INTO chatwoot_outbox (chatwoot_message_id, conversation_id, engagement_id, device_id, platform, "
        "phone, counterpart, content, sender_name) VALUES (:m, :c, :e, :d, :p, :ph, :cp, :t, :s) "
        "ON CONFLICT (chatwoot_message_id) DO NOTHING RETURNING id"),
        {"m": msg_id, "c": conv, "e": row.id, "d": row.device_id, "p": row.platform, "ph": phone,
         "cp": (row.counterpart or "")[:255], "t": content[:4000], "s": (sender.get("name") or "")[:255]})
    oid = res.scalar()
    await db.commit()
    log.info("chatwoot_outbox_queued", outbox_id=oid, conversation_id=conv, message_id=msg_id,
             device_id=row.device_id, platform=row.platform, dup=oid is None)
    return {"ok": True, "id": oid}


# ─────────────────────────── 手机端 ───────────────────────────

class OutboxItem(BaseModel):
    id: int
    platform: str
    phone: str | None = None
    counterpart: str = ""
    text: str


class NextResponse(BaseModel):
    items: list[OutboxItem] = []


class ResultRequest(BaseModel):
    id: int
    success: bool
    failure_reason: str | None = None


async def _housekeep(db) -> list:
    """过期 / 超时重排。返回需要在 Chatwoot 标失败的 (conversation_id, message_id, reason)。"""
    failed = []
    rows = (await db.execute(text(
        f"UPDATE chatwoot_outbox SET status='failed', failure_reason='device_offline', done_at=now() "
        f"WHERE status='pending' AND created_at < now() - interval '{EXPIRE_HOURS} hours' "
        f"RETURNING conversation_id, chatwoot_message_id, failure_reason"))).all()
    failed += rows
    rows = (await db.execute(text(
        f"UPDATE chatwoot_outbox SET status='failed', failure_reason='no_result', done_at=now() "
        f"WHERE status='claimed' AND claimed_at < now() - interval '{CLAIM_TTL_MIN} minutes' "
        f"AND attempts >= {MAX_ATTEMPTS} RETURNING conversation_id, chatwoot_message_id, failure_reason"))).all()
    failed += rows
    await db.execute(text(
        f"UPDATE chatwoot_outbox SET status='pending' "
        f"WHERE status='claimed' AND claimed_at < now() - interval '{CLAIM_TTL_MIN} minutes'"))
    return failed


@router.post("/chatwoot/outbox/next", response_model=NextResponse)
async def outbox_next(device_id: str = Depends(get_device_id), db: AsyncSession = Depends(get_db)):
    failed = await _housekeep(db)
    rows = (await db.execute(text(
        "SELECT id, platform, phone, counterpart, content FROM chatwoot_outbox "
        "WHERE device_id=:d AND status='pending' ORDER BY id LIMIT 5 FOR UPDATE SKIP LOCKED"),
        {"d": device_id})).all()
    if rows:
        await db.execute(text(
            "UPDATE chatwoot_outbox SET status='claimed', claimed_at=now(), attempts=attempts+1 "
            "WHERE id = ANY(:ids)"), {"ids": [r.id for r in rows]})
    await db.commit()
    for conv, mid, reason in failed:
        await _fail_in_chatwoot(conv, mid, reason)
    if rows:
        log.info("chatwoot_outbox_claimed", device_id=device_id, ids=[r.id for r in rows])
    return NextResponse(items=[OutboxItem(id=r.id, platform=r.platform, phone=r.phone,
                                          counterpart=r.counterpart or "", text=r.content) for r in rows])


@router.post("/chatwoot/outbox/result")
async def outbox_result(body: ResultRequest, device_id: str = Depends(get_device_id),
                        db: AsyncSession = Depends(get_db)):
    row = (await db.execute(text(
        "SELECT id, status, attempts, conversation_id, chatwoot_message_id, engagement_id, content "
        "FROM chatwoot_outbox WHERE id=:id AND device_id=:d FOR UPDATE"),
        {"id": body.id, "d": device_id})).first()
    if not row or row.status in ("sent", "failed"):
        await db.rollback()
        return {"ok": False}
    if body.success:
        await db.execute(text("UPDATE chatwoot_outbox SET status='sent', failure_reason=NULL, done_at=now() "
                              "WHERE id=:id"), {"id": row.id})
        eng = (await db.execute(text("SELECT history FROM fb_engagements WHERE id=:id FOR UPDATE"),
                                {"id": row.engagement_id})).first()
        if eng is not None:
            hist = list(eng.history or [])
            # by=human: 日报的"自动回复"条数要排除它; r=us 让后续清洗认得出对方引用/点赞的是我们的话
            hist.append({"r": "us", "t": row.content[:1500], "at": _now_iso(), "by": "human"})
            # 11-11: 坐席亲自回了 = 人工接手。白天模型不再插话 (避免两边同时回), 越南时间 0-8 点模型代回
            await db.execute(text(
                "UPDATE fb_engagements SET history=CAST(:h AS jsonb), handover=true, "
                "human_status=CASE WHEN human_status IN ('pending','done','opted_out') THEN human_status ELSE 'pending' END, "
                "updated_at=now() WHERE id=:id"), {"h": json.dumps(hist, ensure_ascii=False), "id": row.engagement_id})
        await db.commit()
        await chatwoot_notify.set_message_status(row.conversation_id, row.chatwoot_message_id, "delivered")
        # 人工接手 → 指派给管理员, 之后客户新消息都会提醒 (同步会话原来是不指派的)
        await chatwoot_notify.assign(row.conversation_id, label="human")
    else:
        reason = (body.failure_reason or "unknown")[:255]
        retry = row.attempts < MAX_ATTEMPTS and not reason.startswith(
            ("not_on_app", "wrong_thread", "no_phone", "app_not_installed", "unsupported_platform", "empty"))
        if retry:
            await db.execute(text("UPDATE chatwoot_outbox SET status='pending', failure_reason=:r WHERE id=:id"),
                             {"r": reason, "id": row.id})
        else:
            await db.execute(text("UPDATE chatwoot_outbox SET status='failed', failure_reason=:r, done_at=now() "
                                  "WHERE id=:id"), {"r": reason, "id": row.id})
        await db.commit()
        if not retry:
            await _fail_in_chatwoot(row.conversation_id, row.chatwoot_message_id, reason)
    log.info("chatwoot_outbox_result", device_id=device_id, id=body.id, success=body.success,
             reason=body.failure_reason)
    return {"ok": True}
