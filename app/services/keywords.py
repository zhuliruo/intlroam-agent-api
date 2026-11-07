"""
app/services/keywords.py
统一关键词服务 — 取代 services/tiktok_keywords.py + services/threads_keywords.py

抽样逻辑对两个平台是同一套, 只是参数不同:
  1. 按 language 均分预算 (threads 原本的跨语言行为)
  2. 每种语言内部按 category 配额抽 (tiktok 原本的类别配额行为)
  3. 配额内按 weight 加权
单语言时第 1 步退化成空操作, 所以两边的老行为都被保住了。
"""
import random
from datetime import datetime, timezone

from sqlalchemy import Text, cast, func, select, update
from sqlalchemy.dialects.postgresql import ARRAY, array
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import Device
from app.models.keyword import CATEGORIES, DEFAULT_PLATFORMS, Keyword
from app.services.keyword_phrasing import phrase_search_keyword

# ────────────────────────────────────────────────────────────────
# 配置常量
# ────────────────────────────────────────────────────────────────
TIKTOK_SAMPLE_SIZE  = 25
THREADS_SAMPLE_SIZE = 12
MIN_SAMPLE_SIZE     = 4
MAX_SAMPLE_SIZE     = 40

MIN_KEYWORD_LENGTH = 3
MAX_KEYWORD_LENGTH = 80

VALID_LANGUAGES = {"vi", "th", "ja", "ko", "zh-TW", "en"}

# 设备没有单独配语言时, 各平台的默认语言。None = 用库里所有语言 (跨语言混搜)
# 2026-07-14 全面转英语市场: 新设备 (devices.languages = []) 的默认语言改成 en。
# 已有设备都显式配了 ["en"], 这里只影响以后新注册的机器。
PLATFORM_DEFAULT_LANGUAGES: dict[str, list[str] | None] = {
    "tiktok": ["en"],
    "threads": ["en"],
    "facebook": ["en"],
}

# category 抽样配额 (比例, 会按 sample_size 缩放)。
# 老代码的 CATEGORY_QUOTA 只覆盖 5 类, 另外 6 类 (city_* / activity) 共 46 条词
# 只能走"补齐"路径, 长期被系统性冷落。归并 category 后这里覆盖全部 6 类。
#
# 2026-07-31 重新配比 —— 目的地词转为主力。
# 改之前的问题: 配额是按"类别"给的, 但各类词池大小差 20 倍, 于是每个词的实际
# 下发频率被池子大小反向放大。实测 (近 7 天, en):
#     activity     0.20 配额 / 仅  8 个词 → 每词 12.52 次/天
#     destination  0.30 配额 / 有 170 个词 → 每词  1.33 次/天   (9.4 倍差距)
# use_count 榜前 8 名恰好就是全部 8 个 activity 词, 纯粹是配额算出来的, 不是效果好。
# 新配比把 destination 抬到 0.55 主力位, 同时压掉小池子类别的超额抽取,
# 预期每词频率差从 9.4x 收敛到 ~3x。
# hashtag / generic 目前 en 库里是 0 条, 按用户要求保留占位 (以后确认用不到再删)。
#
# 2026-09-15 转型: 新增 reseller 类别 (找代理的搜索词)。
# 配额 0.06 是按"每词下发率与 connectivity/pain_points 同量级"算的 —— 该类只有
# 5 个词, 配额给高了就是把同样 5 个词刷到烂。名额从 destination 扣: 那类词捞回
# 来的游记/风景照转型后一律判 cold, 是当前性价比最低的一类。
CATEGORY_QUOTA_RATIO: dict[str, float] = {
    "destination":  0.49,   # 0.55 -> 0.49, 让出 0.06 给 reseller
    "connectivity": 0.18,
    "pain_points":  0.12,
    "activity":     0.08,
    "reseller":     0.06,   # 新增
    "hashtag":      0.04,
    "generic":      0.03,
}

