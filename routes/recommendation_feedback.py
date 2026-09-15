"""Recommendation Feedback API (Feature #7, Phase 2) — write-only, user-scoped.

Exposes the dormant RecommendationFeedback storage layer (Phase 1) to the
authenticated frontend:

    POST /api/rec/feedback   → record one event, or a bounded batch of events

Conventions mirror routes/notifications.py / routes/smart_lists.py:

- Authentication is mandatory (flask-login); the authenticated session
  identity is authoritative — user_id is NEVER accepted from the client, so
  a client cannot submit feedback on behalf of another user.
- CSRF: Flask-WTF CSRFProtect is enabled globally and protects every
  state-changing verb (POST/PUT/DELETE); this route adds nothing on top —
  same mechanism as quick log / Smart Lists / notification mutations.
- Rate limiting: shared Flask-Limiter (Valkey-backed in production). 120
  per minute — impressions arrive per rendered card per rail render, so the
  endpoint must tolerate bursty rails, while still bounding abuse/storms.
- Validation happens for the COMPLETE request before any persistence, so a
  batch containing one bad event writes nothing (no partial acceptance).
- Persistence goes exclusively through RecommendationFeedback.record() —
  the Phase 1 sanctioned, idempotent write path (duplicate non-impression
  events on the same calendar day are suppressed by the partial unique
  index and reported as `duplicates`, never as errors).

Identity: media_id is the TMDb id (see models/recommendation_feedback.py) —
recommended titles usually have no MediaItem row, so existence of one is
neither required nor created here. No TMDb calls, no ranking, no profile
computation: this endpoint is deliberately lightweight — validate, record,
count.

Privacy: feedback is private user behavioral data. The response exposes
only the caller's own write result (counts). There is intentionally no GET
endpoint, no public aggregation, no admin surface in this phase.
"""
import logging

from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user

from extensions import limiter
from models.recommendation_feedback import (
    MODEL_VERSION, RecommendationFeedback,
)

logger = logging.getLogger(__name__)

recommendation_feedback_bp = Blueprint('recommendation_feedback', __name__)

# A rail renders a bounded number of cards (12–24); rendering several rails
# in quick succession legitimately emits a few dozen impression events, so
# 120/min is far above normal interactive use while still bounding abuse.
RATE_LIMIT = "120 per minute"

# Bounded batch: big enough for every rail on a page in one request,
# small enough that one request cannot become an event storm.
MAX_EVENTS_PER_REQUEST = 100

# Optional free-form context strings stay bounded (reject massive values —
# never silently truncate).
MAX_SOURCE_LEN = 120
MAX_REASON_LEN = 64
MAX_MODEL_VERSION_LEN = 64

# Position bounds: rails are finite; reject absurd/unbounded positions.
MIN_POSITION = 0
MAX_POSITION = 10000

# Required per-event keys; anything else in an event object is rejected so
# typos (e.g. "eveent") fail loudly instead of being silently ignored.
ALLOWED_EVENT_KEYS = frozenset({
    'media_id', 'media_type', 'surface', 'event',
    'source', 'position', 'reason_kind', 'payload', 'model_version',
})


def _validate_position(value):
    """Position must be an integer (bool excluded) in a sane rail range."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError('position must be an integer')
    if value < MIN_POSITION or value > MAX_POSITION:
        raise ValueError(
            f'position must be between {MIN_POSITION} and {MAX_POSITION}')
    return value


def _bounded_str(value, field, max_len):
    """Optional bounded string: str or None, never truncated."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f'{field} must be a string')
    if len(value) > max_len:
        raise ValueError(f'{field} must be ≤ {max_len} characters')
    return value


def _validate_payload(value):
    """Payload must be a dict (analytics context only) within the model's
    MAX_PAYLOAD_CHARS budget — checked up front (same rule record() enforces
    via RecommendationFeedback.serialize_payload) so an oversized payload is
    a 400 before anything persists."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError('payload must be an object')
    RecommendationFeedback.serialize_payload(value)  # raises ValueError
    return value


def _validate_model_version(value):
    """model_version: stored as an Integer (Phase 1 model), so accept an
    int or a numeric string tag ('1'); a bounded string tag is allowed and
    stored when numeric."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError('model_version must be an integer')
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        tag = _bounded_str(value, 'model_version', MAX_MODEL_VERSION_LEN)
        try:
            return int(tag)  # '1' → 1; 'taste-v1' → ValueError
        except (TypeError, ValueError):
            raise ValueError(
                'model_version must be an integer (or numeric string)')
    raise ValueError('model_version must be an integer')


