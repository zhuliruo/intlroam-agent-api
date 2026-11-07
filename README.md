# IntlRoam API v2

> Phase 2 后端服务：FastAPI + PostgreSQL + Redis + Gemini/DeepSeek 双 LLM
> 部署位置：`/opt/intlroam_api/`
> 监听：`127.0.0.1:8000`（由 Nginx 反代到 `https://api.intlroam.com`）

---

## 设计目标

修复 Phase 1 三大痛点：
1. **评论成功率低** → 服务器派群组前严格校验，`/report_result` 详细记录每次评论的失败阶段
2. **设备掉线** → 完全 HTTP 轮询，无长连接
3. **个人主页误识别** → `groups.verified_at` 字段 + 派群组前必须先验证

---

## 快速开始

### 0. 前置依赖
- Python 3.11+
- uv（推荐）或 pip
- PostgreSQL 16 + pgvector（已由 chatwoot_deploy 安装）
- Redis 7（已由 chatwoot_deploy 安装）

### 1. 安装 uv
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc
```

### 2. 同步依赖
```bash
cd /opt/intlroam_api
uv sync     # 创建 venv 并装齐依赖
```

### 3. 配置环境变量
```bash
cp .env.example .env
nano .env
# 至少必填：
#   POSTGRES_PASSWORD（与 chatwoot_deploy 里 INTLROAM_DB_PASSWORD 同一个值）
#   GEMINI_API_KEY、DEEPSEEK_API_KEY
```

### 4. 跑数据库迁移
```bash
uv run alembic upgrade head
```

### 5. 导入群组（首次）
```bash
uv run python scripts/seed_groups.py /path/to/groups.csv
```

### 6. 本地开发模式
```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
# 浏览器打开 http://127.0.0.1:8000/docs 看 API 文档
```

### 7. 生产部署（systemd）
```bash
sudo bash scripts/deploy_systemd.sh
sudo systemctl start intlroam-api
sudo systemctl enable intlroam-api
sudo journalctl -u intlroam-api -f   # 看实时日志
```

### 8. Nginx 反代（已由 chatwoot_deploy 配好）
api.intlroam.com → 127.0.0.1:8000，证书已签发。
启动 systemd 服务后立即可用。

---

## API 端点（v2）

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/health` | 健康检查（无认证） |
| GET | `/api/v2/next_groups` | 给手机派群组（含 30 分钟锁） |
| POST | `/api/v2/analyze_post` | Gemini/DeepSeek 分类 + 回复生成 |
| POST | `/api/v2/report_result` | 上报采集/评论结果 |
| GET | `/api/v2/device_config` | 设备拉取专属配置 |
| POST | `/api/v2/heartbeat` | 心跳监控 |

完整 API 文档：启动后访问 `/docs` 或 `/redoc`。

---

## 项目结构

```
/opt/intlroam_api/
├── pyproject.toml          ← 依赖清单（uv 管理）
├── .env.example            ← 环境变量模板
├── alembic.ini             ← 数据库迁移配置
├── alembic/versions/       ← 迁移脚本
├── app/
│   ├── main.py             ← FastAPI 入口
│   ├── config.py           ← Settings（pydantic-settings）
│   ├── database.py         ← async SQLAlchemy
│   ├── deps.py             ← 依赖注入
│   ├── models/             ← SQLAlchemy 表
│   ├── schemas/            ← Pydantic 请求/响应
│   ├── api/v2/             ← 路由
│   ├── services/           ← 业务逻辑（含 LLM 客户端）
│   └── utils/              ← 工具
├── tests/
├── scripts/
│   ├── seed_groups.py      ← 群组导入
│   └── deploy_systemd.sh   ← 一键部署
└── deploy/
    └── intlroam-api.service
```

---

## 开发节奏

| 阶段 | 任务 | 状态 |
|---|---|---|
| Day 0 | 项目骨架 + 数据库迁移 + 5 端点占位 | ✅ |
| Day 1 | 实现 `/next_groups` + `/report_result` | ⏳ |
| Day 2 | 实现 `/analyze_post`（Gemini + DeepSeek 双活） | ⏳ |
| Day 3 | 实现 `/device_config` + `/heartbeat` + 群组导入 | ⏳ |
| Day 4 | 单元测试 + 灰度 1 台手机 | ⏳ |
| Day 5+ | 调优 prompt、扩到 10 台 | ⏳ |

---

## 故障排查

```bash
# 看服务状态
systemctl status intlroam-api

# 看实时日志
journalctl -u intlroam-api -f

# 看最近 100 行
journalctl -u intlroam-api -n 100 --no-pager

# 重启
systemctl restart intlroam-api

# 数据库连接测试
PGPASSWORD=... psql -h 127.0.0.1 -U intlroam -d intlroam -c "SELECT count(*) FROM groups;"

# Redis 测试
redis-cli ping
```
