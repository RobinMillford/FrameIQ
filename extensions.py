import os
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_mail import Mail

# Rate limiter — set RATELIMIT_STORAGE_URI=redis://... in prod for multi-worker correctness.
# memory:// is per-process; each gunicorn worker has its own counter.
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=os.getenv("RATELIMIT_STORAGE_URI", "memory://"),
    default_limits=["500 per day", "100 per hour"],
    enabled=os.getenv("RATELIMIT_ENABLED", "true").lower() != "false",
)

mail = Mail()
