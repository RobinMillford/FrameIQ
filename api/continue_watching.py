"""Canonical Continue Watching entries (home rail).

User progress rows (WatchProgress / TVShowProgress) are USER STATE, not
authoritative TMDb metadata. This module resolves canonical display data
(title, poster) and — for TV — validates the season/episode against
authoritative TMDb season episode counts so a nonexistent episode is never
emitted.

Guarantees:
- No raw TMDb id is ever used as a card title.
- A TV watch_url is only built for episodes that exist per TMDb season data.
- Metadata failures degrade gracefully (cards degrade or are safely omitted);
  a missing TMDb result never 500s the home page.
- Bounded work: at most ONE cached details fetch per distinct id per TTL
  window (per-process memo + the existing tmdb_cache/single-flight layer).
"""
import logging
import threading
import time

logger = logging.getLogger(__name__)

# Poster paths may be stored as "/abc.jpg" (TMDb raw) or as full URLs
# ("https://image.tmdb.org/...", as the watch page saves them). Normalize.
_POSTER_BASE = "https://image.tmdb.org/t/p/w500"
_FALLBACK_POSTER = "https://via.placeholder.com/500x750?text=No+Image"

# Per-process memo for details lookups. TTL + bounded size prevent growth.
_MEMO_TTL = 30 * 60          # seconds
_MEMO_MAX = 300
_memo = {}
_memo_lock = threading.Lock()


def poster_url(poster_path):
    """Full poster URL from a raw path or full URL; fallback for empty/None."""
    if not poster_path:
        return _FALLBACK_POSTER
    p = str(poster_path)
    if p.startswith("http://") or p.startswith("https://"):
        return p
    if p.startswith("/"):
        return _POSTER_BASE + p
    return _FALLBACK_POSTER


def _memo_get(key):
    now = time.monotonic()
    with _memo_lock:
        hit = _memo.get(key)
        if hit and now - hit[0] < _MEMO_TTL:
            return hit[1]
        if hit:
            del _memo[key]
    return None


