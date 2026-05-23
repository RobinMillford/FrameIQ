import os
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# Use RATELIMIT_STORAGE_URI env var for Redis in prod (memory:// for local/single-worker)
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=os.getenv("RATELIMIT_STORAGE_URI", "memory://"),
    default_limits=["500 per day", "100 per hour"],
)
