"""按群组国家定制发帖/评论 —— 每个国家一款主推产品 + intlroam.com 实时公示价 (2026-10-31)

为什么: 群组发帖和评论原来全是通用话术 ("plans start at $0.99", "~$3-5" 这类
写死在 prompt 里的区间价)。用户要求按群组国家主推一款具体产品 (例: 泰国群推
Thailand (Local DTAC) eSIM Data + Calls), 并且**提到价格就必须是网站公示价**。

价格来源: api.intlroam.com/items/bundles|products —— 就是网站自己读的那套 Directus
公开接口, price_sell 与产品页显示一致 (实测 DTAC $6.31)。不再用 price_client.py
(它指向 Tailscale 上的旧 Directus, 早已不通)。进程内缓存 6h —— 改价后最多 6h 生效,
而且 CF 边缘本身也缓存 /items/* (见 intlroam-api-items-edge-cache)。

主推产品: FEATURED_BUNDLES 里手工指定的优先群国家; 其余国家自动挑"单国 bundle 里
每 GB 最便宜的那个" (与网站排序同一个指标 ref_price_7d)。识别不出国家的群
(Europe/Caribbean/others) 和找不到单国 bundle 的国家返回 None, 走原来的通用话术。

招商 (B2B) 话术**不报价**: 公开报价会被同行压价、帖子会被当广告举报
(见 posting.B2B_FB_FACTS)。B2B 只允许点名主推产品, 批发价私下谈。
"""
from __future__ import annotations

import re
import time

import httpx

from app.utils.logger import get_logger

log = get_logger(__name__)

API = "https://api.intlroam.com/items"
SITE = "https://intlroam.com/esim/"
CACHE_TTL = 6 * 3600

# 优先群国家的主推 bundle (bundles.id)。改主推产品只改这里。
# 选择依据 (2026-10-31): 有本地运营商卡的优先本地卡, 其余取网站排序第一的单国 bundle。
FEATURED_BUNDLES: dict[str, int] = {
    "Thailand": 109,      # Thailand (Local DTAC) eSIM Data + Calls —— 用户指定
    "Vietnam": 111,       # Vietnam (Local Viettel)
    "Japan": 135,         # Japan (Local DOCOMO)
    "South Korea": 113,   # South Korea (Local LG U+) —— 真不限量; 便宜档是 #115 Promo
    "Singapore": 84,      # Singapore
    "China": 118,         # China (China Mobile)
    "Turkey": 30,         # Turkey
    "Italy": 122,         # Europe (33 Countries) —— 意/法/西没有单国 bundle
    "France": 122,
    "Spain": 122,
    "Europe": 122,        # notes 里的 "Europe" 多国群
    "Oman": 8,            # Asia & Europe (54 Countries, including Oman) —— 阿曼代理一直买这款
}
EUROPE_FALLBACK = (122, 34, 93, 66, 85)   # EU 33 / 38 / 41 / 42 / 49

# 主推产品是本地运营商卡的国家 —— 宣传语主打"本地网络" (2026-10-31 用户要求)。
# 只在主推的正是这张本地卡时生效 (bundle id 对得上), 换成漫游卡就自动不再这么说。
LOCAL_NETWORK: dict[str, tuple[int, str]] = {
    "Japan": (135, "DOCOMO"),
    "South Korea": (113, "LG U+"),
    "Thailand": (109, "DTAC"),
    "Vietnam": (111, "Viettel"),
}

_cache: dict[str, tuple[float, object]] = {}


async def _get(path: str, params: dict) -> list[dict] | None:
    key = path + repr(sorted(params.items()))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]  # type: ignore[return-value]
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.get(f"{API}/{path}", params=params)
            r.raise_for_status()
            data = r.json().get("data") or []
    except Exception as e:
        log.warning("country_promo_fetch_failed", path=path, err=str(e)[:120])
        # 拉取失败时沿用过期缓存, 总比整段降级成通用话术强
        return hit[1] if hit else None  # type: ignore[return-value]
    _cache[key] = (time.time(), data)
    return data


