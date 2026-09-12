import os

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_mail import Mail

# ── Shared rate-limit storage (Valkey) ───────────────────────────────────────
#
# RATELIMIT_STORAGE_URI selects where Flask-Limiter keeps its counters:
#
#   unset / memory://   → per-process counters (local dev & CI default).
#                         Documented limits are per-worker under Gunicorn.
#   redis://...@valkey  → counters shared by every worker/container — the
#                         production setting (limits become exact, not N×).
#
# FAILOVER — native Flask-Limiter (in_memory_fallback_enabled):
#   storage error on a request → warn once, count in per-process MemoryStorage
#   until a bounded exponential-backoff `check()` succeeds again. No 500s, no
#   fail-open, no custom retry loops, no background threads. This is
#   protection degradation, never rate-limit removal.
#
# BOOT — construction is safe on a down Valkey: limits' RedisStorage registers
#   Lua scripts without contacting the server, so `limiter.init_app` cannot
#   crash at boot. The app serves with or without Valkey (local fallback).
_URI = os.getenv("RATELIMIT_STORAGE_URI", "")
if _URI.startswith(("redis://", "rediss://", "valkey://")):
    import logging

    logging.getLogger("app.startup").info(
        "Rate limiting: shared storage configured (limiter falls back to "
        "per-worker memory if the shared store is unavailable)")

limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=_URI or "memory://",
    # Degrade to per-process memory counting when the shared store is down —
    # limits stay active (weaker-but-present), requests keep serving.
    in_memory_fallback_enabled=bool(_URI),
    default_limits=["500 per day", "100 per hour"],
    enabled=os.getenv("RATELIMIT_ENABLED", "true").lower() != "false",
)

mail = Mail()
