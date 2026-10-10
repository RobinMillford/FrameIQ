"""Watchlist resurfacing (Feature F10).

Makes saved watchlist titles useful again by giving *neglected* ones a
deliberate, bounded opportunity to reappear — without turning the For You
rail into a duplicate of the watchlist page.

Why this is a service and not a route
-------------------------------------
Every other recommendation surface in FrameIQ is a service in ``api/`` with
the route reduced to authentication plus a bounded parameter
(``api/for_you.py`` + ``routes/for_you.py``, ``api/smart_lists.py`` +
``routes/smart_lists.py``). This module follows the same split: the engine is
callable from tests with plain arguments and never sees a request object.

What already existed, and what was actually missing
---------------------------------------------------
This feature deliberately reuses what is already there rather than inventing a
second ranking system:

* ``user_watchlist.date_added`` and ``.priority`` — the neglect and importance
  signals, already stored but previously used only for sorting.
* ``api.for_you.rank_candidates`` — the deterministic scoring function, reused
  AS IS. No new weight is introduced and no existing weight is altered.
* ``RecommendationFeedback`` ``not_interested`` / ``already_watched`` — the
  dismissal vocabulary. Before F10 nothing read these events at all; they were
  written and never honoured.
* ``MovieReleaseDate`` + ``api.calendar`` release-type labels — the already
  cached, per-region movie release dates and their conservative labelling.
* ``UpcomingEpisode`` — synced per-episode air dates for TV.

The genuinely missing piece was narrower than it looks. ``api/for_you.py``
computes a ``watchlist_intent`` ranking component and a ``W_WATCHLIST_INTENT``
weight, but `_collect_watched_exclusions()` puts watchlist keys into
``exclude_keys`` *and* ``watchlisted_keys``, so by the time
`_apply_exclusions()` tests ``watchlisted_keys`` the title has already been
dropped. The signal is structurally unreachable and the component is always
0.0 for watchlist titles.

This module does NOT resurrect that weight, and the reason is deliberate.
Feeding watchlist titles back through ``rank_candidates`` would let a
taste-genre score outvote a "you deliberately saved this months ago" signal —
which is a much weaker reason to watch something than an explicit save. It
would also perturb the audited ranking and diversity behaviour of ``items``,
whose ordering is a contract other consumers depend on. So resurfacings get
their own small, totally-ordered ranking (:func:`resurface_sort_key`) over the
two stored watchlist signals, and ``TASTE_MATCH_WEIGHTS`` is left untouched.
The dead ``watchlist_intent`` component is left exactly as it is; fixing it is
a separate change to the scored path, not part of F10.

Staleness is NOT ``api.for_you.freshness()``
--------------------------------------------
``freshness()`` measures how RECENT A RELEASE is (release-date recency, clamped
for future releases). "Neglected" means something different: how long ago the
USER saved the title. Reusing ``freshness()`` here would measure the wrong
thing entirely, so staleness is computed here from ``date_added``.

Query budget
------------
Six bounded statements in the worst case, ZERO per-title queries:

1. watchlist ⨝ MediaItem, capped at :data:`WATCHLIST_SCAN_CAP`;
2. blocking-feedback keys, capped at :data:`FEEDBACK_SCAN_CAP`;
3. canonical completed-show ids, one batched read, only when a TV candidate
   exists;
4. movie release context, one batched read, only when a movie candidate exists;
5. TV upcoming-episode context, one batched read, only when a TV candidate exists.

Every read is filtered by an ``IN`` list built from the capped candidate set, so
cost is bounded by the cap rather than by the user's watchlist size.

No schema change
-----------------
Everything here is derived from existing tables and columns. F10 adds no
migration, no column, and no table.
"""
from datetime import date, datetime

from models import (
    MovieReleaseDate, RecommendationFeedback, TVShowProgress, UpcomingEpisode,
    user_watchlist,
)
from models.base import db
from models.media import MediaItem

