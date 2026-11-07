"""
alerting — 运维告警

复用 daily_report.py 那套 Brevo SMTP relay 发告警邮件。
没配 SMTP 凭据时自动降级成"只写 WARNING 日志", 绝不让调用方失败 ——
告警链路本身挂掉不该影响业务。

Redis 去重: 同一个 alert_key 在 dedup_ttl 内只发一封, 防止一台坏设备
每分钟一封把邮箱刷爆。
"""
from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.mime.text import MIMEText
from email.utils import formatdate

import redis.asyncio as redis

from app.config import get_settings
from app.utils.logger import get_logger

log = get_logger(__name__)
settings = get_settings()

# 同一个告警的静默期。设备类故障一般要人工去现场处理, 6 小时提醒一次足够。
DEFAULT_DEDUP_TTL = 6 * 3600


def _send_sync(subject: str, body: str, to_email: str) -> None:
    """阻塞式发信 —— 由 send_alert 丢进线程池, 不占事件循环。"""
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from
    msg["To"] = to_email
    msg["Date"] = formatdate(localtime=True)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as server:
        server.starttls(context=ssl.create_default_context())
        server.login(settings.smtp_user, settings.smtp_password)
        server.sendmail(settings.smtp_from, [to_email], msg.as_string())


async def send_alert(
    rds: redis.Redis | None,
    alert_key: str,
    subject: str,
    body: str,
    dedup_ttl: int = DEFAULT_DEDUP_TTL,
) -> bool:
    """
    发一条告警。返回 True = 这轮真的发出去了邮件。

    alert_key 决定去重粒度, 建议带上设备/平台, 例如
    "fb_not_open:<device_id>" —— 不同设备各自独立告警。
    """
    # 1. 去重。Redis 不可用时不拦截 (宁可重复也不漏报)。
    if rds is not None:
        try:
            first = await rds.set(f"alert_sent:{alert_key}", "1", ex=dedup_ttl, nx=True)
            if not first:
                log.info("alert_deduped", alert_key=alert_key)
                return False
        except Exception as e:
            log.warning("alert_dedup_failed", alert_key=alert_key, err=str(e))

    # 2. 无论能不能发邮件, 先落一条结构化日志 —— journalctl 里永远查得到。
    log.warning("ALERT", alert_key=alert_key, subject=subject, detail=body)

    # 3. 发邮件 (没配就到此为止)
    if not (settings.smtp_user and settings.smtp_password and settings.alert_email):
        log.warning("alert_email_not_configured", alert_key=alert_key)
        return False

    try:
        await asyncio.to_thread(_send_sync, subject, body, settings.alert_email)
        log.info("alert_email_sent", alert_key=alert_key, to=settings.alert_email)
        return True
    except Exception as e:
        log.error("alert_email_failed", alert_key=alert_key, err=str(e))
        return False


# ────────────────────────────────────────────────────────────────
# 手机端通知
#
# 出问题的那台机自己弹一下, 比邮件更直接 —— 但要 APK 配合才看得见。
# 服务端这半边先备好: 告警时把一句话塞进 Redis, 手机下次调 /next_groups
# 就会在响应里拿到 `notice` 字段。
#
# APK 侧需要做的 (还没做):
#   NextGroupsResponse 加一个 `notice: String?`, 非空时用
#   NotificationManager.notify() 发一条 (或 AgentForegroundService
#   直接改前台通知的文案)。读一次就没了, 不会反复弹。
# ────────────────────────────────────────────────────────────────

# 通知的保质期。设备可能几分钟才拉一次, 但隔夜的故障提示已经没意义。
NOTICE_TTL = 3600


def _notice_key(device_id: str) -> str:
    return f"device_notice:{device_id}"


async def push_device_notice(rds: redis.Redis, device_id: str, text: str) -> None:
    """给某台设备留一条待显示的通知 (覆盖旧的 —— 只留最新那条)。"""
    try:
        await rds.set(_notice_key(device_id), text, ex=NOTICE_TTL)
        log.info("device_notice_pushed", device_id=device_id, text=text)
    except Exception as e:
        log.warning("device_notice_push_failed", device_id=device_id, err=str(e))


async def pop_device_notice(rds: redis.Redis, device_id: str) -> str | None:
    """读走并删除 —— 同一条通知只下发一次。"""
    try:
        key = _notice_key(device_id)
        async with rds.pipeline(transaction=True) as pipe:
            pipe.get(key)
            pipe.delete(key)
            got, _ = await pipe.execute()
    except Exception:
        return None
    if not got:
        return None
    return got.decode() if isinstance(got, bytes) else str(got)
