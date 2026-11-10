"""chatwoot_notify — 有意向的分销商 / 客户 → 在 Chatwoot 开会话通知管理员, 人工接管 (2026-11-07)

Chatwoot 与本服务在同一台 HK 机上 (docker, 127.0.0.1:3000)。用一个 API 类型的收件箱
"Agent Leads" 承载这些线索, 每个线索一个会话:
  - 第一条 incoming 消息 = 对方最近说的话 (管理员一眼看到对方在问什么)
  - 一条私有备注 = 来源平台 / 设备 / 对方电话 + wa.me/zalo.me 直达链接 / 完整对话记录
  - 指派给管理员 (CHATWOOT_ASSIGNEE_ID) + 打标签 → Chatwoot 推送/邮件通知
之后对方再发消息 (自动回复已停), 由 forward() 追加到同一个会话里。

环境变量 (.env):
  CHATWOOT_URL            默认 http://127.0.0.1:3000
  CHATWOOT_API_TOKEN      管理员的 access token (Profile → Access Token)
  CHATWOOT_ACCOUNT_ID     默认 1
  CHATWOOT_LEADS_INBOX_ID API 收件箱 id
  CHATWOOT_ASSIGNEE_ID    可选, 指派给谁
没配 token/收件箱时只写 WARNING 日志, 不影响自动回复主流程。
"""
from __future__ import annotations

import os
import re

import httpx

from app.utils.logger import get_logger

log = get_logger(__name__)


def _cfg():
    return {
        "url": os.getenv("CHATWOOT_URL", "http://127.0.0.1:3000").rstrip("/"),
        "token": os.getenv("CHATWOOT_API_TOKEN", "").strip().strip("'\""),
        "account": os.getenv("CHATWOOT_ACCOUNT_ID", "1"),
        "inbox": os.getenv("CHATWOOT_LEADS_INBOX_ID", "").strip(),
        "assignee": os.getenv("CHATWOOT_ASSIGNEE_ID", "").strip(),
    }


def enabled() -> bool:
    c = _cfg()
    return bool(c["token"] and c["inbox"])


async def _call(client: httpx.AsyncClient, method: str, path: str, **kw):
    c = _cfg()
    r = await client.request(
        method, f"{c['url']}/api/v1/accounts/{c['account']}{path}",
        headers={"api_access_token": c["token"],
                 # chatwoot.env 里 FORCE_SSL=true: 内网 http 不带这个头会被 301 到 https
                 "X-Forwarded-Proto": "https"},
        timeout=15, **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"chatwoot {method} {path} → {r.status_code}: {r.text[:200]}")
    return r.json() if r.content else {}


def _contact_of(d: dict) -> dict:
    p = d.get("payload", d)
    if isinstance(p, dict) and "contact" in p:
        return p["contact"]
    if isinstance(p, list):
        return p[0] if p else {}
    return p


async def _contact_source_id(client, name: str, phone: str | None, identifier: str) -> tuple[int, str]:
    """找到或新建联系人, 返回 (contact_id, 该收件箱下的 source_id)。"""
    c = _cfg()
    inbox = int(c["inbox"])
    found = None
    for q in filter(None, [phone, identifier]):
        res = await _call(client, "GET", "/contacts/search", params={"q": q})
        for ct in res.get("payload", []):
            if (phone and ct.get("phone_number") == phone) or ct.get("identifier") == identifier:
                found = ct
                break
        if found:
            break
    if not found:
        body = {"inbox_id": inbox, "name": name[:100] or (phone or identifier), "identifier": identifier}
        if phone:
            body["phone_number"] = phone
        res = await _call(client, "POST", "/contacts", json=body)
        found = _contact_of(res)
        ci = (res.get("payload") or {}).get("contact_inbox") or {}
        if ci.get("source_id"):
            return found["id"], ci["source_id"]
    for ci in found.get("contact_inboxes") or []:
        if int((ci.get("inbox") or {}).get("id", -1)) == inbox and ci.get("source_id"):
            return found["id"], ci["source_id"]
    res = await _call(client, "POST", f"/contacts/{found['id']}/contact_inboxes", json={"inbox_id": inbox})
    return found["id"], res.get("source_id")


def direct_link(platform: str, phone: str | None) -> str | None:
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    if platform == "whatsapp":
        return f"https://wa.me/{digits}"
    if platform == "zalo":
        local = "0" + digits[2:] if digits.startswith("84") else digits
        return f"https://zalo.me/{local}"
    return None


_PLATFORM_LABEL = {"facebook": "Messenger", "tiktok": "TikTok", "whatsapp": "WhatsApp", "zalo": "Zalo"}
_TYPE_LABEL = {"reseller": "🤝 意向分销商", "customer": "🛒 意向客户"}


