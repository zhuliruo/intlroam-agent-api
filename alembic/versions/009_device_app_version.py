"""devices.app_version / app_version_code — 车队版本可见性

Revision ID: 009
Revises: 008
Create Date: 2026-09-26

设备上报的 APK 版本原来只写 Redis (dev_app_ver:*, 7 天 TTL)。问题是掉线超过
7 天的机器会**直接从版本列表里消失** —— 而那恰恰是最该被发现的一台。
落到 devices 表: 不过期、能排序、日报能直接读。

app_version_code 是判新旧的唯一依据 (versionName 按字符串比大小是错的:
"1.9" > "1.10")。老客户端 UA 里没有 code, 那一列会留 NULL。
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "devices",
        sa.Column("app_version", sa.String(32), nullable=True),
    )
    op.add_column(
        "devices",
        sa.Column("app_version_code", sa.Integer(), nullable=True),
    )
    op.add_column(
        "devices",
        sa.Column("app_version_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("devices", "app_version_at")
    op.drop_column("devices", "app_version_code")
    op.drop_column("devices", "app_version")
