"""应用配置 — 从环境变量加载"""
from __future__ import annotations

from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # 运行环境
    env: str = "production"
    log_level: str = "info"
    tz: str = "Asia/Hong_Kong"

    # 监听
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    app_workers: int = 2

    # 数据库
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5432
    postgres_user: str = "intlroam"
    postgres_password: str
    postgres_db: str = "intlroam"

    # Redis
    redis_url: str = "redis://127.0.0.1:6379/0"

    # LLM
    llm_primary: str = "gemini"
    llm_fallback: str = "deepseek"

    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_base_url: str = ""  # Hotfix 2: Cloudflare Worker proxy URL，空则走官方 endpoint

    gemini_use_vertex: bool = False
    gcp_project: str = ""
    gcp_location: str = "us-central1"

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-chat"

    llm_timeout: int = 10
    llm_max_retries: int = 2
    # 输出上限，两个 provider 共用。发帖 prompt 本身就限死 50-80 词 / 400 字符，
    # 分类 ~80 token、回复 ~150 token，400 足够且留了 buffer。
    llm_max_output_tokens: int = 400

    # 业务
    group_lock_ttl_seconds: int = 1800
    max_daily_comments_fb: int = 100
    max_daily_comments_ig: int = 15

    # 告警 (复用 daily_report 的 Brevo relay; 留空则只写 WARNING 日志)
    smtp_host: str = "smtp-relay.brevo.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "support@intlroam.com"
    alert_email: str = ""

    # 安全
    device_token_required: bool = False

    @property
    def database_url(self) -> str:
        """SQLAlchemy async URL"""
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def database_url_sync(self) -> str:
        """同步 URL，给 Alembic 用"""
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    """单例 Settings"""
    return Settings()  # type: ignore[call-arg]
