from app import app
from models import db
from sqlalchemy import text

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
