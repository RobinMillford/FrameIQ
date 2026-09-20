"""Sync watchlist movie release dates (Feature 10B).

Scheduled daily (see .github/workflows/sync-watchlist-release-data.yml).
Bounded, failure-isolated, idempotent — see api/watchlist_release_sync.
"""
import sys
import os

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..')))

from app import app                                       # noqa: E402
from api.watchlist_release_sync import (                  # noqa: E402
    sync_watchlist_release_data,
)


def main():
    print("=" * 60)
    print("SYNCING WATCHLIST RELEASE DATES")
    print("=" * 60)
    with app.app_context():
        result = sync_watchlist_release_data()
    print("Sync complete: %s" % result)


if __name__ == '__main__':
    main()
