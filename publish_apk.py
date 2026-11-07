#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布一个 APK 到 OTA 通道。

用法 (在服务器上):
    python3 publish_apk.py /tmp/IntlRoamAgent-1.28.apk --version 1.28 --code 29 \
        --notes "FB 作者名采集修复" [--mandatory]

做三件事: 放文件 → 算 sha256 → 写清单。写清单是**最后一步**且用原子替换,
所以不存在"清单指向一个还没传完的 APK"的中间态 —— 弱网下的手机会正好撞上。
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

APK_DIR = Path("/opt/intlroam_api/static/apk")
MANIFEST = APK_DIR / "latest.json"


def sha256_of(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("apk")
    ap.add_argument("--version", required=True, help="versionName, 如 1.28")
    ap.add_argument("--code", required=True, type=int, help="versionCode, 必须递增")
    ap.add_argument("--notes", default="")
    ap.add_argument("--mandatory", action="store_true")
    a = ap.parse_args()

    src = Path(a.apk)
    if not src.exists():
        print(f"找不到 {src}", file=sys.stderr)
        return 1

    APK_DIR.mkdir(parents=True, exist_ok=True)

    # 旧清单里的 version_code 只能升不能降, 否则已经装了新包的机器会被劝退级安装
    if MANIFEST.exists():
        try:
            old = json.loads(MANIFEST.read_text(encoding="utf-8"))
            if a.code <= int(old.get("version_code", 0)):
                print(f"version_code {a.code} 不高于当前已发布的 "
                      f"{old.get('version_code')}, 拒绝发布", file=sys.stderr)
                return 1
        except Exception:
            pass   # 旧清单坏了就当没有

    fname = f"IntlRoamAgent-{a.version}.apk"
    dst = APK_DIR / fname
    shutil.copy2(src, dst)
    os.chmod(dst, 0o644)
    digest = sha256_of(dst)

    manifest = {
        "version": a.version,
        "version_code": a.code,
        "file": fname,
        "sha256": digest,
        "notes": a.notes,
        "mandatory": a.mandatory,
    }
    # 原子替换: 先写临时文件再 rename, 避免客户端读到半截清单
    tmp = MANIFEST.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, MANIFEST)

    print(f"已发布 {fname}")
    print(f"  versionCode {a.code}   {dst.stat().st_size / 1e6:.1f} MB")
    print(f"  sha256 {digest}")
    print(f"  下载地址 https://agent.intlroam.com/apk/{fname}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