async def open_handover(*, platform: str, device_id: str, counterpart: str, phone: str | None,
                        lead_type: str | None, reason: str, history: list[dict],
                        engagement_id: int | None, source_text: str | None = None) -> int | None:
    """开 Chatwoot 会话通知管理员。返回 conversation id (失败返回 None, 绝不抛)。"""
    if not enabled():
        log.warning("chatwoot_not_configured", platform=platform, counterpart=counterpart)
        return None
    pl = _PLATFORM_LABEL.get(platform, platform)
    ident = f"agent:{platform}:" + (re.sub(r"\D", "", phone) if phone else f"{device_id}:{counterpart.lower()}")
    their = [h["t"] for h in history if h.get("r") == "them"][-3:]
    transcript = "\n".join(("我们: " if h.get("r") == "us" else "对方: ") + h.get("t", "") for h in history[-16:])
    link = direct_link(platform, phone)
    note = "\n".join(filter(None, [
        f"【{_TYPE_LABEL.get(lead_type or '', '需要人工跟进')}】{pl} · {counterpart}",
        f"原因: {reason}",
        f"电话: {phone}" if phone else None,
        f"直接联系: {link}" if link else None,
        f"Agent 设备: {device_id}  (自动回复已停止, 对方新消息会转到这里)",
        f"engagement id: {engagement_id}" if engagement_id else None,
        f"\n号码来源帖子:\n{source_text[:400]}" if source_text else None,
        f"\n对话记录:\n{transcript}" if transcript else None,
    ]))
    c = _cfg()
    try:
        async with httpx.AsyncClient() as client:
            contact_id, source_id = await _contact_source_id(client, counterpart, phone, ident)
            body = {"source_id": source_id, "inbox_id": int(c["inbox"]), "contact_id": contact_id,
                    "status": "open", "additional_attributes": {"agent_platform": platform,
                                                                "agent_device": device_id}}
            if c["assignee"]:
                body["assignee_id"] = int(c["assignee"])
            conv = await _call(client, "POST", "/conversations", json=body)
            cid = conv.get("id")
            await _call(client, "POST", f"/conversations/{cid}/messages",
                        json={"content": "\n".join(their) or "(无文字)", "message_type": "incoming"})
            await _call(client, "POST", f"/conversations/{cid}/messages",
                        json={"content": note, "message_type": "outgoing", "private": True})
            labels = ["agent-lead", platform] + ([lead_type] if lead_type else [])
            try:
                await _call(client, "POST", f"/conversations/{cid}/labels", json={"labels": labels})
            except Exception as e:
                log.warning("chatwoot_label_failed", err=str(e)[:120])
        log.info("chatwoot_handover_opened", conversation_id=cid, platform=platform,
                 lead_type=lead_type, engagement_id=engagement_id)
        return cid
    except Exception as e:
        log.error("chatwoot_handover_failed", platform=platform, err=str(e)[:300])
        return None


async def set_message_status(conversation_id: int, message_id: int, status: str,
                             error: str | None = None) -> bool:
    """API 收件箱专用: 把我们代发的那条消息标成 delivered / failed (Chatwoot 界面上显示送达或红色失败)。"""
    if not enabled():
        return False
    body = {"status": status}
    if error:
        body["external_error"] = error[:250]
    try:
        async with httpx.AsyncClient() as client:
            await _call(client, "PATCH", f"/conversations/{conversation_id}/messages/{message_id}", json=body)
        return True
    except Exception as e:
        log.warning("chatwoot_status_failed", conversation_id=conversation_id, message_id=message_id, err=str(e)[:200])
        return False


async def note(conversation_id: int, text: str) -> bool:
    """在会话里加一条私有备注 (只有坐席看得到)。"""
    if not enabled() or not conversation_id:
        return False
    try:
        async with httpx.AsyncClient() as client:
            await _call(client, "POST", f"/conversations/{conversation_id}/messages",
                        json={"content": text[:3000], "message_type": "outgoing", "private": True})
        return True
    except Exception as e:
        log.warning("chatwoot_note_failed", conversation_id=conversation_id, err=str(e)[:200])
        return False


async def forward(conversation_id: int, text: str) -> bool:
    """转人工之后对方又发来的消息 → 追加进同一个会话 (incoming, 会触发未读提醒)。"""
    if not enabled() or not conversation_id or not text.strip():
        return False
    try:
        async with httpx.AsyncClient() as client:
            await _call(client, "POST", f"/conversations/{conversation_id}/messages",
                        json={"content": text[:3000], "message_type": "incoming"})
            # 会话可能已被管理员 resolve, 新消息进来要重新打开
            await _call(client, "POST", f"/conversations/{conversation_id}/toggle_status",
                        json={"status": "open"})
        return True
    except Exception as e:
        log.warning("chatwoot_forward_failed", conversation_id=conversation_id, err=str(e)[:200])
        return False
