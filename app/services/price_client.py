"""价格 API 客户端 (Hotfix 3.11 Step 4 终极版)

基于 bundle 架构：
- product_coverage 表: country → bundle_name 映射 (170 country → 91 bundle)
- products 表: bundle_name → 实际套餐 + 价格

流程:
1. fetch_coverage_map(): 拉 product_coverage 建立 country → bundle dict
2. fetch_bundle_prices(): 对每个 unique bundle 拉最优 7 天套餐
3. 建立 country → price 映射缓存到 Redis (24h TTL)
4. lazy fetch: generate_reply 时如果没缓存，走 country → bundle → product
"""
from __future__ import annotations
import asyncio
import json
import re
from typing import Any

import httpx

from app.utils.logger import get_logger

log = get_logger(__name__)

API_BASE = "http://100.82.195.91:8055"
def _env_token() -> str:
    # 2026-11-07 密钥挪到 .env (PRICE_API_TOKEN); systemd 用 EnvironmentFile 注入,
    # cron 脚本直接 import 时没有注入 -> 退回读 .env 文件
    import os
    v = os.getenv("PRICE_API_TOKEN", "").strip()
    if not v:
        try:
            for line in open(os.path.join(os.path.dirname(__file__), "..", "..", ".env")):
                if line.startswith("PRICE_API_TOKEN="):
                    v = line.split("=", 1)[1].strip().strip("\"\x27")
        except OSError:
            pass
    return v


API_TOKEN = _env_token()
DISCOUNT = 1.0  # Hotfix 3.16 H3: 大降价 -31.2%, WELCOME20 作废, 直接用零售价
USD_TO_VND = 27000

HEADERS = {
    "Authorization": f"Bearer {API_TOKEN}",
    "Accept": "application/json",
}

# Country alias - 把 LLM 返回的简称映射到 Directus 标准名
COUNTRY_ALIAS = {
    "UK": "United Kingdom",
    "USA": "United States",
    "America": "United States",
    "Korea": "South Korea",
    "Russia": "Russian Federation",
}


# 越南语国家名（用于 reply 注入）
DEST_VI = {
    "Japan": "Nhật Bản 🇯🇵",
    "Korea": "Hàn Quốc 🇰🇷",
    "South Korea": "Hàn Quốc 🇰🇷",
    "Taiwan": "Đài Loan 🇹🇼",
    "Singapore": "Singapore 🇸🇬",
    "Thailand": "Thái Lan 🇹🇭",
    "Vietnam": "Việt Nam 🇻🇳",
    "China": "Trung Quốc 🇨🇳",
    "Hong Kong": "Hồng Kông 🇭🇰",
    "Macao": "Ma Cao 🇲🇴",
    "Australia": "Úc 🇦🇺",
    "New Zealand": "New Zealand 🇳🇿",
    "United States": "Mỹ 🇺🇸",
    "United States of America": "Mỹ 🇺🇸",
    "USA": "Mỹ 🇺🇸",
    "Canada": "Canada 🇨🇦",
    "Mexico": "Mexico 🇲🇽",
    "United Kingdom": "Anh 🇬🇧",
    "UK": "Anh 🇬🇧",
    "France": "Pháp 🇫🇷",
    "Germany": "Đức 🇩🇪",
    "Italy": "Ý 🇮🇹",
    "Spain": "Tây Ban Nha 🇪🇸",
    "Netherlands": "Hà Lan 🇳🇱",
    "Switzerland": "Thụy Sĩ 🇨🇭",
    "Sweden": "Thụy Điển 🇸🇪",
    "Norway": "Na Uy 🇳🇴",
    "Denmark": "Đan Mạch 🇩🇰",
    "Finland": "Phần Lan 🇫🇮",
    "Poland": "Ba Lan 🇵🇱",
    "Russia": "Nga 🇷🇺",
    "Turkey": "Thổ Nhĩ Kỳ 🇹🇷",
    "Egypt": "Ai Cập 🇪🇬",
    "Morocco": "Morocco 🇲🇦",
    "South Africa": "Nam Phi 🇿🇦",
    "United Arab Emirates": "UAE 🇦🇪",
    "Qatar": "Qatar 🇶🇦",
    "Saudi Arabia": "Saudi Arabia 🇸🇦",
    "Israel": "Israel 🇮🇱",
    "India": "Ấn Độ 🇮🇳",
    "Pakistan": "Pakistan 🇵🇰",
    "Bangladesh": "Bangladesh 🇧🇩",
    "Sri Lanka": "Sri Lanka 🇱🇰",
    "Nepal": "Nepal 🇳🇵",
    "Bhutan": "Bhutan 🇧🇹",
    "Maldives": "Maldives 🇲🇻",
    "Malaysia": "Malaysia 🇲🇾",
    "Indonesia": "Indonesia 🇮🇩",
    "Philippines": "Philippines 🇵🇭",
    "Cambodia": "Cambodia 🇰🇭",
    "Laos": "Lào 🇱🇦",
    "Myanmar": "Myanmar 🇲🇲",
    "Brunei": "Brunei 🇧🇳",
    "Mongolia": "Mongolia 🇲🇳",
    "Fiji": "Fiji 🇫🇯",
    "Brazil": "Brazil 🇧🇷",
    "Argentina": "Argentina 🇦🇷",
}


