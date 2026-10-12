"""Smart Lists v1 migration — creates the smart_list table.

db.create_all() on app startup already creates the table for new
deployments; this script exists for existing production databases and to
verify the schema, following the project's manual-migration convention.

Idempotent — safe to re-run. Run: python migrates/migrate_smart_lists.py
"""
# F22: authorize the database target BEFORE any application import, engine
# construction or DDL/DML. `import app` builds an engine and runs the schema
# guard, so a check placed after it would already be too late.
import os as _f22_os  # F22: make `utils` importable regardless of cwd  # noqa: E402
import sys as _f22_sys  # noqa: E402
_f22_sys.path.insert(0, _f22_os.path.dirname(
    _f22_os.path.dirname(_f22_os.path.abspath(__file__))))
from utils.legacy_migration_guard import authorize_legacy_migration  # noqa: E402

authorize_legacy_migration(__file__)

from app import app  # noqa: E402
from models import db  # noqa: E402


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
