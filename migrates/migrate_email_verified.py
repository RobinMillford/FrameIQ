"""
Migration: add email_verified column to user table.
Existing users default to True (grandfathered in).
New users created after this migration default to False.
"""
import os
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()


# F22: authorize the database target BEFORE any application import, engine
# construction or DDL/DML. `import app` builds an engine and runs the schema
# guard, so a check placed after it would already be too late.
import os as _f22_os  # F22: make `utils` importable regardless of cwd  # noqa: E402
import sys as _f22_sys  # noqa: E402
_f22_sys.path.insert(0, _f22_os.path.dirname(
    _f22_os.path.dirname(_f22_os.path.abspath(__file__))))
from utils.legacy_migration_guard import authorize_legacy_migration  # noqa: E402

authorize_legacy_migration(__file__)


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
                ALTER TABLE "user"
                ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT TRUE
            """))
            print("email_verified column added (existing users grandfathered as verified).")
            trans.commit()
        except Exception as e:
            trans.rollback()
            print(f"Migration failed: {e}")
            raise


if __name__ == '__main__':
    run_migration()
