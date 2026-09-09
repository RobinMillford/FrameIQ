"""Continue Watching — intent-based (STARTED → FINISHED / REMOVED).

FrameIQ is a tracking application, not a playback engine:

  FRAMEIQ  → remembers WHAT the user started (ContinueWatchingItem rows)
  PROVIDER → remembers WHERE the user stopped (resume position)

Continue Watching requires ZERO player telemetry — no player messages, no
positions, no percentages, no timers, no polling. Completion is
deterministic — the user explicitly presses "✓ Finished".

All display metadata (title/poster) is resolved from the existing cached
TMDb layer; stored title/poster on the item are only a same-request hint.
TV episodes are validated against authoritative TMDb season episode counts;
a nonexistent episode is never emitted. Unaired next episodes are hidden.
"""
import logging
import threading
import time

logger = logging.getLogger(__name__)

_POSTER_BASE = "https://image.tmdb.org/t/p/w500"
_FALLBACK_POSTER = "https://via.placeholder.com/500x750?text=No+Image"

# Per-process memo for details lookups. TTL + bounded size prevent growth.
# The underlying fetchers already provide bounded TTL cache + single-flight;
# the memo avoids repeated cache-layer hits across home renders.
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


def show_details(tmdb_id):
    """Cached TMDb show details dict, or None on any failure."""
    key = ("show", tmdb_id)
    hit = _memo_get(key)
    if hit is not None:
        return hit
    try:
        from api.tmdb_client import fetch_tv_show_details
        data = fetch_tv_show_details(tmdb_id, max_retries=1)
    except Exception:
        data = None
    _memo_put(key, data)
    return data


def movie_details(tmdb_id):
    """Cached TMDb movie details dict, or None on any failure."""
    key = ("movie", tmdb_id)
    hit = _memo_get(key)
    if hit is not None:
        return hit
    try:
        from api.tmdb_client import fetch_movie_details
        data = fetch_movie_details(tmdb_id, max_retries=1)
    except Exception:
        data = None
    _memo_put(key, data)
    return data


def clear_memo():
    """Test helper — clears the process-local details memo."""
    _memo.clear()


def season_episode_counts(show_details):
    """TMDb season_number -> episode_count for non-special seasons."""
    counts = {}
    for season in (show_details or {}).get("seasons", []):
        sn = season.get("season_number")
        if sn and sn > 0:
            counts[sn] = season.get("episode_count") or 0
    return counts


# ── Start (idempotent upsert) ────────────────────────────────────────────────

def start_item(user_id, media_type, tmdb_id, season=None, episode=None,
               title=None, poster_path=None):
    """Record that the user started this movie/episode.

    Idempotent: re-opening the same item updates started_at instead of
    creating a duplicate. One logical item per unfinished movie/episode.
    """
    from models import ContinueWatchingItem, db

    item = ContinueWatchingItem.query.filter_by(
        user_id=user_id, media_type=media_type, tmdb_id=tmdb_id,
        season=season, episode=episode,
    ).first()
    if item:
        from datetime import datetime
        item.started_at = datetime.utcnow()
        if title:
            item.title = title[:255]
        if poster_path:
            item.poster_path = poster_path[:500]
    else:
        item = ContinueWatchingItem(
            user_id=user_id,
            media_type=media_type,
            tmdb_id=tmdb_id,
            season=season,
            episode=episode,
            title=(title or None),
            poster_path=poster_path,
        )
        db.session.add(item)
    db.session.commit()
    return item


def remove_item(user_id, media_type, tmdb_id, season=None, episode=None):
    """Remove an item from Continue Watching WITHOUT marking it watched."""
    from models import ContinueWatchingItem, db

    deleted = ContinueWatchingItem.query.filter_by(
        user_id=user_id, media_type=media_type, tmdb_id=tmdb_id,
        season=season, episode=episode,
    ).delete()
    db.session.commit()
    return deleted > 0


# ── TV finish + next-episode promotion ──────────────────────────────────────