def _validate_media_id(value):
    """media_id: positive integer TMDb id. Bool is not an integer here."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError('media_id must be an integer')
    if value <= 0:
        raise ValueError('media_id must be positive')
    return value


def _validate_event_dict(item, index):
    """Validate ONE event object. Returns normalized kwargs for record().

    Raises ValueError with an index-tagged, client-safe message. No DB, no
    network, no model instantiation — pure input validation.
    """
    if not isinstance(item, dict):
        raise ValueError(f'events[{index}] must be an object')

    unknown = set(item) - ALLOWED_EVENT_KEYS
    if unknown:
        raise ValueError(
            f"events[{index}] has unknown field(s): {', '.join(sorted(unknown))}")

    for field in ('media_id', 'media_type', 'surface', 'event'):
        if field not in item:
            raise ValueError(f'events[{index}] missing required field {field!r}')

    return dict(
        media_id=_validate_media_id(item['media_id']),
        media_type=RecommendationFeedback.validate_media_type(item['media_type']),
        surface=RecommendationFeedback.validate_surface(item['surface']),
        event=RecommendationFeedback.validate_event(item['event']),
        # source is optional for the caller but NOT NULL in storage — an
        # omitted source persists as an empty string, never NULL.
        source=_bounded_str(item.get('source'), 'source', MAX_SOURCE_LEN) or '',
        position=_validate_position(item.get('position')),
        reason_kind=_bounded_str(
            item.get('reason_kind'), 'reason_kind', MAX_REASON_LEN),
        payload=_validate_payload(item.get('payload')),
        # Omitted model_version falls back to the model's established V1
        # default (explicit None would bypass the column default and violate
        # the NOT NULL constraint).
        model_version=_validate_model_version(item.get('model_version'))
        or MODEL_VERSION,
    )


def _extract_events(data):
    """Normalize the single-event and batch request shapes into a list.

    Single:  the body IS the event (media_id/media_type/surface/event keys).
    Batch:   {"events": [ {...}, ... ]} — a list; one event per element.
    Raises ValueError for malformed shapes.
    """
    if not isinstance(data, dict):
        raise ValueError('request body must be a JSON object')
    if 'events' in data:
        events = data['events']
        if not isinstance(events, list):
            raise ValueError('events must be a list')
        if not events:
            raise ValueError('events must not be empty')
        if len(events) > MAX_EVENTS_PER_REQUEST:
            raise ValueError(
                f'events exceeds the maximum of {MAX_EVENTS_PER_REQUEST} '
                'per request')
        return events
    # Single-event form — media_id must be present at the top level.
    if 'media_id' not in data:
        raise ValueError('missing required field media_id')
    return [data]


@recommendation_feedback_bp.route('/api/rec/feedback', methods=['POST'])
@login_required
@limiter.limit(RATE_LIMIT)
def api_record_feedback():
    """Record one or more feedback events for the authenticated user.

    Response: {"ok": true, "recorded": N, "duplicates": M} — N newly
    persisted rows, M suppressed same-day duplicates (idempotent success,
    not an error). Validation failures → 400 with nothing written.
    """
    data = request.get_json(silent=True)
    if data is None:
        return jsonify({'error': 'Request body must be valid JSON'}), 400

    try:
        raw_events = _extract_events(data)
        validated = [
            _validate_event_dict(item, i) for i, item in enumerate(raw_events)
        ]
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    # Validate-everything-first is done; persistence is delegated entirely
    # to the model's sanctioned idempotent write path — one commit per event
    # inside record() keeps a mid-batch database failure isolated to that
    # event without leaving a stale session transaction open.
    recorded = 0
    duplicates = 0
    try:
        for kwargs in validated:
            row = RecommendationFeedback.record(
                user_id=current_user.id, commit=True, **kwargs)
            if row is None:
                duplicates += 1
            else:
                recorded += 1
    except Exception:
        # A storage failure must be a clean 500 — no internals leaked, no
        # SQLAlchemy traceback to the client.
        logger.error('Failed to record recommendation feedback for user %s',
                     current_user.id, exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500

    return jsonify({'ok': True, 'recorded': recorded,
                    'duplicates': duplicates})