async def _bundles() -> list[dict]:
    return await _get("bundles", {
        "limit": "-1", "filter[is_active][_eq]": "true",
        "fields": "id,name,slug,seo_title,keywords,ref_price_7d,calls_badge",
    }) or []


async def _products(bundle_id: int) -> list[dict]:
    return await _get("products", {
        "limit": "-1", "filter[bundle_id][_eq]": str(bundle_id),
        "filter[is_active][_eq]": "true",
        "fields": "name,price_sell,duration_days", "sort": "price_sell",
    }) or []


# keywords 每个国家一行, 以 ISO 码开头 ("TH,THA,Thailand,..."); 有的 bundle 还多一行
# 小写搜索别名或地区名 ("europe,..."), 所以按 ISO 行数判单国, 不能按总行数。
_ISO_LINE = re.compile(r"^[A-Z]{2},[A-Z]{3},")


def _auto_bundle(bundles: list[dict], country: str) -> dict | None:
    """单国 bundle (ISO 行 ≤ 1) 里, 别名含这个国家的, 取 ref_price_7d 最低 (= 网站排序第一)。"""
    want = country.strip().lower()
    best = None
    for b in bundles:
        lines = [x for x in (b.get("keywords") or "").replace("\xa0", " ").split("\n") if x.strip()]
        if sum(1 for x in lines if _ISO_LINE.match(x.strip())) > 1:
            continue
        aliases = {a.strip().lower() for x in lines for a in x.split(",")}
        if want not in aliases:
            continue
        score = float(b.get("ref_price_7d") or 1e9)
        if best is None or score < best[0]:
            best = (score, b)
    return best[1] if best else None


_GB = re.compile(r"(\d+(?:\.\d+)?)\s*GB", re.I)


def _clean_plan_name(name: str, bundle_name: str = "") -> str:
    """供应商原名 → 读者看得懂的描述。数字一个不改 (价格/天数/流量都要和网站一致)。"""
    s = re.sub(r"\[[^\]]*\]", "", name).strip()
    if bundle_name and s.lower().startswith(bundle_name.lower()):
        s = s[len(bundle_name):]                       # "EU 33M 7Days 1GB" -> "7Days 1GB"
    s = re.sub(r"^.*?(?=\b\d+\s*Days?\b)", "", s, flags=re.I)   # 其余写法的 bundle 名前缀
    s = re.sub(r"(\d+)\s*Days?", r"\1 days", s, flags=re.I)
    s = re.sub(r"unlimited\s*\d+\s*kbps", "then unlimited at reduced speed", s, flags=re.I)
    s = re.sub(r"\bTerminate\b|\beSIM\b", "", s, flags=re.I)
    s = re.sub(r"\bDaily\b", "daily", s)
    s = re.sub(r"\bwith Voice\b", "+ local calls", s, flags=re.I)
    s = re.sub(r"\bunlimited Data\b", "unlimited data", s)
    return re.sub(r"\s+", " ", s).strip(" -,")


def _pick_plans(products: list[dict], bundle_name: str = "") -> list[tuple[str, str]]:
    """挑 2-3 个代表性套餐: 最便宜的 ≥7 天档 / 7 天 ≥10GB 档 / 30 天 ≥20GB 档。"""
    def gb(p):
        n = p["name"]
        if "unlimited data" in n.lower():
            return 999.0
        m = _GB.search(n)
        if not m:
            return 0.0
        v = float(m.group(1))
        return v * (p.get("duration_days") or 1) if "daily" in n.lower() else v

    ps = [p for p in products if p.get("price_sell")]
    picks = []
    wk = [p for p in ps if (p.get("duration_days") or 0) >= 7]
    if wk:
        picks.append(wk[0])
    for days, min_gb in ((7, 10), (30, 20)):
        c = [p for p in ps if p.get("duration_days") == days and gb(p) >= min_gb]
        if c:
            picks.append(c[0])
    if not picks and ps:
        picks.append(ps[0])
    out, seen = [], set()
    for p in picks:
        if p["name"] in seen:
            continue
        seen.add(p["name"])
        out.append((_clean_plan_name(p["name"], bundle_name), f"${float(p['price_sell']):.2f}"))
    return out


