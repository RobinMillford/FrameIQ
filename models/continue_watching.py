"""Continue Watching (intent-based) — persistent watch-intent items.

FrameIQ remembers WHAT the user started; the third-party provider remembers
WHERE they stopped. Playback position is deliberately NOT tracked here.

An item is created when the user opens a watch page (START), and is removed
when the user explicitly finishes it (which records canonical watched state)
or removes it (which does not). No telemetry, no percentages, no timers.
"""
import logging

from models import db

logger = logging.getLogger(__name__)


class ContinueWatchingItem(db.Model):
    """A started-but-not-finished movie or TV episode.

    One logical item per (user, media, position). Rows are created on
    watch-page open (idempotent upsert — re-opening updates started_at) and
    deleted on explicit finish/remove. TV position (season, episode) is
    NULL for movies.
    """
    __tablename__ = 'continue_watching_item'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False,
                        index=True)
    media_type = db.Column(db.String(10), nullable=False)  # 'movie' | 'tv'
    tmdb_id = db.Column(db.Integer, nullable=False, index=True)
    season = db.Column(db.Integer, nullable=True)   # NULL for movies
    episode = db.Column(db.Integer, nullable=True)  # NULL for movies
    title = db.Column(db.String(255))               # display hint; canonical metadata wins at render
    poster_path = db.Column(db.String(500))
    started_at = db.Column(db.DateTime, nullable=False, default=db.func.now())
    updated_at = db.Column(db.DateTime, nullable=False, default=db.func.now(),
                           onupdate=db.func.now())

    __table_args__ = (
        db.UniqueConstraint(
            'user_id', 'media_type', 'tmdb_id', 'season', 'episode',
            name='uq_continue_watching_user_item'),
    )

    def to_dict(self):
        return {
            'media_type': self.media_type,
            'tmdb_id': self.tmdb_id,
            'season': self.season,
            'episode': self.episode,
            'started_at': self.started_at.isoformat() if self.started_at else None,
        }
