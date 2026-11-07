# -*- coding: utf-8 -*-
"""OTA 通道 —— 服务端下发新版本, 客户端确认后下载安装 (2026-09-15)。

背景: 车队 7 台机长期版本不一, 而版本不一是**真实故障源** —— 同一天里 5a5cf 的
TikTok 发送全失败、9ed4 全成功, 差别就在装没装新包。以前只能人肉挨台插 USB,
所以实际上永远铺不齐 (今天查: 6/7 台还停在几个月前的包)。

设计取舍:
- **清单 (JSON) 走 FastAPI, APK 本体走 nginx 静态**。APK 是 8MB 二进制, 走 Python
  既占 worker 又不支持 Range 续传; nginx 直出可断点续传, 手机弱网下重要。
- **清单落磁盘文件而不是数据库**。发版是低频人工动作, 一个可以 cat 出来看的 JSON
  比一张表更容易排错, 也不需要迁移。
- **sha256 必须下发**。客户端装包前要自校验 —— 弱网下截断的 APK 装上去会让 agent
  直接起不来, 而那台机在远端没人能救。
- **不做强制静默安装**。用户要的是"点击确认即可", 且静默安装需要系统签名或
  device owner 权限, 这批机都没有。
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Request

from app.utils.logger import get_logger

router = APIRouter()
log = get_logger(__name__)

# 清单与 APK 都放这里; nginx 以 /apk/ 别名直出同一个目录
APK_DIR = Path("/opt/intlroam_api/static/apk")
MANIFEST = APK_DIR / "latest.json"


@router.get("/app/latest")
async def app_latest(request: Request):
    """返回当前应发布的版本清单。

    客户端拿 version_code 跟自己的 BuildConfig.VERSION_CODE 比, 新的才提示。
    没有发布过任何版本时返回 available=false —— 客户端据此静默跳过, 不弹任何东西。
    """
    device_id = request.headers.get("X-Device-Id", "")

    if not MANIFEST.exists():
        return {"available": False}

    try:
        m = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except Exception as e:
        # 清单写坏了不该让整个车队卡住, 也不该让客户端以为"没有新版"
        log.warning("apk_manifest_unreadable", err=str(e))
        return {"available": False}

    apk = APK_DIR / m.get("file", "")
    if not apk.exists():
        log.warning("apk_file_missing", file=m.get("file"), device_id=device_id)
        return {"available": False}

    # 下发绝对 URL: 客户端直接拿去下载, 不用自己拼
    base = str(request.base_url).rstrip("/")
    log.info("apk_manifest_served", device_id=device_id, version=m.get("version"))
    return {
        "available": True,
        "version": m.get("version"),
        "version_code": m.get("version_code"),
        "url": f"{base}/apk/{m['file']}",
        "size": apk.stat().st_size,
        "sha256": m.get("sha256"),
        "notes": m.get("notes", ""),
        # mandatory 只影响客户端提示的措辞与可否"稍后再说", 不做强制安装
        "mandatory": bool(m.get("mandatory", False)),
    }
