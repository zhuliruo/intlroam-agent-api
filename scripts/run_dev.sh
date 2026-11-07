#!/usr/bin/env bash
# 本地开发模式 — 带 reload 自动重启
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -d .venv ]]; then
    echo "[run_dev] 首次运行，同步依赖..."
    uv sync
fi

# 跑迁移（幂等）
uv run alembic upgrade head

# 启动 dev server
exec uv run uvicorn app.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --reload \
    --log-level debug