async def featured_for_country(country: str | None) -> dict | None:
    """{country, title, url, plans:[(desc, '$x.xx')]} 或 None。"""
    if not country:
        return None
    bundles = await _bundles()
    if not bundles:
        return None
    bid = FEATURED_BUNDLES.get(country)
    b = next((x for x in bundles if x["id"] == bid), None) if bid else _auto_bundle(bundles, country)
    if not b:
        # 没有单国 bundle 的欧洲国家 (Greece/Croatia/...) 退到欧洲多国卡: 先主推的 EU 33,
        # 不覆盖再依次试更大的 EU 38 / 41 / 42 / 49 (Switzerland/Albania/Montenegro 等)。
        for eid in EUROPE_FALLBACK:
            eu = next((x for x in bundles if x["id"] == eid), None)
            if eu and country.strip().lower() in {
                    a.strip().lower() for a in (eu.get("keywords") or "").replace(",", "\n").split("\n")}:
                b = eu
                break
    if not b:
        return None
    plans = _pick_plans(await _products(b["id"]), b["name"])
    if not plans:
        return None
    return {
        "country": country,
        "title": (b.get("seo_title") or b["name"]).strip(),
        "url": SITE + b["slug"],
        "plans": plans,
        "bundle_id": b["id"],          # 2026-11-03: 私信回复要拉全量套餐 (engagement)
        "bundle_name": b["name"],
        "local_carrier": (LOCAL_NETWORK[country][1]
                          if country in LOCAL_NETWORK and LOCAL_NETWORK[country][0] == b["id"]
                          else None),
    }


def consumer_block(f: dict) -> str:
    plans = "\n".join(f"  • {d} — {p}" for d, p in f["plans"])
    return f"""

## FEATURED PRODUCT FOR THIS GROUP ({f['country']}) — overrides any earlier product/price guidance

When you mention what to use, point to THIS plan: "{f['title']}" ({f['url'].replace('https://', '')}).
Current prices on intlroam.com (USD):
{plans}

PRICE RULE: if you mention a price, use ONLY a figure from the list above, exactly as written
(e.g. "{f['plans'][0][1]}"), together with what it buys. Never round it, never write a range,
never quote "$0.99" or any other number for this destination. Leaving the price out is fine.
You may use the product title exactly as written above, including the network name in it —
it is our real product, so this is the ONE exception to any earlier rule against naming
carriers. Do not name any other mobile network.""" + _local_consumer(f)


def _local_consumer(f: dict) -> str:
    c = f.get("local_carrier")
    if not c:
        return ""
    return f"""

## LEAD WITH THE LOCAL NETWORK — this is the main selling point for {f['country']}

This plan is a genuine {f['country']} local line on {c} — the same network locals use, not a
roaming partner hop. Make that the centre of what you say about it: local-level coverage
and speed, behaves like a local SIM. Say it in your own words and vary the phrasing every
time ("runs on {c} directly", "an actual local {c} line", "local network, not roaming"...).
Do not invent speed figures, coverage percentages or comparisons with other brands.
Never imply IntlRoam itself was the weak option (no "I used IntlRoam but wished I'd had a
local line") — the local {c} line IS the IntlRoam plan; contrast it with roaming in general."""


def _local_b2b(f: dict) -> str:
    c = f.get("local_carrier")
    if not c:
        return ""
    return f"""
Lead the pitch with the LOCAL NETWORK: this is a genuine {f['country']} local {c} line, not
roaming — their clients get the network locals use, which is what makes it an easy upsell
and keeps "my data isn't working" messages away from the agency. Vary the wording."""


def b2b_block(f: dict) -> str:
    return f"""

## OUR HEADLINE LINE FOR {f['country'].upper()}

"{f['title']}" is the plan we push for this destination. You may name it once, exactly as
written, as an example of what an agency could offer its clients — that exact title is the
ONE exception to the rule against naming carriers (it is our real product). The pitch is still
WHOLESALE: trade pricing with real margin for them, no stock, instant provisioning.
Still no prices, percentages or margins in public — trade pricing is discussed privately.""" + _local_b2b(f)
