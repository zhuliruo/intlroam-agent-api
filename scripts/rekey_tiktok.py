# One-off: rekey existing TikTok comment-type rows in fb_engagements to the new
# (platform, counterpart, kind) key so upgraded phones don't re-reply to people already answered.
# Run on HK inside /opt/intlroam_api with the app's venv. Dry-run unless --apply.
import asyncio, sys
from sqlalchemy import text
from app.api.v2.engagement import thread_key
from app.database import SessionLocal as async_session

APPLY = "--apply" in sys.argv

async def main():
    async with async_session() as db:
        rows = (await db.execute(text(
            "SELECT id, device_id, kind, thread_key, counterpart FROM fb_engagements "
            "WHERE platform='tiktok' AND kind <> 'dm' ORDER BY id"))).all()
        taken = {(r.device_id, r.kind, r.thread_key) for r in rows}
        n = 0
        for r in rows:
            nk = thread_key(r.kind, r.counterpart, None, "tiktok")
            if nk == r.thread_key or (r.device_id, r.kind, nk) in taken:
                continue
            print(f"id={r.id} {r.device_id[:6]} {r.kind} {r.counterpart!r}: {r.thread_key} -> {nk}")
            taken.add((r.device_id, r.kind, nk)); n += 1
            if APPLY:
                await db.execute(text("UPDATE fb_engagements SET thread_key=:k WHERE id=:i"),
                                 {"k": nk, "i": r.id})
        if APPLY:
            await db.commit()
        print(f"{'updated' if APPLY else 'would update'} {n} of {len(rows)} rows")

asyncio.run(main())