def watched_episode_keys(user_id, show_id):
    """Set of watched (season, episode) positions for one show.

    Rewatch events are excluded: a rewatch of an episode does not change
    which episode is 'next' in the canonical progression.
    """
    from models import TVEpisodeWatch, db
    rows = (
        db.session.query(
            TVEpisodeWatch.season_number, TVEpisodeWatch.episode_number)
        .filter(
            TVEpisodeWatch.user_id == user_id,
            TVEpisodeWatch.show_id == show_id,
            TVEpisodeWatch.is_rewatch == False,  # noqa: E712
        )
        .all()
    )
    return {(sn, en) for sn, en in rows}


def find_next_episode(user_id, show_id, after=None, counts=None):
    """Next valid, unwatched, aired episode for a show, or None.

    With `after` (the just-finished position) the search starts at the first
    position AFTER that episode — finishing S2E4 promotes S2E5, and a show
    whose earlier episodes are unwatched still advances forward instead of
    jumping back. Without `after`, the earliest unwatched episode overall is
    returned (first-open behavior).

    Validation uses authoritative TMDb season episode counts (cached) —
    episodes are never invented (no S1E11 of a 10-episode season) and
    specials (season 0) are skipped per the application's existing policy.
    Aired state comes from the synced UpcomingEpisode table; an episode
    with no synced row and no known air date is treated as aired so shows
    outside the 60-day sync window still continue.
    """
    from datetime import datetime
    from models import UpcomingEpisode

    details = show_details(show_id)
    counts = counts if counts is not None else season_episode_counts(details)
    if not counts:
        return None

    watched = watched_episode_keys(user_id, show_id)
    today = datetime.utcnow().date()
    upcoming = {
        (u.season_number, u.episode_number): u
        for u in UpcomingEpisode.query.filter_by(show_id=show_id).all()
    }

    for sn in sorted(counts):
        if after is not None and sn < after[0]:
            continue
        start_en = 1
        if after is not None and sn == after[0]:
            start_en = after[1] + 1
        for en in range(start_en, counts[sn] + 1):
            if (sn, en) in watched:
                continue
            u = upcoming.get((sn, en))
            air_date = u.air_date if u else None
            aired = air_date is None or air_date <= today
            result = {"season": sn, "episode": en, "aired": aired}
            if air_date:
                result["air_date"] = air_date.isoformat()
            if u and u.episode_name:
                result["title"] = u.episode_name
            return result
    return None


def finish_tv_episode(user_id, show_id, season, episode):
    """Mark the exact episode watched via the canonical TV tracking path and
    promote the next valid unwatched episode into Continue Watching.

    Returns dict describing the outcome; never invents episodes.
    """
    from routes.tv_tracking import mark_episode_watched_core

    # Canonical watched state first (creates/maintains TVShowProgress,
    # TVEpisodeWatch, season progress, show completion gating).
    mark_episode_watched_core(user_id, show_id, season, episode)

    # The finished episode leaves Continue Watching.
    remove_item(user_id, "tv", show_id, season=season, episode=episode)

    # Promote the next valid unwatched episode, if any.
    next_ep = find_next_episode(user_id, show_id, after=(season, episode))
    if next_ep and next_ep.get("aired"):
        start_item(
            user_id, "tv", show_id,
            season=next_ep["season"], episode=next_ep["episode"],
            title=next_ep.get("title"),
        )
        # Prefer canonical show metadata for the hint fields.
        details = show_details(show_id)
        if details:
            from models import ContinueWatchingItem, db
            item = ContinueWatchingItem.query.filter_by(
                user_id=user_id, media_type="tv", tmdb_id=show_id,
                season=next_ep["season"], episode=next_ep["episode"],
            ).first()
            if item:
                item.title = (details.get("name") or item.title)
                if details.get("poster_path"):
                    item.poster_path = details["poster_path"][:500]
                db.session.commit()
        return {"finished": True, "next": {
            "season": next_ep["season"], "episode": next_ep["episode"]}}

    return {"finished": True, "next": None}


def mark_movie_finished(user_id, tmdb_id, title=None, poster_path=None):
    """Record canonical movie watched state and remove from Continue Watching.

    Uses the existing canonical watched-state path (Feature 01): a DiaryEntry
    watch event plus the derived viewed-state sync. No playback math.
    """
    from routes.diary import quick_log_movie_core

    result = quick_log_movie_core(user_id, tmdb_id, title=title,
                                  poster_path=poster_path)
    remove_item(user_id, "movie", tmdb_id)
    return result


