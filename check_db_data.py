from app import app
from models import db, User, Review

def check_data():
    with app.app_context():
        users = User.query.all()
        print(f"{'ID':<10} | {'Username':<20} | {'Reviews':<10}")
        print("-" * 45)
        for u in users:
            reviews_count = Review.query.filter_by(user_id=u.id).count()
            print(f"{u.id:<10} | {u.username:<20} | {reviews_count:<10}")

if __name__ == "__main__":
    check_data()
