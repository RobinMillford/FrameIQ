"""Taste Profile (Feature #6, Phase 1) — persisted derived personalization state.

One row per user. Stores the COMPUTED taste representation (genre/decade
weights, director affinity, runtime preference, confidence) that later
phases (api/taste_profile.py, For You, CineBot context) will read.

TasteProfile is DERIVED STATE, never a source of truth:

    raw user signals (reviews, diary, likes, watchlist, TV tracking)
        ↓  (computed later by api/taste_profile.py — NOT in this module)
    persisted TasteProfile
        ↓
    homepage For You · CineBot · future Smart List filters

This table deliberately does NOT duplicate DiaryEntry/Review/MediaLike/
watchlist/episode rows, does NOT store recommendation results or candidate
titles, and is NOT an event table (RecommendationFeedback comes later).

JSON-ish fields follow the project convention established by SmartList:
a db.Text column plus a Python property that serializes/parses JSON
(SmartList.filters_json / .filters).
"""
import json
from datetime import datetime

from models.base import db


class TasteProfile(db.Model):
    """Persisted, explainable taste profile for one user (1:1)."""
    __tablename__ = 'taste_profile'

    id = db.Column(db.Integer, primary_key=True)

    # One profile per user. Cascade matches User-owned model conventions
    # (notification.py, smart_lists.py): deleting a user removes the profile.
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False,
                        unique=True, index=True)

    # ── Computed dimensions (all serialized JSON documents) ────────────────
    # {"Thriller": 12.5, "Drama": 8.4} — explainable weighted sums.
    genre_weights_json = db.Column(db.Text, nullable=False, default='{}')
    # {"2010s": 8.0, "1990s": 5.5}
    decade_weights_json = db.Column(db.Text, nullable=False, default='{}')
    # {"Denis Villeneuve": 3.0, ...} — top-N explainable directors.
    director_affinity_json = db.Column(db.Text, nullable=False, default='{}')
    # {"p25": 95, "p75": 140, "sample_count": 17} — null/empty until enough
    # runtime samples exist.
    runtime_pref_json = db.Column(db.Text, nullable=False, default='{}')
    # {"movie": 0.6, "tv": 0.4} — normalized ratio.
    media_type_pref_json = db.Column(db.Text, nullable=False, default='{}')
    # Deferred dimension (audit: no reliable source data yet). Stays null
    # until the computation phase populates it.
    mood_tags_json = db.Column(db.Text, nullable=True)

    # Confidence 0..1 gating personalization vs cold-start behavior, plus the
    # evidence counts that explain it (how many weighted signals, how many
    # distinct titles contributed).
    confidence = db.Column(db.Float, nullable=False, default=0.0)
    signal_count = db.Column(db.Integer, nullable=False, default=0)
    distinct_title_count = db.Column(db.Integer, nullable=False, default=0)

    # Bumped by the computation service whenever the algorithm/version
    # changes so result caches keyed on it invalidate cleanly.
    profile_version = db.Column(db.Integer, nullable=False, default=1)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow,
                           onupdate=datetime.utcnow, index=True)

    # One-to-one with User. The backref is not loaded eagerly (plain select
    # on access only), so ordinary User queries gain no extra queries.
    user = db.relationship(
        'User',
        backref=db.backref('taste_profile', uselist=False,
                           cascade='all, delete-orphan'))

    __table_args__ = (
        db.Index('idx_taste_profile_updated', 'updated_at'),
    )

    def __init__(self, **kwargs):
        super(TasteProfile, self).__init__(**kwargs)

    # ── JSON accessors (same pattern as SmartList.filters) ─────────────────
    @staticmethod
    def _loads(raw):
        try:
            data = json.loads(raw or '{}')
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _loads_list_or_none(raw):
        """Parse a JSON document that may legitimately be a list (mood_tags)
        or deliberately null. Returns None for null/unparseable input."""
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return data if isinstance(data, (list, dict)) else None

    @staticmethod
    def _dumps(value):
        return json.dumps(value or {}, sort_keys=True)

    @property
    def genre_weights(self):
        return self._loads(self.genre_weights_json)

    @genre_weights.setter
    def genre_weights(self, value):
        self.genre_weights_json = self._dumps(value)

    @property
    def decade_weights(self):
        return self._loads(self.decade_weights_json)

    @decade_weights.setter
    def decade_weights(self, value):
        self.decade_weights_json = self._dumps(value)

    @property
    def director_affinity(self):
        return self._loads(self.director_affinity_json)

    @director_affinity.setter
    def director_affinity(self, value):
        self.director_affinity_json = self._dumps(value)

    @property
    def runtime_pref(self):
        return self._loads(self.runtime_pref_json)

    @runtime_pref.setter
    def runtime_pref(self, value):
        self.runtime_pref_json = self._dumps(value)

    @property
    def media_type_pref(self):
        return self._loads(self.media_type_pref_json)

    @media_type_pref.setter
    def media_type_pref(self, value):
        self.media_type_pref_json = self._dumps(value)

    @property
    def mood_tags(self):
        return self._loads_list_or_none(self.mood_tags_json)

    @mood_tags.setter
    def mood_tags(self, value):
        self.mood_tags_json = self._dumps(value) if value is not None else None

    def to_dict(self):
        return {
            'user_id': self.user_id,
            'genre_weights': self.genre_weights,
            'decade_weights': self.decade_weights,
            'director_affinity': self.director_affinity,
            'runtime_pref': self.runtime_pref,
            'media_type_pref': self.media_type_pref,
            'mood_tags': self.mood_tags,
            'confidence': self.confidence,
            'signal_count': self.signal_count,
            'distinct_title_count': self.distinct_title_count,
            'profile_version': self.profile_version,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self):
        return (f'<TasteProfile user={self.user_id} '
                f'confidence={self.confidence} signals={self.signal_count}>')