# ── Canonical builder ────────────────────────────────────────────────────────

def _display_metadata_movie(item, resolve):
    """(title, poster) for a movie item; None details → stored hint or skip."""
    details = resolve(item.tmdb_id, True)
    if details:
        title = details.get("title") or item.title
        raw = details.get("poster_path")
        poster = (_POSTER_BASE + raw) if (
            raw and isinstance(raw, str) and raw.startswith("/")) else (
            raw or item.poster_path)
    else:
        title, poster = item.title, item.poster_path
    if not title:
        return None  # no canonical metadata and no hint → never render "Movie 603"
    return title, poster_url(poster)


def _display_metadata_tv(item, resolve):
    """(title, poster, season, episode, watch_url) for a TV item, or None.

    The stored episode is validated against TMDb season counts. A stale
    (correctable) position is repaired; an unvalidatable one is omitted.
    """
    details = resolve(item.tmdb_id, False)
    counts = season_episode_counts(details)
    season, episode = item.season, item.episode

    if counts and season is not None and episode is not None:
        in_range = (season in counts and 1 <= episode <= counts[season])
        if not in_range:
            if season in counts and counts[season] > 0 and episode > counts[season]:
                # Stored episode beyond the real season length: the user was
                # progressing — correct to the most recent valid episode.
                season, episode = season, counts[season]
            else:
                next_ep = find_next_episode(item.user_id, item.tmdb_id,
                                            counts=counts)
                if not next_ep:
                    return None
                season, episode = next_ep["season"], next_ep["episode"]
    elif not counts:
        # No authoritative season data → cannot validate → omit (no broken
        # URLs, no invented episodes).
        return None

    if details:
        title = details.get("name") or item.title
        raw = details.get("poster_path")
        poster = (_POSTER_BASE + raw) if (
            raw and isinstance(raw, str) and raw.startswith("/")) else (
            raw or item.poster_path)
    else:
        title, poster = item.title, item.poster_path
    if not title:
        return None
    return title, poster_url(poster), season, episode, f"/watch/tv/{item.tmdb_id}/{season}/{episode}"


def continue_watching_entries(user_id, movie_limit=12, tv_limit=6):
    """Canonical Continue Watching entries: started-but-not-finished items,
    most recently started first.

    Metadata resolution per card:
      1. canonical cached TMDb details when they resolve,
      2. the item's stored title/poster hint (captured at start),
      3. omitted entirely when neither exists — a raw TMDb id is never
         rendered as a title.

    TV positions are validated against authoritative TMDb season data.
    No playback state, no percentages, no telemetry of any kind.
    """
    from models import ContinueWatchingItem

    rows = (
        ContinueWatchingItem.query.filter_by(user_id=user_id)
        .order_by(ContinueWatchingItem.started_at.desc())
        .limit(movie_limit + tv_limit)
        .all()
    )
    if not rows:
        return []

    cache = {}

    def resolve(tmdb_id, is_movie):
        key = (tmdb_id, is_movie)
        if key not in cache:
            cache[key] = (movie_details if is_movie else show_details)(tmdb_id)
        return cache[key]

    entries = []
    for item in rows:
        try:
            if item.media_type == "movie":
                meta = _display_metadata_movie(item, resolve)
                if meta is None:
                    continue
                title, poster = meta
                entries.append({
                    "media_type": "movie",
                    "tmdb_id": item.tmdb_id,
                    "title": title,
                    "poster": poster,
                    "started_at": item.started_at.isoformat(),
                    "watch_url": f"/watch/movie/{item.tmdb_id}",
                    "label": "Continue Watching",
                })
            else:
                meta = _display_metadata_tv(item, resolve)
                if meta is None:
                    continue
                title, poster, season, episode, watch_url = meta
                entries.append({
                    "media_type": "tv",
                    "tmdb_id": item.tmdb_id,
                    "title": title,
                    "poster": poster,
                    "season": season,
                    "episode": episode,
                    "started_at": item.started_at.isoformat(),
                    "watch_url": watch_url,
                    "label": f"S{season}E{episode} · Continue Watching",
                })
        except Exception:
            logger.exception(
                "Continue Watching: failed to build entry for item %s", item.id)
            continue
    return entries
