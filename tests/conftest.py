import os
import pytest

os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TMDB_API_KEY", "test-tmdb-key")
os.environ.setdefault("WTF_CSRF_ENABLED", "False")


@pytest.fixture(scope="session")
def app():
    from app import app as flask_app, db as _db

    flask_app.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=False,
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        SERVER_NAME=None,
    )

    with flask_app.app_context():
        _db.create_all()
        yield flask_app
        _db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def db(app):
    from app import db as _db
    return _db
