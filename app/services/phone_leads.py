"""phone_leads — 从 agent 采集到的帖子文字里提取电话号码 (2026-11-07)

两个入口:
  1. 手机端直接上报 (POST /api/v2/leads/phones): 手机拿的是**完整**帖子文字,
     而 analyze_post 只收前 500 字 —— 联系方式常在帖子末尾, 只靠服务端会漏。
  2. 后台任务 harvest_raw_posts(): 扫 raw_posts 新行 (含历史回填), 两条路判重靠 phone_e164 唯一。

号码必须能确定国家区号:
  - 带 + 或 00 的国际写法 → 直接解析
  - "WhatsApp 971501234567" 这类不带 + 但紧跟 WhatsApp/Zalo/Viber 等字样的 → 按国际号试
  - 本地写法 (0901 234 567) → 用来源群组的国家 (groups.country); 越南语帖子默认 VN;
    都推不出来就丢弃 (宁缺毋滥 —— 区号猜错等于给陌生人发消息)
  - 只收手机号 (WhatsApp/Zalo 只能加手机号), 固话丢弃

越南 (+84) → phone_leads_vn (Zalo); 其他 → phone_leads_intl (WhatsApp)。
"""
from __future__ import annotations

import asyncio
import re

import phonenumbers
from phonenumbers import PhoneNumberType
from sqlalchemy import text

from app.database import db_context
from app.utils.logger import get_logger

log = get_logger(__name__)

TABLE = {"zalo": "phone_leads_vn", "whatsapp": "phone_leads_intl"}
MAX_PER_TEXT = 5                 # 一条帖子里号码再多也只取前 5 个 (号码墙/广告帖)

# groups.country 用的是标准英文国名 (见 alembic 010)。只列旅行群常见的; 查不到就不给默认区号。
COUNTRY_REGION = {
    "vietnam": "VN", "thailand": "TH", "japan": "JP", "south korea": "KR", "korea": "KR",
    "china": "CN", "hong kong": "HK", "taiwan": "TW", "singapore": "SG", "malaysia": "MY",
    "indonesia": "ID", "philippines": "PH", "cambodia": "KH", "laos": "LA", "myanmar": "MM",
    "india": "IN", "sri lanka": "LK", "nepal": "NP", "maldives": "MV", "bangladesh": "BD",
    "pakistan": "PK", "oman": "OM", "united arab emirates": "AE", "uae": "AE", "saudi arabia": "SA",
    "qatar": "QA", "bahrain": "BH", "kuwait": "KW", "jordan": "JO", "egypt": "EG", "morocco": "MA",
    "turkey": "TR", "türkiye": "TR", "israel": "IL", "georgia": "GE", "armenia": "AM",
    "azerbaijan": "AZ", "uzbekistan": "UZ", "kazakhstan": "KZ", "kenya": "KE", "tanzania": "TZ",
    "south africa": "ZA", "nigeria": "NG", "united kingdom": "GB", "uk": "GB", "ireland": "IE",
    "france": "FR", "italy": "IT", "spain": "ES", "portugal": "PT", "germany": "DE",
    "netherlands": "NL", "belgium": "BE", "switzerland": "CH", "austria": "AT", "greece": "GR",
    "croatia": "HR", "czech republic": "CZ", "czechia": "CZ", "poland": "PL", "hungary": "HU",
    "iceland": "IS", "norway": "NO", "sweden": "SE", "denmark": "DK", "finland": "FI",
    "united states": "US", "usa": "US", "canada": "CA", "mexico": "MX", "brazil": "BR",
    "argentina": "AR", "peru": "PE", "colombia": "CO", "chile": "CL", "australia": "AU",
    "new zealand": "NZ", "fiji": "FJ", "russia": "RU",
}

_VI_CHARS = re.compile(r"[ạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹđĐ]")
_VI_HINT = re.compile(r"\b(zalo|sđt|sdt|lh|liên hệ|hotline)\b", re.I)
# "WhatsApp: 971 50 123 4567" —— 不带 + 但明显是国际号
_APP_PREFIX = re.compile(
    r"(?:whats\s*app|whatsapp|wa\.me/|wa|viber|telegram|zalo|wechat|line|tel|phone|call|mobile|hotline|"
    r"contact|liên hệ|sđt|lh)\W{0,6}(\+?\d[\d\s().-]{7,18}\d)", re.I)
_OK_TYPES = {PhoneNumberType.MOBILE, PhoneNumberType.FIXED_LINE_OR_MOBILE}


def region_for_country(country: str | None) -> str | None:
    return COUNTRY_REGION.get((country or "").strip().lower()) if country else None


def _accept(num: phonenumbers.PhoneNumber) -> bool:
    if not phonenumbers.is_valid_number(num):
        return False
    return phonenumbers.number_type(num) in _OK_TYPES


