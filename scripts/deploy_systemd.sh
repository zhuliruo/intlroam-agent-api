#!/usr/bin/env bash
# ====================================================================
# deploy_systemd.sh - 部署 intlroam-api 为 systemd 服务
# ====================================================================
set -euo pipefail

LOG_PREFIX="[deploy_systemd]"
log() { echo -e "\033[36m${LOG_PREFIX}\033[0m $*"; }
err() { echo -e "\033[31m${LOG_PREFIX} ERROR\033[0m $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || err "请用 root 运行：sudo bash $0"

PROJECT_DIR="/opt/intlroam_api"
SERVICE_FILE="$PROJECT_DIR/deploy/intlroam-api.service"
SERVICE_DST="/etc/systemd/system/intlroam-api.service"
RUN_USER="intlroam"

[[ -d "$PROJECT_DIR" ]] || err "$PROJECT_DIR 不存在"
[[ -f "$SERVICE_FILE" ]] || err "$SERVICE_FILE 不存在"
[[ -f "$PROJECT_DIR/.env" ]] || err "$PROJECT_DIR/.env 不存在，先配置环境变量"

# ---------- 创建专用用户 ----------
if ! id -u "$RUN_USER" > /dev/null 2>&1; then
    log "创建用户 $RUN_USER..."
    useradd --system --create-home --shell /bin/bash "$RUN_USER"
    log "✓ 用户创建完成"
fi

# ---------- 装 uv（如果用户没装）----------
if [[ ! -x "/home/$RUN_USER/.local/bin/uv" ]]; then
    log "为 $RUN_USER 安装 uv..."
    sudo -u "$RUN_USER" bash -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
    log "✓ uv 安装完成"
fi

# ---------- 设置项目目录权限 ----------
log "调整项目目录权限..."
chown -R "$RUN_USER":"$RUN_USER" "$PROJECT_DIR"
chmod 600 "$PROJECT_DIR/.env"   # env 含密码，仅 owner 可读

# ---------- 同步依赖 ----------
log "用 uv sync 安装依赖（在 $RUN_USER 用户下）..."
sudo -u "$RUN_USER" bash -lc "cd $PROJECT_DIR && /home/$RUN_USER/.local/bin/uv sync"

# ---------- 跑迁移 ----------
log "运行 alembic upgrade head..."
sudo -u "$RUN_USER" bash -lc "cd $PROJECT_DIR && /home/$RUN_USER/.local/bin/uv run alembic upgrade head"

# ---------- 安装 systemd unit ----------
log "安装 systemd unit..."
cp "$SERVICE_FILE" "$SERVICE_DST"
systemctl daemon-reload

log ""
log "============================================================"
log "✅ 部署完成"
log "============================================================"
log ""
log "启动服务："
log "  systemctl start intlroam-api"
log ""
log "开机自启："
log "  systemctl enable intlroam-api"
log ""
log "查看状态："
log "  systemctl status intlroam-api"
log ""
log "查看日志："
log "  journalctl -u intlroam-api -f"
log ""
log "测试访问："
log "  curl http://127.0.0.1:8000/health"