def _memo_put(key, value):
    with _memo_lock:
        if len(_memo) >= _MEMO_MAX:
            oldest = sorted(_memo.items(), key=lambda kv: kv[1][0])
            for k, _ in oldest[:max(1, _MEMO_MAX // 10)]:
                del _memo[k]
        _memo[key] = (time.monotonic(), value)


def _details(tmdb_id, is_movie):
    """Cached TMDb details dict for one id, or None on any failure.

    Delegates to the project's existing cached fetchers (bounded TTL cache,
    single-flight, retries). Memoized so repeated home renders do not even
    hit the TMDb cache layer. None results are memoized too — a dead id
    should not be retried on every render.
    """
    key = ("details", is_movie, tmdb_id)
    hit = _memo_get(key)
    if hit is not None:
        return hit
    try:
        if is_movie:
            from api.tmdb_client import fetch_movie_details
            data = fetch_movie_details(tmdb_id, max_retries=1)
        else:
            from api.tmdb_client import fetch_tv_show_details
            data = fetch_tv_show_details(tmdb_id, max_retries=1)
    except Exception:
        data = None
    _memo_put(key, data)
    return data


def season_episode_counts(show_details):
    """TMDb season_number -> episode_count for non-special seasons."""
    counts = {}
    for season in (show_details or {}).get("seasons", []):
        sn = season.get("season_number")
        if sn and sn > 0:
            counts[sn] = season.get("episode_count") or 0
    return counts


def next_unwatched_from_counts(watched, counts):
    """Earliest unwatched (season, episode) per authoritative TMDb counts."""
    for sn in sorted(counts):
        for en in range(1, counts[sn] + 1):
            if (sn, en) not in watched:
                return (sn, en)
    return None


def last_valid_episode(counts):
    """Highest valid (season, episode) per TMDb season counts."""
    last = None
    for sn in sorted(counts):
        if counts[sn] > 0:
            last = (sn, counts[sn])
    return last


def _canonical_title_poster(details, stored_title, stored_poster, name_key):
    """Canonical (title, poster) with stored values as fallback.

    When TMDb details resolve, canonical metadata wins (stale stored rows are
    corrected). Otherwise the stored denormalized values are used as-is.
    """
    title = stored_title or None
    poster = stored_poster
    if details:
        title = details.get(name_key) or title
        raw = details.get("poster_path")
        if raw and isinstance(raw, str) and raw.startswith("/"):
            poster = _POSTER_BASE + raw
        elif raw and raw != _FALLBACK_POSTER:
            poster = raw
    return title or "Untitled", poster_url(poster)


def _build_movie_entry(wp, details):
    """Canonical movie Continue Watching entry. Movies are never omitted —
    when metadata cannot be resolved they degrade to Untitled/fallback
    poster instead of showing a raw numeric id."""
    title, poster = _canonical_title_poster(
        details, wp.title, wp.poster_path, "title")
    return {
        "id": wp.tmdb_id,
        "tmdb_id": wp.tmdb_id,
        "title": title,
        "poster": poster,
        "progress": wp.progress_pct,
        "watch_url": f"/watch/movie/{wp.tmdb_id}",
        "media_type": "movie",
    }


def _build_tv_entry(wp, details):
    """Canonical TV entry from a partial-playback row, or None.

    The stored/current episode is validated against authoritative TMDb
    season episode counts. Invalid/stale positions are corrected to the most
    recent valid episode in the same season (when the stored number was out
    of range) or to the first valid episode; a card is omitted only when no
    valid episode data exists at all. Episodes are never invented.
    """
    if not details:
        # Without authoritative season data we cannot validate the episode —
        # omit rather than risk a broken watch URL.
        return None

    counts = season_episode_counts(details)
    if not counts:
        return None

    season = wp.season if wp.season is not None else 1
    episode = wp.episode if wp.episode is not None else 1

    if season not in counts or episode < 1 or episode > counts[season]:
        resolved = None
        if season in counts and counts[season] > 0 and episode > counts[season]:
            # Stored episode number is beyond the real season length — the
            # most recent valid episode in that season is the resumable one.
            resolved = (season, counts[season])
        if resolved is None:
            for sn in sorted(counts):
                if counts[sn] > 0:
                    resolved = (sn, 1)
                    break
        if resolved is None:
            return None
        season, episode = resolved

    type_param = wp.media_type if wp.media_type in ("tv", "anime") else "tv"
    title, poster = _canonical_title_poster(
        details, wp.title, wp.poster_path, "name")
    return {
        "id": wp.tmdb_id,
        "tmdb_id": wp.tmdb_id,
        "title": title,
        "poster": poster,
        "media_type": type_param,
        "season": season,
        "episode": episode,
        "progress": wp.progress_pct,
        "watch_url": f"/watch/tv/{wp.tmdb_id}/{season}/{episode}?type={type_param}",
        "label": f"S{season}E{episode} · {wp.progress_pct:.0f}%",
    }


def _build_unfinished_show_entry(s, watched, details):
    """Canonical unfinished-show entry from TVShowProgress, or None.

    Next episode is derived from authoritative TMDb season counts (never by
    naive E+1 arithmetic); falls back to the last valid episode when
    everything per TMDb is already watched (stale counts). Returns None when
    no valid episode exists — the card is omitted, not emitted broken.
    """
    if not details:
        return None
    counts = season_episode_counts(details)
    if not counts:
        return None

    next_ep = next_unwatched_from_counts(watched, counts)
    if next_ep is None:
        next_ep = last_valid_episode(counts)
    if next_ep is None:
        return None

    season, episode = next_ep
    title, poster = _canonical_title_poster(details, None, None, "name")
    pct = s.calculate_progress_percentage()
    return {
        "id": s.show_id,
        "tmdb_id": s.show_id,
        "title": title,
        "poster": poster,
        "media_type": "tv",
        "season": season,
        "episode": episode,
        "progress": pct,
        "watch_url": f"/watch/tv/{s.show_id}/{season}/{episode}",
        "label": f"S{season}E{episode} · {pct:.0f}%",
        "is_paused": s.status == "paused",
    }


def _load_progress_rows(user_id, limit):
    """Partial-playback rows (movies + TV), most recently updated first."""
    from models import WatchProgress
    return (
        WatchProgress.query.filter_by(user_id=user_id)
        .filter(
            WatchProgress.duration > 60,
            WatchProgress.current_time < WatchProgress.duration * 0.9,
        )
        .order_by(WatchProgress.updated_at.desc())
        .limit(limit)
        .all()
    )


def _load_unfinished_shows(user_id, limit):
    """Unfinished tracked shows (watching/paused, not complete)."""
    from models import TVShowProgress
    from sqlalchemy import or_
    return (
        TVShowProgress.query.filter(
            TVShowProgress.user_id == user_id,
            TVShowProgress.status.in_(["watching", "paused"]),
            or_(
                TVShowProgress.total_episodes == 0,
                TVShowProgress.watched_episodes < TVShowProgress.total_episodes,
            ),
        )
        .order_by(TVShowProgress.last_watched.desc())
        .limit(limit)
        .all()
    )


def _completed_ids_for_user(user_id):
    """Completion cross-check: (movie_dates, episode_times).

    Real completed state wins over stale resume state:
      - movies: tmdb_id -> latest DiaryEntry watched_date (canonical history),
      - TV: (show_id, season, episode) -> latest TVEpisodeWatch.updated_at.

    Recency decides: a resume row NEWER than the completed event means a
    rewatch is genuinely in progress and is kept; a resume row at or older
    than the event is stale and must not reappear on Continue Watching.
    """
    from models import DiaryEntry, TVEpisodeWatch, MediaItem, db

    movie_rows = (
        db.session.query(MediaItem.tmdb_id, DiaryEntry.watched_date)
        .join(DiaryEntry, DiaryEntry.media_id == MediaItem.id)
        .filter(DiaryEntry.user_id == user_id,
                DiaryEntry.media_type == "movie")
        .all()
    )
    movie_dates = {}
    for tmdb_id, watched_date in movie_rows:
        prev = movie_dates.get(tmdb_id)
        if prev is None or (watched_date and watched_date > prev):
            movie_dates[tmdb_id] = watched_date

    episode_rows = (
        db.session.query(
            TVEpisodeWatch.show_id,
            TVEpisodeWatch.season_number,
            TVEpisodeWatch.episode_number,
            TVEpisodeWatch.updated_at,
        )
        .filter(TVEpisodeWatch.user_id == user_id)
        .all()
    )
    ep_times = {}
    for sid, sn, en, ts in episode_rows:
        key = (sid, sn, en)
        prev = ep_times.get(key)
        if prev is None or (ts and ts > prev):
            ep_times[key] = ts
    return movie_dates, ep_times


def _load_show_state(user_id, show_ids):
    """Batched per-show state for unfinished shows.

    Returns (watched_by_show, info_by_show): watched episode positions and
    UpcomingEpisode display info — two queries total, no N+1.
    """
    from models import TVEpisodeWatch, UpcomingEpisode, db
    watched_by_show = {}
    info_by_show = {}
    if not show_ids:
        return watched_by_show, info_by_show

    watched_rows = (
        db.session.query(
            TVEpisodeWatch.show_id,
            TVEpisodeWatch.season_number,
            TVEpisodeWatch.episode_number,
        )
        .filter(
            TVEpisodeWatch.user_id == user_id,
            TVEpisodeWatch.show_id.in_(show_ids),
            TVEpisodeWatch.is_rewatch == False,  # noqa: E712
        )
        .all()
    )
    for sid, sn, en in watched_rows:
        watched_by_show.setdefault(sid, set()).add((sn, en))

    info_rows = (
        db.session.query(
            UpcomingEpisode.show_id,
            UpcomingEpisode.show_name,
            UpcomingEpisode.poster_path,
        )
        .filter(UpcomingEpisode.show_id.in_(show_ids))
        .all()
    )
    for sid, name, poster in info_rows:
        info_by_show.setdefault(sid, {"name": name, "poster_path": poster})
    return watched_by_show, info_by_show


def _details_resolver():
    """Details fetcher with per-render deduplication (one call max per id)."""
    cache = {}

    def resolve(tmdb_id, is_movie):
        if tmdb_id not in cache:
            cache[tmdb_id] = _details(tmdb_id, is_movie)
        return cache[tmdb_id]

    return resolve


def _apply_show_fallback(entry, info_by_show, tmdb_id):
    """Last-resort display data from the tracked-show cache."""
    if entry is None:
        return None
    info = info_by_show.get(tmdb_id)
    if info:
        if entry.get("title") == "Untitled" and info.get("name"):
            entry["title"] = info["name"]
        if info.get("poster_path") and entry.get("poster") == _FALLBACK_POSTER:
            entry["poster"] = poster_url(info["poster_path"])
    return entry


def _partial_playback_entries(
        progress, resolve, info_by_show, completed_movies, completed_ep_times):
    """Canonical entries from partial-playback rows (movies + TV).

    Rows whose resolved position is genuinely completed (per the completion
    cross-check) are skipped — stale resume data must not resurface after
    real completion. A resume row NEWER than its completed event is a rewatch
    in progress and is kept.
    """
    entries = []
    for wp in progress:
        try:
            if wp.media_type == "movie":
                if not _movie_stale(
                        wp, completed_movies.get(wp.tmdb_id)):
                    needs_meta = (
                        not wp.title or wp.title == "Unknown"
                        or not wp.poster_path
                    )
                    details = resolve(wp.tmdb_id, True) if needs_meta else None
                    entries.append(_build_movie_entry(wp, details))
            else:
                # TV/anime partial playback: episode must be validated, which
                # requires authoritative TMDb season data.
                entry = _build_tv_entry(wp, resolve(wp.tmdb_id, False))
                if entry is not None and not _episode_stale(
                        wp, completed_ep_times.get(
                            (wp.tmdb_id, entry["season"], entry["episode"]))):
                    entry = _apply_show_fallback(entry, info_by_show, wp.tmdb_id)
                    entries.append(entry)
        except Exception:
            logger.exception(
                "Continue Watching: failed to build entry for %s", wp)
            continue
    return entries


def _movie_stale(wp, completed_date):
    """True when the resume row predates the movie's completed (diary) event."""
    if completed_date is None:
        return False
    row_ts = wp.updated_at
    row_date = row_ts.date() if row_ts else None
    return row_date is None or row_date <= completed_date


def _episode_stale(wp, completed_ts):
    """True when the resume row predates the episode's completed event."""
    if completed_ts is None:
        return False
    return wp.updated_at is None or wp.updated_at <= completed_ts


def _unfinished_show_entries(
        shows, watched_by_show, info_by_show, covered_ids, resolve):
    """Canonical entries for unfinished shows without a partial card."""
    entries = []
    for s in shows:
        if s.show_id in covered_ids:
            continue
        try:
            details = resolve(s.show_id, False)
            entry = _build_unfinished_show_entry(
                s, watched_by_show.get(s.show_id, set()), details)
            entry = _apply_show_fallback(entry, info_by_show, s.show_id)
            if entry is not None:
                entries.append(entry)
        except Exception:
            logger.exception(
                "Continue Watching: failed to build unfinished show %s",
                s.show_id)
            continue
    return entries


def continue_watching_entries(user_id, movie_limit=12, tv_limit=6):
    """Canonical home Continue Watching entries: partial playback + unfinished
    shows, most-recently-watched first.

    Metadata resolution per card:
      1. canonical cached TMDb details when they resolve (stale stored
         title/poster rows are corrected),
      2. denormalized WatchProgress title/poster as fallback,
      3. tracked-show display data (UpcomingEpisode cache) as last resort.
    TV episodes are always validated against TMDb season episode counts;
    invalid/stale rows are corrected or omitted, never emitted broken.
    """
    progress = _load_progress_rows(user_id, movie_limit + tv_limit)
    shows = _load_unfinished_shows(user_id, tv_limit)
    watched_by_show, info_by_show = _load_show_state(
        user_id, [s.show_id for s in shows])

    completed_movies, completed_ep_times = _completed_ids_for_user(user_id)

    resolve = _details_resolver()
    entries = _partial_playback_entries(
        progress, resolve, info_by_show, completed_movies, completed_ep_times)

    covered = {
        e["id"] for e in entries if e["media_type"] in ("tv", "anime")
    }
    entries.extend(_unfinished_show_entries(
        shows, watched_by_show, info_by_show, covered, resolve))
    return entries