# ── Bounds (audited, inspectable) ────────────────────────────────────────────

# Titles returned to the caller. Small on purpose: resurfacing is a reminder,
# not a second recommendation rail competing with For You.
RESURFACE_MAX = 6

# Mirrors api/calendar.py's _WATCHLIST_CAP. A watchlist is scanned in a single
# capped statement, so a user with 5,000 saved titles cannot turn this into an
# unbounded read. Ties are broken deterministically below, so the cap selects a
# stable subset rather than an arbitrary one.
WATCHLIST_SCAN_CAP = 200

# PRODUCT HEURISTIC — TUNABLE, NOT EVIDENCE-BACKED.
#
# Only a title the user has actually let go of is eligible. The value was
# chosen to be short enough that the rail still feels timely and long enough
# that a normal "I'll watch that this weekend" plan is not interrupted. There
# is NO behavioural data behind it: it is a judgement call, not a validated
# finding, and is expected to be tuned from real usage. Deliberately a plain
# module constant — no settings page, DB setting, or config system.
#
# Note the interaction with the canonical completion gate: this threshold
# governs NEGLECT only. A fully watched title is excluded regardless of how
# long ago it was saved, and a freshly saved but already-watched title is
# excluded too.
STALE_AFTER_DAYS = 21

# RecommendationFeedback events that mean "do not show me this again". These
# are the existing dismissal vocabulary; F10 is the first consumer of them.
BLOCKING_EVENTS = ('not_interested', 'already_watched')

# The ONE TVShowProgress.status value that means "the user finished this".
# 'watching' / 'plan_to_watch' / 'dropped' deliberately do NOT suppress a
# resurfacING title — only a sealed completion does. See _completed_shows().
COMPLETED_SHOW_STATUS = 'completed'

# Bounded read of blocking feedback. A user's real dismissals are far fewer
# than this, and the cap keeps the statement predictable.
FEEDBACK_SCAN_CAP = 400

# Release types that describe a release people can act on. Mirrors
# api/calendar.py's EVENT_RELEASE_TYPES, and deliberately excludes premiere /
# physical / tv types, which are not availability-relevant.
EVENT_RELEASE_TYPES = (3, 4)
_RELEASE_LABELS = {3: 'theatrical', 4: 'digital'}

# Importance order for the existing user_watchlist.priority column. Unknown or
# NULL values sort last rather than raising or silently defaulting to 'high'.
PRIORITY_RANK = {'high': 0, 'medium': 1, 'low': 2}
PRIORITY_UNKNOWN_RANK = 3

PRIORITY_LABELS = {'high': 'high', 'medium': 'medium', 'low': 'low'}


# ══════════════════════════════════════════════════════════════════════════
# Pure helpers — deterministic, no DB, no network. Independently testable.
# ══════════════════════════════════════════════════════════════════════════

def neglect_days(date_added, today):
    """Whole days since the title was saved, or None when unusable.

    Returns ``None`` rather than 0 for a missing date so a NULL ``date_added``
    can never masquerade as "saved today" (which would silently exclude the
    title) or as infinitely stale.
    """
    if date_added is None or today is None:
        return None
    try:
        added = date_added
        if isinstance(added, datetime):
            added = added.date()
        elif not isinstance(added, date):
            # Tolerate an ISO string; anything else is unusable.
            added = datetime.strptime(str(added)[:10], '%Y-%m-%d').date()
        reference = today if isinstance(today, date) and not isinstance(
            today, datetime) else (
                today.date() if isinstance(today, datetime) else None)
        if reference is None:
            return None
    except (TypeError, ValueError):
        return None
    return max(0, (reference - added).days)


def is_stale(date_added, today, min_days=STALE_AFTER_DAYS):
    """True when a saved title has been neglected for at least ``min_days``."""
    days = neglect_days(date_added, today)
    return days is not None and days >= min_days


