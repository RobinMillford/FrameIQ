"""
FrameIQ — Flask application entry point.

create_app() is the application factory.
"""

# ── Standard library ──────────────────────────────────────────────────────────
import logging
import os
from datetime import timedelta

# ── Third-party ───────────────────────────────────────────────────────────────
from dotenv import load_dotenv
from flask import Flask
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix

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
import routes.view_state as _view_state_routes  # noqa: F401 — attaches to main
import routes.account_export as _account_export_routes  # noqa: F401 — attaches to main
import routes.account_import as _account_import_routes  # noqa: F401 — attaches to main
from routes.tags import tags_bp
from routes.likes import likes_bp
from routes.media_comments import media_comments_bp
from routes.watchlist_priorities import priorities_bp
from routes.tmdb_proxy import tmdb_proxy_bp
from routes.availability import availability_bp

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
from routes.seo import seo_bp

# ── Routes: private taste profile (Taste DNA) ─────────────────────────────────
from routes.taste_profile import taste_profile_bp
from routes.statistics import statistics_bp

# Feature 10 — unified personal entertainment calendar
from routes.calendar import calendar_bp

# ── Routes: stats, TV, watch ──────────────────────────────────────────────────
from routes.stats import stats_bp
from routes.tv_tracking import tv_tracking
from routes.watch import watch_bp
from routes.notifications import notifications_bp
from routes.smart_lists import smart_lists_bp
from routes.recommendation_feedback import recommendation_feedback_bp
from routes.for_you import for_you_bp

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
    """Normalise DATABASE_URL: add sslmode when missing on production hosts."""
    if not raw_url.startswith("postgresql://"):
        return raw_url
    # URL already has sslmode — trust it exactly as provided (Neon, Supabase, etc.)
    if "sslmode=" in raw_url:
        return raw_url
    # No sslmode in URL — add it only on known production platforms
    is_production = bool(os.getenv("RENDER") or os.getenv("K_SERVICE"))
    if is_production:
        separator = "&" if "?" in raw_url else "?"
        return raw_url + f"{separator}sslmode=require"
    return raw_url


_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' cdn.tailwindcss.com cdn.jsdelivr.net cdnjs.cloudflare.com; "
    "style-src 'self' 'unsafe-inline' cdn.tailwindcss.com cdn.jsdelivr.net "
    "           cdnjs.cloudflare.com fonts.googleapis.com; "
    "font-src 'self' fonts.gstatic.com cdnjs.cloudflare.com; "
    "img-src 'self' data: blob: https: via.placeholder.com; "
    "frame-src www.youtube.com youtube.com www.vidking.net vidking.net "
    "           www.rivestream.app rivestream.app vidy.st www.vidy.st "
    "           1embed.cc; "
    "connect-src 'self';"
)


# ── Application factory ───────────────────────────────────────────────────────

def create_app() -> Flask:
    app = Flask(__name__)

    # ── Proxy fix (nginx → gunicorn) ──────────────────────────────────────────
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    # ── Config ────────────────────────────────────────────────────────────────
    is_production = bool(
        os.getenv("RENDER") or os.getenv("K_SERVICE") or os.getenv("APP_ENV") == "production"
    )

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
        SQLALCHEMY_ENGINE_OPTIONS={
            "pool_pre_ping": True,
            "pool_recycle": 300,
            # connect_timeout only valid for psycopg2 (PostgreSQL), not SQLite
            **({"connect_args": {"connect_timeout": 10}}
               if os.getenv("DATABASE_URL", "").startswith("postgresql") else {}),
        },
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
        availability_bp,
        # Social & discovery
        social, analytics, trending, activity_feed, friends_activity,
        profile_enhancements, user_discovery, popular_bp, recommendations_bp,
        # SEO
        seo_bp,
        # Stats, TV, watch, notifications, smart lists
        stats_bp, tv_tracking, watch_bp, notifications_bp, smart_lists_bp,
        recommendation_feedback_bp, for_you_bp, taste_profile_bp,
        statistics_bp,
        calendar_bp,
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
    #
    # THE INVARIANT: starting the application never changes the schema.
    #
    # This used to call db.create_all() unconditionally, which made every app
    # import a DDL statement against whatever DATABASE_URL pointed at. Two
    # things were wrong with that:
    #
    #   1. A production web worker silently owned the schema. A forgotten
    #      migration would "fix itself" on boot, so the deploy workflow's
    #      migration step was not actually load-bearing — and a rollout could
    #      half-apply DDL before failing on the next statement.
    #   2. Any diagnostic that imported `app` inherited that authority. During
    #      Task F7 a read-only schema investigation ran `python -c "from app
    #      import app ..."` with DATABASE_URL pointing at production Neon. The
    #      import attempted DDL. It rolled back, but reaching a live database
    #      through a module import is a hazard that has to be designed out
    #      rather than remembered.
    #
    # So schema creation is now OFF by default and must be asked for explicitly:
    #
    #   production   migrations only. The web process never issues DDL.
    #   tests        tests/conftest.py calls db.create_all() in a fixture.
    #   dev          scripts/bootstrap_dev_schema.py (or `make dev-schema`),
    #                 which says out loud that it is creating tables.
    #
    # The schema guard below is what enforces the result: if a migration has
    # not been applied, startup FAILS LOUDLY instead of quietly repairing
    # itself. Failing to boot on an unprepared schema is the correct outcome —
    # it is the same posture the guard already took for missing columns.
    with app.app_context():
        _log.info("Database engine: %s",
                  db.engine.url.render_as_string(hide_password=True))

        if os.getenv("FRAMEIQ_AUTO_CREATE_SCHEMA") == "1":
            # Explicit, opt-in, and deliberately loud. Intended for a local
            # database and for tests that set it deliberately; never for a
            # production web process.
            _log.warning(
                "FRAMEIQ_AUTO_CREATE_SCHEMA=1 — creating tables. This must "
                "not be set for a production web process.")
            try:
                db.create_all()
                _log.info("Database tables created successfully")
            except Exception as exc:
                _log.error("Error creating database tables: %s", exc)
                raise
        else:
            _log.info(
                "Schema creation disabled (default). The schema is owned by "
                "migrates/; the parity guard below will refuse to boot if a "
                "migration has not been applied.")

        # ── Schema parity guard (read-only) ───────────────────────────────────
        # The guard exists because migrations were forgotten, and it compares
        # declared models against the live schema read-only. With implicit
        # creation gone it is the only thing standing between a half-migrated
        # database and a worker that boots "successfully" and then serves
        # broken pages. It runs once per worker boot.
        from utils.schema_guard import ensure_schema_compatible
        ensure_schema_compatible(app)

    return app


# ── Entry point ───────────────────────────────────────────────────────────────

app = create_app()

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=os.getenv("FLASK_DEBUG", "false").lower() == "true",
    )
