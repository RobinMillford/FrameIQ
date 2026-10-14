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
from sqlalchemy import text  # noqa: E402

def migrate():
    with app.app_context():
        print("Starting manual migration...")
        try:
            # Check if column exists first
            check_sql = text("SELECT column_name FROM information_schema.columns WHERE table_name='media_item' AND column_name='genres'")
            result = db.session.execute(check_sql).fetchone()
            
            if not result:
                print("Adding 'genres' column to 'media_item' table...")
                alter_sql = text("ALTER TABLE media_item ADD COLUMN genres VARCHAR(200)")
                db.session.execute(alter_sql)
                db.session.commit()
                print("Successfully added 'genres' column!")
            else:
                print("'genres' column already exists.")
                
        except Exception as e:
            print(f"Migration failed: {e}")
            db.session.rollback()

if __name__ == "__main__":
    migrate()