# 2026-08-19: Threads 专属配比。全局配比里 destination 占 0.55, 捞回来的是游记/风景照/
# 签证广告 —— 实测 threads 抓的 1298 条帖子里 68.5% 被判 not_travel, 品牌接得上的只有
# ~10%。而 connectivity / pain_points 类的词捞回来的("Rookie mistakes in Japan:
# roaming instead of an eSIM")正是品牌能自然落地的帖型。
#
# **只改 threads**: facebook (76.5%) 和 tiktok (81.1%) 的品牌率已经在目标位, 不该被扰动。
PLATFORM_CATEGORY_QUOTA: dict[str, dict[str, float]] = {
    "threads": {
        "destination":  0.29,   # 0.55 -> 0.35 -> 0.29 (再让 0.06 给 reseller)
        "connectivity": 0.30,   # 0.18 -> 0.30
        "pain_points":  0.20,   # 0.12 -> 0.20
        "activity":     0.08,
        "reseller":     0.06,   # 2026-09-15 新增
        "hashtag":      0.04,
        "generic":      0.03,
    },
}


def category_quota_for(platform: str | None) -> dict[str, float]:
    return PLATFORM_CATEGORY_QUOTA.get((platform or "").lower(), CATEGORY_QUOTA_RATIO)


FORBIDDEN_SUBSTRINGS = ["http://", "https://", "www.", "zalo.me", "t.me/", "@gmail.com"]

# 还没升级的 APK 仍会发老的 category 值 (两套词表各一份), 在入库前折算成统一词表。
# 与 alembic 008 里的 CASE 保持一致。
LEGACY_CATEGORY_MAP: dict[str, str] = {
    "destination_sim": "destination",
    "city_jp": "destination", "city_kr": "destination", "city_sea": "destination",
    "city_tw_cn": "destination", "city_west": "destination",
    "generic_esim": "connectivity",
    "activity_based": "activity",
    "travel": "activity",
    "misc": "generic",
}


def map_legacy_category(category: str | None) -> str:
    if not category:
        return "generic"
    if category in CATEGORIES:
        return category
    return LEGACY_CATEGORY_MAP.get(category, "generic")


class KeywordSubmitError(Exception):
    pass


def _normalize(keyword: str) -> str:
    return " ".join(keyword.strip().lower().split())


def _weighted_sample(pool: list[Keyword], k: int) -> list[Keyword]:
    """从 pool 里按 weight 加权抽 k 个 (不重复)。"""
    if k <= 0:
        return []
    if len(pool) <= k:
        return list(pool)
    selected: list[Keyword] = []
    seen: set[int] = set()
    weights = [max(1, kw.weight) for kw in pool]
    attempts = 0
    while len(selected) < k and attempts < k * 10:
        attempts += 1
        chosen = random.choices(pool, weights=weights, k=1)[0]
        if chosen.id not in seen:
            selected.append(chosen)
            seen.add(chosen.id)
    return selected


def _sample_one_language(
    pool: list[Keyword], budget: int, platform: str | None = None
) -> list[Keyword]:
    """单一语言内部按 category 配额抽样, 不足的从整池补齐。

    platform 决定用哪套配比 (threads 有专属配比, 见 PLATFORM_CATEGORY_QUOTA)。
    """
    quota_map = category_quota_for(platform)
    by_category: dict[str, list[Keyword]] = {}
    for kw in pool:
        by_category.setdefault(kw.category, []).append(kw)

    # 空类别的配额要重新分给还有词的类别, 否则那部分预算会掉进最后的
    # "从整池补齐"路径 —— 那条路径对所有类别一视同仁, 等于把配比又抹平一次。
    # (en 库里 hashtag / generic 是 0 条, 合计 7% 的配额一直在这么漏。)
    live_ratio = {
        cat: quota_map.get(cat, 0.0)
        for cat in CATEGORIES
        if by_category.get(cat)
    }
    ratio_sum = sum(live_ratio.values())
    if ratio_sum > 0:
        live_ratio = {cat: r / ratio_sum for cat, r in live_ratio.items()}

    selected: list[Keyword] = []
    for cat in CATEGORIES:
        bucket = by_category.get(cat)
        if not bucket:
            continue
        quota = max(1, round(live_ratio.get(cat, 0.0) * budget))
        selected.extend(_weighted_sample(bucket, quota))

    # 库里出现了 CATEGORIES 之外的 category (人工塞的), 别让它彻底抽不到
    for cat, bucket in by_category.items():
        if cat not in quota_map and bucket:
            selected.extend(_weighted_sample(bucket, 1))

    if len(selected) < budget:
        chosen_ids = {kw.id for kw in selected}
        remaining = [kw for kw in pool if kw.id not in chosen_ids]
        selected.extend(_weighted_sample(remaining, budget - len(selected)))

    return selected[:budget]


