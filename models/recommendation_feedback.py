"""Recommendation Feedback (Feature #7, Phase 1) — append-only event history.

Every meaningful recommendation interaction (impression, click,
not_interested, already_watched, saved, rated) is one immutable row. This
table is EVENT HISTORY, not mutable preference state: future aggregation
derives current preference from the history; nothing ever UPDATEs or
DELETEs old rows as part of normal application behavior.

    recommendation surfaces (home_for_you, profile_recs, more_like_this)
        ↓  record()  (validated, idempotent — the only sanctioned write path)
    append-only RecommendationFeedback rows
        ↓  nightly batch aggregation (later phase)
    TasteProfile recomputation + feedback-aware ranking

MEDIA IDENTITY — deliberately TMDb id, NOT MediaItem.id:
The existing recommendation consumers operate entirely in TMDb identity
space. routes/recommendations.py (`get_recommendations`) forwards
`media_id` straight to TMDb `{media_type}/{media_id}/recommendations`, its
frontend (static/js/more-like-this.js) links `/{mediaType}/{item.id}`, and
profile recommendations (routes/auth.py `_build_recommendations`) render
TMDb results that may never exist as MediaItem rows. Storing
`media_item.id` here would be unpopulatable for most impressions and
unstable to PK reuse. So `media_id` is the TMDb id and `media_type`
disambiguates movie/tv — the pair is stable across restarts and usable by
future analytics without a join. There is intentionally NO MediaItem
relationship.

IDEMPOTENCY (audited V1 rule) — partial unique index:
Non-impression events are once per calendar day per
(user_id, media_id, media_type, surface, event):

    UNIQUE (user_id, media_id, media_type, surface, event, event_date)
        WHERE event != 'impression'

Impressions legitimately repeat (every page render), so they are excluded
from the constraint via a PARTIAL unique index. Both production
PostgreSQL and the hermetic SQLite test database support partial indexes
with identical condition text, so one portable declaration serves both —
no PostgreSQL-only expression SQL, no fragile "application-only" dedup
race. `RecommendationFeedback.record()` is the validated write path: it
validates enumerations, bounds the payload, inserts, and converts the
constraint violation into a silent duplicate suppression (returns None).

`event_date` exists purely as the calendar-day component of that rule
(and for daily analytics); `created_at` remains the precise timestamp.

PRIVACY — feedback is private user behavioral data: no public routes, no
social visibility, payload limited to compact recommendation-specific
context (record() rejects anything over MAX_PAYLOAD_CHARS).

This module is DORMANT after Phase 1: no route, template, agent or job
reads or writes it yet.
"""
import json
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from models.base import db

# ── Table-driven enumerations (single validation source for the future
#    API phase — never scatter string checks across routes). ────────────────
MEDIA_TYPES = ('movie', 'tv')
SURFACES = (
    'home_for_you',     # For You rail (Feature #6, later phase)
    'profile_recs',     # profile recommendations
    'more_like_this',   # detail-page More Like This
)
EVENTS = (
    'impression',       # shown to the user (repeatable)
    'click',            # opened the title
    'not_interested',   # explicit dismissal
    'already_watched',  # "already seen" dismissal
    'saved',            # added to watchlist from the recommendation
    'rated',            # rated the title after being recommended
)

# Bumped whenever the recommendation model that produced the impressions
# changes, so historical feedback stays attributable to its generator.
MODEL_VERSION = 1

# Payloads stay compact: recommendation context only, never page state.
MAX_PAYLOAD_CHARS = 2048


def _utcnow_date():
    """Calendar-day default (UTC), aligned with the project's naive-UTC
    datetime.utcnow() convention."""
    return datetime.utcnow().date()


