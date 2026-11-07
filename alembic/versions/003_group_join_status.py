"""003 group join status

Revision ID: 003_group_join_status
Revises: 002_group_health
Create Date: 2026-05-11

加 join_status 字段记录工作机的加群状态。
join_status 取值:
  - 'joined'             : 已加群（可正常评论）
  - 'unknown'            : 未知（导入时默认）
  - 'not_joined_public'  : 公开群未加群（看到"加入小组"按钮）
  - 'pending_approval'   : 私密群已申请等审核
  - 'failed'             : 加群失败（暂时不再尝试）

迁移逻辑：
  - 现有 5 个测试群（id 1-5）→ 假定已加群（join_status='joined'）
  - 现有 45 个导入群（id 6+）→ unknown（首次访问时检测）
"""
from alembic import op
import sqlalchemy as sa


revision = "003_group_join_status"
down_revision = "002_group_health"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 加 join_status 字段
    op.add_column(
        "groups",
        sa.Column(
            "join_status",
            sa.String(20),
            nullable=False,
            server_default="unknown",
        ),
    )
    op.create_index("idx_groups_join_status", "groups", ["join_status"])

    # 加 join_attempt_count
    op.add_column(
        "groups",
        sa.Column(
            "join_attempt_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )

    # 加 last_join_attempt_at
    op.add_column(
        "groups",
        sa.Column(
            "last_join_attempt_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    # 数据迁移：joined_at < 2026-05-10 的群组假定已加群（5 个原始测试群）
    op.execute(
        """
        UPDATE groups
        SET join_status = 'joined'
        WHERE joined_at IS NOT NULL
          AND joined_at < '2026-05-10 00:00:00+00'
        """
    )


def downgrade() -> None:
    op.drop_index("idx_groups_join_status", table_name="groups")
    op.drop_column("groups", "last_join_attempt_at")
    op.drop_column("groups", "join_attempt_count")
    op.drop_column("groups", "join_status")