def priority_rank(priority):
    """Sort rank for the existing priority column (lower sorts first)."""
    if not isinstance(priority, str):
        return PRIORITY_UNKNOWN_RANK
    return PRIORITY_RANK.get(priority.strip().lower(), PRIORITY_UNKNOWN_RANK)


def priority_label(priority):
    """Human-facing importance label, or None when the value is unusable.

    Never invents a level: an unknown stored value yields no label rather than
    being rewritten to 'medium'.
    """
    if not isinstance(priority, str):
        return None
    value = priority.strip().lower()
    return PRIORITY_LABELS.get(value)


def resurface_sort_key(entry):
    """Deterministic ordering: importance, then neglect, then identity.

    Deliberately NOT a plain oldest-first list: a high-priority title saved
    long ago outranks a low-priority one saved yesterday. ``tmdb_id`` last makes
    the ordering total, so two runs over the same data return the same order
    (there is no hash- or time-based tie-break anywhere in this module).
    """
    return (entry['priority_rank'], -entry['neglect_days'],
            entry['tmdb_id'], entry['media_type'])


def release_label(release_type):
    """Conservative label for a TMDb movie release-type integer.

    Mirrors ``api/calendar.py``'s conservative policy: only types 3
    (theatrical) and 4 (digital) carry meaning here. Returns ``'theatrical'``,
    ``'digital'``, or ``None`` for anything unrecognised — never a guessed-at
    label, and never the string ``'unknown'``.
    """
    if release_type in (None, ''):
        return None
    try:
        rtype = int(release_type)
    except (TypeError, ValueError):
        return None
    return _RELEASE_LABELS.get(rtype)


def movie_release_context(rtype, release_date, today):
    """Conservative (label, kind, date) context for a cached movie release.

    The distinction that matters: a PAST date means *released*, never
    *streamed/available*. ``MediaItem.release_date`` and the release cache are
    release dates, not availability evidence — conflating them is exactly the
    false claim this function exists to avoid.

    Returns ``None`` when the type is not availability-relevant (release_label
    gives None) or the date is missing, so nothing false is rendered by
    omission.
    """
    label = release_label(rtype)
    if label is None or release_date is None:
        return None
    upcoming = release_date >= today
    if not upcoming:
        # Past date: "released" only. Applies to both labels.
        text = '%s — released' % release_date.isoformat()
    elif label == 'theatrical':
        text = 'In theaters %s' % release_date.isoformat()
    else:
        text = 'Digital release %s' % release_date.isoformat()
    return {
        'kind': 'movie_release',
        'label': label,
        'date': release_date.isoformat(),
        'upcoming': upcoming,
        'text': text,
    }


def episode_release_context(air_date, today):
    """Conservative context for a TV episode air date.

    An episode air date is confirmed broadcast metadata, so naming the date is
    accurate. It is still not a streaming-availability claim, so no provider or
    "watch now" language is ever produced.
    """
    if air_date is None:
        return None
    upcoming = air_date >= today
    return {
        'kind': 'episode_air_date',
        'label': 'episode',
        'date': air_date.isoformat(),
        'upcoming': upcoming,
        'text': ('New episode %s' % air_date.isoformat() if upcoming
                 else 'Episode aired %s' % air_date.isoformat()),
    }


def resurface_reason(days, priority):
    """Structured reason shown on a resurfaced card.

    States only what is true and known: how long the title has been saved, and
    its stored priority when that value is recognised.
    """
    if days == 1:
        age = 'Saved a day ago'
    elif days and days < 60:
        age = 'Saved %d days ago' % days
    elif days:
        months = round(days / 30.0)
        age = 'Saved %d months ago' % max(1, months)
    else:
        age = 'On your watchlist'
    label = priority_label(priority)
    if label:
        age = '%s · %s priority' % (age, label)
    return {
        'kind': 'watchlist_neglected',
        'text': age,
        'evidence': [{'days_since_added': days},
                     ({'priority': label} if label else {})],
    }


