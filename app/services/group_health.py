"""群组健康度服务 (Hotfix 3.4)

核心评分逻辑：
- 14 天窗口内的拒绝率 + hot/warm 产出率
- 加群 < 3 天 → 给宽限期，拒绝率不计入
- 满足条件自动 quarantine
- 30 天后自动重激活
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.group import Group
from app.models.raw_post import RawPost
from app.models.comment_attempt import CommentAttempt

log = logging.getLogger(__name__)


# ─── 评分常量（基于业务洞察）───
GRACE_PERIOD_DAYS = 3              # 加群 < 3 天给宽限期，不计入拒绝率
HEALTH_WINDOW_DAYS = 14            # 14 天评分窗口
QUARANTINE_DURATION_DAYS = 30      # 30 天 quarantine 后自动重激活

# 拒绝率规则（双轴）
MIN_ATTEMPTS_FOR_REJECTION_RULE = 20
REJECTION_RATE_THRESHOLD = 0.80    # 80% 以上拒绝率 → quarantine

# 产出效率规则
MIN_POSTS_FOR_HOTWARM_RULE = 200
HOTWARM_RATIO_THRESHOLD = 0.005    # 0.5% 以下 → quarantine


async def compute_group_health(db: AsyncSession, group_id: int) -> dict:
    """计算单个群组的健康度，必要时自动 quarantine
    
    返回字典含：
      health_score: 0-100
      action: "quarantined" / "skipped" / "ok"
      reason: 文字描述
    """
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=HEALTH_WINDOW_DAYS)
    
    # 取群组
    grp = (await db.execute(
        select(Group).where(Group.id == group_id)
    )).scalar_one_or_none()
    
    if grp is None:
        return {"action": "skipped", "reason": "group_not_found"}
    
    # 已经 quarantined 的群组：检查是否到了 30 天可以重激活
    if grp.quarantined_at is not None:
        days_since_quarantine = (now - grp.quarantined_at).days
        if days_since_quarantine >= QUARANTINE_DURATION_DAYS:
            await db.execute(
                update(Group)
                .where(Group.id == group_id)
                .values(
                    quarantined_at=None,
                    quarantine_reason=None,
                    is_active=True,
                    last_health_check_at=now,
                )
            )
            await db.commit()
            log.info(f"group {group_id} auto-unquarantined after {days_since_quarantine} days")
            return {"action": "unquarantined", "reason": f"served_{QUARANTINE_DURATION_DAYS}d_quarantine"}
        return {"action": "skipped", "reason": "still_in_quarantine"}
    
    # 加群成熟度检查（< 3 天给宽限期）
    if grp.joined_at is None:
        # 旧群组没记录 joined_at，用 created_at 兜底
        joined_at = grp.created_at
    else:
        joined_at = grp.joined_at
    
    days_since_joined = (now - joined_at).days
    is_in_grace_period = days_since_joined < GRACE_PERIOD_DAYS
    
    # ━━━ 计算 14 天窗口的拒绝率 ━━━
    # 注意：fb_rejected 才算"拒绝"，find_input/find_button 等是 selector 问题不算
    rejection_query = select(func.count(CommentAttempt.id)).where(
        CommentAttempt.attempt_at >= window_start,
        CommentAttempt.failure_stage == "fb_rejected",
        CommentAttempt.raw_post_id.in_(
            select(RawPost.id).where(RawPost.group_id == group_id)
        ),
    )
    rejected_count = (await db.execute(rejection_query)).scalar() or 0
    
    total_attempts_query = select(func.count(CommentAttempt.id)).where(
        CommentAttempt.attempt_at >= window_start,
        CommentAttempt.raw_post_id.in_(
            select(RawPost.id).where(RawPost.group_id == group_id)
        ),
    )
    total_attempts = (await db.execute(total_attempts_query)).scalar() or 0
    
    # ━━━ 计算 14 天窗口的 hot/warm 产出 ━━━
    hotwarm_query = select(func.count(RawPost.id)).where(
        RawPost.group_id == group_id,
        RawPost.captured_at >= window_start,
        RawPost.intent.in_(("hot", "warm")),
    )
    hotwarm_count = (await db.execute(hotwarm_query)).scalar() or 0
    
    total_posts_query = select(func.count(RawPost.id)).where(
        RawPost.group_id == group_id,
        RawPost.captured_at >= window_start,
    )
    total_posts = (await db.execute(total_posts_query)).scalar() or 0
    
    # ━━━ 决策：是否 quarantine ━━━
    quarantine_reason = None
    
    # 规则 1: 拒绝率太高（不在宽限期 + 样本足够 + 拒绝率 > 80%）
    if (
        not is_in_grace_period
        and total_attempts >= MIN_ATTEMPTS_FOR_REJECTION_RULE
        and rejected_count / total_attempts > REJECTION_RATE_THRESHOLD
    ):
        quarantine_reason = "fb_rejection_high"
    
    # 规则 2: 产出效率太低（样本足够 + hotwarm 比例 < 0.5%）
    elif (
        total_posts >= MIN_POSTS_FOR_HOTWARM_RULE
        and hotwarm_count / total_posts < HOTWARM_RATIO_THRESHOLD
    ):
        quarantine_reason = "hotwarm_low"
    
    # ━━━ 计算 health_score（0-100，运营查看用）━━━
    health_score = _compute_score(
        rejected_count, total_attempts, hotwarm_count, total_posts, is_in_grace_period,
    )
    
    # ━━━ 写回 DB ━━━
    update_values = {
        "health_score": health_score,
        "last_health_check_at": now,
    }
    
    if quarantine_reason:
        update_values["quarantined_at"] = now
        update_values["quarantine_reason"] = quarantine_reason
        update_values["is_active"] = False
        log.warning(
            f"group {group_id} ({grp.external_id}) quarantined: "
            f"reason={quarantine_reason} "
            f"score={health_score} "
            f"rejected={rejected_count}/{total_attempts} "
            f"hotwarm={hotwarm_count}/{total_posts}"
        )
    
    await db.execute(
        update(Group).where(Group.id == group_id).values(**update_values)
    )
    await db.commit()
    
    return {
        "action": "quarantined" if quarantine_reason else "ok",
        "reason": quarantine_reason or "healthy",
        "health_score": health_score,
        "rejected": rejected_count,
        "total_attempts": total_attempts,
        "hotwarm": hotwarm_count,
        "total_posts": total_posts,
        "in_grace_period": is_in_grace_period,
    }


def _compute_score(
    rejected: int,
    total_attempts: int,
    hotwarm: int,
    total_posts: int,
    is_in_grace_period: bool,
) -> int:
    """0-100 健康度评分，越高越健康
    
    评分公式：
    - 50 分：评论成功率（非拒绝 / 总尝试）
    - 50 分：内容质量（hot/warm 产出率）
    - 宽限期内的群组按基础分 60 算（避免被误判）
    """
    if is_in_grace_period:
        return 60  # 宽限期默认中等分
    
    # 评论健康分 0-50
    if total_attempts == 0:
        comment_score = 25  # 没数据 → 中位
    else:
        rejection_rate = rejected / total_attempts
        comment_score = int(50 * max(0, 1 - rejection_rate))
    
    # 内容质量分 0-50
    if total_posts == 0:
        content_score = 25  # 没数据 → 中位
    elif total_posts < 50:
        # 样本太少，内容分按 hotwarm 数量给（每个 hotwarm 给 10 分上限 50）
        content_score = min(50, hotwarm * 10)
    else:
        # 样本足够，按比例（hotwarm 率 1% = 10 分，2% = 20 分，5% = 50 分）
        ratio = hotwarm / total_posts
        content_score = min(50, int(ratio * 1000))
    
    return comment_score + content_score


async def compute_all_groups_health(db: AsyncSession) -> dict:
    """对所有需要评分的群组跑一遍健康度
    
    返回统计：{ checked, quarantined, unquarantined, skipped, ok }
    """
    # 取所有 active=true 的群组（包括 quarantined 的，因为可能要 unquarantine）
    groups = (await db.execute(
        select(Group.id).where(Group.is_active.is_(True) | Group.quarantined_at.isnot(None))
    )).scalars().all()
    
    stats = {
        "checked": 0,
        "quarantined": 0,
        "unquarantined": 0,
        "skipped": 0,
        "ok": 0,
    }
    
    for group_id in groups:
        try:
            result = await compute_group_health(db, group_id)
            stats["checked"] += 1
            action = result.get("action", "skipped")
            if action == "quarantined":
                stats["quarantined"] += 1
            elif action == "unquarantined":
                stats["unquarantined"] += 1
            elif action == "ok":
                stats["ok"] += 1
            else:
                stats["skipped"] += 1
        except Exception as e:
            log.exception(f"compute_group_health failed for group {group_id}: {e}")
            stats["skipped"] += 1
    
    log.info(f"group health check summary: {stats}")
    return stats
