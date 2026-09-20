"""Movie release dates cached from TMDb (Feature 10B).

Why this table exists: MediaItem.release_date is written once at
watchlist-add time (utils/collections.get_or_create_media_item) and is
never refreshed — future-dated releases shift and missing dates stay
missing forever. The calendar needs a refreshable, per-region source of
truth for watchlisted movies.

Design constraints:
- Keyed by (tmdb_id, region, release_type): the TMDb release-date API
  is per-region and one title can have several release types, but the
  same date/type never duplicates. Re-running the sync upserts in place.
- ``release_type`` stores the TMDb release type INTEGER verbatim (1-6)
  so later phases can refine labeling without a migration; the calendar
  maps it to conservative labels and treats unknown values as
  ``unknown`` — never fabricated.
- ``fetched_at`` drives staleness-bounded synchronization; the calendar
  read path never touches TMDb, it consumes only this cache.
"""
from datetime import datetime

from models.base import db


class MovieReleaseDate(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tmdb_id = db.Column(db.Integer, nullable=False, index=True)
    region = db.Column(db.String(2), nullable=False, default="US")
    release_type = db.Column(db.Integer, nullable=False)
    release_date = db.Column(db.Date, nullable=False)
    fetched_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint(
            "tmdb_id", "region", "release_type",
            name="uq_release_tmdb_region_type"),
        db.Index("idx_release_tmdb_region", "tmdb_id", "region"),
    )

    def __repr__(self):
        return (
            "<MovieReleaseDate tmdb=%s region=%s type=%s date=%s>"
            % (self.tmdb_id, self.region, self.release_type,
               self.release_date))