# ══════════════════════════════════════════════════════════════════════════
# Bounded reads
# ══════════════════════════════════════════════════════════════════════════

def _watchlist_entries(user_id, today, scan_cap=WATCHLIST_SCAN_CAP,
                       min_days=STALE_AFTER_DAYS):
    """One capped read: stale watchlist rows joined to their MediaItem.

    The join is deliberate. Filtering in SQL means a title whose MediaItem row
    was deleted simply does not appear — no per-title existence check, and no
    row is ever created to make a resurfacing possible.
    """
    rows = (
        db.session.query(
            user_watchlist.c.media_id,
            user_watchlist.c.media_type,
            user_watchlist.c.date_added,
            user_watchlist.c.priority,
            MediaItem.tmdb_id,
            MediaItem.title,
            MediaItem.poster_path,
            MediaItem.release_date,
        )
        .select_from(user_watchlist)
        .join(MediaItem, MediaItem.id == user_watchlist.c.media_id)
        .where(user_watchlist.c.user_id == user_id)
        .order_by(user_watchlist.c.date_added.asc().nullslast(),
                  user_watchlist.c.media_id.asc())
        .limit(scan_cap)
        .all()
    )

    entries = []
    for (media_id, media_type, date_added, priority,
         tmdb_id, title, poster_path, release_date) in rows:
        days = neglect_days(date_added, today)
        # Eligibility is decided from the stored value, never defaulted.
        if days is None or days < min_days:
            continue
        if tmdb_id is None or media_type not in ('movie', 'tv'):
            continue
        entries.append({
            'media_id': media_id,
            'media_type': media_type,
            'tmdb_id': tmdb_id,
            'title': title,
            'poster_path': poster_path,
            'legacy_release_date': release_date,
            'date_added': date_added,
            'priority': priority,
            'neglect_days': days,
            'priority_rank': priority_rank(priority),
        })
    return entries


def _blocking_keys(user_id, candidate_ids, cap=FEEDBACK_SCAN_CAP):
    """(media_type, tmdb_id) keys the user has explicitly dismissed.

    The first consumer of the ``not_interested`` / ``already_watched``
    vocabulary in the repository. ``impression`` / ``click`` are excluded on
    purpose: being shown a title or opening it is interest, not a dismissal.
    """
    if not candidate_ids or not BLOCKING_EVENTS:
        return set()
    rows = (
        db.session.query(
            RecommendationFeedback.media_id,
            RecommendationFeedback.media_type)
        .filter(
            RecommendationFeedback.user_id == user_id,
            RecommendationFeedback.event.in_(BLOCKING_EVENTS),
            RecommendationFeedback.media_id.in_(candidate_ids))
        .limit(cap)
        .all()
    )
    return {(media_type, media_id) for media_id, media_type in rows}


def _completed_shows(user_id, show_ids, cap=FEEDBACK_SCAN_CAP):
    """TMDb show ids the canonical subsystem has sealed as fully watched.

    Why ``status == 'completed'`` and not ``watched >= total``:

    ``api.user_view_state.apply_completion_gating()`` is the ONLY writer of
    this status, and it seals a show only when THREE conditions hold —

      1. ``total_episodes > 0`` (a real verifiably-AIRED denominator, per
         the F1 contract; ``total_episodes`` counts aired episodes, not
         TMDb's ``number_of_episodes``),
      2. ``watched_episodes >= total_episodes``, and
      3. TMDb reports the series ``status`` as ``Ended`` or ``Canceled``.

    It also *un-seals* (back to ``'watching'``) when aired reality later
    shows the show is incomplete, so the stored value is a maintained
    verdict rather than a write-once flag.

    Reusing that stored verdict means this module does NOT define its own
    notion of "fully watched" and never calls TMDb. Deriving completion
    locally from ``watched_episodes >= total_episodes`` would be unsound:
    with a missing or zero aired denominator (metadata failure, unsynced
    calendar) ``0 >= 0`` reads as complete, manufacturing a completion
    claim out of absent data.

    Conservative by construction: a missing progress row, an unknown status,
    a zero denominator, or partial progress all leave the title ELIGIBLE.
    Ambiguity never suppresses a title the user may genuinely want to see.
    """
    if not show_ids:
        return set()
    rows = (
        db.session.query(TVShowProgress.show_id)
        .filter(
            TVShowProgress.user_id == user_id,
            TVShowProgress.status == COMPLETED_SHOW_STATUS,
            TVShowProgress.show_id.in_(show_ids))
        .limit(cap)
        .all()
    )
    return {show_id for (show_id,) in rows}