def _calc_promo(price_sell: str) -> tuple[float, int]:
    sell = float(price_sell)
    promo = round(sell * DISCOUNT, 2)
    vnd = int(promo * USD_TO_VND)
    return promo, vnd


def _vi_name(country: str) -> str:
    return DEST_VI.get(country, country)


# ============= 1. coverage 表：country → bundle 映射 =============

async def fetch_coverage_map(client: httpx.AsyncClient) -> dict[str, list[str]]:
    """拉 product_coverage 建立 country → [bundle_names] 映射

    一个 country 可能在多个 bundle 里（如 France 在 ASIA+EU 54 + EU 38 + EU 41 + ...）
    返回所有 bundle 给后续选最优价格。
    """
    url = f"{API_BASE}/items/product_coverage"
    params = {"fields": "country,bundle_name", "limit": -1}
    resp = await client.get(url, params=params, headers=HEADERS, timeout=30.0)
    resp.raise_for_status()
    data = resp.json().get("data", [])

    country_to_bundles: dict[str, set[str]] = {}
    for item in data:
        country = item.get("country")
        bundle = item.get("bundle_name")
        if country and bundle:
            country_to_bundles.setdefault(country, set()).add(bundle)

    result = {k: sorted(v) for k, v in country_to_bundles.items()}
    log.info("fetched_coverage_map",
             countries=len(result),
             total_bundles=len({b for bundles in result.values() for b in bundles}))
    return result


# ============= 2. products 表：bundle → 最优 7 天套餐 =============

async def fetch_bundle_products(client: httpx.AsyncClient, bundle_name: str) -> list:
    """拉某 bundle 的所有 active 产品"""
    import urllib.parse
    keyword = urllib.parse.quote(bundle_name)
    url = (
        f"{API_BASE}/items/products"
        f"?filter[bundle_name][_eq]={keyword}"
        f"&filter[is_active][_eq]=true"
        f"&filter[status][_eq]=published"
        f"&sort=price_sell"
        f"&limit=50"
    )
    try:
        resp = await client.get(url, headers=HEADERS, timeout=10.0)
        resp.raise_for_status()
        return resp.json().get("data", [])
    except Exception as e:
        log.warning("fetch_bundle_products_failed", bundle=bundle_name, error=str(e))
        return []


def _best_plan_from_products(products: list) -> dict | None:
    """从套餐里选最优 7 天 (5GB > 3GB > 10GB > 2GB > 1GB)"""
    seven_day = [p for p in products if p.get("duration_days") == 7]
    best = None
    for gb in ["5GB", "3GB", "10GB", "2GB", "1GB"]:
        for p in seven_day:
            if gb.lower() in p["name"].lower() and "daily" not in p["name"].lower():
                best = p
                break
        if best:
            break
    if not best and seven_day:
        best = seven_day[0]
    if not best and products:
        best = products[0]
    if not best:
        return None
    promo_usd, vnd = _calc_promo(best["price_sell"])
    m = re.search(r"(Daily \d+(?:GB|MB)|\d+(?:GB|MB))", best["name"], re.I)
    data_label = m.group(1) if m else "data"
    return {
        "name": best["name"],
        "data": data_label,
        "duration_days": best.get("duration_days", 7),
        "price_usd": float(best["price_sell"]),
        "promo_usd": promo_usd,
        "promo_vnd_k": vnd // 1000,
        "bundle_name": best.get("bundle_name"),
    }


# ============= 3. 单国家拉价格（lazy fetch） =============

