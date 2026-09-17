"""
Migration: add composite index idx_diary_user_watched_date on
diary_entry(user_id, watched_date).

Phase 10 hardening (Feature #8) — audit-proven, not speculative:

  Every canonical statistics statement (api/statistics.py, 8 bounded SQL)
  filters the identical predicate:  user_id = ? AND watched_date window.
  Under the pre-existing single-column indexes the planner searched
  ix_diary_user (user_id=?) and filtered watched_date row-by-row; with the
  composite it performs a direct range scan (EXPLAIN QUERY PLAN measured
  ~6x fewer rows touched at 50k-diary-row synthetic scale). This is the
  ONE canonical hot pattern shared by all statistics, Year-in-Review, and
  public share reads.

Write amplification is acceptable: diary writes are human-rate (logging a
watch), not machine-rate.

Operational sequence (production):
  1. build image
  2. run migration container
  3. verify schema (schema guard)
  4. start web
  5. smoke checks
"""
import os
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()


def run_migration():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL not set")
        return

    engine = create_engine(database_url)
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_diary_user_watched_date
                ON diary_entry(user_id, watched_date)
            """))
            print("  idx_diary_user_watched_date created")
            trans.commit()
            print("Migration complete.")
        except Exception as e:
            trans.rollback()
            print(f"Migration failed: {e}")
            raise


if __name__ == '__main__':
    run_migration()