def _movie_release_map(movie_ids, region, today):
    """{tmdb_id: context} from the already-cached release table.

    Reads only ``MovieReleaseDate`` — a table populated by the scheduled sync.
    This never calls TMDb, so rendering a resurfaced card costs no network.
    When several types exist for one title the soonest UPCOMING one wins,
    because that is the actionable one; otherwise the earliest known date.
    """
    if not movie_ids:
        return {}
    rows = (
        db.session.query(
            MovieReleaseDate.tmdb_id,
            MovieReleaseDate.release_type,
            MovieReleaseDate.release_date)
        .filter(
            MovieReleaseDate.tmdb_id.in_(movie_ids),
            MovieReleaseDate.region == region,
            MovieReleaseDate.release_type.in_(EVENT_RELEASE_TYPES))
        .order_by(MovieReleaseDate.release_date.asc())
        .all()
    )

    best = {}
    for tmdb_id, rtype, release_date in rows:
        context = movie_release_context(rtype, release_date, today)
        if context is None:
            continue
        current = best.get(tmdb_id)
        # Prefer upcoming; among upcoming prefer the soonest; among past
        # prefer the most recent. Deterministic either way.
        if current is None:
            best[tmdb_id] = context
            continue
        if context['upcoming'] and not current['upcoming']:
            best[tmdb_id] = context
        elif context['upcoming'] == current['upcoming'] \
                and context['date'] < current['date']:
            best[tmdb_id] = context
    return best


def _episode_air_date_map(show_ids, today):
    """{show_id: context} from synced UpcomingEpisode rows. No network."""
    if not show_ids:
        return {}
    rows = (
        db.session.query(
            UpcomingEpisode.show_id, UpcomingEpisode.air_date)
        .filter(UpcomingEpisode.show_id.in_(show_ids),
                UpcomingEpisode.air_date >= today)
        .order_by(UpcomingEpisode.air_date.asc(),
                  UpcomingEpisode.season_number.asc(),
                  UpcomingEpisode.episode_number.asc())
        .limit(FEEDBACK_SCAN_CAP)
        .all()
    )
    out = {}
    for show_id, air_date in rows:
        context = episode_release_context(air_date, today)
        if context is not None and show_id not in out:
            out[show_id] = context      # ordered soonest-first
    return out


# ══════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════

def _card(entry, release, reason):
    """Final resurfaced-card shape.

    Carries the same identity/display fields ``api.for_you._item`` emits
    (``tmdb_id``/``media_type``/``title``/``poster_path``/``release_date``) so
    one card layout can render both, plus two additive keys: the ``reason``
    and the ``release`` context. Note that ``static/js/for-you.js`` renders
    these from a SEPARATE container with its own renderer rather than reusing
    ``card()`` — see the comment there on why sharing it would mis-attribute
    feedback to the wrong surface.
    """
    card = {
        'tmdb_id': entry['tmdb_id'],
        'media_type': entry['media_type'],
        'title': entry['title'],
        'poster_path': entry['poster_path'],
        'release_date': entry['legacy_release_date'].isoformat()
        if entry['legacy_release_date'] else None,
        'source': 'watchlist_resurface',
        'reason': reason,
    }
    if release:
        card['release'] = release
    return card


