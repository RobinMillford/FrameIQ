"""In-app notifications (Feature 04).

Minimal, user-scoped notification model. Types are free-form strings so future
categories (season_premiere, availability_change, social, ...) can be added
without schema changes; only 'new_episode' exists today.

Notifications are created ONLY by backend jobs (today: the UpcomingEpisode
sync) at meaningful state transitions — never during page rendering — and are
idempotent via uq_notification_user_episode (user + type + show + season +
episode). Read state lives in read_at (NULL = unread).
"""
from datetime import datetime

from models.base import db


class Notification(db.Model):
    """A user-scoped in-app notification."""
    __tablename__ = 'notification'

    TYPE_NEW_EPISODE = 'new_episode'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)

    # Type discriminator (see class constants). Kept as a plain string so new
    # categories can be introduced later without migrations.
    type = db.Column(db.String(40), nullable=False, default=TYPE_NEW_EPISODE)

    # Presentation
    title = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text)

    # Navigation — ALWAYS server-generated from validated identifiers; never
    # accepted from client input.
    target_url = db.Column(db.String(500))

    # Entity identifiers (nullable for future non-episode types). Kept on the
    # row itself so idempotency and dedup checks are single-table queries.
    show_id = db.Column(db.Integer, index=True)
    season = db.Column(db.Integer)
    episode = db.Column(db.Integer)
    episode_name = db.Column(db.String(200))
    poster_path = db.Column(db.String(500))

    # State
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    read_at = db.Column(db.DateTime, nullable=True, index=True)

    __table_args__ = (
        # Idempotency: one new-episode notification per user+episode. Nullable
        # (season/episode) columns make this constraint a no-op for future
        # types that don't identify episodes.
        db.UniqueConstraint('user_id', 'type', 'show_id', 'season', 'episode',
                            name='uq_notification_user_episode'),
        # Unread badge lookup (COUNT WHERE user_id=? AND read_at IS NULL).
        db.Index('idx_notification_unread', 'user_id', 'read_at'),
    )

    def __init__(self, **kwargs):
        super(Notification, self).__init__(**kwargs)

    @property
    def is_read(self):
        return self.read_at is not None

    def to_dict(self):
        return {
            'id': self.id,
            'type': self.type,
            'title': self.title,
            'body': self.body,
            'target_url': self.target_url,
            'show_id': self.show_id,
            'season': self.season,
            'episode': self.episode,
            'episode_name': self.episode_name,
            'poster_path': self.poster_path,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'read': self.is_read,
            'read_at': self.read_at.isoformat() if self.read_at else None,
        }

    def __repr__(self):
        return f'<Notification User:{self.user_id} {self.type} {self.title!r}>'