async def resolve_languages(
    db: AsyncSession, device_id: str, platform: str
) -> list[str] | None:
    """设备自己配的语言优先; 没配就用平台默认。None = 不限语言。"""
    row = (await db.execute(
        select(Device.languages).where(Device.device_id == device_id)
    )).scalar_one_or_none()
    if row:
        langs = [l for l in row if l in VALID_LANGUAGES]
        if langs:
            return langs
    return PLATFORM_DEFAULT_LANGUAGES.get(platform)


def _platform_filter(platform: str):
    # cast 是必需的: array(['tiktok']) 默认推断成 varchar[], 与列的 text[] 不匹配,
    # Postgres 会直接报 "operator does not exist: text[] @> character varying[]"。
    return Keyword.platforms.contains(cast(array([platform]), ARRAY(Text)))


async def pick_search_keywords(
    db: AsyncSession,
    device_id: str,
    platform: str,
    sample_size: int,
    languages: list[str] | None = None,
) -> list[dict]:
    """为一次 session 抽 sample_size 个搜索词, 并累加 use_count。"""
    sample_size = max(MIN_SAMPLE_SIZE, min(MAX_SAMPLE_SIZE, sample_size))
    if languages is None:
        languages = await resolve_languages(db, device_id, platform)

    stmt = select(Keyword).where(
        Keyword.enabled.is_(True),
        Keyword.role == "search",
        _platform_filter(platform),
    )
    if languages:
        stmt = stmt.where(Keyword.language.in_(languages))

    pool = list((await db.execute(stmt)).scalars().all())
    if not pool:
        return []

    by_lang: dict[str, list[Keyword]] = {}
    for kw in pool:
        by_lang.setdefault(kw.language, []).append(kw)

    langs = list(by_lang)
    random.shuffle(langs)
    per_lang = max(1, sample_size // len(langs))

    selected: list[Keyword] = []
    for lang in langs:
        selected.extend(_sample_one_language(by_lang[lang], per_lang, platform))

    if len(selected) < sample_size:
        chosen_ids = {kw.id for kw in selected}
        remaining = [kw for kw in pool if kw.id not in chosen_ids]
        selected.extend(_weighted_sample(remaining, sample_size - len(selected)))
    # 先洗牌再截断。老代码是先截断再洗牌 —— 抽多了的时候, 留下来的永远是
    # CATEGORIES 元组里排在前面的类别 (destination 第一, generic 最后),
    # 相当于在配额之外又加了一层隐性偏置。
    random.shuffle(selected)
    selected = selected[:sample_size]

    await db.execute(
        update(Keyword)
        .where(Keyword.id.in_([kw.id for kw in selected]))
        .values(use_count=Keyword.use_count + 1, last_used_at=datetime.now(timezone.utc))
    )
    await db.commit()

    # 下发前扩写成搜索短语 ("tokyo" -> "visit Tokyo", "esim" -> "esim for japan")。
    # 库里存的仍是裸词; 每次抽样随机换说法, 所以同一个词长期能覆盖多种搜法。
    return [
        {
            "id": kw.id,
            "keyword": phrase_search_keyword(kw.keyword, kw.category, kw.language),
            "keyword_base": kw.keyword,   # 排查用: 这条短语是从哪个库里的词扩出来的
            "language": kw.language,
            "category": kw.category,
        }
        for kw in selected
    ]


async def get_target_keywords(
    db: AsyncSession,
    platform: str,
    device_id: str | None = None,
    languages: list[str] | None = None,
) -> list[dict]:
    """评论区扫描用的意图词 (hot/warm)。"""
    if languages is None and device_id is not None:
        languages = await resolve_languages(db, device_id, platform)

    stmt = select(Keyword).where(
        Keyword.enabled.is_(True),
        Keyword.role == "target",
        _platform_filter(platform),
    )
    if languages:
        stmt = stmt.where(Keyword.language.in_(languages))

    rows = (await db.execute(stmt)).scalars().all()
    return [{"keyword": r.keyword, "intent": r.intent or "hot"} for r in rows]


async def find_duplicate(
    db: AsyncSession, keyword: str, language: str, role: str, exclude_id: int | None = None
) -> Keyword | None:
    """唯一键 (lower(keyword), language, role) —— DB 有表达式唯一索引, 这里先查一次
    是为了返回 409/duplicate 而不是抛 IntegrityError。"""
    stmt = select(Keyword).where(
        func.lower(Keyword.keyword) == keyword.strip().lower(),
        Keyword.language == language,
        Keyword.role == role,
    )
    if exclude_id is not None:
        stmt = stmt.where(Keyword.id != exclude_id)
    return (await db.execute(stmt)).scalar_one_or_none()


async def submit_keyword(
    db: AsyncSession,
    keyword: str,
    submitted_by: str,
    platforms: list[str] | None = None,
    role: str = "search",
    category: str = "generic",
    language: str = "en",
    intent: str | None = None,
) -> dict:
    """手机端 Settings 提交。重复时返回 duplicate=True 而不是报错。"""
    if not keyword or not keyword.strip():
        raise KeywordSubmitError("keyword is empty")

    normalized = _normalize(keyword)
    if len(normalized) < MIN_KEYWORD_LENGTH:
        raise KeywordSubmitError(f"keyword too short (min {MIN_KEYWORD_LENGTH})")
    if len(normalized) > MAX_KEYWORD_LENGTH:
        raise KeywordSubmitError(f"keyword too long (max {MAX_KEYWORD_LENGTH})")
    if any(b in normalized for b in FORBIDDEN_SUBSTRINGS):
        raise KeywordSubmitError("keyword contains forbidden content")

    if language not in VALID_LANGUAGES:
        raise KeywordSubmitError(f"unsupported language: {language}")
    if role not in ("search", "target"):
        raise KeywordSubmitError(f"unsupported role: {role}")

    platforms = [p for p in (platforms or DEFAULT_PLATFORMS) if p in ("tiktok", "threads", "facebook")]
    if not platforms:
        raise KeywordSubmitError("platforms is empty")

    category = category if category in CATEGORIES else "generic"

    existing = await find_duplicate(db, normalized, language, role)
    if existing:
        # 已存在的词换个平台提交时, 补进 platforms 而不是当成无事发生
        added = [p for p in platforms if p not in existing.platforms]
        if added:
            existing.platforms = sorted(set(existing.platforms) | set(platforms))
            await db.commit()
            await db.refresh(existing)
        return {
            "ok": True, "duplicate": True, "id": existing.id, "keyword": existing.keyword,
            "platforms": list(existing.platforms), "platforms_added": added,
        }

    row = Keyword(
        keyword=normalized,
        language=language,
        role=role,
        category=category,
        intent=(intent or "hot") if role == "target" else None,
        platforms=sorted(set(platforms)),
        weight=10,
        submitted_by=submitted_by,
        enabled=True,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {
        "ok": True, "duplicate": False, "id": row.id, "keyword": row.keyword,
        "platforms": list(row.platforms), "platforms_added": list(row.platforms),
    }