def resurface_cards(user_id, watched_keys=None, region='US', today=None,
                    limit=RESURFACE_MAX, scan_cap=WATCHLIST_SCAN_CAP,
                    min_days=STALE_AFTER_DAYS):
    """Neglected watchlist titles as recommendation cards, best first.

    ``watched_keys`` is the caller's canonical engaged-with exclusion set —
    ``(media_type, tmdb_id)`` pairs already resolved by the host engine
    (``viewed ∪ diary ∪ review``, see ``api.for_you._load_local_state``), so
    this module never re-reads that history.

    It is a parameter rather than a query precisely so For You can hand over
    what it already loaded. Callers MUST pass the engaged-with set and must
    not reconstruct it as ``exclude_keys - watchlisted_keys``: ``exclude_keys``
    also holds watchlist members whose exclusion is only duplicate
    suppression, so that difference silently drops every title that is both
    watched and still listed — exactly what this rail surfaces.

    TV show completion is resolved HERE (not by the caller), because it lives
    in ``TVShowProgress`` which the For You engine does not read. See
    :func:`_completed_shows`.

    ``limit`` is clamped to :data:`RESURFACE_MAX`; ``scan_cap`` bounds the
    single watchlist read.
    """
    if user_id is None:
        return []
    if today is None:
        today = datetime.utcnow().date()
    elif isinstance(today, datetime):
        today = today.date()
    region = (region or 'US').upper()[:2]
    limit = max(0, min(int(limit or 0), RESURFACE_MAX))
    if limit == 0:
        return []
    watched_keys = watched_keys or set()

    entries = _watchlist_entries(user_id, today, scan_cap=scan_cap,
                                 min_days=min_days)
    if not entries:
        return []

    entries.sort(key=resurface_sort_key)

    # Exclusion filters run BEFORE the limit trim. Trimming first would let a
    # dismissed/completed title consume one of the six slots and leave the
    # rail short even though further eligible titles exist.
    #
    # Both exclusion reads stay single statements; their IN-lists are bounded
    # by the watchlist scan cap (WATCHLIST_SCAN_CAP), not by the watchlist
    # size, so the ordering change does not introduce unbounded reads.
    blocked = _blocking_keys(
        user_id, {e['tmdb_id'] for e in entries})
    eligible = [e for e in entries
                if (e['media_type'], e['tmdb_id']) not in watched_keys
                and (e['media_type'], e['tmdb_id']) not in blocked]

    # Canonical TV completion, resolved here rather than by the caller because
    # it lives in TVShowProgress, which the For You engine does not read.
    # Applied only when a TV candidate exists — a movie-only resurfacING costs
    # no extra query. Partially watched and unwatched shows are NOT removed:
    # only a show the canonical subsystem sealed as completed is.
    tv_ids = {e['tmdb_id'] for e in eligible if e['media_type'] == 'tv'}
    if tv_ids:
        done = _completed_shows(user_id, tv_ids)
        if done:
            eligible = [e for e in eligible
                        if not (e['media_type'] == 'tv'
                                and e['tmdb_id'] in done)]

    # Now trim: everything below is bounded by what can actually be returned.
    eligible = eligible[:limit]
    if not eligible:
        return []

    # Release context only for titles that survived: two batched reads, only
    # when that media type is actually present.
    movie_ids = {e['tmdb_id'] for e in eligible if e['media_type'] == 'movie'}
    show_ids = {e['tmdb_id'] for e in eligible if e['media_type'] == 'tv'}
    releases = _movie_release_map(movie_ids, region, today)
    episodes = _episode_air_date_map(show_ids, today)

    cards = []
    for entry in eligible:
        release = (releases.get(entry['tmdb_id'])
                   if entry['media_type'] == 'movie'
                   else episodes.get(entry['tmdb_id']))
        cards.append(_card(
            entry, release,
            resurface_reason(entry['neglect_days'], entry['priority'])))
    return cards[:limit]
