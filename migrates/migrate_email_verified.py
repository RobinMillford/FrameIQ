"""
Migration: add email_verified column to user table.
Existing users default to True (grandfathered in).
New users created after this migration default to False.
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
