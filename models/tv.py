"""TV tracking models: show progress, episode watches, upcoming episodes."""
from datetime import datetime

from models.base import db


class TVShowProgress(db.Model):
    """Track user's progress through TV shows - overall show tracking"""
    __tablename__ = 'tv_show_progress'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    show_id = db.Column(db.Integer, nullable=False, index=True)  # TMDB show ID

    # Progress tracking
    total_seasons = db.Column(db.Integer, default=0)  # Total seasons in show
    watched_seasons = db.Column(db.Integer, default=0)  # Completed seasons
    total_episodes = db.Column(db.Integer, default=0)  # Total episodes in show
    watched_episodes = db.Column(db.Integer, default=0)  # Episodes watched

    # Status
    status = db.Column(db.String(20), default='watching')  # 'watching', 'completed', 'plan_to_watch', 'dropped'
    is_favorite = db.Column(db.Boolean, default=False)

    # Timestamps
    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_watched = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = db.relationship('User', backref=db.backref('tv_progress', lazy='dynamic'))
    episodes = db.relationship('TVEpisodeWatch', backref='show_progress', cascade='all, delete-orphan', lazy='dynamic')

    # Constraints - one progress entry per user per show
    __table_args__ = (
        db.UniqueConstraint('user_id', 'show_id', name='unique_user_show_progress'),
    )

    def __init__(self, **kwargs):
        super(TVShowProgress, self).__init__(**kwargs)

    def calculate_progress_percentage(self):
        """Calculate completion percentage"""
        if self.total_episodes == 0:
            return 0
        return round((self.watched_episodes / self.total_episodes) * 100, 1)

    def to_dict(self):
        """Convert to dictionary for JSON responses"""
        return {
            'id': self.id,
            'user_id': self.user_id,
            'show_id': self.show_id,
            'total_seasons': self.total_seasons,
            'watched_seasons': self.watched_seasons,
            'total_episodes': self.total_episodes,
            'watched_episodes': self.watched_episodes,
            'progress_percentage': self.calculate_progress_percentage(),
            'status': self.status,
            'is_favorite': self.is_favorite,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'last_watched': self.last_watched.isoformat() if self.last_watched else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'created_at': self.created_at.isoformat()
        }

    def __repr__(self):
        return (
            f'<TVShowProgress User:{self.user_id} Show:{self.show_id} '
            f'{self.watched_episodes}/{self.total_episodes} episodes>'
        )


class TVEpisodeWatch(db.Model):
    """Track individual episode watches"""
    __tablename__ = 'tv_episode_watch'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    show_id = db.Column(db.Integer, nullable=False, index=True)  # TMDB show ID
    progress_id = db.Column(db.Integer, db.ForeignKey('tv_show_progress.id'), nullable=True, index=True)

    # Episode identification
    season_number = db.Column(db.Integer, nullable=False)
    episode_number = db.Column(db.Integer, nullable=False)
    episode_name = db.Column(db.String(200))  # Episode title

    # Watch details
    watched_date = db.Column(db.Date, nullable=False, default=datetime.utcnow)
    rating = db.Column(db.Float)  # Optional rating 0.5 to 5.0
    notes = db.Column(db.Text)  # User notes about episode
    is_rewatch = db.Column(db.Boolean, default=False)

    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = db.relationship('User', backref=db.backref('episode_watches', lazy='dynamic'))

    # Constraints
    __table_args__ = (
        db.CheckConstraint('rating IS NULL OR (rating >= 0.5 AND rating <= 5.0)', name='valid_episode_rating'),
        db.Index('idx_user_show_season_episode', 'user_id', 'show_id', 'season_number', 'episode_number'),
    )

    def __init__(self, **kwargs):
        super(TVEpisodeWatch, self).__init__(**kwargs)

    def to_dict(self):
        """Convert to dictionary for JSON responses"""
        return {
            'id': self.id,
            'user_id': self.user_id,
            'show_id': self.show_id,
            'season_number': self.season_number,
            'episode_number': self.episode_number,
            'episode_name': self.episode_name,
            'watched_date': self.watched_date.isoformat() if self.watched_date else None,
            'rating': self.rating,
            'notes': self.notes,
            'is_rewatch': self.is_rewatch,
            'created_at': self.created_at.isoformat()
        }

    def __repr__(self):
        return f'<TVEpisodeWatch User:{self.user_id} S{self.season_number}E{self.episode_number}>'


class UpcomingEpisode(db.Model):
    """Calendar of upcoming episodes for shows user is tracking"""
    __tablename__ = 'upcoming_episode'

    id = db.Column(db.Integer, primary_key=True)
    show_id = db.Column(db.Integer, nullable=False, index=True)  # TMDB show ID
    show_name = db.Column(db.String(200), nullable=False)
    poster_path = db.Column(db.String(200))

    # Episode details
    season_number = db.Column(db.Integer, nullable=False)
    episode_number = db.Column(db.Integer, nullable=False)
    episode_name = db.Column(db.String(200))
    episode_overview = db.Column(db.Text)

    # Air date
    air_date = db.Column(db.Date, nullable=False, index=True)
    air_time = db.Column(db.String(10))  # e.g., "21:00"

    # Metadata
    runtime = db.Column(db.Integer)  # Episode runtime in minutes
    still_path = db.Column(db.String(200))  # Episode screenshot

    # Tracking
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Constraints - one entry per episode
    __table_args__ = (
        db.UniqueConstraint('show_id', 'season_number', 'episode_number', name='unique_episode'),
        db.Index('idx_air_date', 'air_date'),
    )

    def __init__(self, **kwargs):
        super(UpcomingEpisode, self).__init__(**kwargs)

    def days_until_air(self):
        """Calculate days until episode airs"""
        if self.air_date:
            delta = self.air_date - datetime.utcnow().date()
            return delta.days
        return None

    def to_dict(self):
        """Convert to dictionary for JSON responses"""
        return {
            'id': self.id,
            'show_id': self.show_id,
            'show_name': self.show_name,
            'poster_path': self.poster_path,
            'season_number': self.season_number,
            'episode_number': self.episode_number,
            'episode_name': self.episode_name,
            'episode_overview': self.episode_overview,
            'air_date': self.air_date.isoformat() if self.air_date else None,
            'air_time': self.air_time,
            'runtime': self.runtime,
            'still_path': self.still_path,
            'days_until_air': self.days_until_air(),
            'created_at': self.created_at.isoformat()
        }

    def __repr__(self):
        return f'<UpcomingEpisode {self.show_name} S{self.season_number}E{self.episode_number} on {self.air_date}>'
