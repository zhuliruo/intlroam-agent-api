"""群组分配服务 — 派发 + Redis 锁 + 真实性校验"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import redis.asyncio as redis
from sqlalchemy import and_, exists, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.group import Group
from app.models.device_group import DeviceGroupMembership
from app.models.device_group_blacklist import DeviceGroupBlacklist
from app.services.stranded_replies import get_retry_group_ids
from app.utils.logger import get_logger

log = get_logger(__name__)
settings = get_settings()

# pending_approval 不是终态: 手机复核后才能发现「审核已通过」。
# 冷却期内不重复派发, 冷却期后放回池子让手机再看一眼。
PENDING_RECHECK_AFTER = timedelta(hours=24)
# 设备级黑名单的有效期: 账号日后恢复访问权限时可自动回流, 不再永久损失群组。
BLACKLIST_TTL = timedelta(days=30)

# ── 设备级 pending 复查退避 (2026-07-31) ──
# 私密群的加入申请多数是管理员从来没批。实测 88 条 pending 里 50 条超过 30 天没变化,
# 最老的 2 个月 18 天, 但每 24h 还会被派发一次: 开群页 → 看到"审核中" → 退出。
# 12d18 一台就有 52 个这样的群, 07-30 的 5 次群访问里 3 次 (60%) 就是这么烧掉的。
# 复查间隔改成按复查次数指数退避 (24h → 48h → 4d → 8d → 16d → 封顶 30d)。
# 不设终态: 管理员哪天批了仍然能被发现, 只是不再天天去敲门。
PENDING_BACKOFF_BASE_HOURS = 24
PENDING_BACKOFF_MAX_HOURS = 24 * 30

# ── 优先群组 (2026-09-15) ────────────────────────────────────────────────
# 业务转型找代理之后, 某些群 (例: 阿曼那批, Rashid 就是从那里主动联系过来的)
# 需要**定期巡一遍**, 不能听天由命地等随机派发轮到。
#
# 用 groups.notes 打标而不是加列: 免 alembic 迁移, 零风险。标记方式:
#   UPDATE groups SET notes = coalesce(notes,'') || ' #priority' WHERE id IN (...);
# 排序里让"打了标且超过 PRIORITY_REVISIT 没巡"的群排最前面。
PRIORITY_TAG = "#priority"
PRIORITY_REVISIT = timedelta(days=10)
# 2026-10-31: 40h -> 10 天 (用户决定)。重点群从 20 个阿曼群扩到 11 国 360 个群
# (阿曼/越南/泰国/新加坡/日本/韩国/中国/土耳其/意大利/法国/西班牙, 按 groups.country
# 打的 #priority)。360 / 10 天 ≈ 每天 36 个, 占全舰队日浏览量 (60~155) 的 25~60%。
# 2026-09-26: 20h -> 40h。重点群从 7 个扩到 54 个 (补进 oman.txt 那批阿曼群)
# 之后, 20h 周期 = 每天要巡 54 个群, 而全舰队 FB 群浏览量只有 60~155 个/天,
# 重点群会吃掉 35~90% 的日预算, 把其余 960 个群挤到几乎不动。40h 占用减半。
# 想再压就继续往上调; 想回到"每天一巡"就改回 20h, 但先看浏览总量够不够。

# 单台手机最多能加入活跃群总数的多大比例 (2026-07-31)。
# 防止所有账号的群列表完全重合 —— 那是最容易被平台串起来的关联特征之一。
# 到顶之后该机只再拿"自己已是成员"的群浏览/评论, 不再加新群。
MAX_MEMBERSHIP_RATIO = 0.80

# 本设备在该群处于 pending_approval 且还没到下次复查时间 → 本轮不派给这台设备。
# (群级 join_status 是所有账号共用的, 拦不住"A 已入群、B 还在排队"这种情况,
#  只有设备级的 device_group_membership 能。)
_PENDING_BACKOFF_NOT_DUE = text(
    "device_group_membership.last_attempt_at > now() - (interval '1 hour' * "
    "LEAST(:pend_max_h, :pend_base_h * POWER(2, "
    "GREATEST(device_group_membership.attempt_count - 1, 0))))"
).bindparams(
    pend_max_h=PENDING_BACKOFF_MAX_HOURS,
    pend_base_h=PENDING_BACKOFF_BASE_HOURS,
)


def _lock_key(group_id: int) -> str:
    return f"group_lock:{group_id}"


async def acquire_group_lock(rds: redis.Redis, group_id: int, device_id: str) -> bool:
    """尝试获取群组锁。SETNX 30 分钟 TTL。
    Hotfix 3.15: 加 cooldown 检查 - 群组在 cooldown 期间不可被任何设备锁定
    """
    # 1. 检查 cooldown
    cd_key = f"group_cooldown:{group_id}"
    if await rds.get(cd_key):
        return False  # 还在 30 分钟冷却期
    # 2. 尝试加锁
    ok = await rds.set(
        _lock_key(group_id),
        device_id,
        ex=settings.group_lock_ttl_seconds,
        nx=True,
    )
    return bool(ok)


async def release_group_lock(rds: redis.Redis, group_id: int, device_id: str) -> bool:
    """释放群组锁。仅当锁是自己持有时才释放（用 Lua 保证原子性）。
    Hotfix 3.15: 释放锁后立刻加 30 分钟 cooldown，防止多设备连续访问同群组。
    """
    lua = """
    if redis.call("get", KEYS[1]) == ARGV[1] then
      redis.call("del", KEYS[1])
      redis.call("set", KEYS[2], "1", "EX", 1800)
      return 1
    else
      return 0
    end
    """
    cd_key = f"group_cooldown:{group_id}"
    result = await rds.eval(lua, 2, _lock_key(group_id), cd_key, device_id)
    return bool(result)


MAX_JOINS_PER_DAY = 30


def _joins_key(device_id: str) -> str:
    from datetime import datetime, timezone
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"joins:{device_id}:{day}"


async def joins_today(rds: redis.Redis, device_id: str) -> int:
    try:
        v = await rds.get(_joins_key(device_id))
        return int(v) if v else 0
    except Exception:
        return 0


async def incr_joins_today(rds: redis.Redis, device_id: str) -> int:
    """加群成功时 +1, 计数 36h 后自动过期。返回今日累计。"""
    key = _joins_key(device_id)
    try:
        n = await rds.incr(key)
        if n == 1:
            await rds.expire(key, 36 * 3600)
        return n
    except Exception:
        return 0


async def get_next_groups(
    db: AsyncSession,
    rds: redis.Redis,
    device_id: str,
    platform: str = "facebook",
    count: int = 5,
    joined_only: bool = False,
) -> list[Group]:
    """
    给指定手机派发要采集的群组。

    规则：
    1. is_active = True
    2. 排除 pending_approval / failed 的群组
    3. 候选顺序完全随机（2026-07-31 改，原来是 last_crawl_at 越早越优先）
    4. 每个群组用 Redis SETNX 加 30 分钟锁，已被其他手机锁的跳过
    5. 一次最多拿 count 个
    6. 单机已加入的活跃群数达到 MAX_MEMBERSHIP_RATIO 后，只再派"本机已是成员"的群

    Hotfix 3.13: 删除 verified_at 强制要求（群组人工维护，默认合法）

    返回：成功锁定的 Group 列表
    """
    if count > 20:
        count = 20

    # 一次多查一些（防止大部分被锁导致返回空），但限制上限
    candidate_limit = max(count * 4, 20)

    # 待补发滞留回复的群 (2026-09-14)。Redis 不通 / 没有滞留时是空集,
    # 排序退化回原来的纯随机。
    retry_ids = await get_retry_group_ids(rds, platform)
    if retry_ids:
        log.info("retry_groups_prioritized",
                 device_id=device_id, platform=platform, count=len(retry_ids))

    # 加群限速: 今天加群已达上限 -> 本轮只派"已加入"的群, 不再让设备去加新群。
    # 评论/浏览已加入的群不受影响。计数器见 report_group_join。
    join_cap_reached = await joins_today(rds, device_id) >= MAX_JOINS_PER_DAY
    if join_cap_reached:
        log.info("join_cap_reached", device_id=device_id, cap=MAX_JOINS_PER_DAY)

    # ── 群组重合度上限 (2026-07-31) ──
    # 老的派发顺序是全局 last_crawl_at 升序, 对每台手机都一样 -> 所有账号沿同一条
    # 路径领群, 最后必然收敛到同一批群里。实测 12d18 已加 241 个、074f 203 个,
    # 而 facebook 活跃群总共才 ~304 个 —— 几个账号的群列表高度重合, 是很明显的
    # 同源特征 (同一批群里反复出现同一组"陌生人"互相点赞评论)。
    # 这里给单机加入的活跃群数封顶; 到顶之后本机只再拿"自己已经是成员"的群来浏览评论,
    # 不再加新群, 把剩下那 20% 留给其他账号去分化。
    active_total = (await db.execute(
        select(func.count(Group.id)).where(
            Group.platform == platform,
            Group.is_active.is_(True),
        )
    )).scalar_one() or 0
    device_joined = (await db.execute(
        select(func.count(DeviceGroupMembership.id))
        .join(Group, Group.id == DeviceGroupMembership.group_id)
        .where(
            DeviceGroupMembership.device_id == device_id,
            DeviceGroupMembership.status == "joined",
            Group.platform == platform,
            Group.is_active.is_(True),
        )
    )).scalar_one() or 0
    membership_cap = int(active_total * MAX_MEMBERSHIP_RATIO)
    membership_cap_reached = active_total > 0 and device_joined >= membership_cap
    if membership_cap_reached:
        log.info("membership_cap_reached", device_id=device_id, platform=platform,
                 joined=device_joined, cap=membership_cap, active_total=active_total)

    # 2026-11-03: 单机"只派已加入的群"开关。账号被 FB 限制加群 (blocked_dialog) 时,
    # 手机端关了 auto_join 也会被派到没加入的群, 每个白开 30-40 秒再跳过。
    # 开: redis-cli set dispatch_joined_only:<device_id> 1   关: redis-cli del ...
    try:
        joined_only_flag = bool(await rds.exists(f"dispatch_joined_only:{device_id}"))
    except Exception:
        joined_only_flag = False
    # 手机端关了"自动加群" (1.91+ 会带 joined_only=1) -> 同样只派已加入的群
    joined_only_flag = joined_only_flag or joined_only
    if joined_only_flag:
        log.info("dispatch_joined_only", device_id=device_id, platform=platform)

    stmt = (
        select(Group)
        .where(
            and_(
                Group.platform == platform,
                Group.is_active.is_(True),
                # 加群限速: 达上限后只保留已加入的群 (join_status='joined')
                *([Group.join_status == "joined"] if join_cap_reached else []),
                # 重合度上限: 达上限后只派"本机已是成员"的群 (设备级, 不是群级 ——
                # 群级 join_status 是所有账号共用的, 拦不住"别的账号加过"这种情况)
                *([exists().where(
                    and_(
                        DeviceGroupMembership.group_id == Group.id,
                        DeviceGroupMembership.device_id == device_id,
                        DeviceGroupMembership.status == "joined",
                    )
                )] if (membership_cap_reached or joined_only_flag) else []),
                # Hotfix 3.13: 删除 verified_at 强制要求（群组人工维护）
                # failed 仍是终态; pending_approval 冷却后可被重新派发,
                # 否则它永远拿不到任务 -> 永远无法上报状态 -> 永远卡在 pending。
                or_(
                    Group.join_status.not_in(["pending_approval", "failed"]),
                    and_(
                        Group.join_status == "pending_approval",
                        or_(
                            Group.last_crawl_at.is_(None),
                            Group.last_crawl_at
                            < datetime.now(timezone.utc) - PENDING_RECHECK_AFTER,
                        ),
                    ),
                ),
                # 黑名单: 排除本机被拉黑的群组 (device_id, group_id), 超过 TTL 的不再算数
                ~exists().where(
                    and_(
                        DeviceGroupBlacklist.group_id == Group.id,
                        DeviceGroupBlacklist.device_id == device_id,
                        DeviceGroupBlacklist.created_at
                        > datetime.now(timezone.utc) - BLACKLIST_TTL,
                    )
                ),
                # 本机在该群的加入申请还在审核中且没到复查时间 → 跳过 (见上方常量注释)
                ~exists().where(
                    and_(
                        DeviceGroupMembership.group_id == Group.id,
                        DeviceGroupMembership.device_id == device_id,
                        DeviceGroupMembership.status == "pending_approval",
                        DeviceGroupMembership.last_attempt_at.is_not(None),
                        _PENDING_BACKOFF_NOT_DUE,
                    )
                ),
            )
        )
        .order_by(
            # 2026-07-31: 改成完全随机。
            # 原来是 (last_crawl_at asc nulls first, id asc) —— 这个顺序对所有手机
            # 是同一个, 于是每台机都从"最久没人碰过的那批群"开头往下走, 路径几乎重合,
            # 长期下来大家加的群就是同一批。随机排序让每台手机各走各的。
            # 重复访问由 Redis 的 30 分钟群锁 + 30 分钟 cooldown 兜底 (见 acquire_group_lock)。
            #
            # 2026-09-14: 随机之前插一层 —— 有滞留待补发回复的群排最前。
            # 这些群里躺着"文案已生成但没发出去"的帖子, 手机重新进群扫到时
            # analyze_post 会免费返回已存的文案 (不再调 LLM)。补发窗口只有 24h,
            # 纯随机的话大概率轮不到。见 services/stranded_replies.py。
            # 优先群组: 打了 #priority 且超过 PRIORITY_REVISIT (10 天) 没巡的, 排在所有人前面。
            # 放在 retry_ids 之前 —— 补发是"别浪费已花的钱", 优先群是
            # "别错过今天的代理线索", 后者更要紧。
            and_(
                Group.notes.ilike(f"%{PRIORITY_TAG}%"),
                or_(
                    Group.last_crawl_at.is_(None),
                    Group.last_crawl_at < datetime.now(timezone.utc) - PRIORITY_REVISIT,
                ),
            ).desc(),
            *([Group.id.in_(retry_ids).desc()] if retry_ids else []),
            func.random(),
        )
        .limit(candidate_limit)
    )

    result = await db.execute(stmt)
    candidates = result.scalars().all()

    locked: list[Group] = []
    for g in candidates:
        if len(locked) >= count:
            break
        ok = await acquire_group_lock(rds, g.id, device_id)
        if ok:
            # V14: 立即更新 last_crawl_at, 即使手机异常退出也不反复选
            # (相当于"开始浏览就标记时间", 不等手机上报)
            g.last_crawl_at = datetime.now(timezone.utc)
            g.crawl_count = (g.crawl_count or 0) + 1
            locked.append(g)
    
    # V14: 提交所有 last_crawl_at 更新 (一次性 commit, 效率高)
    if locked:
        await db.commit()

    if not locked:
        log.warning("no_groups_available",
                    device_id=device_id, platform=platform,
                    candidates=len(candidates))
    else:
        log.info("groups_assigned",
                 device_id=device_id, platform=platform,
                 count=len(locked), candidates_examined=len(candidates))

    return locked


async def mark_group_crawled(db: AsyncSession, group_id: int) -> None:
    """更新群组的最后采集时间和次数（在 report_result 时调用）"""
    g = await db.get(Group, group_id)
    if g is None:
        log.warning("mark_crawled_missing_group", group_id=group_id)
        return
    g.last_crawl_at = datetime.now(timezone.utc)
    g.crawl_count = (g.crawl_count or 0) + 1


async def mark_group_verified(db: AsyncSession, group_id: int) -> None:
    """手机首次访问时校验通过 → 标记可派发"""
    g = await db.get(Group, group_id)
    if g is None:
        return
    if g.verified_at is None:
        g.verified_at = datetime.now(timezone.utc)
        log.info("group_verified", group_id=group_id, name=g.name)


async def mark_group_verify_failed(db: AsyncSession, group_id: int) -> None:
    """校验失败（疑似个人主页等）→ 累计失败次数（仅记录，不自动停用）

    Hotfix 3.5: 不再因 verify_failures 自动停用群组。
    自动淘汰由 hotfix 3.4 的 group_health 服务负责（14 天 + fb_rejected 80%）。
    verify_failures 字段保留做统计用。
    """
    g = await db.get(Group, group_id)
    if g is None:
        return
    g.verify_failures = (g.verify_failures or 0) + 1
    log.info("group_verify_failed",
             group_id=group_id, failures=g.verify_failures)
