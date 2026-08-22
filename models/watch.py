"""Streaming playback progress (Continue Watching feature)."""
from datetime import datetime

from models.base import db


class WatchProgress(db.Model):
    """Tracks in-progress viewing for Continue Watching feature."""
    __tablename__ = 'watch_progress'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    tmdb_id = db.Column(db.Integer, nullable=False, index=True)
    media_type = db.Column(db.String(10), nullable=False)  # 'movie', 'tv', 'anime'

    # TV / anime only
    season = db.Column(db.Integer, nullable=True)
    episode = db.Column(db.Integer, nullable=True)

    # Playback state
    current_time = db.Column(db.Float, default=0)
    duration = db.Column(db.Float, default=0)

    # Cached display info
    title = db.Column(db.String(300))
    poster_path = db.Column(db.String(300))

    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = db.relationship('User', backref=db.backref('watch_progress', lazy='dynamic'))

    __table_args__ = (
        # Partial unique indexes handle NULL season/episode correctly (NULL != NULL in PG).
        # uq_watch_progress is kept as a fallback for non-PG databases.
        db.UniqueConstraint('user_id', 'tmdb_id', 'media_type', 'season', 'episode',
                            name='uq_watch_progress'),
        db.Index('idx_wp_movie_uniq', 'user_id', 'tmdb_id', 'media_type',
                 unique=True,
                 postgresql_where=db.text('season IS NULL AND episode IS NULL')),
        db.Index('idx_wp_tv_uniq', 'user_id', 'tmdb_id', 'media_type', 'season', 'episode',
                 unique=True,
                 postgresql_where=db.text('season IS NOT NULL AND episode IS NOT NULL')),
        db.Index('idx_wp_user_updated', 'user_id', 'updated_at'),
    )

    @property
    def progress_pct(self):
        if not self.duration:
            return 0
        return min(100, round(self.current_time / self.duration * 100, 1))

    @property
    def watch_url(self):
        if self.media_type == 'movie':
            return f'/watch/movie/{self.tmdb_id}'
        return f'/watch/tv/{self.tmdb_id}/{self.season}/{self.episode}?type={self.media_type}'

    def to_dict(self):
        return {
            'id': self.id,
            'tmdb_id': self.tmdb_id,
            'media_type': self.media_type,
            'season': self.season,
            'episode': self.episode,
            'current_time': self.current_time,
            'duration': self.duration,
            'progress_pct': self.progress_pct,
            'title': self.title,
            'poster_path': self.poster_path,
            'watch_url': self.watch_url,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self):
        return f'<WatchProgress user={self.user_id} {self.media_type} {self.tmdb_id}>'
