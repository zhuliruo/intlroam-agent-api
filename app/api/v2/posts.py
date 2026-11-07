"""帖子端点：POST /analyze_post + POST /report_result"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Literal

import redis.asyncio as redis
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db, get_device_id, get_redis
from app.models.comment_attempt import CommentAttempt
from app.models.daily_stats import DailyDeviceStats
from app.models.device_group import DeviceGroupMembership, DeviceGroupVisit
from app.models.raw_post import RawPost
from app.services.device_guard import record_visit_outcome
from app.services.group_manager import (
    mark_group_crawled, release_group_lock,
)
from app.services.post_analyzer import (
    classify_intent, content_hash, generate_reply, is_own_reply, is_truncated,
)
from app.services.keyword_filter import filter_post
from app.services.post_analyzer import detect_language
from app.services.country_promo import b2b_block, consumer_block, featured_for_country


async def resolve_group_promo(db, platform: str | None, group_id: int | None,
                              intent: str | None) -> str | None:
    """群组国家的主推产品段落 (2026-10-31)。hot = 消费者话术 (可报网站公示价);
    warm = 代理线索, 只点名产品、主打批发、不报价。没有国家/找不到产品 → None (通用话术)。"""
    if (platform or "facebook").lower() not in ("facebook", "instagram") or not group_id:
        return None
    from app.models.group import Group
    country = (await db.execute(
        select(Group.country).where(Group.id == group_id)
    )).scalar_one_or_none()
    f = await featured_for_country(country)
    if not f:
        return None
    return b2b_block(f) if (intent or "").lower() == "warm" else consumer_block(f)


async def resolve_post_language(db, platform: str | None, group_id: int | None,
                                content: str) -> str:
    """决定用哪套 prompt。

    Facebook/Instagram: 以群组的 language_hint 为准 —— 群组是我们自己导入的,
      比猜帖子语言可靠 (英语群里偶尔混一条别的语言, 不该切走整套 prompt)。
    TikTok/Threads: 没有群组概念, 从内容里认 (越南语声调符号是极强的信号)。
    """
    p = (platform or "facebook").lower()
    if p in ("facebook", "instagram") and group_id:
        from app.models.group import Group
        from sqlalchemy import select
        hint = (await db.execute(
            select(Group.language_hint).where(Group.id == group_id)
        )).scalar_one_or_none()
        if hint:
            return "en" if hint.lower().startswith("en") else "vi"
    return detect_language(content or "")
import hashlib
import re
from app.utils.logger import get_logger

# 2026-11-02: 见 analyze_post 里 should_generate 的注释
REPLY_PAUSED_PLATFORMS = {"threads", "reddit"}
CONSUMER_COLD_PLATFORMS = {"facebook", "tiktok"}


def _client_intent(intent: str | None, reply: str | None) -> str | None:
    """回给手机的 intent。手机端 (FacebookWorkflow / TikTokSearchAndReply) 只在
    intent ∈ {hot, warm} 时才发评论; cold 帖配了直客文案时报成 hot, 免得为此重装 APK。
    库里 raw_posts.intent 仍存真实的 cold, 统计不受影响。"""
    if intent == "cold" and reply:
        return "hot"
    return intent

router = APIRouter()
log = get_logger(__name__)


# ============= /analyze_post =============

class AnalyzePostRequest(BaseModel):
    content: str
    platform: str = "facebook"
    group_id: int | None = None
    fb_post_url: str | None = None
    author_name: str | None = None
    # Hotfix 3.11 Step 5: 加 author_user_id + post_created_at
    author_user_id: str | None = None
    post_created_at: datetime | None = None
    # V15: 帖子图片数量, >1 直接 cold (旅游广告通常多图, 个人提问 0-1 图)
    image_count: int | None = None
    # V17: 帖子评论数, ≥5 走 /generate_reply 端点直送 (跳过 LLM 分类)
    comment_count: int | None = None


class AnalyzePostResponse(BaseModel):
    post_id: int
    intent: Literal["hot", "warm", "cold", "competitor", "ignore"]
    confidence: float
    reply: str | None = None
    reasoning: str | None = None
    matched_keywords: list[str] = Field(default_factory=list)
    destination_country: str | None = None
    # predraft: hot/warm 时返回 pending attempt_id, APP 评论后回传 confirm
    attempt_id: int | None = None


@router.post("/analyze_post", response_model=AnalyzePostResponse)
async def analyze_post(
    body: AnalyzePostRequest,
    device_id: str = Depends(get_device_id),
    db: AsyncSession = Depends(get_db),
    rds: redis.Redis = Depends(get_redis),
):
    """
    LLM 分类 + 回复生成。
    幂等：同一 group + content + author 的帖子复用已分析结果。
    """
    # Hotfix 2: 个人主页保护 - group_id 为空说明手机被切回主页
    # Sprint 1: TikTok 没有 group_id 概念, 此保护仅对 FB 生效
    if not body.group_id and (body.platform or "facebook").lower() == "facebook":
        return AnalyzePostResponse(
            post_id=0,
            intent="ignore",
            confidence=1.0,
            reply=None,
            reasoning="no_group_personal_feed",
            matched_keywords=[],
            destination_country=None,
        )
    if not body.content.strip():
        raise HTTPException(status_code=400, detail="content is empty")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Hotfix 3.11 Step 5: 4 层前置去重（节省 LLM token）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    # V17: L1 时间过滤已迁移到 APP 端 (节省 35.9% HTTP 请求)
    # 服务端保留作为兜底, 防止 APP 旧版本不传 post_created_at
    if body.post_created_at:
        post_age = (datetime.now(timezone.utc) - body.post_created_at).total_seconds() / 86400
        if post_age > 7:
            log.info("h311_skip_too_old_fallback", age_days=int(post_age))
            return AnalyzePostResponse(
                post_id=0,
                intent="ignore",
                confidence=1.0,
                reply=None,
                reasoning=f"post_too_old_{int(post_age)}d",
            )

    # L1c: 这条"帖子"是不是我们自己发出去的评论 (2026-09-16)。
    # Threads 把评论当帖子抓, 转型后评论量涨了一个量级, 于是开始回收自己的输出:
    # 实测 35 小时里 50 条逐字命中, 其中 8 条真被回复了 —— 公开 thread 里自己
    # 夸自己。必须在关键词门之前拦, 因为我们自己的文案当然含连接性关键词。
    if await is_own_reply(rds, body.content):
        # 撞上自己发过的内容。如果是在群里撞上的, 说明那条群帖**已经可见** ——
        # 对需审核的群而言这就是"过审了"的证据, 打个标记让发帖闸门放行。
        # (没有这一环, 需审核的群会被永久锁成 7 天一贴, 哪怕早就过审。)
        if body.group_id:
            try:
                await rds.set(f"group_post_seen:{body.group_id}", "1", ex=14 * 24 * 3600)
            except Exception:
                pass
        log.info("skip_self_authored", platform=body.platform,
                 group_id=body.group_id, content_preview=body.content[:60])
        return AnalyzePostResponse(
            post_id=0,
            intent="ignore",
            confidence=1.0,
            reply=None,
            reasoning="self_authored",
        )

    # L2: 关键词预筛选（不含旅游/连接关键词 → ignore）
    # Threads: 关键词搜索已在 APP 端预筛相关性, 且帖子可能是 TH/JA/KO 等
    # vi_keyword_filter 未覆盖的语言 → 跳过此门, 避免误杀 (与 TikTok 同理)
    if (body.platform or "").lower() == "threads":
        from app.services.keyword_filter import filter_post_threads
        passed_keyword, matched_kws = filter_post_threads(body.content)
    else:
        passed_keyword, matched_kws = filter_post(body.content)
    if not passed_keyword:
        log.info("h311_skip_no_keyword", content_preview=body.content[:60])
        return AnalyzePostResponse(
            post_id=0,
            intent="ignore",
            confidence=1.0,
            reply=None,
            reasoning="no_travel_keyword",
        )

    # 这条帖子该用哪套 prompt (vi / en) —— 下面的规则和 LLM 调用都要用
    post_lang = await resolve_post_language(db, body.platform, body.group_id, body.content)

    # L2b: Hotfix 3.16 H4 - 强关键词直判 (跳过 LLM 分类, 节省 token)
    from app.services.keyword_filter import has_strong_competitor_intent, has_strong_hot_intent, has_advertiser_intent, is_domestic_only
    if has_strong_competitor_intent(body.content):
        log.info("h316_h4_skip_competitor", content_preview=body.content[:60])
        return AnalyzePostResponse(
            post_id=0,
            intent="competitor",
            confidence=1.0,
            reply=None,
            reasoning="keyword_competitor_match",
        )

    # L2b2: V17 - 营销广告关键词直判 (VI VU + 国家 / PHẢI CÓ SIM 等)
    # P2: 越南国内目的地黑名单 (TikTok + 越南语 only)
    # 英语市场不适用: 外国人去岘港/富国岛照样需要国际 eSIM, 判 cold 会误杀。
    if (body.platform or "").lower() == "tiktok" and post_lang == "vi":
        is_dom, matched = is_domestic_only(body.content)
        if is_dom:
            log.info("p2_skip_domestic", matched=matched, content_preview=body.content[:60],
                     comment_count=body.comment_count)
            return AnalyzePostResponse(
                post_id=0, intent="cold", confidence=1.0,
                reply=None, reasoning=f"domestic_only_vn:{matched}",
                matched_keywords=[], destination_country=None,
            )
    if has_advertiser_intent(body.content):
        log.info("v17_skip_advertiser", content_preview=body.content[:60])
        return AnalyzePostResponse(
            post_id=0,
            intent="cold",
            confidence=1.0,
            reply=None,
            reasoning="advertiser_marketing_pattern",
        )

    # L2c 已移除: 图片数量不再作为筛选依据。image_count 仍然接收并记录,
    # 但多图不再等同于广告 —— 真实的旅行提问经常配多张照片。

    chash = content_hash(body.content, body.author_name, body.group_id)

    # L3: Redis 24h post_seen 缓存（替代 4h post_attempted）
    seen_key = f"post_seen:{chash}"
    cached_intent = await rds.get(seen_key)
    if cached_intent:
        log.info("h311_cache_hit_redis", chash=chash[:12], intent=cached_intent)
        # 仍走 DB 查询找完整数据返回（保持一致性）

    # V17: L4 author_spam 已删除 (24h 0% 触发, 因 APP 端没传 author_user_id)

    # L5: 内容指纹 24h 重复 → ignore（去除 emoji/空白后 hash）
    normalized = re.sub(r"[^\w\u4e00-\u9fff\u00c0-\u024fà-ỹ]+", "", body.content.lower())
    fingerprint = hashlib.sha256(normalized.encode()).hexdigest()[:16]
    fp_key = f"fingerprint:{fingerprint}"

    # 2026-09-14: content_hash 查重提前到指纹门之前 —— 补发滞留回复必须绕开这道门。
    # 滞留帖 (pending + 文案已生成) 的指纹一定还在这个 24h 缓存里, 原来的顺序会让它
    # 在这里就被判 content_fingerprint_duplicate 返回 ignore, 于是"重新进群把它补发
    # 出去"永远补不上。见 services/stranded_replies.py。
    # 放行只对滞留帖生效, 普通的刷屏重复帖照拦。
    existing = await db.execute(
        select(RawPost).where(RawPost.content_hash == chash).limit(1)
    )
    post = existing.scalar_one_or_none()
    is_stranded_retry = (
        post is not None
        and post.action_status == "pending"
        and bool(post.suggested_reply)
    )

    if await rds.get(fp_key) and not is_stranded_retry:
        log.info("h311_skip_fingerprint_dup", fp=fingerprint)
        return AnalyzePostResponse(
            post_id=0,
            intent="ignore",
            confidence=1.0,
            reply=None,
            reasoning="content_fingerprint_duplicate",
        )
    await rds.set(fp_key, "1", ex=86400)
    if is_stranded_retry:
        log.info("stranded_retry_hit", post_id=post.id, fp=fingerprint)


    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    # 1. /find_input.*type_text查重 — 已分析过的直接返回
    #    (post 已在上面的指纹门处查好, 这里不再重复查一次库)

    if post and post.intent is not None:
        # ─── Hotfix 3.7: 多重去重检查 ───

        # predraft: 检查 post_intent_locked 锁 (别的设备正在评论中)
        try:
            locked_by = await rds.get(f"post_intent_locked:{post.id}")
            if locked_by:
                locked_str = locked_by.decode() if isinstance(locked_by, bytes) else str(locked_by)
                if locked_str != device_id:
                    log.info("dedup_intent_locked", post_id=post.id, locked_by=locked_str)
                    return AnalyzePostResponse(
                        post_id=post.id,
                        intent="ignore",
                        confidence=1.0,
                        reply=None,
                        reasoning="intent_locked_by_other_device",
                    )
        except Exception as e:
            log.warn("intent_lock_check_failed", post_id=post.id, err=str(e))

        # A. 已成功评论过 → 永不再发
        if post.action_status == "replied":
            log.info("dedup_already_replied", post_id=post.id)
            return AnalyzePostResponse(
                post_id=post.id,
                intent="ignore",
                confidence=1.0,
                reply=None,
                reasoning="already_replied_successfully",
            )

        # A2. 补发窗口已过 → 永不再发 (2026-09-14)
        # stranded_replies 把滞留超过 24h 的标成 expired。没有这道短路的话,
        # 原来的逻辑只拦 'replied', expired 帖照样会命中下面的 cache_hit 分支
        # 把老文案再发出去 —— 那正是"在沉底老帖刷评论"的反作弊红旗。
        if post.action_status == "expired":
            log.info("dedup_reply_expired", post_id=post.id)
            return AnalyzePostResponse(
                post_id=post.id,
                intent="ignore",
                confidence=1.0,
                reply=None,
                reasoning="reply_expired",
            )

        # B. 帖子超过 7 天 → 不再评论（避免在陈年老帖刷评论，反作弊红旗）
        if post.captured_at:
            post_age_days = (datetime.now(timezone.utc) - post.captured_at).days
            if post_age_days > 7:
                log.info("dedup_post_too_old", post_id=post.id, age_days=post_age_days)
                return AnalyzePostResponse(
                    post_id=post.id,
                    intent="ignore",
                    confidence=1.0,
                    reply=None,
                    reasoning=f"post_too_old_{post_age_days}d",
                )

        # Hotfix 3.11 Step 5: 删除 4h 锁，被 L3 24h post_seen 取代
        await rds.set(seen_key, post.intent, ex=86400)  # 24h
        log.info("analyze_cache_hit_db", post_id=post.id, intent=post.intent)
        return AnalyzePostResponse(
            post_id=post.id,
            intent=_client_intent(post.intent, post.suggested_reply),  # type: ignore[arg-type]
            confidence=post.intent_confidence or 0.0,
            reply=post.suggested_reply,
            reasoning=post.llm_reasoning,
        )

    # 2. 没有则新建/更新
    if post is None:
        post = RawPost(
            group_id=body.group_id,
            device_id=device_id,
            platform=body.platform,
            fb_post_url=body.fb_post_url,
            content=body.content,
            author_name=body.author_name,
            content_hash=chash,
            # Hotfix 3.11 Step 5
            author_user_id=body.author_user_id,
            post_created_at=body.post_created_at,
        )
        db.add(post)
        await db.flush()

    # 3. 分类
    cls = await classify_intent(
        body.content, body.platform, comment_count=body.comment_count, language=post_lang,
    )
    post.intent = cls.intent
    post.intent_confidence = cls.confidence
    post.llm_reasoning = cls.reasoning
    post.llm_provider = cls.llm_provider
    post.llm_model = cls.llm_model
    post.llm_latency_ms = cls.llm_latency_ms
    post.analyzed_at = datetime.now(timezone.utc)

    # 4. 严格 intent 过滤 (Hotfix 3.11 Step 5)
    #    hot          → generate_reply
    #    warm conf>=0.7 → generate_reply
    #    其他          → 不调 LLM（节省 token）
    reply_obj = None
    # ── PIVOT 2026-09-15: 直客收紧到 hot, 额度让给找代理 ──────────────────
    #
    # 原来 warm(conf>=0.7) 也生成回复。实测两周: facebook warm 1714 条里
    # **只有 1 条**提到 esim/sim/流量/漫游 —— 也就是说我们在 1186 条跟连接
    # 毫无关系的旅游帖下反复提品牌。那既是浪费, 也正是 shadow_ban 率
    # 从 3% 升到 8% 的原因 (同一账号条条念同一个品牌名)。
    #
    # 现在只回两种:
    #   hot  —— 直客直接问 SIM/eSIM/流量 (两周仅 19 条, 但条条对口) → 消费者话术
    #   warm —— **已重定义**为旅行社/导游/本地 SIM 卖家 = 代理线索 → B2B 话术
    # 原来那类"有出行计划的普通旅行者"已在分类器里下沉到 cold, 不再消耗额度。
    # 所以这里形式上还是 hot|warm, 但 warm 的含义完全换了, 别按老理解读。
    # 2026-09-16: 这里**试过**"截断内容把 warm 门槛提到 0.85", 已撤销 ——
    # 回放过去 24h 会拦掉 7 条, 其中 6 条是真代理 (机场接送/半日游/当地导游),
    # 只有 1 条是旅客。0.80 是分类器里"卖旅游服务"的正常档, 不是低置信度;
    # 拿置信度当"内容完整度"的代理指标是错的, 两者不相关。
    # 截断问题的根治在客户端 (v1.39 展开 See more); 在那之前日报会把截断的
    # 线索标出来, 让人工知道那条判断只基于半句话。
    # 2026-11-02 (用户拍板): ① 普通旅行者 (cold, 置信度 ≥0.7) 在 FB/TikTok 重新评论,
    # 走直客话术 —— 只回代理线索时可评论的帖子太少 (5 天 3141 条里只有 575 条),
    # 每机每小时 <1 条, 目标是 ~10 条。代理线索 (warm) 仍走 B2B 话术。
    # ② Threads / Reddit 暂停 —— 手机端取消勾选是主开关, 这里是兜底, 不生成文案。
    plat_l = (body.platform or "").lower()
    should_generate = plat_l not in REPLY_PAUSED_PLATFORMS and (
        cls.intent == "hot"
        or (cls.intent == "warm" and cls.confidence >= 0.7)
        or (cls.intent == "cold" and cls.confidence >= 0.7
            and plat_l in CONSUMER_COLD_PLATFORMS)
    )
    if should_generate:
        # Hotfix 3.11: 传 Redis client 让 generate_reply 注入实时价格
        reply_obj = await generate_reply(
            content=body.content,
            intent=cls.intent,
            destination_country=cls.destination_country,
            redis=rds,
            platform=body.platform,
            comment_count=body.comment_count,
            language=post_lang,
            promo=await resolve_group_promo(db, body.platform, body.group_id, cls.intent),
        )
    else:
        log.info("h311_skip_reply_gen", intent=cls.intent, conf=cls.confidence)
    if reply_obj:
        post.suggested_reply = reply_obj.reply

    log.info(
        "analyzed",
        post_id=post.id, intent=cls.intent, conf=cls.confidence,
        provider=cls.llm_provider, latency_ms=cls.llm_latency_ms,
        has_reply=reply_obj is not None,
    )

    # Hotfix 3.11 Step 5: 写 24h post_seen 缓存
    await rds.set(seen_key, cls.intent, ex=86400)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # predraft: 如果生成了 reply, 预写 pending attempt + Redis 锁 24h
    # 这样即使 APP 评论后崩溃/关机不上报, 也能保证:
    #   1. 服务端知道"已预占用这帖" → 不会被别的机重复分配
    #   2. 1h 后 cleanup task 自动释放
    # APP 评论完成后调 /confirm_attempt 把 pending → success/failure
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    attempt_id: int | None = None
    if reply_obj is not None:
        try:
            pending_attempt = CommentAttempt(
                raw_post_id=post.id,
                device_id=device_id,
                platform=body.platform,
                success=None,                  # NULL = pending (DB 已 ALTER 允许 NULL)
                failure_stage="pending",       # sentinel: 待 confirm
                reply_text=reply_obj.reply,
                attempt_at=datetime.now(timezone.utc),
            )
            db.add(pending_attempt)
            await db.flush()
            attempt_id = pending_attempt.id
            # Redis 锁 24h, 防止其他设备同分到此帖
            await rds.setex(f"post_intent_locked:{post.id}", 86400, device_id)
            log.info("predraft_attempt_created", post_id=post.id, attempt_id=attempt_id, device_id=device_id)
        except Exception as e:
            log.warn("predraft_failed", post_id=post.id, err=str(e))
            attempt_id = None

    return AnalyzePostResponse(
        post_id=post.id,
        intent=_client_intent(cls.intent, reply_obj.reply if reply_obj else None),  # type: ignore[arg-type]
        confidence=cls.confidence,
        reply=reply_obj.reply if reply_obj else None,
        reasoning=cls.reasoning,
        matched_keywords=cls.matched_keywords,
        destination_country=cls.destination_country,
        attempt_id=attempt_id,
    )


# ============= /report_result =============

class CommentAttemptItem(BaseModel):
    raw_post_id: int | None = None
    success: bool
    failure_stage: Literal[
        "find_button", "find_input", "type_text", "find_send",
        "click_send", "verify", "shadow_ban", "fb_rejected", "other"
    , 'already_commented'] | None = None
    failure_reason: str | None = None
    elapsed_ms: int | None = None
    screenshot_url: str | None = None
    reply_text: str | None = None
    # 2026-09-16: 帖子永久链接。客户端评论成功后从 "⋯ → Copy link" 取
    # (无障碍树里没有 URL, 只能复制到剪贴板再粘进输入框读出来)。
    post_url: str | None = None


class ReportResultRequest(BaseModel):
    group_id: int
    platform: str = "facebook"

    # 群组真实性校验结果
    group_verified: bool = True
    group_verify_failure_reason: str | None = None

    # 这次进群到底做了什么 (客户端 TaskResult.action_taken)。
    # 没有它就无法区分"跳过了"和"卡住了" —— 两者都是 attempts=0。
    action_taken: str | None = None

    # 聚合统计
    posts_scanned: int = 0
    posts_analyzed: int = 0
    likes_given: int = 0
    joined_group: bool = False

    # 详细评论尝试
    comment_attempts: list[CommentAttemptItem] = Field(default_factory=list)

    # Hotfix 3.16 H1: 群组旧帖太多触发切群 (连续 5 篇 >7 天)
    group_stale: bool = False


class ReportResultResponse(BaseModel):
    ok: bool = True
    saved_attempts: int
    succeeded: int
    failed: int



# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# V17: /generate_reply 端点 - 评论数 ≥5 直送, 跳过 LLM 分类
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@router.post("/generate_reply", response_model=AnalyzePostResponse)
async def generate_reply_endpoint(
    body: AnalyzePostRequest,
    device_id: str = Depends(get_device_id),
    db: AsyncSession = Depends(get_db),
    rds: redis.Redis = Depends(get_redis),
):
    """
    V17: 评论数 ≥5 的帖子直送, 跳过 LLM 分类阶段, 直接生成评论回复.
    
    设计:
    - 假设是 warm (热门帖通常有讨论价值)
    - 仍走 ADVERTISER + COMPETITOR 拦截 (防营销/竞品)
    - 直接调 generate_reply LLM 生成回复
    - 节省 1 次 LLM 分类调用
    """
    # 基础验证 - Sprint 1: TikTok 跳过
    if not body.group_id and (body.platform or "facebook").lower() == "facebook":
        return AnalyzePostResponse(
            post_id=0, intent="ignore", confidence=1.0,
            reply=None, reasoning="no_group_personal_feed",
            matched_keywords=[], destination_country=None,
        )
    if not body.content.strip():
        raise HTTPException(status_code=400, detail="content is empty")
    
    # 安全拦截 1: 营销广告关键词 (vi vu, phải có sim 等)
    from app.services.keyword_filter import has_advertiser_intent, has_strong_competitor_intent, is_domestic_only
    # P2: 越南国内目的地黑名单 (TikTok only)
    if (body.platform or "").lower() == "tiktok":
        is_dom, matched = is_domestic_only(body.content)
        if is_dom:
            log.info("p2_skip_domestic", matched=matched, content_preview=body.content[:60],
                     comment_count=body.comment_count)
            return AnalyzePostResponse(
                post_id=0, intent="cold", confidence=1.0,
                reply=None, reasoning=f"domestic_only_vn:{matched}",
                matched_keywords=[], destination_country=None,
            )
    if has_advertiser_intent(body.content):
        log.info("v17_gr_skip_advertiser", content_preview=body.content[:60],
                 comment_count=body.comment_count)
        return AnalyzePostResponse(
            post_id=0, intent="cold", confidence=1.0,
            reply=None, reasoning="advertiser_marketing_pattern",
            matched_keywords=[], destination_country=None,
        )
    
    # 安全拦截 1a2: Facebook 关键词门 (2026-09-16)
    # 直送通道原本只给 Threads 补了门, Facebook 一直漏着 —— 于是一条学校义卖帖
    # ("Getting ready for September 8'th event 🇩🇰♥️🇰🇭… more", 68 条评论)
    # 靠"评论数高"直送进来, 被判 warm 0.85 并发了招商评论。
    # 评论数衡量的是热度, 不是相关性。
    if (body.platform or "").lower() == "facebook":
        _passed, _kws = filter_post(body.content)
        if not _passed:
            log.info("v17_gr_skip_no_keyword", content_preview=body.content[:60],
                     comment_count=body.comment_count)
            return AnalyzePostResponse(
                post_id=0, intent="ignore", confidence=1.0,
                reply=None, reasoning="no_travel_keyword_direct",
                matched_keywords=[], destination_country=None,
            )

    # 安全拦截 1b: Threads 排除层 (2026-09-14)
    # /generate_reply 这条"直送"路径对任何平台都没有关键词门 —— Threads 走到这里
    # 时, 连 analyze_post 里那道 filter_post_threads 都绕过了。补上, 否则
    # "只抓到作者头部行"的碎片会从这里溜进 LLM 并被评论出去。
    if (body.platform or "").lower() == "threads":
        from app.services.keyword_filter import threads_exclude_reason
        _excl = threads_exclude_reason(body.content)
        if _excl:
            log.info("v17_gr_skip_threads_excluded", reason=_excl,
                     content_preview=body.content[:60])
            return AnalyzePostResponse(
                post_id=0, intent="ignore", confidence=1.0,
                reply=None, reasoning=f"threads_excluded:{_excl}",
                matched_keywords=[], destination_country=None,
            )

    # 安全拦截 2: SIM/eSIM 竞品销售
    if has_strong_competitor_intent(body.content):
        log.info("v17_gr_skip_competitor", content_preview=body.content[:60],
                 comment_count=body.comment_count)
        return AnalyzePostResponse(
            post_id=0, intent="competitor", confidence=1.0,
            reply=None, reasoning="keyword_competitor_match",
            matched_keywords=[], destination_country=None,
        )
    
    # 内容指纹去重 (避免重复评论)
    import hashlib, re as _re
    normalized = _re.sub(r"[^\w\u4e00-\u9fff\u00c0-\u024fà-ỹ]+", "", body.content.lower())
    fingerprint = hashlib.sha256(normalized.encode()).hexdigest()[:16]
    fp_key = f"fingerprint:{fingerprint}"
    if await rds.get(fp_key):
        log.info("v17_gr_skip_fingerprint_dup", fp=fingerprint)
        return AnalyzePostResponse(
            post_id=0, intent="ignore", confidence=1.0,
            reply=None, reasoning="content_fingerprint_duplicate",
            matched_keywords=[], destination_country=None,
        )
    await rds.set(fp_key, "1", ex=86400)
    
    # 直接调 LLM 生成回复 (假设 warm, 跳过分类)
    from app.services.post_analyzer import generate_reply
    gr_lang = await resolve_post_language(db, body.platform, body.group_id, body.content)
    reply_result = await generate_reply(
        content=body.content,
        intent="warm",
        destination_country=None,
        redis=rds,
        platform=body.platform,
        language=gr_lang,
        promo=await resolve_group_promo(db, body.platform, body.group_id, "warm"),
    )
    
    if not reply_result or not reply_result.reply:
        log.warning("v17_gr_reply_failed", content_preview=body.content[:60])
        return AnalyzePostResponse(
            post_id=0, intent="cold", confidence=0.5,
            reply=None, reasoning="reply_generation_failed",
            matched_keywords=[], destination_country=None,
        )
    
    # 写入数据库
    chash = content_hash(body.content, body.author_name, body.group_id)
    post = RawPost(
        group_id=body.group_id,
        device_id=device_id,
        platform=body.platform,
        fb_post_url=body.fb_post_url,
        content=body.content,
        author_name=body.author_name,
        content_hash=chash,
        intent="warm",  # V17: 直送都假设 warm
        intent_confidence=0.85,
        suggested_reply=reply_result.reply,
        llm_reasoning=f"v17_direct_high_comments_{body.comment_count}",
        llm_provider=reply_result.llm_provider,
        llm_model=reply_result.llm_model,
        llm_latency_ms=reply_result.llm_latency_ms,
        analyzed_at=datetime.now(timezone.utc),
        action_status="pending",
    )
    db.add(post)
    await db.commit()
    await db.refresh(post)
    
    log.info("v17_gr_success", post_id=post.id, 
             comment_count=body.comment_count,
             reply_preview=reply_result.reply[:60])
    
    return AnalyzePostResponse(
        post_id=post.id,
        intent="warm",
        confidence=0.85,
        reply=reply_result.reply,
        reasoning=f"v17_direct_high_comments_{body.comment_count}",
        matched_keywords=[],
        destination_country=None,
    )


@router.post("/report_result", response_model=ReportResultResponse)
async def report_result(
    body: ReportResultRequest,
    device_id: str = Depends(get_device_id),
    db: AsyncSession = Depends(get_db),
    rds: redis.Redis = Depends(get_redis),
):
    """
    手机上报采集和评论结果。
    
    流程：
    1. 写 comment_attempts（详细每次失败/成功）
    2. 更新 raw_posts 的 action_status
    3. 更新 daily_device_stats（聚合）
    4. 更新 groups.last_crawl_at + crawl_count
    5. 释放 Redis 锁
    6. 群组校验失败的话 → mark_group_verify_failed
    """
    saved = 0
    succeeded = 0
    failed = 0

    # 1. 写 comment_attempts
    for att in body.comment_attempts:
        rec = CommentAttempt(
            raw_post_id=att.raw_post_id,
            device_id=device_id,
            platform=body.platform,
            success=att.success,
            failure_stage=att.failure_stage,
            failure_reason=att.failure_reason,
            elapsed_ms=att.elapsed_ms,
            screenshot_url=att.screenshot_url,
            reply_text=att.reply_text,
        )
        db.add(rec)
        saved += 1

        # 永久链接回填。fb_post_url 一直是空的 (无障碍树里拿不到 URL), 日报线索
        # 只能拿群内搜索凑合 —— 而那个实地验证会跳到别的帖, 人工点过去看不到
        # 我们的留言, 就会以为"根本没发出去"。
        if att.post_url and att.raw_post_id:
            from sqlalchemy import update as _sa_update
            await db.execute(
                _sa_update(RawPost)
                .where(RawPost.id == att.raw_post_id)
                .values(fb_post_url=att.post_url[:400])
            )

        if att.success:
            succeeded += 1
            # 同步更新 raw_posts.action_status
            if att.raw_post_id is not None:
                post = await db.get(RawPost, att.raw_post_id)
                if post is not None:
                    post.action_status = "replied"
                    post.replied_at = datetime.now(timezone.utc)
                    post.actual_reply_text = att.reply_text
        else:
            failed += 1
            if att.raw_post_id is not None:
                post = await db.get(RawPost, att.raw_post_id)
                if post is not None and post.action_status == "pending":
                    # 2026-09-15: already_commented 不是失败, 是**我们早就评论过了**。
                    #
                    # 它原来被当普通失败标成 failed, 而去重只拦 replied/expired,
                    # 于是这条帖子会被反复派发: 手机跑过去 → 扫评论 → 看到自己的
                    # IntlRoam 评论 → 再报一次 already_commented → 无限循环。
                    # 近 9 小时全队 17 次失败里它是最大一项, 群 1418 被撞了 11 次。
                    #
                    # 成因链: 评论其实发出去了, 但当时判成失败 (多为 shadow_ban ——
                    # "输入框清空了但评论看不见"), 标 failed → 没被去重挡住 → 再派。
                    # 手机亲眼在评论区看到我们的品牌, 就是"已发出"的铁证, 标 replied
                    # 终结这个循环。
                    if att.failure_stage == "already_commented":
                        post.action_status = "replied"
                        post.replied_at = datetime.now(timezone.utc)
                        log.info("already_commented_marked_replied",
                                 post_id=post.id, device_id=device_id)
                    else:
                        post.action_status = "failed"

    # 2. 更新 daily_device_stats（用 ON CONFLICT 累加，避免并发问题）
    today = datetime.now(timezone.utc).date()
    stmt = pg_insert(DailyDeviceStats).values(
        date=today,
        device_id=device_id,
        platform=body.platform,
        posts_scanned=body.posts_scanned,
        posts_analyzed=body.posts_analyzed,
        comments_attempted=len(body.comment_attempts),
        comments_succeeded=succeeded,
        likes_given=body.likes_given,
        groups_visited=1,
    ).on_conflict_do_update(
        index_elements=["date", "device_id", "platform"],
        set_={
            "posts_scanned": DailyDeviceStats.posts_scanned + body.posts_scanned,
            "posts_analyzed": DailyDeviceStats.posts_analyzed + body.posts_analyzed,
            "comments_attempted": DailyDeviceStats.comments_attempted + len(body.comment_attempts),
            "comments_succeeded": DailyDeviceStats.comments_succeeded + succeeded,
            "likes_given": DailyDeviceStats.likes_given + body.likes_given,
            "groups_visited": DailyDeviceStats.groups_visited + 1,
        },
    )
    await db.execute(stmt)

    # 2b. 落一行访问流水 —— 这次进群做了什么 / 为什么什么都没做。
    #     以前失败原因只做 groups.verify_failures += 1, 字符串直接丢弃, 事后无法区分
    #     "未加入被跳过" / "开不了 FB" / "导航失败" / "正常浏览但群里没内容"。
    db.add(DeviceGroupVisit(
        device_id=device_id,
        group_id=body.group_id,
        platform=body.platform,
        verified=body.group_verified,
        failure_reason=body.group_verify_failure_reason,
        action_taken=body.action_taken,
        posts_scanned=body.posts_scanned,
        posts_analyzed=body.posts_analyzed,
        likes_given=body.likes_given,
        comment_attempts=len(body.comment_attempts),
        joined_group=body.joined_group,
    ))

    # 3. V14: 删除 mark_group_crawled 调用
    # V14 在 get_next_groups 选群组时已立即更新 last_crawl_at + crawl_count
    # 避免双倍 +1 (选时 +1, 上报时 +1)
    # await mark_group_crawled(db, body.group_id)  # V14 移除

    # Hotfix 3.16 H1: 群组旧帖太多, 软标记 (排序时降低优先级, 不冻结)
    if body.group_stale:
        from sqlalchemy import update
        from app.models.group import Group
        await db.execute(
            update(Group)
            .where(Group.id == body.group_id)
            .values(last_stale_at=datetime.now(timezone.utc))
        )
        log.info("group_stale_marked", group_id=body.group_id, device_id=device_id)

    # 3b. 空跑熔断 (2026-09-14): 连续开不了 App 就停派这台机 + 告警。
    #     以前服务端对此毫无反应 —— 4824e1fb 在 09-04 空跑了 159 次群访问,
    #     整天的派发额度全烧在一台打不开 Facebook 的机器上。
    await record_visit_outcome(
        rds,
        device_id=device_id,
        platform=body.platform,
        verified=body.group_verified,
        failure_reason=body.group_verify_failure_reason,
    )

    # 4. 释放 Redis 锁（不管成功失败都释放）
    await release_group_lock(rds, body.group_id, device_id)

    log.info(
        "report_result_saved",
        device_id=device_id, group_id=body.group_id,
        attempts=saved, succeeded=succeeded, failed=failed,
        posts_scanned=body.posts_scanned,
        action=body.action_taken,
        verified=body.group_verified,
        reason=body.group_verify_failure_reason,
    )

    return ReportResultResponse(
        ok=True, saved_attempts=saved,
        succeeded=succeeded, failed=failed,
    )


# ============= /report_group_join (Hotfix 3.9) =============

class ReportGroupJoinRequest(BaseModel):
    group_id: int
    kind: Literal[
        "public", "private_pending", "pending", "already_member"
    ] = "public"
    """kind:
      - 'public'           : 公开群成功加入
      - 'private_pending'  : 私密群申请已发送
      - 'pending'          : 看到"已申请"按钮（之前已申请，等审核）
      - 'already_member'   : 打开群组时发现本来就是成员（不是一次加群尝试）
    """
    success: bool = True

    # 失败时卡在哪一步。没有它就无法区分"FB 限制了这个账号"和"我们的选择器没认出来"。
    #   no_join_button           : 页面上根本没有 Join 按钮
    #   already_member_ui        : 按钮显示已加入 (状态判定矛盾)
    #   questionnaire_unanswered : 入群问卷里没有"是/同意"类选项, 不代用户选
    #   not_verified             : 点了 Join 并走完流程, 但没读到成员态标志
    #   blocked_dialog           : FB 弹了限制/风控提示
    reason: str | None = None


class ReportGroupJoinResponse(BaseModel):
    ok: bool = True
    new_status: str


@router.post("/report_group_join", response_model=ReportGroupJoinResponse)
async def report_group_join(
    body: ReportGroupJoinRequest,
    device_id: str = Depends(get_device_id),
    db: AsyncSession = Depends(get_db),
    rds: redis.Redis = Depends(get_redis),
):
    """手机上报加群事件 → 服务端记录 join_status"""
    from app.models.group import Group
    
    g = await db.get(Group, body.group_id)
    if g is None:
        raise HTTPException(status_code=404, detail="group not found")
    
    # already_member 不是一次加群尝试, 只是手机确认了既有成员身份。
    # 每轮浏览都会上报一次, 若计入 attempt_count 会把计数刷爆并覆盖 joined_at。
    if body.kind != "already_member":
        g.join_attempt_count = (g.join_attempt_count or 0) + 1
        g.last_join_attempt_at = datetime.now(timezone.utc)

    newly_joined = False
    if body.success:
        if body.kind in ("public", "already_member"):
            was_joined = g.join_status == "joined"
            g.join_status = "joined"
            if g.joined_at is None:
                g.joined_at = datetime.now(timezone.utc)
            # 只有 kind=public 且此前不是 joined, 才算一次"新加群"
            newly_joined = body.kind == "public" and not was_joined
        elif body.kind in ("private_pending",):
            # 刚发出申请。已经有别的账号在群里的话不要降级 —— 群级状态是所有设备
            # 共用的, 降下去会连累那台已是成员、本来能正常评论的设备。
            newly_joined = g.join_status not in ("pending_approval", "joined")
            if g.join_status != "joined":
                g.join_status = "pending_approval"
        elif body.kind == "pending":
            # 2026-07-31: 这里以前是无条件 g.join_status = "pending_approval"。
            # 客户端每次落到"申请审核中"页面都会发一次 kind=pending, 于是
            #   A 设备已加入 (群级 joined) → B 设备还在排队 → 群级被拉回 pending_approval
            #   → get_next_groups 把整个群冷冻 24h → A 设备也进不去了。
            # 实测 70 个群处于"一台已 joined、另一台还 pending"的状态, 其中 16 条
            # pending 记录指向的群群级已经是 joined。审核中是**设备**的状态, 不是群的,
            # 下面的 device_group_membership 才是它该待的地方。
            if g.join_status not in ("joined",):
                g.join_status = "pending_approval"
    else:
        # 加群失败 — 失败次数过多就标记 failed
        #
        # 2026-09-26: 加了"有设备已在群里就不标 failed"的守卫 —— 别让已在群里的
        # 设备陪葬。上面的 pending_approval 分支 2026-07-31 就修过同一个毛病
        # (审核中是**设备**的状态不是群的), 但这个 failed 分支漏了。
        #
        # `join_attempt_count` 是**群级累计**, 跨所有设备: 三台机各失败一次这个群
        # 就被判死刑, 而 failed 在 get_next_groups 里是终态, 整个群从此不再派发。
        # 实测被这一枪打中的: 200 个群级 failed 里 188 个 (94%) 其实已有设备加入,
        # 370 对 (设备,群) 被白白锁在外面 —— 包括 2 个阿曼重点群, 所以"重点群
        # 每天巡一次"的规则对它们一直没生效。
        has_member = (await db.execute(
            select(DeviceGroupMembership.id).where(
                and_(
                    DeviceGroupMembership.group_id == g.id,
                    DeviceGroupMembership.status == "joined",
                )
            ).limit(1)
        )).scalar_one_or_none() is not None

        if g.join_attempt_count >= 3 and not has_member:
            g.join_status = "failed"
        elif g.join_attempt_count >= 3 and has_member:
            # 这台机进不去, 但群里有别的机 —— 群是好的, 只是这台不行。
            # 设备级失败已经记在 device_group_membership 里了。
            log.info("join_failed_but_group_has_members",
                     group_id=g.id, device_id=device_id,
                     attempts=g.join_attempt_count)
    
    joins_count = None
    if newly_joined:
        from app.services.group_manager import incr_joins_today
        joins_count = await incr_joins_today(rds, device_id)

    # ── 设备维度的成员状态 (groups.join_status 是群级别的, 多账号共用一批群时不可信) ──
    now = datetime.now(timezone.utc)
    if body.success:
        dev_status = {
            "public": "joined", "already_member": "joined",
            "private_pending": "pending_approval", "pending": "pending_approval",
        }.get(body.kind, "unknown")
    else:
        dev_status = "failed"

    ins = pg_insert(DeviceGroupMembership).values(
        device_id=device_id,
        group_id=body.group_id,
        status=dev_status,
        joined_at=now if dev_status == "joined" else None,
        attempt_count=0 if body.kind == "already_member" else 1,
        last_attempt_at=None if body.kind == "already_member" else now,
        last_reason=body.reason,
    )
    await db.execute(ins.on_conflict_do_update(
        index_elements=["device_id", "group_id"],
        set_={
            "status": dev_status,
            # joined_at 只认第一次, 别被后续上报覆盖
            "joined_at": func.coalesce(
                DeviceGroupMembership.joined_at,
                now if dev_status == "joined" else None,
            ),
            # already_member 只是确认既有成员身份, 不是一次加群尝试
            "attempt_count": DeviceGroupMembership.attempt_count + (
                0 if body.kind == "already_member" else 1
            ),
            "last_attempt_at": (
                DeviceGroupMembership.last_attempt_at if body.kind == "already_member" else now
            ),
            "last_reason": body.reason,
            "updated_at": now,
        },
    ))

    log.info(
        "group_join_reported",
        group_id=body.group_id, device_id=device_id,
        kind=body.kind, success=body.success, reason=body.reason,
        new_status=g.join_status, device_status=dev_status,
        joins_today=joins_count,
    )
    
    return ReportGroupJoinResponse(ok=True, new_status=g.join_status)