def extract_phones(content: str, default_region: str | None = None) -> list[tuple[str, str, int, str]]:
    """→ [(e164, region, country_code, raw)]; 已去重, 最多 MAX_PER_TEXT 个。"""
    if not content or not re.search(r"\d{3}", content):
        return []
    region = default_region
    if region is None and (_VI_CHARS.search(content) or _VI_HINT.search(content)):
        region = "VN"
    out: dict[str, tuple[str, str, int, str]] = {}

    def add(num: phonenumbers.PhoneNumber, raw: str) -> None:
        if len(out) >= MAX_PER_TEXT or not _accept(num):
            return
        e164 = phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)
        reg = phonenumbers.region_code_for_number(num) or ""
        out.setdefault(e164, (e164, reg, num.country_code, raw.strip()[:60]))

    # 1) phonenumbers 自带匹配器: 国际写法 (+/00) 不需要 region; 本地写法用推断出的 region
    for m in phonenumbers.PhoneNumberMatcher(content, region or "ZZ", leniency=phonenumbers.Leniency.VALID):
        add(m.number, m.raw_string)
    # 2) "WhatsApp 9715..." 不带 + 的国际号: 先按国际号试, 不行再按默认 region
    for m in _APP_PREFIX.finditer(content):
        raw = m.group(1)
        digits = re.sub(r"\D", "", raw)
        if not 8 <= len(digits) <= 15:
            continue
        for cand, reg in ((("+" + digits) if not raw.startswith("+") else raw, None), (raw, region)):
            try:
                num = phonenumbers.parse(cand, reg)
            except phonenumbers.NumberParseException:
                continue
            if _accept(num):
                add(num, raw)
                break
    return list(out.values())


def channel_for(region: str) -> str:
    return "zalo" if region == "VN" else "whatsapp"


async def upsert_phones(db, content: str, *, default_region: str | None, platform: str | None,
                        device_id: str | None, group_id: int | None, raw_post_id: int | None,
                        author: str | None) -> int:
    """提取并入库, 返回新增条数。已有号码只累计 seen_count (保留第一次的来源)。"""
    found = extract_phones(content, default_region)
    new = 0
    for e164, region, cc, raw in found:
        t = TABLE[channel_for(region)]
        # 片段: 号码前后各 ~250 字, 给打招呼的 LLM 看对方是做什么的
        idx = content.find(raw[:8]) if raw else -1
        snippet = content[max(0, idx - 250): idx + 250] if idx >= 0 else content[:500]
        inserted = (await db.execute(text(
            f"INSERT INTO {t} (phone_e164, country_code, region, raw_match, source_platform, source_device, "
            f"source_group_id, source_raw_post_id, source_author, source_text) "
            f"VALUES (:p, :cc, :rg, :raw, :pf, :dv, :g, :rp, :au, :tx) "
            f"ON CONFLICT (phone_e164) DO UPDATE SET seen_count = {t}.seen_count + 1, last_seen_at = now() "
            f"RETURNING (xmax = 0) AS inserted"),
            {"p": e164, "cc": str(cc), "rg": region or None, "raw": raw, "pf": platform,
             "dv": device_id, "g": group_id, "rp": raw_post_id, "au": (author or "")[:200] or None,
             "tx": snippet})).scalar()
        if inserted:
            new += 1
            log.info("phone_lead_new", channel=channel_for(region), region=region, platform=platform,
                     group_id=group_id, device_id=device_id)
    return new


async def group_region(db, group_id: int | None) -> str | None:
    if not group_id:
        return None
    c = (await db.execute(text("SELECT country FROM groups WHERE id=:g"), {"g": group_id})).scalar()
    return region_for_country(c)


# ─────────────── 后台任务: 扫 raw_posts (含历史回填) ───────────────

INTERVAL_SECONDS = 600
BATCH = 3000
WATERMARK_KEY = "phone_harvest:last_raw_post_id"


async def harvest_once(rds) -> int:
    # uvicorn 多 worker 时每个进程都起了这个循环 → 抢锁, 同一时刻只有一个在扫
    try:
        if not await rds.set("phone_harvest:lock", "1", ex=300, nx=True):
            return 0
        wm = int(await rds.get(WATERMARK_KEY) or 0)
    except Exception:
        return 0
    try:
        return await _harvest_from(rds, wm)
    finally:
        try:
            await rds.delete("phone_harvest:lock")
        except Exception:
            pass


async def _harvest_from(rds, wm: int) -> int:
    total_new = 0
    async with db_context() as db:
        rows = (await db.execute(text(
            "SELECT r.id, r.content, r.platform, r.device_id, r.group_id, r.author_name, g.country "
            "FROM raw_posts r LEFT JOIN groups g ON g.id = r.group_id "
            "WHERE r.id > :wm ORDER BY r.id LIMIT :n"), {"wm": wm, "n": BATCH})).all()
        if not rows:
            return 0
        for r in rows:
            if r.content and re.search(r"\d[\d\s().-]{7,}\d", r.content):
                total_new += await upsert_phones(
                    db, r.content, default_region=region_for_country(r.country), platform=r.platform,
                    device_id=r.device_id, group_id=r.group_id, raw_post_id=r.id, author=r.author_name)
        await db.commit()
    try:
        await rds.set(WATERMARK_KEY, rows[-1].id)
    except Exception as e:
        log.warning("phone_harvest_wm_failed", err=str(e))
    log.info("phone_harvest_batch", scanned=len(rows), new=total_new, last_id=rows[-1].id)
    return len(rows)


async def harvest_raw_posts(rds) -> None:
    """lifespan 里起的后台循环。首次会把历史 raw_posts 分批回填完 (批与批之间让出 1 秒)。"""
    await asyncio.sleep(30)
    while True:
        try:
            n = await harvest_once(rds)
            if n >= BATCH:
                await asyncio.sleep(1)        # 回填中: 紧接着下一批
                continue
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("phone_harvest_failed", err=str(e)[:200])
        await asyncio.sleep(INTERVAL_SECONDS)
