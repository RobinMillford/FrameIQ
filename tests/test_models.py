import pytest
from datetime import date, datetime
from werkzeug.security import generate_password_hash, check_password_hash


def test_user_password_hash(app, db):
    from models import User

    with app.app_context():
        u = User(
            username="testuser",
            email="test@example.com",
            password_hash=generate_password_hash("SecurePass1"),
        )
        db.session.add(u)
        db.session.commit()

        fetched = User.query.filter_by(username="testuser").first()
        assert fetched is not None
        assert check_password_hash(fetched.password_hash, "SecurePass1")
        assert not check_password_hash(fetched.password_hash, "WrongPass")

        db.session.delete(fetched)
        db.session.commit()


def test_media_item_creation(app, db):
    from models import MediaItem

    with app.app_context():
        m = MediaItem(
            tmdb_id=550,
            media_type="movie",
            title="Fight Club",
            release_date=date(1999, 10, 15),
        )
        db.session.add(m)
        db.session.commit()

        fetched = MediaItem.query.filter_by(tmdb_id=550).first()
        assert fetched is not None
        assert fetched.title == "Fight Club"
        assert fetched.media_type == "movie"

        db.session.delete(fetched)
        db.session.commit()


def test_review_requires_user_and_media(app, db):
    from models import User, MediaItem, Review
    from werkzeug.security import generate_password_hash

    with app.app_context():
        u = User(
            username="reviewer",
            email="reviewer@example.com",
            password_hash=generate_password_hash("Pass1234"),
        )
        m = MediaItem(tmdb_id=999, media_type="movie", title="Test Film")
        db.session.add_all([u, m])
        db.session.flush()

        r = Review(
            user_id=u.id,
            media_id=m.id,
            media_type="movie",
            rating=4.0,
            content="Great film",
        )
        db.session.add(r)
        db.session.commit()

        fetched = Review.query.filter_by(user_id=u.id).first()
        assert fetched is not None
        assert fetched.rating == 4.0
        assert not fetched.is_deleted

        db.session.delete(fetched)
        db.session.delete(m)
        db.session.delete(u)
        db.session.commit()
