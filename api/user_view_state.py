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

      "Aired" means an episode that has actually aired, per the
      application's existing episode data: the synced UpcomingEpisode
      calendar (scripts/sync_upcoming_episodes.py, a rolling recent-airing
      window) unioned with the cached TMDb details payload — the
      ``last_episode_to_air`` anchor (everything up to it in its season is
      aired) PLUS the full episode lists of every historical season below
      that anchor (``seasons[].episode_count``, already carried by the same
      cached payload; no extra TMDb calls). A multi-season finished show
      therefore resolves its COMPLETE historical aired set (Banshee S4E8
      anchor ⇒ S1E1..10 ∪ S2E1..10 ∪ S3E1..10 ∪ S4E1..8 = 38), not just the
      latest season. Future/unreleased episodes never enter the
      denominator, so a finished show drops below 100% the moment a new
      episode airs and returns to 100% after the user watches it — no
      stored percentage, no reset at season boundaries.

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
from datetime import date, datetime

from models import (db, DiaryEntry, MediaItem, TVEpisodeWatch,
                    TVShowProgress, UpcomingEpisode)


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


def user_logged_today_keys(user, today=None):
    """Set of (tmdb_id, media_type) the user has logged a watch event for
    TODAY (DiaryEntry.watched_date == today).

    Distinguishes "viewed at some point" from "logged today" for the
    movie quick-log action without any per-card queries. Anonymous users
    get an empty set; a defensive except degrades to "no state".
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return set()
    try:
        rows = (
            db.session.query(MediaItem.tmdb_id, MediaItem.media_type)
            .join(DiaryEntry, DiaryEntry.media_id == MediaItem.id)
            .filter(
                DiaryEntry.user_id == user.id,
                DiaryEntry.watched_date == (today or date.today()),
            )
            .all()
        )
        return {(tmdb_id, mtype) for tmdb_id, mtype in rows}
    except Exception:
        return set()


def _historical_seasons(details, anchor_season, today):
    """Episode counts of seasons COMPLETELY aired before the anchor.

    Task E: ``last_episode_to_air`` only identifies the most recent aired
    episode — it says nothing about earlier seasons. A season strictly
    BELOW the anchor season has, by definition, finished airing, so its
    full TMDb episode list (``seasons[].episode_count``, carried by the
    SAME cached details payload — no extra requests) enters the aired set.
    Specials (season 0) are skipped and never re-added; a season whose own
    metadata explicitly dates it in the future (contradictory data) is
    excluded per the air-date correctness rule.
    """
    counts = {}
    for season in (details or {}).get("seasons") or []:
        sn = _coerce_int(season.get("season_number"))
        ep_count = _coerce_int(season.get("episode_count"))
        if not sn or not ep_count or sn >= anchor_season:
            continue
        raw = season.get("air_date")
        if raw:
            try:
                if datetime.strptime(
                        str(raw)[:10], "%Y-%m-%d").date() > today:
                    continue
            except ValueError:
                pass  # unparseable date → trust the anchor evidence
        counts[sn] = ep_count
    return counts


def _aired_positions(details):
    """Aired (season, episode) positions from TMDb show details.

    Two sources from the SAME cached payload:

      1. the ``last_episode_to_air`` anchor — every position of that
         season up to the anchor episode is aired (TMDb orders episode
         numbers);
      2. every historical season strictly BELOW the anchor season, in
         full, from TMDb season metadata (``_historical_seasons``) —
         reconstructing e.g. Banshee (S4E8 anchor) as S1E1..10 ∪
         S2E1..10 ∪ S3E1..10 ∪ S4E1..8 instead of the anchor season's
         range alone.

    Everything after the anchor (later seasons, later episodes) is the
    future and must stay out of the denominator.
    """
    last = (details or {}).get("last_episode_to_air") or {}
    season = _coerce_int(last.get("season_number"))
    episode = _coerce_int(last.get("episode_number"))
    if not season or not episode:
        return set()
    today = datetime.utcnow().date()
    aired = {(season, en) for en in range(1, episode + 1)}
    for sn, ep_count in _historical_seasons(details, season, today).items():
        aired |= {(sn, en) for en in range(1, ep_count + 1)}
    return aired


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


def aired_positions_for_show(show_id, details_loader=None):
    """The EXACT aired (season, episode) set used by tv_aired_progress
    for ONE show — the single shared rule for the bulk "Mark as Viewed"
    operation (Task D) so the write side and the read side can never
    diverge.

    Union of:
      - the synced UpcomingEpisode calendar rows with ``air_date <= today``
      - the cached TMDb details payload: the ``last_episode_to_air``
        anchor range PLUS every historical season below that anchor in
        full (season metadata rides on the same payload — no extra TMDb
        calls)
    with specials (season 0) excluded on both sides. Empty set when
    nothing has verifiably aired. Same failure policy as
    ``_compose_progress``: a details-loader failure degrades to
    calendar-only data instead of raising.
    """
    show_id = _coerce_int(show_id)
    if not show_id:
        return set()
    aired = _batched_aired_calendar(
        [show_id], datetime.utcnow().date()).get(show_id, set())
    loader = details_loader or _default_details_loader
    try:
        details = loader(show_id)
    except Exception:
        details = None
    aired |= _aired_positions(details)
    return {(s, e) for (s, e) in aired if s > 0}


def season_aired_for_show(show_id, details_loader=None):
    """``{season_number: aired_episode_count}`` for one show, derived from
    the same shared aired set (specials excluded). Backs the season-card
    denominators so "100% Complete" can never be computed against future
    episodes."""
    aired = aired_positions_for_show(show_id, details_loader)
    counts = {}
    for season, episode in aired:
        counts[season] = max(counts.get(season, 0), episode)
    return counts


def view_state_payload(user, movie_ids, tv_ids, details_loader=None,
                       movie_media_ids=None):
    """Batched JSON-ready personalized state for ONE page payload.

    Returns ``{"viewed_movie_ids": [...], "logged_today_movie_ids": [...],
    "tv_progress": {...}}`` — the shape the client store
    (static/js/view-state.js) consumes. ``logged_today_movie_ids`` is the
    Task D additive field distinguishing viewed movies from movies with a
    watch event dated today (quick-log wording); existing consumers that
    ignore it keep working unchanged.

    Bounded by construction: ``tv_ids`` never expands beyond the caller's
    list (Phase 17 overfetch rule) and movie ids are filtered to the ones
    actually on the surface. Anonymous users get every field empty.

    ``movie_media_ids`` (optional): MediaItem rows the caller already has —
    rows may carry a ``logged_today`` flag and then skip the batched
    logged-today lookup.
    """
    viewed_movie_ids = sorted(
        mid for (mid, mtype) in user_viewed_keys(user)
        if mtype == "movie")
    if movie_ids:
        wanted = {int(m) for m in movie_ids if _coerce_int(m)}
        viewed_movie_ids = [m for m in viewed_movie_ids if m in wanted]
    else:
        wanted = None  # no page scoping requested

    logged_today_ids = []
    if user is not None and getattr(user, "is_authenticated", False):
        if movie_media_ids is None:
            pairs = user_logged_today_keys(user)
        else:
            pairs = {
                (m.tmdb_id, m.media_type) for m in movie_media_ids
                if getattr(m, "logged_today", False)}
        logged_today_ids = sorted(
            tmdb_id for (tmdb_id, mtype) in pairs
            if mtype == "movie"
            and (wanted is None or tmdb_id in wanted))

    return {
        "viewed_movie_ids": viewed_movie_ids,
        "logged_today_movie_ids": logged_today_ids,
        "tv_progress": tv_aired_progress(user, tv_ids, details_loader),
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

    ``details_loader`` (optional) resolves cached TMDb show details for
    the aired-resolution anchor and historical season metadata; it is
    invoked ONLY for shows the user has actually started, and failures
    degrade to calendar-only data.
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


# ── Canonical TV WRITE core (Task F1) ────────────────────────────────
#
# ONE aired-episode definition for the whole application:
#     aired_positions_for_show()
# Everything below derives from it. No second semantic definition of
# "aired" may be introduced — see module docstring for the exact rules
# (historical seasons in, future out, specials out, duplicates collapsed,
# rewatches never inflating progress).
#
# Query contract (enforced by tests):
#   - aired set          : ≤ 1 calendar SELECT + ≤ 1 cached details load
#   - existing watches   : exactly 1 SELECT (season, episode, is_rewatch)
#   - inserts            : exactly 1 bulk INSERT, or 0 when nothing is missing
# Nothing here is per-episode. TMDb season metadata rides on the one
# cached details payload; no user-specific state is cached globally.


def aired_by_season(aired):
    """``{season: highest aired episode}`` from an already-resolved set.

    Pure helper so callers that already hold the canonical set never
    re-resolve it just to count seasons.
    """
    counts = {}
    for season, episode in aired:
        if season > 0:
            counts[season] = max(counts.get(season, 0), episode)
    return counts


def memoized_details_loader(details_loader=None):
    """One-details-resolution-per-operation wrapper (request-scoped).

    Wraps the caller's loader so a single bulk operation resolves each
    show's cached TMDb details payload at most once — the aired set, the
    counter recompute and the completion gate all read the SAME
    resolution. The memo lives only inside the calling operation; it is
    never a global cache and never holds user-specific state (public TMDb
    metadata caching stays with the existing shared cache).
    """
    base = details_loader or _default_details_loader
    cache = {}

    def loader(show_id):
        if show_id not in cache:
            cache[show_id] = base(show_id)
        return cache[show_id]

    return loader


def _watched_rows_for_show(user_id, show_id):
    """``(all_positions, non_rewatch_positions)`` in ONE bounded SELECT.

    ``all_positions`` covers every row (including rewatch rows).
    ``non_rewatch_positions`` is the canonical FIRST-WATCH set: a rewatch
    never inflates progress, and a position holding ONLY a rewatch row
    has no canonical watch yet (the bulk core inserts it).
    """
    rows = (
        db.session.query(
            TVEpisodeWatch.season_number,
            TVEpisodeWatch.episode_number,
            TVEpisodeWatch.is_rewatch,
        )
        .filter(
            TVEpisodeWatch.user_id == user_id,
            TVEpisodeWatch.show_id == show_id,
        )
        .all()
    )
    all_positions = {(s, e) for s, e, _ in rows}
    first_watch = {(s, e) for s, e, rw in rows if not rw}
    return all_positions, first_watch


def get_or_create_tv_progress(user_id, show_id):
    """Fetch (or create) the TVShowProgress row for one user + show.

    Counters are deliberately left at zero here — they are derived from
    the canonical aired state by ``sync_tv_progress_counters`` rather than
    from TMDb's ``number_of_episodes``, which counts unaired episodes.
    """
    progress = TVShowProgress.query.filter_by(
        user_id=user_id, show_id=show_id).first()
    if progress is not None:
        return progress
    progress = TVShowProgress(
        user_id=user_id,
        show_id=show_id,
        total_seasons=0,
        total_episodes=0,
        watched_seasons=0,
        watched_episodes=0,
        status='watching',
    )
    db.session.add(progress)
    db.session.flush()
    return progress


def sync_tv_progress_counters(progress, user_id, show_id, aired=None,
                              details_loader=None):
    """Recompute every TVShowProgress counter from the canonical state.

    ``progress.total_episodes`` now means "AIRED episodes", not "TMDb
    total". ``progress.watched_episodes`` counts distinct non-rewatch
    positions. ``watched_seasons`` counts seasons whose every AIRED
    episode is watched — a season with no aired episodes is never
    complete. That makes ``calculate_progress_percentage()`` correct
    without the model ever becoming network-aware.
    """
    if aired is None:
        aired = aired_positions_for_show(show_id, details_loader)
    all_positions, first_watch = _watched_rows_for_show(user_id, show_id)
    aired_seasons = aired_by_season(aired)
    watched_by_season = aired_by_season(first_watch & set(aired))

    progress.total_episodes = len(aired)
    progress.watched_episodes = len(first_watch)
    progress.total_seasons = len(aired_seasons)
    progress.watched_seasons = sum(
        1 for season, count in aired_seasons.items()
        if watched_by_season.get(season, 0) >= count
    )
    return progress


def apply_completion_gating(progress, show_id, details_loader=None):
    """Seal a show as 'completed' only when TMDb reports Ended/Canceled.

    Comparison uses the canonical counters (aired denominator), so a
    running show is never sealed and a completed show whose denominator
    grows again falls back to 'watching' without any stored percentage.
    A show with NO verifiably aired data (metadata failure / empty set)
    never changes status — un-sealing requires positive aired evidence.
    """
    complete = (progress.total_episodes > 0
                and progress.watched_episodes >= progress.total_episodes)
    if complete:
        loader = details_loader or _default_details_loader
        try:
            details = loader(show_id) or {}
        except Exception:
            details = {}
        if details.get('status') in ('Ended', 'Canceled'):
            if progress.status != 'completed':
                progress.completed_at = datetime.utcnow()
            progress.status = 'completed'
            return progress
    if (progress.status == 'completed'
            and progress.total_episodes > 0):
        # Aired reality exists and shows incompleteness — un-seal.
        progress.status = 'watching'
        progress.completed_at = None
    return progress


def mark_aired_positions_watched(user, show_id, progress=None, aired=None,
                                 counter_aired=None, details_loader=None,
                                 watched_date=None, commit=True):
    """Canonical bulk "mark watched" core — the single write path.

    Resolves the canonical aired set, inserts ONLY the positions that
    lack a canonical first-watch row, and never touches an existing row:

      * no DELETE, ever — ratings, notes, dates and rewatch rows survive;
      * no rewatch rows are manufactured (a position holding only a
        rewatch row still receives its canonical first-watch row — the
        rewatch record itself is preserved untouched);
      * no diary events are fabricated;
      * idempotent — a second call inserts nothing and mutates nothing;
      * season-scoped callers pass ``aired`` narrowed to one season and
        ``counter_aired`` as the FULL canonical set, so the show-level
        counters always reflect the whole show's aired reality (never a
        single-season denominator). Mark Season Watched and Mark All
        Watched share this exact code path.

    The details payload is resolved at most ONCE per operation (shared
    by the aired set, the counter recompute and the completion gate).

    Returns ``(progress, inserted_count, inserted_set_size)``.
    """
    user_id = getattr(user, 'id', user)
    loader = memoized_details_loader(details_loader)
    insert_set = aired
    if insert_set is None:
        insert_set = counter_aired
    if insert_set is None:
        insert_set = counter_aired = aired_positions_for_show(
            show_id, details_loader=loader)
    if not insert_set:
        # Nothing has verifiably aired — never manufacture state.
        return progress, 0, 0

    if progress is None:
        progress = get_or_create_tv_progress(user_id, show_id)
    if progress.id is None:
        db.session.flush()

    _, first_watch = _watched_rows_for_show(user_id, show_id)
    missing = sorted(insert_set - first_watch)
    if missing:
        day = watched_date or datetime.utcnow().date()
        db.session.bulk_insert_mappings(
            TVEpisodeWatch,
            [
                {
                    'user_id': user_id,
                    'show_id': show_id,
                    'progress_id': progress.id,
                    'season_number': season,
                    'episode_number': episode,
                    'watched_date': day,
                    'is_rewatch': False,
                }
                for season, episode in missing
            ],
        )
        db.session.flush()

    progress.last_watched = datetime.utcnow()
    sync_tv_progress_counters(
        progress, user_id, show_id, aired=counter_aired,
        details_loader=loader)
    apply_completion_gating(progress, show_id, details_loader=loader)
    if commit:
        db.session.commit()
    return progress, len(missing), len(insert_set)


def mark_season_aired_watched(user, show_id, season, details_loader=None,
                              watched_date=None, commit=True):
    """Season-scoped wrapper over ``mark_aired_positions_watched``.

    The aired set is narrowed to ``season`` AFTER canonical resolution, so
    a season mark can never select episodes outside the aired rule and
    can never touch specials or another season.
    """
    loader = memoized_details_loader(details_loader)
    aired = aired_positions_for_show(show_id, details_loader=loader)
    season_int = _coerce_int(season)
    if not aired or not season_int:
        return None, 0, 0
    scoped = {pos for pos in aired if pos[0] == season_int}
    if not scoped:
        return None, 0, 0
    return mark_aired_positions_watched(
        user, show_id, aired=scoped, counter_aired=aired,
        details_loader=loader, watched_date=watched_date, commit=commit)


def canonical_tv_progress(user, show_id, details_loader=None):
    """Canonical ``{watched, aired, percent}`` for one show, or ``None``.

    ``None`` means "no personalized progress" (the user has not watched
    anything), never "0%". Callers use it instead of the stored
    ``TVShowProgress`` counters so no endpoint can publish a competing
    denominator.
    """
    result = tv_aired_progress(user, [show_id],
                               details_loader).get(show_id)
    return dict(result) if result else None
