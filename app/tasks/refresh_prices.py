"""价格刷新 cron 任务 (Hotfix 3.11)

每天凌晨 5:00 (HKT) 由 cron 调用，从 product_coverage 动态拉所有国家的价格。

cron 配置:
  0 5 * * * cd /opt/intlroam_api && /opt/intlroam_api/.venv/bin/python -m app.tasks.refresh_prices >> /var/log/intlroam_prices.log 2>&1

手动运行测试:
  cd /opt/intlroam_api && .venv/bin/python -m app.tasks.refresh_prices
"""
import asyncio
import sys

import redis.asyncio as aioredis

from app.config import get_settings
from app.services.price_client import (
    refresh_all_prices,
    set_cached_price,
)
from app.utils.logger import get_logger

log = get_logger(__name__)
settings = get_settings()


async def main():
    log.info("refresh_prices_task_start")
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        results = await refresh_all_prices()
        valid = 0
        total = len(results)
        for dest, price in results.items():
            await set_cached_price(redis, dest, price)
            if price:
                valid += 1
                log.info("price_cached", destination=dest,
                         data=price.get("data"),
                         duration=price.get("duration_days"),
                         vnd_k=price.get("promo_vnd_k"))
        log.info("refresh_prices_task_done", total=total, valid=valid)
        print(f"OK: refreshed {valid}/{total} destinations")
    finally:
        await redis.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        log.error("refresh_prices_task_failed", error=str(e))
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
