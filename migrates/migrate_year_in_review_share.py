"""
Migration (Feature #8, Phase 9): create the year_in_review_share table.

The share record is a minimal authorization row binding ONE user to ONE
calendar year through an opaque share token. Only a SHA-256 hash of the
token is stored; the raw token is never persisted or logged.

Operational sequence (see README / Makefile):
  1. build image
  2. run migration container:  python migrates/migrate_year_in_review_share.py
  3. verify migration (re-run prints status without altering anything)
  4. bring web up
  5. schema guard validates the expected table exists
  6. run smoke checks

Re-running is safe: the CREATE TABLE is guarded by a catalog check.
"""
import os
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()

# Must match models/year_in_review_share.py.
TABLE_SQL = """
CREATE TABLE IF NOT EXISTS year_in_review_share (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    year SMALLINT NOT NULL,
    token_hash CHAR(64) NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW() NOT NULL,
    revoked_at TIMESTAMP WITHOUT TIME ZONE
)
"""


def run_migration():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL not set")
        return

    engine = create_engine(database_url)
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            exists = conn.execute(text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'year_in_review_share'"
            )).fetchone()

            if exists:
                print("  year_in_review_share already exists — nothing to do")
                trans.commit()
                return

            conn.execute(text(TABLE_SQL))
            print("  year_in_review_share created")

            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "uq_yir_share_active_user_year "
                "ON year_in_review_share (user_id, year) "
                "WHERE revoked_at IS NULL"
            ))
            print("  uq_yir_share_active_user_year created (partial unique index)")

            trans.commit()
            print("Migration complete.")
        except Exception as e:
            trans.rollback()
            print(f"Migration failed: {e}")
            raise


if __name__ == '__main__':
    run_migration()
