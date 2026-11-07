"""定期跑群组健康度检查 (Hotfix 3.4)

直接 python 运行，cron 调度：
    0 */6 * * * cd /opt/intlroam_api && /opt/intlroam_api/.venv/bin/python -m app.tasks.group_health_check

如果用 systemd timer，看 README 里的样例。
"""
import asyncio
import logging
import sys

from app.database import db_context
from app.services.group_health import compute_all_groups_health


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("group_health_check")


async def main():
    log.info("group health check started")
    async with db_context() as db:
        stats = await compute_all_groups_health(db)
    log.info(f"group health check done: {stats}")
    
    # 输出 JSON 给 cron 监控用
    import json as _json
    print(_json.dumps(stats))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        log.exception("group health check failed")
        sys.exit(1)
