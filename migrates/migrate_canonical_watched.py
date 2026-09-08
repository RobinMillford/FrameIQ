#!/usr/bin/env python3
"""
Migration: reconcile derived watched state with canonical diary history.

FrameIQ's canonical movie watch history is DiaryEntry (one row per watch
event, rewatches included). The user_viewed junction table is a derived
boolean set ("has watched at least once") that must stay in sync.

This one-time, idempotent backfill reconciles pre-existing data in both
directions — no rows are ever deleted:

  1. DiaryEntry (movie) with no user_viewed row  -> insert user_viewed row
     (date_viewed = the user's first watch event for that movie).
  2. user_viewed (movie) row with no DiaryEntry  -> insert one DiaryEntry
     (watched_date = date_viewed date, is_rewatch=False) so the diary is the
     authoritative history going forward.

Safe to run multiple times; each pass converges and re-runs are no-ops.
TV rows in user_viewed are intentionally left untouched (TV tracking is a
separate system).

Usage:
    python migrates/migrate_canonical_watched.py
"""
import sys
from datetime import datetime
from pathlib import Path

# Allow running as `python migrates/migrate_canonical_watched.py` from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app import app
from models import db, DiaryEntry, user_viewed


def migrate_canonical_watched():
    with app.app_context():
        print("🎬 Canonical watched-state backfill (DiaryEntry <-> user_viewed)")
        print("=" * 60)

        # ── Pass 1: diary -> viewed ────────────────────────────────────────
        diary_rows = db.session.execute(
            select(DiaryEntry.user_id, DiaryEntry.media_id)
            .where(DiaryEntry.media_type == 'movie')
            .distinct()
        ).all()
        viewed_keys = {
            (u, m) for u, m in db.session.execute(
                select(user_viewed.c.user_id, user_viewed.c.media_id)
                .where(user_viewed.c.media_type == 'movie')
            ).all()
        }

        missing = [k for k in diary_rows if k not in viewed_keys]
        inserted_viewed = 0
        for user_id, media_id in missing:
            first = db.session.query(DiaryEntry.watched_date).filter_by(
                user_id=user_id, media_id=media_id, media_type='movie',
            ).order_by(DiaryEntry.watched_date.asc()).first()
            watched_at = (
                datetime.combine(first[0], datetime.min.time())
                if first and first[0] else datetime.utcnow()
            )
            db.session.execute(user_viewed.insert().values(
                user_id=user_id,
                media_id=media_id,
                media_type='movie',
                date_viewed=watched_at,
            ))
            inserted_viewed += 1
        db.session.commit()
        print(f"✅ user_viewed rows inserted from diary: {inserted_viewed}")

        # ── Pass 2: viewed -> diary ────────────────────────────────────────
        viewed_rows = db.session.execute(
            select(user_viewed.c.user_id, user_viewed.c.media_id,
                   user_viewed.c.date_viewed)
            .where(user_viewed.c.media_type == 'movie')
        ).all()
        diary_keys = {
            (u, m) for u, m in db.session.execute(
                select(DiaryEntry.user_id, DiaryEntry.media_id)
                .where(DiaryEntry.media_type == 'movie')
            ).all()
        }

        inserted_diary = 0
        for user_id, media_id, date_viewed in viewed_rows:
            if (user_id, media_id) in diary_keys:
                continue
            watched_date = (
                date_viewed.date() if date_viewed else datetime.utcnow().date()
            )
            db.session.add(DiaryEntry(
                user_id=user_id,
                media_id=media_id,
                media_type='movie',
                watched_date=watched_date,
                rating=None,
                is_rewatch=False,
            ))
            inserted_diary += 1
        db.session.commit()
        print(f"✅ DiaryEntry rows inserted from user_viewed: {inserted_diary}")

        # ── Verification ───────────────────────────────────────────────────
        final_diary = {
            (u, m) for u, m in db.session.execute(
                select(DiaryEntry.user_id, DiaryEntry.media_id)
                .where(DiaryEntry.media_type == 'movie')
            ).all()
        }
        final_viewed = {
            (u, m) for u, m in db.session.execute(
                select(user_viewed.c.user_id, user_viewed.c.media_id)
                .where(user_viewed.c.media_type == 'movie')
            ).all()
        }
        only_diary = final_diary - final_viewed
        only_viewed = final_viewed - final_diary
        print(f"\n📊 Verification: diary={len(final_diary)} viewed={len(final_viewed)}")
        print(f"   diary-only keys: {len(only_diary)}")
        print(f"   viewed-only keys: {len(only_viewed)}")
        if not only_diary and not only_viewed:
            print("🎉 Migration complete — movie watched state is fully reconciled.")
        else:
            print("⚠️  Residual mismatch — re-run this script to converge.")


if __name__ == '__main__':
    migrate_canonical_watched()
