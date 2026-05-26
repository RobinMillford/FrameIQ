"""
FrameIQ — Flask application entry point.

create_app() is the application factory.
"""

# ── Standard library ──────────────────────────────────────────────────────────
import logging
import os
import urllib.parse
from datetime import timedelta

# ── Third-party ───────────────────────────────────────────────────────────────
from dotenv import load_dotenv
from flask import Flask
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect

# ── Local ─────────────────────────────────────────────────────────────────────
from extensions import limiter, mail
from models import db, User

# ── Routes: core ──────────────────────────────────────────────────────────────
from routes.auth import auth
from routes.main import main
from routes.details import details
from routes.oauth import oauth

# ── Routes: features ──────────────────────────────────────────────────────────
from routes.chat import chat
from routes.reviews import reviews
from routes.reviews_enhanced import reviews_enhanced_bp
from routes.lists import lists
from routes.lists_advanced import lists_advanced
from routes.diary import diary
from routes.tags import tags_bp
from routes.likes import likes_bp
from routes.media_comments import media_comments_bp
from routes.watchlist_priorities import priorities_bp
from routes.tmdb_proxy import tmdb_proxy_bp

# ── Routes: social & discovery ────────────────────────────────────────────────
from routes.social import social
from routes.analytics import analytics
from routes.trending import trending
from routes.activity_feed import activity_feed
from routes.friends_activity import friends_activity
from routes.profile_enhancements import profile_enhancements
from routes.user_discovery import user_discovery
from routes.popular_with_friends import popular_bp
from routes.recommendations import recommendations_bp

# ── Routes: stats, TV, watch ──────────────────────────────────────────────────
from routes.stats import stats_bp
from routes.tv_tracking import tv_tracking
from routes.watch import watch_bp

# ── Routes: AI ────────────────────────────────────────────────────────────────
from src.api.flask_integration import agent_chat

# ── Environment ───────────────────────────────────────────────────────────────
load_dotenv()

_log = logging.getLogger("app.startup")

_REQUIRED_ENV = ["SECRET_KEY", "DATABASE_URL", "TMDB_API_KEY"]
_OPTIONAL_ENV = ["CLOUDINARY_URL", "RATELIMIT_STORAGE_URI",
                 "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"]

for _var in _REQUIRED_ENV:
    if not os.getenv(_var):
        raise RuntimeError(f"Required env var {_var!r} is not set")

for _var in _OPTIONAL_ENV:
    if not os.getenv(_var):
        _log.warning("Optional env var %r not set — related feature may be disabled", _var)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_db_url(raw_url: str) -> str:
    """Normalise DATABASE_URL: add sslmode on Render, strip it for local dev."""
    if not raw_url.startswith("postgresql://"):
        return raw_url
    is_production = bool(os.getenv("RENDER") or os.getenv("K_SERVICE"))
    parsed = urllib.parse.urlparse(raw_url)
    if is_production:
        if not parsed.query:
            return raw_url + "?sslmode=require"
        if "sslmode" not in parsed.query:
            return raw_url + "&sslmode=require"
    else:
        raw_url = raw_url.replace("?sslmode=require", "").replace("&sslmode=require", "")
    return raw_url


_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' cdn.tailwindcss.com cdn.jsdelivr.net cdnjs.cloudflare.com; "
    "style-src 'self' 'unsafe-inline' cdn.tailwindcss.com cdn.jsdelivr.net "
    "           cdnjs.cloudflare.com fonts.googleapis.com; "
    "font-src 'self' fonts.gstatic.com cdnjs.cloudflare.com; "
    "img-src 'self' data: blob: https: via.placeholder.com; "
    "frame-src www.youtube.com youtube.com www.vidking.net vidking.net; "
    "connect-src 'self' db.videasy.net;"
)


# ── Application factory ───────────────────────────────────────────────────────

def create_app() -> Flask:
    app = Flask(__name__)

    # ── Config ────────────────────────────────────────────────────────────────
    is_production = bool(os.getenv("RENDER") or os.getenv("K_SERVICE"))

    app.config.update(
        SECRET_KEY=os.getenv("SECRET_KEY"),
        TMDB_API_KEY=os.getenv("TMDB_API_KEY"),
        MAX_CONTENT_LENGTH=5 * 1024 * 1024,
        # Session security
        SESSION_COOKIE_SECURE=is_production,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        WTF_CSRF_TIME_LIMIT=3600,
        # OAuth
        GOOGLE_CLIENT_ID=os.getenv("GOOGLE_CLIENT_ID"),
        GOOGLE_CLIENT_SECRET=os.getenv("GOOGLE_CLIENT_SECRET"),
        # Database
        SQLALCHEMY_DATABASE_URI=_build_db_url(os.getenv("DATABASE_URL", "")),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True, "pool_recycle": 300},
        # Mail
        MAIL_SERVER=os.getenv("MAIL_SERVER", ""),
        MAIL_PORT=int(os.getenv("MAIL_PORT", "587")),
        MAIL_USE_TLS=os.getenv("MAIL_USE_TLS", "true").lower() == "true",
        MAIL_USERNAME=os.getenv("MAIL_USERNAME", ""),
        MAIL_PASSWORD=os.getenv("MAIL_PASSWORD", ""),
        MAIL_DEFAULT_SENDER=os.getenv("MAIL_DEFAULT_SENDER", "noreply@frameiq.app"),
    )

    # ── Extensions ────────────────────────────────────────────────────────────
    CSRFProtect(app)
    limiter.init_app(app)
    mail.init_app(app)
    db.init_app(app)

    # ── Auth ──────────────────────────────────────────────────────────────────
    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"  # type: ignore[assignment]
    login_manager.login_message = "Please log in to access this page."  # type: ignore[assignment]
    login_manager.remember_cookie_duration = timedelta(days=30)  # type: ignore[assignment]

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    # ── Blueprints ────────────────────────────────────────────────────────────
    blueprints = [
        # Core
        auth, main, details, oauth,
        # Features
        chat, reviews, reviews_enhanced_bp, lists, lists_advanced,
        diary, tags_bp, likes_bp, media_comments_bp, priorities_bp, tmdb_proxy_bp,
        # Social & discovery
        social, analytics, trending, activity_feed, friends_activity,
        profile_enhancements, user_discovery, popular_bp, recommendations_bp,
        # Stats, TV, watch
        stats_bp, tv_tracking, watch_bp,
        # AI
        agent_chat,
    ]
    for bp in blueprints:
        app.register_blueprint(bp)

    # ── Security headers ──────────────────────────────────────────────────────
    @app.after_request
    def set_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = _CSP
        if is_production:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response

    # ── Health check ──────────────────────────────────────────────────────────
    @app.route("/health")
    def health_check():
        return {"status": "ok"}, 200

    # ── Database init ─────────────────────────────────────────────────────────
    with app.app_context():
        _log.info("Database engine: %s",
                  db.engine.url.render_as_string(hide_password=True))
        try:
            db.create_all()
            _log.info("Database tables created successfully")
        except Exception as exc:
            _log.error("Error creating database tables: %s", exc)

    return app


# ── Entry point ───────────────────────────────────────────────────────────────

app = create_app()

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=os.getenv("FLASK_DEBUG", "false").lower() == "true",
    )