class RecommendationFeedback(db.Model):
    """One immutable recommendation interaction event for one user."""
    __tablename__ = 'recommendation_feedback'

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False,
                        index=True)

    # TMDb identity — see module docstring. Not a FK: recommended titles
    # usually have no MediaItem row, by design.
    media_id = db.Column(db.Integer, nullable=False)
    media_type = db.Column(db.String(20), nullable=False)   # MEDIA_TYPES

    surface = db.Column(db.String(32), nullable=False)      # SURFACES
    source = db.Column(db.String(120), nullable=False)      # e.g. trending,
    # genre_discover, similar_to:<tmdb_id>, director:<name> — kept as a
    # compact string, deliberately not normalized into another table.
    event = db.Column(db.String(24), nullable=False)        # EVENTS

    # Card position when the event happened; null where unavailable.
    position = db.Column(db.Integer, nullable=True)
    # Structured explanation kind ("similar_to", "top_genre", ...) — no LLM
    # text; the human wording is derived at render time.
    reason_kind = db.Column(db.String(64), nullable=True)
    payload_json = db.Column(db.Text, nullable=True)        # bounded context

    model_version = db.Column(db.Integer, nullable=False, default=MODEL_VERSION)

    # Calendar day (UTC) backing the once-per-day idempotency rule.
    event_date = db.Column(db.Date, nullable=False, default=_utcnow_date)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    # No updated_at, no onupdate: the table is append-only by contract.

    # Deleting a user removes their feedback (project's User-owned model
    # convention). lazy='dynamic' keeps it a query — no eager load, no N+1
    # on ordinary User queries.
    user = db.relationship(
        'User',
        backref=db.backref('recommendation_feedback', lazy='dynamic',
                           cascade='all, delete-orphan'))

    __table_args__ = (
        # Once-per-day rule for every non-impression event — see module
        # docstring. Identical WHERE text on PostgreSQL and SQLite.
        db.Index(
            'uq_recommendation_feedback_event_daily',
            'user_id', 'media_id', 'media_type', 'surface', 'event',
            'event_date',
            unique=True,
            postgresql_where=text("event != 'impression'"),
            sqlite_where=text("event != 'impression'"),
        ),
        # Nightly aggregation scans events in time order.
        db.Index('ix_recommendation_feedback_created_at', 'created_at'),
        # Title-level feedback summaries ("how did users respond to X").
        db.Index('ix_recommendation_feedback_media', 'media_id', 'media_type'),
    )

    def __init__(self, **kwargs):
        super(RecommendationFeedback, self).__init__(**kwargs)

    # ── Validation (reusable by the future API phase) ───────────────────────
    @classmethod
    def validate_media_type(cls, value):
        if value not in MEDIA_TYPES:
            raise ValueError(
                f"Invalid media_type {value!r}; expected one of {MEDIA_TYPES}")
        return value

    @classmethod
    def validate_surface(cls, value):
        if value not in SURFACES:
            raise ValueError(
                f"Invalid surface {value!r}; expected one of {SURFACES}")
        return value

    @classmethod
    def validate_event(cls, value):
        if value not in EVENTS:
            raise ValueError(
                f"Invalid event {value!r}; expected one of {EVENTS}")
        return value

    # ── Sanctioned write path ────────────────────────────────────────────────
    @classmethod
    def serialize_payload(cls, payload):
        """Serialize + size-bound a payload — the single source of the
        MAX_PAYLOAD_CHARS rule, reused by the API's validate-before-persist
        step and by record() itself."""
        serialized = json.dumps(payload, sort_keys=True)
        if len(serialized) > MAX_PAYLOAD_CHARS:
            raise ValueError(
                'payload_json exceeds MAX_PAYLOAD_CHARS '
                f'({len(serialized)} > {MAX_PAYLOAD_CHARS})')
        return serialized

    @classmethod
    def record(cls, user_id, media_id, media_type, surface, event, source,
               position=None, reason_kind=None, payload=None,
               model_version=MODEL_VERSION, commit=True):
        """Validate + insert one feedback event (idempotent).

        Returns the new row, or None when the once-per-day rule already
        recorded this (user, media, type, surface, event) today — the
        duplicate is silently suppressed by the partial unique index.

        Raises ValueError for invalid enumerations or an oversized payload.
        Set commit=False to let a caller batch multiple events into one
        transaction (IntegrityError then surfaces at the caller's commit).
        """
        cls.validate_media_type(media_type)
        cls.validate_surface(surface)
        cls.validate_event(event)

        serialized = None
        if payload is not None:
            serialized = cls.serialize_payload(payload)

        feedback = cls(
            user_id=user_id, media_id=media_id, media_type=media_type,
            surface=surface, source=source, event=event, position=position,
            reason_kind=reason_kind, payload_json=serialized,
            model_version=model_version,
        )
        db.session.add(feedback)
        if commit:
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                return None  # duplicate suppressed — idempotent by design
        return feedback

    # ── JSON accessor (bounded payload only) ────────────────────────────────
    @property
    def payload(self):
        if self.payload_json is None:
            return None
        try:
            data = json.loads(self.payload_json)
        except (TypeError, ValueError):
            return None
        return data if isinstance(data, (dict, list)) else None

    @payload.setter
    def payload(self, value):
        self.payload_json = (json.dumps(value, sort_keys=True)
                             if value is not None else None)

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'media_id': self.media_id,
            'media_type': self.media_type,
            'surface': self.surface,
            'source': self.source,
            'event': self.event,
            'position': self.position,
            'reason_kind': self.reason_kind,
            'payload': self.payload,
            'model_version': self.model_version,
            'event_date': self.event_date.isoformat() if self.event_date else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return (f'<RecommendationFeedback user={self.user_id} '
                f'{self.media_type}/{self.media_id} {self.event} '
                f'@{self.surface} day={self.event_date}>')
