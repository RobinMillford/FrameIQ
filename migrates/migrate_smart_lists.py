"""Smart Lists v1 migration — creates the smart_list table.

db.create_all() on app startup already creates the table for new
deployments; this script exists for existing production databases and to
verify the schema, following the project's manual-migration convention.

Idempotent — safe to re-run. Run: python migrates/migrate_smart_lists.py
"""
from app import app
from models import db


def migrate():
    with app.app_context():
        inspector = db.inspect(db.engine)
        tables = inspector.get_table_names()

        if 'smart_list' in tables:
            print("✅ smart_list table already exists")
        else:
            print("⚙️  Creating smart_list...")
            db.create_all()
            print("✅ smart_list created")

        inspector = db.inspect(db.engine)
        if 'smart_list' in inspector.get_table_names():
            print("\n🎉 Migration complete — Feature 05 schema ready.")
            return 0
        print("\n❌ Migration verification failed (smart_list missing)")
        return 1


if __name__ == '__main__':
    raise SystemExit(migrate())
