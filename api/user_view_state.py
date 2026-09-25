"""User-scoped view state for media surfaces (single shared contract).

One module so every card/hero surface renders the SAME personalized state:

  MOVIE  →  ``user_viewed_keys(user)``
      Canonical viewed state: the (tmdb_id, media_type) pairs already used by
      the detail pages and collection surfaces (the ``user_viewed`` junction
      table in models/associations.py, exposed as ``user.viewed_media``,
      written by DiaryEntry quick-log and ``/mark_as_viewed``). Cards never
      invent a second "watched" definition.

  TV  →  ``tv_aired_progress(user, show_ids, details_loader=...)``
      Show-level completion against AIRING reality, recomputed on every read:

          watched unique aired episodes
          -----------------------------  × 100
          total aired non-special episodes

      "Aired" means an episode whose air date has passed per the
      application's existing episode data: the synced UpcomingEpisode
      calendar (scripts/sync_upcoming_episodes.py) plus the show's
      ``last_episode_to_air`` from cached TMDb details. Future/unreleased
      episodes never enter the denominator, so a finished show drops below
      100% the moment a new episode airs and returns to 100% after the user
      watches it — no stored percentage, no reset at season boundaries.

Performance contract (no per-card queries):
  - movie viewed state: derived from the user relationship the routes
    already touch (one lazy SELECT, same as get_user_collection_ids today);
  - TV progress: exactly two SQL statements per page payload regardless of
    card count; cached TMDb details are consulted only for shows the user
    has actually started (via the existing shared cache).

Privacy contract: every function is strictly user-scoped; anonymous users
always get empty results and no personalized state can reach another user.
Nothing here is cached globally — callers render per request.
"""
from datetime import datetime

from models import db, TVEpisodeWatch, UpcomingEpisode


def user_viewed_keys(user):
    """Set of (tmdb_id, media_type) the user has canonically viewed.

    Anonymous users get an empty set. A defensive except keeps a broken
    session from failing a whole page (degrades to "no badges").
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return set()
    try:
        return {(i.tmdb_id, i.media_type) for i in user.viewed_media}
    except Exception:
        return set()


def user_watchlist_keys(user):
    """Set of (tmdb_id, media_type) watchlist pairs (same contract)."""
    if user is None or not getattr(user, "is_authenticated", False):
        return set()
    try:
        return {(i.tmdb_id, i.media_type) for i in user.watchlist}
    except Exception:
        return set()


def _coerce_int(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _aired_positions(details):
    """Aired (season, episode) positions from TMDb show details.

    ``last_episode_to_air`` is TMDb's marker for the most recent episode
    that has actually aired — every earlier position of that season is
    aired by definition (TMDb orders episode numbers). Everything after it
    is the future and must stay out of the denominator.
    """
    last = (details or {}).get("last_episode_to_air") or {}
    season = _coerce_int(last.get("season_number"))
    episode = _coerce_int(last.get("episode_number"))
    if not season or not episode:
        return set()
    return {(season, en) for en in range(1, episode + 1)}


def _default_details_loader(show_id):
    """Resolve cached TMDb show details via the existing shared cache."""
    from api.continue_watching import show_details
    return show_details(show_id)


def _batched_watched(user, ids):
    """Unique non-rewatch watched positions per show (single statement)."""
    rows = (
        db.session.query(
            TVEpisodeWatch.show_id,
            TVEpisodeWatch.season_number,
            TVEpisodeWatch.episode_number,
        )
        .filter(
            TVEpisodeWatch.user_id == user.id,
            TVEpisodeWatch.show_id.in_(ids),
            TVEpisodeWatch.is_rewatch == False,  # noqa: E712
        )
        .all()
    )
    watched = {}
    for show_id, season, episode in rows:
        watched.setdefault(show_id, set()).add((season, episode))
    return watched


def _batched_aired_calendar(ids, today):
    """Already-aired positions per show from the synced calendar."""
    rows = (
        db.session.query(
            UpcomingEpisode.show_id,
            UpcomingEpisode.season_number,
            UpcomingEpisode.episode_number,
        )
        .filter(
            UpcomingEpisode.show_id.in_(ids),
            UpcomingEpisode.air_date <= today,
        )
        .all()
    )
    calendar = {}
    for show_id, season, episode in rows:
        calendar.setdefault(show_id, set()).add((season, episode))
    return calendar


def _compose_progress(watched_set, calendar_set, details_loader, show_id):
    """Aired-reality progress for one started show (no DB access here)."""
    aired_set = set(calendar_set)
    try:
        details = details_loader(show_id)
    except Exception:
        details = None  # degrade to calendar-only data
    aired_set |= _aired_positions(details)
    # Specials (season 0) never count on either side.
    aired_set = {(s, e) for (s, e) in aired_set if s > 0}
    if not aired_set:
        return None  # nothing has verifiably aired yet
    numerator = len({pos for pos in watched_set if pos in aired_set})
    return {
        "watched": numerator,
        "aired": len(aired_set),
        "percent": round((numerator / len(aired_set)) * 100, 1),
    }


def tv_aired_progress(user, show_ids, details_loader=None):
    """Overall aired-episode progress for the given TMDb show ids.

    Returns ``{show_id: {"watched": int, "aired": int, "percent": float}}``
    — entries exist only for shows the user has actually started (at least
    one unique non-rewatch episode watch), so "no watched episodes" means
    no personalized progress at all.

    Rewatch events are excluded from the numerator and duplicates collapse
    into unique positions (matching the canonical next-episode logic in
    api/continue_watching.py). Specials (season 0) are excluded on both
    sides, per the application's existing policy.

    ``details_loader`` (optional) resolves cached TMDb show details for the
    ``last_episode_to_air`` anchor; it is invoked ONLY for shows the user
    has actually started, and failures degrade to calendar-only data.
    """
    if not show_ids or user is None or not getattr(
            user, "is_authenticated", False):
        return {}
    ids = {sid for sid in map(_coerce_int, show_ids) if sid}

    # Batch 1 — unique watched positions for every show on the page.
    watched = _batched_watched(user, ids)
    if not watched:
        return {}

    # Batch 2 — already-aired positions from the synced episode calendar.
    calendar = _batched_aired_calendar(ids, datetime.utcnow().date())

    # Compose per started show (pure set math — no further DB queries).
    # TMDb details are consulted only for shows with watch rows, through
    # the existing shared cache (api.continue_watching.show_details).
    loader = details_loader or _default_details_loader
    progress = {}
    for show_id, watched_set in watched.items():
        result = _compose_progress(
            watched_set, calendar.get(show_id, ()), loader, show_id)
        if result:
            progress[show_id] = result
    return progress
