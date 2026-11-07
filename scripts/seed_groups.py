"""群组导入脚本

用法 1：从 CSV 批量导入
    uv run python scripts/seed_groups.py --csv groups.csv

CSV 格式（第一行是表头）：
    url,name,external_id,member_count,notes
    https://www.facebook.com/groups/duongdanhomenam/,"DU LICH NHAT BAN","duongdanhomenam",125000,"主力日本群"
    https://www.facebook.com/groups/12345678/,"Travel KR",12345678,80000,
    
    必填: url（可只填 URL，脚本会自动从 URL 提取 external_id）
    可选: name, external_id, member_count, notes

用法 2：交互式逐条添加
    uv run python scripts/seed_groups.py --interactive

用法 3：从命令行单条
    uv run python scripts/seed_groups.py --url 'https://www.facebook.com/groups/123/' --name 'Foo'
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import re
import sys
from pathlib import Path
from typing import Any

# 让脚本能导入 app
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.database import db_context  # noqa: E402
from app.models.group import Group  # noqa: E402


GROUP_URL_RE = re.compile(
    r"facebook\.com/groups/([A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)


def extract_external_id(url: str) -> str | None:
    """从 FB 群组 URL 提取 external_id (数字 ID 或 vanity name)"""
    m = GROUP_URL_RE.search(url)
    if not m:
        return None
    raw = m.group(1).rstrip("/")
    return raw or None


def normalize_url(url: str) -> str:
    """规范化 URL（去掉查询参数、统一 m. → www.）"""
    url = url.strip().split("?")[0].split("#")[0]
    if url.startswith("https://m.facebook.com"):
        url = url.replace("https://m.facebook.com", "https://www.facebook.com")
    if not url.endswith("/"):
        url += "/"
    return url


async def upsert_group(
    url: str,
    name: str | None = None,
    external_id: str | None = None,
    member_count: int | None = None,
    notes: str | None = None,
    platform: str = "facebook",
    pre_verified: bool = False,
) -> tuple[Group, bool]:
    """upsert 一个群组。返回 (group, created)。"""
    url = normalize_url(url)
    if external_id is None:
        external_id = extract_external_id(url)
    if external_id is None:
        raise ValueError(f"无法从 URL 提取 external_id: {url}")

    async with db_context() as db:
        # 查重
        existing = await db.execute(
            select(Group).where(
                Group.platform == platform,
                Group.external_id == external_id,
            )
        )
        g = existing.scalar_one_or_none()

        if g is not None:
            # 更新已有
            updated = False
            if name and g.name != name:
                g.name = name; updated = True
            if member_count is not None and g.member_count != member_count:
                g.member_count = member_count; updated = True
            if notes and g.notes != notes:
                g.notes = notes; updated = True
            return g, False

        # 新建
        from datetime import datetime, timezone
        g = Group(
            platform=platform,
            external_id=external_id,
            url=url,
            name=name,
            member_count=member_count,
            notes=notes,
            verified_at=datetime.now(timezone.utc) if pre_verified else None,
        )
        db.add(g)
        return g, True


async def import_csv(csv_path: Path, pre_verified: bool = False) -> dict[str, int]:
    """从 CSV 批量导入"""
    stats = {"created": 0, "updated": 0, "errors": 0}
    with csv_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader, 2):  # 行号从 2 开始（跳过表头）
            url = (row.get("url") or "").strip()
            if not url:
                continue
            try:
                _g, created = await upsert_group(
                    url=url,
                    name=(row.get("name") or "").strip() or None,
                    external_id=(row.get("external_id") or "").strip() or None,
                    member_count=int(row["member_count"]) if row.get("member_count") else None,
                    notes=(row.get("notes") or "").strip() or None,
                    pre_verified=pre_verified,
                )
                if created:
                    stats["created"] += 1
                    print(f"  ✓ 新增 [{i}] {url}")
                else:
                    stats["updated"] += 1
                    print(f"  ~ 更新 [{i}] {url}")
            except Exception as e:
                stats["errors"] += 1
                print(f"  ✗ 错误 [{i}] {url}: {e}")
    return stats


async def import_single(url: str, name: str | None, pre_verified: bool = False) -> None:
    g, created = await upsert_group(url=url, name=name, pre_verified=pre_verified)
    print(f"{'新增' if created else '已存在'}: id={g.id} name={g.name}")


async def import_interactive(pre_verified: bool = False) -> None:
    """交互式逐条添加（在终端粘贴 URL）"""
    print("交互式导入。粘贴 FB 群组 URL，每行一个。空行结束。")
    print(f"模式：{'pre-verified（直接可派发）' if pre_verified else '需要手机首次访问校验'}")
    while True:
        try:
            url = input("URL > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not url:
            break
        try:
            name_in = input("Name (可选) > ").strip() or None
            g, created = await upsert_group(url=url, name=name_in, pre_verified=pre_verified)
            print(f"  {'新增' if created else '已存在'}: id={g.id}\n")
        except Exception as e:
            print(f"  错误: {e}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="导入 FB 群组到 intlroam DB")
    parser.add_argument("--csv", type=Path, help="CSV 文件路径")
    parser.add_argument("--url", help="单个群组 URL")
    parser.add_argument("--name", help="群组名称（与 --url 配合）")
    parser.add_argument("--interactive", action="store_true", help="交互式逐条添加")
    parser.add_argument(
        "--pre-verified", action="store_true",
        help="标记为已校验（直接可派发，跳过手机首次访问校验）。"
             "仅当你确认这些群组真实存在且可访问时使用。"
    )
    args = parser.parse_args()

    if args.csv:
        if not args.csv.exists():
            print(f"❌ CSV 文件不存在: {args.csv}")
            sys.exit(1)
        print(f"导入 {args.csv}（pre_verified={args.pre_verified}）")
        stats = asyncio.run(import_csv(args.csv, pre_verified=args.pre_verified))
        print(f"\n完成：新增 {stats['created']}，更新 {stats['updated']}，错误 {stats['errors']}")
    elif args.url:
        asyncio.run(import_single(args.url, args.name, pre_verified=args.pre_verified))
    elif args.interactive:
        asyncio.run(import_interactive(pre_verified=args.pre_verified))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
