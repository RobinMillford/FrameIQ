"""Notification preferences (Feature F12) — categories, defaults, persistence.

The single place that answers "may this user receive this category right now?".
Every producer routes through :func:`allows`, so a category can never be
silently ignored by a producer.

Supported categories (and why the list is exactly this)
------------------------------------------------------
``new_episode``
    An episode of a tracked show has aired. This is the pre-existing,
    long-running producer (``api.notifications.notify_newly_aired_episodes``),
    so the default is True and existing users see no behaviour change.

``season_premiere``
    The first episode of a season has aired. Derived from the SAME reliable
    source as ``new_episode`` (an aired ``UpcomingEpisode`` row where
    ``episode_number == 1``); it is a different *classification* of that event,
    never a second notification for it. Defaults False so it is opt-in.

Deliberately NOT modelled, because there is no trustworthy event source:
    ``availability_change``
        FrameIQ's availability data is a cached, region-scoped, TTL-bounded
        view of TMDb provider lists. A missing record, a failed fetch, an
        expired cache entry or a user changing region all look identical to
        "this title became unavailable". Treating any of them as a change
        would manufacture false notifications.
    ``social``
        The application has no social notification producer at all today
        (no follow/interaction notification is generated anywhere). Exposing a
        preference for a non-existent event type would be a control that does
        nothing.

Both are documented deferrals, not half-built features.
"""
import logging

from sqlalchemy.exc import IntegrityError

from models import db
from models.notification_preferences import UserNotificationPreference

logger = logging.getLogger(__name__)

#: Every category a user may configure. Anything else is rejected.
CATEGORIES = ('new_episode', 'season_premiere')

#: Behaviour for a user with no stored preference row — i.e. every user who
#: existed before F12 and has never saved settings. ``new_episode`` is True to
#: preserve the established behaviour exactly; ``season_premiere`` is False
#: because turning it on would change what an existing user receives.
DEFAULTS = {
    'new_episode': True,
    'season_premiere': False,
}

#: Human-readable explanations, used by the settings UI so each control says
#: what it actually does rather than just naming a category.
DESCRIPTIONS = {
    'new_episode': 'When an episode of a show you track has aired.',
    'season_premiere': 'When the first episode of a new season has aired.',
}


class PreferenceError(ValueError):
    """Raised for an invalid preference payload (user-safe message)."""


def validate_preferences(payload):
    """Validate a submitted preference object; return the canonical dict.

    Rejects unknown category names rather than persisting them, so a stale or
    hand-crafted client can never smuggle a category into storage.
    """
    if not isinstance(payload, dict):
        raise PreferenceError('Preferences must be an object')
    unknown = set(payload) - set(CATEGORIES)
    if unknown:
        raise PreferenceError(
            f'Unknown notification categor{"y" if len(unknown) == 1 else "ies"}: '
            f'{", ".join(sorted(unknown))}')
    clean = {}
    for category in CATEGORIES:
        if category not in payload:
            continue
        value = payload[category]
        if isinstance(value, bool):
            clean[category] = value
        elif value in (0, 1, '0', '1', 'true', 'false'):
            # HTML checkboxes/selects commonly submit strings.
            clean[category] = str(value).lower() in ('1', 'true')
        else:
            raise PreferenceError(f'{category} must be true or false')
    if not clean:
        raise PreferenceError('No notification categories supplied')
    return clean


def get_preferences(user_id):
    """The user's effective preferences. NEVER creates a row.

    A user with no stored record gets ``DEFAULTS``. Keeping the read pure is
    what makes "existing users are unaffected" true without a backfill.
    """
    row = (UserNotificationPreference.query
           .filter_by(user_id=user_id)
           .first())
    if row is None:
        return dict(DEFAULTS)
    return row.to_dict()


def preferences_for_users(user_ids):
    """Batch-resolve preferences for many users. One query, no N+1.

    Users with no row fall back to ``DEFAULTS``, so the returned map covers
    every requested id.
    """
    ids = {int(uid) for uid in user_ids}
    if not ids:
        return {}
    rows = (UserNotificationPreference.query
            .filter(UserNotificationPreference.user_id.in_(ids))
            .all())
    resolved = {row.user_id: row.to_dict() for row in rows}
    return {uid: dict(resolved.get(uid, DEFAULTS)) for uid in ids}


def allows(user_id, category, batch=None):
    """Whether this user currently wants ``category``.

    Pass ``batch`` (from :func:`preferences_for_users`) to avoid a lookup per
    candidate inside a fan-out loop.
    """
    if category not in CATEGORIES:
        raise PreferenceError(f'Unknown notification category: {category}')
    if batch is not None:
        return bool(batch.get(user_id, DEFAULTS).get(category, False))
    return bool(get_preferences(user_id).get(category, False))


def save_preferences(user_id, payload):
    """Validate and persist preferences; return the stored values.

    Update-then-insert with an IntegrityError retry, so two concurrent first
    saves converge on the single UNIQUE(user_id) row instead of raising.
    """
    clean = validate_preferences(payload)
    row = (UserNotificationPreference.query
           .filter_by(user_id=user_id)
           .first())
    if row is not None:
        for category, value in clean.items():
            setattr(row, category, value)
        db.session.commit()
        return row.to_dict()

    merged = dict(DEFAULTS)
    merged.update(clean)
    row = UserNotificationPreference(user_id=user_id, **merged)
    db.session.add(row)
    try:
        db.session.commit()
    except IntegrityError:
        # A concurrent first save won the race. Roll back to a clean
        # transaction and apply the values onto the row that now exists.
        db.session.rollback()
        existing = (UserNotificationPreference.query
                    .filter_by(user_id=user_id)
                    .first())
        if existing is None:  # pragma: no cover - would mean a broader fault
            raise
        for category, value in clean.items():
            setattr(existing, category, value)
        db.session.commit()
        return existing.to_dict()
    return row.to_dict()


def describe_categories():
    """Serializable catalogue for the settings UI."""
    return [
        {'key': key, 'label': key.replace('_', ' ').title(),
         'description': DESCRIPTIONS[key]}
        for key in CATEGORIES
    ]