"""IntlRoam API v2 入口"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator

from app.api.v2 import app_update, devices, groups, monitoring, posts, comments, confirm, posting
from app.api.v2 import posts_w1
from app.config import get_settings
from app.deps import close_redis, get_redis
from app.utils.logger import get_logger, setup_logging

setup_logging()
log = get_logger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动 / 关闭时的事件"""
    log.info("startup", env=settings.env, port=settings.app_port)
    # 预热 Redis 连接
    redis_client = await get_redis()
    try:
        await redis_client.ping()
        log.info("redis_ok")
    except Exception as e:
        log.error("redis_unreachable", error=str(e))
        # Redis 不通不阻塞启动，业务调用时会重连

    # predraft: 启动 pending attempt cleanup 后台任务
    import asyncio as _asyncio
    from app.services.attempt_cleanup import cleanup_pending_attempts
    cleanup_task = _asyncio.create_task(cleanup_pending_attempts(redis_client))
    log.info("cleanup_task_started")

    # 2026-09-14: 回收"文案已生成但没发出去"的滞留帖 (见 services/stranded_replies.py)
    from app.services.stranded_replies import sweep_stranded_replies
    stranded_task = _asyncio.create_task(sweep_stranded_replies(redis_client))
    log.info("stranded_sweep_task_started")

    # 2026-11-07: 从 raw_posts 采集电话号码 (含历史回填) → phone_leads_vn / phone_leads_intl
    from app.services.phone_leads import harvest_raw_posts
    phone_task = _asyncio.create_task(harvest_raw_posts(redis_client))
    log.info("phone_harvest_task_started")

    yield

    log.info("shutdown")
    for t in (cleanup_task, stranded_task, phone_task):
        t.cancel()
        try:
            await t
        except _asyncio.CancelledError:
            pass
    await close_redis()


app = FastAPI(
    title="IntlRoam API v2",
    version="2.0.0",
    description="Phase 2 backend: HTTP-first agent communication",
    lifespan=lifespan,
)

# Prometheus 指标（自动暴露 /metrics）
Instrumentator().instrument(app).expose(app, endpoint="/metrics")

# CORS（仅 dev/调试用，生产 Nginx 层处理）
if settings.env == "dev":
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )


# ---------- 路由 ----------
app.include_router(monitoring.router, tags=["monitoring"])
app.include_router(groups.router, prefix="/api/v2", tags=["groups"])
app.include_router(posts_w1.router, prefix="/api/v2", tags=["v2_w1"])
app.include_router(posts.router, prefix="/api/v2", tags=["posts"])
app.include_router(comments.router, prefix="/api/v2", tags=["comments"])
app.include_router(devices.router, prefix="/api/v2", tags=["devices"])
app.include_router(confirm.router, prefix="/api/v2", tags=["confirm"])
app.include_router(posting.router, prefix="/api/v2", tags=["posting"])
from app.api.v2 import admin
app.include_router(admin.router, prefix="/api/v2", tags=["admin"])
from app.api.v2 import tiktok as tiktok_api
app.include_router(tiktok_api.router, prefix="/api/v2", tags=["tiktok"])
from app.api.v2 import threads as threads_api
app.include_router(threads_api.router, prefix="/api/v2", tags=["threads"])
from app.api.v2 import keywords as keywords_api
app.include_router(keywords_api.router, prefix="/api/v2", tags=["keywords"])
app.include_router(app_update.router, prefix="/api/v2", tags=["app_update"])
from app.api.v2 import engagement as engagement_api
app.include_router(engagement_api.router, prefix="/api/v2", tags=["engagement"])
from app.api.v2 import outreach as outreach_api
app.include_router(outreach_api.router, prefix="/api/v2", tags=["outreach"])
from app.api.v2 import chatwoot_outbox as chatwoot_outbox_api
app.include_router(chatwoot_outbox_api.router, prefix="/api/v2", tags=["chatwoot"])

@app.get("/")
async def root():
    return {
        "service": "intlroam-api",
        "version": app.version,
        "docs": "/docs",
    }
# Hotfix 3: 422 验证错误的完整 body 日志
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

@app.exception_handler(RequestValidationError)
async def log_422(request, exc):
    body = await request.body()
    body_str = body.decode("utf-8", errors="replace")[:1000]
    import logging as _log_h3
    _log_h3.warning(f"VALIDATION_422 path={request.url.path} errors={exc.errors()}")
    _log_h3.warning(f"VALIDATION_422 body={body_str}")
    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors()},
    )
