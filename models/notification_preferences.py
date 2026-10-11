"""Per-user notification preferences (Feature F12).

One row per user (enforced by a UNIQUE constraint), holding the enabled/disabled
state for each SUPPORTED in-app notification category.

Why a table and not a column on ``user``: a preference is a small record with
its own lifecycle (created the first time the user saves), and keeping it
separate means adding a supported category is an additive migration rather than
an ``ALTER TABLE user`` on the hottest table in the database. This mirrors
``models/streaming.py`` — one small per-user settings table with an explicit
column per setting, rather than an opaque JSON blob.

Only categories with a REAL producer are modelled here. A category with no
reliable event source is deliberately absent rather than present-but-fake: see
``api/notification_preferences.CATEGORIES`` for the supported set and the
documented deferrals.

Reading preferences NEVER creates a row. A user who has never opened the
settings page has no record and receives the documented DEFAULTS, which is what
keeps existing users' behaviour unchanged without a backfill migration.
"""
from datetime import datetime

from models.base import db


class UserNotificationPreference(db.Model):
    """Which in-app notification categories a user wants. One row per user."""
    __tablename__ = 'user_notification_preference'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'),
                        nullable=False, index=True)

    # ── Supported categories ────────────────────────────────────────────────
    # new_episode: an episode of a tracked show has aired.
    #   Defaults True — this is the pre-F12 behaviour and must not change for
    #   existing users.
    new_episode = db.Column(db.Boolean, nullable=False, default=True)

    # season_premiere: the FIRST episode of a season has aired. Defaults False
    #   so opting in is explicit and no existing user starts receiving a
    #   different notification type without asking for it.
    season_premiere = db.Column(db.Boolean, nullable=False, default=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    user = db.relationship('User', backref=db.backref(
        'notification_preference', uselist=False, cascade='all, delete-orphan'))

    __table_args__ = (
        # One preference record per user: makes duplicate rows impossible at
        # the storage layer, so concurrent first-saves cannot fork the record.
        db.UniqueConstraint('user_id', name='uq_user_notification_preference'),
    )

    def __init__(self, **kwargs):
        super(UserNotificationPreference, self).__init__(**kwargs)

    def to_dict(self):
        return {
            'new_episode': bool(self.new_episode),
            'season_premiere': bool(self.season_premiere),
        }

    def __repr__(self):
        return (f'<UserNotificationPreference u={self.user_id} '
                f'new_episode={self.new_episode} '
                f'season_premiere={self.season_premiere}>')