"""导入所有模型，供 Alembic autogenerate 使用"""
from app.models.comment_attempt import CommentAttempt
from app.models.daily_stats import DailyDeviceStats
from app.models.device import Device
from app.models.device_group import DeviceGroupMembership, DeviceGroupVisit
from app.models.group import Group
from app.models.keyword import Keyword
from app.models.raw_post import RawPost

__all__ = [
    "Group",
    "Keyword",
    "Device",
    "RawPost",
    "CommentAttempt",
    "DailyDeviceStats",
    "DeviceGroupMembership",
    "DeviceGroupVisit",
]