async def fetch_destination_price(destination: str) -> dict | None:
    """根据国家名拉最优价格

    流程: country → 找所有 bundles → 每个 bundle 拉 products → 选总体最优 7 天套餐
    """
    canonical = COUNTRY_ALIAS.get(destination, destination)
    dest_vi = _vi_name(destination)
    try:
        async with httpx.AsyncClient() as client:
            # Step 1: country -> bundles (alias to canonical)
            coverage_map = await fetch_coverage_map(client)
            bundles = coverage_map.get(canonical, [])
            if not bundles:
                log.debug("no_bundle_for_country", country=destination)
                return None

            # Step 2: 对每个 bundle 拉 products，合并所有产品
            all_products = []
            for bundle in bundles:
                products = await fetch_bundle_products(client, bundle)
                all_products.extend(products)
                await asyncio.sleep(0.1)

            if not all_products:
                log.debug("no_products_in_bundles", country=destination, bundles=bundles)
                return None

            best = _best_plan_from_products(all_products)
            if best:
                best["dest_vi"] = dest_vi
            return best
    except httpx.TimeoutException:
        log.warning("price_api_timeout", destination=destination)
        return None
    except Exception as e:
        log.error("price_api_error", destination=destination, error=str(e))
        return None


# ============= 4. 全量刷新（凌晨 cron 用，优化版） =============

async def refresh_all_prices() -> dict[str, dict | None]:
    """凌晨 cron：从 coverage 拉所有国家 + 高效拉 bundles

    优化：一个 bundle 只拉一次 products，避免重复 API 调用
    """
    async with httpx.AsyncClient() as client:
        # Step 1: country → bundles 映射
        coverage_map = await fetch_coverage_map(client)
        log.info("refresh_started",
                 countries=len(coverage_map),
                 unique_bundles=len({b for bs in coverage_map.values() for b in bs}))

        # Step 2: 收集所有 unique bundles
        all_bundles = sorted({b for bundles in coverage_map.values() for b in bundles})

        # Step 3: 对每个 unique bundle 拉一次 products 缓存
        bundle_products: dict[str, list] = {}
        for bundle in all_bundles:
            bundle_products[bundle] = await fetch_bundle_products(client, bundle)
            await asyncio.sleep(0.15)
        log.info("fetched_all_bundles", count=len(bundle_products))

    # Step 4: 对每个 country 选最优产品
    results: dict[str, dict | None] = {}
    for country, bundles in coverage_map.items():
        all_products = []
        for b in bundles:
            all_products.extend(bundle_products.get(b, []))
        if not all_products:
            results[country] = None
            continue
        best = _best_plan_from_products(all_products)
        if best:
            best["dest_vi"] = _vi_name(country)
        results[country] = best

    valid = sum(1 for v in results.values() if v is not None)
    log.info("refresh_done", total=len(coverage_map), valid=valid)
    return results


# ============= 5. Redis 缓存 =============

CACHE_TTL_SECONDS = 24 * 3600
CACHE_KEY_PREFIX = "price_cache:v1:"


async def get_cached_price(redis, destination: str) -> dict | None:
    canonical = COUNTRY_ALIAS.get(destination, destination)
    key = f"{CACHE_KEY_PREFIX}{canonical}"
    raw = await redis.get(key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


async def set_cached_price(redis, destination: str, price_data: dict | None) -> None:
    canonical = COUNTRY_ALIAS.get(destination, destination)
    key = f"{CACHE_KEY_PREFIX}{canonical}"
    if price_data is None:
        await redis.set(key, json.dumps({"_no_data": True}), ex=3600)
        return
    await redis.set(key, json.dumps(price_data), ex=CACHE_TTL_SECONDS)


async def get_or_fetch_price(redis, destination: str) -> dict | None:
    if not destination:
        return None
    cached = await get_cached_price(redis, destination)
    if cached is not None:
        if cached.get("_no_data"):
            return None
        return cached
    log.info("price_lazy_fetch", destination=destination)
    price = await fetch_destination_price(destination)
    await set_cached_price(redis, destination, price)
    return price


def format_price_for_reply(price: dict) -> str:
    if not price:
        return ""
    return (
        f"Current IntlRoam price for {price['dest_vi']}: "
        f"{price['data']}/{price['duration_days']} ngày = "
        f"{price['promo_vnd_k']}k VNĐ"  # Hotfix 3.16 H3: 删 WELCOME20
    )


# ============= 测试 =============

if __name__ == "__main__":
    async def test():
        async with httpx.AsyncClient() as c:
            coverage = await fetch_coverage_map(c)
            print(f"\n=== Coverage: {len(coverage)} countries ===")
            for cn in ["France", "Germany", "UK", "United States", "Japan"]:
                print(f"  {cn}: {coverage.get(cn, [])}")

        # 测试单国家
        for dest in ["France", "United Kingdom", "Russia", "Germany", "Japan"]:
            print(f"\n=== {dest} ===")
            price = await fetch_destination_price(dest)
            print(json.dumps(price, indent=2, ensure_ascii=False))

    asyncio.run(test())
