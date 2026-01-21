from app import app
from models import db
from sqlalchemy import text

def migrate():
    with app.app_context():
        print("Starting comment threading migration...")
        try:
            # Check if column exists first
            check_sql = text("SELECT column_name FROM information_schema.columns WHERE table_name='review_comment' AND column_name='parent_id'")
            result = db.session.execute(check_sql).fetchone()
            
            if not result:
                print("Adding 'parent_id' column to 'review_comment' table...")
                alter_sql = text("ALTER TABLE review_comment ADD COLUMN parent_id INTEGER REFERENCES review_comment(id)")
                db.session.execute(alter_sql)
                db.session.commit()
                print("Successfully added 'parent_id' column!")
            else:
                print("'parent_id' column already exists.")
                
        except Exception as e:
            print(f"Migration failed: {e}")
            db.session.rollback()

if __name__ == "__main__":
    migrate()
