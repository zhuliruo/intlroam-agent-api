"""quota — 手机每轮开工前问一次: 今天哪些平台的活已经干满了 (2026-11-11)

用户要求: FB / TikTok 发帖评论到了日限额就不再跑 (不再刷、不再养号), WhatsApp / Zalo 打招呼
到了日限额就不再发新的; agent 保持在线, 客户回复照常即时处理 (通知内联回复 + Chatwoot 同步)。

各平台的限额口径沿用原来各自的闸门, 这里只是汇总, 不另立一套:
  - facebook  评论: comment_attempts 今日尝试数 (UTC 自然日) vs MAX_DAILY_COMMENTS_FB —— 和 next_groups 闸门同一函数
              发帖: 个人帖 1/天、群帖 DEVICE_DAILY_GROUP_POSTS/天 (只报数, 不参与 done 判断:
                    群帖受可发的群数量限制, 很多机器一天凑不满, 拿它当条件 FB 就永远停不下来;
                    而每个 FB 阶段开头就会先尝试发帖, 评论跑满时发帖早已试过)
  - tiktok    评论: tiktok_sessions 今日 comments_posted + replies_posted vs TIKTOK_DAILY_COMMENTS (默认 50, 11-11 用户由 30 调到 50)
              (TikTok 以前没有日上限, 只靠 session 时长; 近几天单机 0-31 条/天)
              发帖: device_post_history 今日成功数 (只报数, 理由同上)
  - whatsapp / zalo  打招呼: outreach_cap Redis 计数 vs DAILY_GREET_CAP —— 和 /outreach/next 同一个 key;
              受限暂停 (outreach_pause) 也算 done (反正发不了)
done=true 的平台手机当天跳过; 网络失败时手机按"没满"处理 (保持旧行为)。
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import redis.asyncio as redis
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.deps import get_db, get_device_id, get_redis
from app.services.device_guard import comment_attempts_today, daily_comment_limit
from app.utils.logger import get_logger

router = APIRouter()
log = get_logger(__name__)


def tiktok_daily_comments() -> int:
    try:
        return int(os.getenv("TIKTOK_DAILY_COMMENTS", "50"))
    except ValueError:
        return 50


@router.get("/quota_status")
async def quota_status(device_id: str = Depends(get_device_id), db: AsyncSession = Depends(get_db),
                       rds: redis.Redis = Depends(get_redis), settings: Settings = Depends(get_settings)):
    from app.api.v2.outreach import DAILY_GREET_CAP, _greet_cap_key, _pause_key
    from app.api.v2.posting import DEVICE_DAILY_GROUP_POSTS

    now = datetime.now(timezone.utc)
    day0 = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    out: dict = {"server_time": now.isoformat()}

    # Facebook
    fb_used = await comment_attempts_today(db, device_id, "facebook")
    fb_cap = daily_comment_limit(settings, "facebook")
    live = "(success = true OR (success IS NULL AND posted_at > NOW() - INTERVAL '5 minutes'))"
    personal = (await db.execute(text(
        f"SELECT count(*) FROM personal_posts WHERE device_id=:d AND posted_at >= :s AND {live}"),
        {"d": device_id, "s": day0})).scalar() or 0
    group = (await db.execute(text(
        f"SELECT count(*) FROM group_posts WHERE device_id=:d AND posted_at >= :s AND {live}"),
        {"d": device_id, "s": day0})).scalar() or 0
    out["facebook"] = {"done": fb_used >= fb_cap, "comments": fb_used, "comments_cap": fb_cap,
                       "personal_posts": personal, "personal_cap": 1,
                       "group_posts": group, "group_cap": DEVICE_DAILY_GROUP_POSTS}

    # TikTok
    tt = (await db.execute(text(
        "SELECT coalesce(sum(coalesce(comments_posted,0) + coalesce(replies_posted,0)),0) "
        "FROM tiktok_sessions WHERE device_id=:d AND started_at >= :s"), {"d": device_id, "s": day0})).scalar() or 0
    tt_posts = (await db.execute(text(
        "SELECT count(*) FROM device_post_history WHERE device_id=:d AND platform='tiktok' "
        "AND success = TRUE AND posted_at >= date_trunc('day', now())"), {"d": device_id})).scalar() or 0
    tt_cap = tiktok_daily_comments()
    out["tiktok"] = {"done": int(tt) >= tt_cap, "comments": int(tt), "comments_cap": tt_cap, "posts": tt_posts}

    # WhatsApp / Zalo 打招呼
    for ch, cap in DAILY_GREET_CAP.items():
        try:
            used = int(await rds.get(_greet_cap_key(device_id, ch)) or 0)
            paused = bool(await rds.get(_pause_key(device_id, ch)))
        except Exception:
            used, paused = 0, False
        out[ch] = {"done": used >= cap or paused, "greeted": used, "cap": cap, "paused": paused}

    log.info("quota_status", device_id=device_id,
             done=[k for k, v in out.items() if isinstance(v, dict) and v.get("done")])
    return out
