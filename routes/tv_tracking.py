"""TV show tracking API: episode/season progress, status, bulk operations."""
import logging
from datetime import datetime

from flask import jsonify, request
from flask_login import current_user, login_required

from api.tmdb_client import cached_tmdb_request, fetch_tv_show_details
from api.tmdb.config import TMDB_API_KEY
from api.user_view_state import (
    aired_positions_for_show, apply_completion_gating,
    canonical_progress_map, canonical_tv_progress,
    get_or_create_tv_progress, mark_aired_positions_watched,
    mark_season_aired_watched, memoized_details_loader,
    season_aired_for_show, sync_tv_progress_counters, tv_aired_progress,
)
from models import (
    MediaItem, TVEpisodeWatch, TVShowProgress, UpcomingEpisode, db,
)
from routes._tv_bp import TMDB_BASE_URL, tv_tracking

# Register page + calendar routes on the shared blueprint so app.py's single
# register_blueprint() call picks up every endpoint.
from routes import tv_calendar as _calendar_routes  # noqa: F401,E402
from routes import tv_pages as _page_routes  # noqa: F401,E402

logger = logging.getLogger(__name__)


@tv_tracking.route('/api/tv/<int:show_id>/start-tracking', methods=['POST'])
@login_required
def start_tracking_show(show_id):
    """Start tracking a TV show"""
    try:
        # Check if already tracking
        existing = TVShowProgress.query.filter_by(
            user_id=current_user.id,
            show_id=show_id
        ).first()
        
        if existing:
            return jsonify({'error': 'Already tracking this show'}), 400
        
        # Fetch show details from TMDb (display metadata only)
        show = fetch_tv_show_details(show_id)
        
        # Create progress entry. Counters start at zero and are synced
        # from the canonical AIRED set (never TMDb number_of_episodes,
        # which counts unaired episodes as an aired denominator).
        progress = TVShowProgress(
            user_id=current_user.id,
            show_id=show_id,
            total_seasons=0,
            total_episodes=0,
            watched_seasons=0,
            watched_episodes=0,
            status='watching'
        )
        db.session.add(progress)
        db.session.flush()
        sync_tv_progress_counters(progress, current_user.id, show_id)

        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': 'Started tracking show',
            'progress': progress.to_dict()
        }), 201
        
    except Exception:
        db.session.rollback()
        logger.error("Unexpected error in tv_tracking", exc_info=True)
        return jsonify({'error': 'An unexpected error occurred'}), 500


@tv_tracking.route('/api/tv/<int:show_id>/aired-progress', methods=['GET'])
@login_required
def get_show_aired_progress(show_id):
    """User-scoped overall AIRED-episode progress for one show.

    Backs the live progress line on the TV detail page (view-state.js):
    recomputed per request so a newly aired episode immediately lowers a
    previously complete show. Never cached globally — personalized data.

    Also carries ``season_aired`` — the per-season AIRED episode counts from
    the same shared rules — so the season cards can compute "% Complete"
    against airing reality (future episodes never inflate a denominator).
    """
    try:
        from api.user_view_state import season_aired_for_show, tv_aired_progress
        progress = tv_aired_progress(current_user, [show_id]).get(show_id)
        season_aired = season_aired_for_show(show_id)
        return jsonify({
            'tv_progress': progress,
            'season_aired': {str(s): n for s, n in season_aired.items()},
        }), 200
    except Exception:
        logger.error("Unexpected error in tv_tracking", exc_info=True)
        return jsonify({'error': 'An unexpected error occurred'}), 500


@tv_tracking.route('/api/tv/<int:show_id>/progress', methods=['GET'])
@login_required
def get_show_progress(show_id):
    """Get user's progress for a TV show"""
    try:
        progress = TVShowProgress.query.filter_by(
            user_id=current_user.id,
            show_id=show_id
        ).first()
        
        if not progress:
            return jsonify({'progress': None}), 200
        
        # Publish the CANONICAL aired-reality payload (recomputed per
        # request) so this endpoint can never expose a stale stored
        # denominator. NOTE: unlike the pre-F1 version this does NOT
        # refresh ``total_episodes`` from TMDb ``number_of_episodes`` —
        # that clobbered the canonical aired count with a future-inflated
        # one.
        canonical = canonical_tv_progress(current_user, show_id)
        
        # Get watched episodes
        watched_episodes = TVEpisodeWatch.query.filter_by(
            user_id=current_user.id,
            show_id=show_id
        ).order_by(
            TVEpisodeWatch.season_number,
            TVEpisodeWatch.episode_number
        ).all()
        
        # Task F2: no canonical aired-evidence progress exists. A pre-F1
        # legacy row can still carry stale counters — never publish them
        # as progress. Blank the derived counter values so a legacy row
        # reads as "no progress" until canonical aired state exists (the
        # tracking row itself is kept: tracking is not watch state; the
        # episode ledger below still renders).
        if canonical is None:
            progress_dict = progress.to_dict()
            progress_dict['total_episodes'] = 0
            progress_dict['watched_episodes'] = 0
            progress_dict['total_seasons'] = 0
            progress_dict['watched_seasons'] = 0
            progress_dict['progress_percentage'] = 0
        else:
            progress_dict = progress.to_dict(canonical)

        return jsonify({
            'progress': progress_dict,
            'watched_episodes': [ep.to_dict() for ep in watched_episodes]
        }), 200
        
    except Exception:
        logger.error("Unexpected error in tv_tracking", exc_info=True)
        return jsonify({'error': 'An unexpected error occurred'}), 500


@tv_tracking.route('/api/tv/<int:show_id>/episode/<int:season>/<int:episode>/mark-watched', methods=['POST'])
@login_required
def mark_episode_watched(show_id, season, episode):
    """Mark an episode as watched"""
    try:
        data = request.get_json(silent=True) or {}
        progress = mark_episode_watched_core(
            current_user.id, show_id, season, episode, data)
        return jsonify({
            'success': True,
            'message': 'Episode marked as watched',
            'progress': progress.to_dict(
                canonical_tv_progress(current_user, show_id))
        }), 200
    except Exception:
        db.session.rollback()
        logger.error("Unexpected error in tv_tracking", exc_info=True)
        return jsonify({'error': 'An unexpected error occurred'}), 500


def mark_show_aired_watched_core(user_id, show_id):
    """Bulk "Mark as Viewed" for a TV show — state sync, not rewatch
    generation.

    Task F1: this is now a THIN DELEGATE to the canonical write core
    (``api.user_view_state.mark_aired_positions_watched``). It owns no
    episode-eligibility logic of its own — the aired set comes from
    ``aired_positions_for_show()`` inside the core, exactly as it does
    for Mark Season Watched and Mark All Watched.

    Preserved contract (all previously pinned by
    tests/test_tv_mark_as_viewed_sync.py):
      * existing watch records — ratings, notes, dates, rewatch rows —
        are preserved untouched; nothing is ever deleted;
      * repeated calls insert nothing and manufacture no rewatch entries;
      * no diary events are fabricated;
      * returns ``(progress, inserted_count, aired_count)`` and, when
        nothing has verifiably aired, a zero-state rather than an error.
    """
    from models import User

    user = db.session.get(User, user_id) if user_id else None
    if user is None:
        return TVShowProgress.query.filter_by(
            user_id=user_id, show_id=show_id).first(), 0, 0
    return mark_aired_positions_watched(user, show_id)


def unmark_show_watched_core(user_id, show_id):
    """Bulk "Unmark as Viewed" for a TV show — the inverse write path.

    Task F2 (spec §43, option A): unmarking a TV show is an explicit
    removal of its watched state, identical in kind to unmark-season and
    unmark-episode (both already delete rows, rewatches included, and
    recompute from canonical aired reality). This clears EVERY
    TVEpisodeWatch row for (user, show), re-syncs the TVShowProgress
    counters from the canonical aired set (⇒ watched 0), un-seals a
    'completed' status back to 'watching', and keeps the zero-progress
    row so the user's tracking state survives the unmark.

    Preserved contract:
      * one bounded DELETE for the user's own rows — user-scoped, nothing
        shared is touched; other users' rows are unreachable by filter;
      * no ratings/notes are orphaned (they live on the deleted rows and
        the deletion IS the documented unmark semantics);
      * no TMDb writes, no diary events, no rewatch manufacturing.

    Returns the (refreshed) TVShowProgress row, or ``None`` when the user
    was not tracking the show.
    """
    progress = TVShowProgress.query.filter_by(
        user_id=user_id, show_id=show_id).first()

    # Bounded, user-scoped removal of the episode ledger for this show.
    # Delete by column filter (NOT progress_id) so rows written before
    # the tracking row existed are cleared too.
    TVEpisodeWatch.query.filter_by(
        user_id=user_id, show_id=show_id).delete()

    if progress is not None:
        db.session.flush()
        sync_tv_progress_counters(progress, user_id, show_id)
        progress.watched_seasons = 0
        progress.last_watched = datetime.utcnow()
        # apply_completion_gating with zero watches: complete=False, and
        # the existing un-seal branch (total_episodes > 0) flips a sealed
        # 'completed' back to 'watching' — the exact F1 un-seal path.
        apply_completion_gating(progress, show_id)
    db.session.commit()
    return progress


def mark_episode_watched_core(user_id, show_id, season, episode, data=None):
    """Canonical episode-watched state (shared with Continue Watching finish).

    Task F1: the row write here stays explicit (a single episode carries
    user-supplied rating/notes/date/rewatch metadata that a bulk path must
    never invent), but every DERIVED value now comes from the canonical
    aired reality:

      * ``TVShowProgress`` counters — canonical aired denominators via
        ``sync_tv_progress_counters`` (never TMDb ``number_of_episodes``);
      * season completion — canonical aired season denominators;
      * completion gating — shared ``apply_completion_gating``.

    Idempotent per episode: re-marking updates the existing watch record.
    """
    logger.debug("Mark episode watched: show=%s S%sE%s user=%s", show_id, season, episode, user_id)

    data = data or {}

    progress = get_or_create_tv_progress(user_id, show_id)
    if progress.id is None:
        db.session.flush()

    existing = TVEpisodeWatch.query.filter_by(
        user_id=user_id,
        show_id=show_id,
        season_number=season,
        episode_number=episode
    ).first()

    if existing:
        existing.watched_date = datetime.strptime(data.get('watched_date', datetime.utcnow().strftime('%Y-%m-%d')), '%Y-%m-%d').date()
        existing.rating = data.get('rating')
        existing.notes = data.get('notes')
        existing.is_rewatch = data.get('is_rewatch', False)
    else:
        db.session.add(TVEpisodeWatch(
            user_id=user_id,
            show_id=show_id,
            progress_id=progress.id,
            season_number=season,
            episode_number=episode,
            episode_name=data.get('episode_name'),
            watched_date=datetime.strptime(data.get('watched_date', datetime.utcnow().strftime('%Y-%m-%d')), '%Y-%m-%d').date(),
            rating=data.get('rating'),
            notes=data.get('notes'),
            is_rewatch=data.get('is_rewatch', False)
        ))

    progress.last_watched = datetime.utcnow()
    db.session.flush()

    # Canonical counters + gating — identical math to the bulk paths.
    loader = memoized_details_loader()
    aired = aired_positions_for_show(show_id, details_loader=loader)
    sync_tv_progress_counters(progress, user_id, show_id, aired=aired)
    apply_completion_gating(progress, show_id, details_loader=loader)
    logger.debug(
        "Progress %s: %s/%s aired", show_id,
        progress.watched_episodes, progress.total_episodes)

    db.session.commit()
    return progress


@tv_tracking.route('/api/tv/<int:show_id>/season/<int:season>/mark-watched', methods=['POST'])
@login_required
def mark_season_watched(show_id, season):
    """Mark every AIRED episode of one season as watched (Task F1).

    Rewritten to delegate to the canonical season core
    (``mark_season_aired_watched`` → ``mark_aired_positions_watched``).
    It no longer fetches the TMDb season episode list, no longer filters
    by air date itself, and — critically — no longer DELETES existing
    ``TVEpisodeWatch`` rows before re-inserting them, which used to
    destroy ratings, notes, watched dates and rewatch history.

    Future episodes, specials and other seasons are excluded because the
    eligible set is the canonical aired set narrowed to this season.
    Idempotent: a repeat call inserts nothing.
    """
    try:
        logger.debug("Mark season watched: show=%s season=%s user=%s",
                     show_id, season, current_user.id)

        data = request.get_json(silent=True) or {}
        raw_date = data.get('watched_date')
        watched_date = None
        if raw_date:
            try:
                watched_date = datetime.strptime(
                    raw_date, '%Y-%m-%d').date()
            except ValueError:
                watched_date = None

        progress, inserted, aired = mark_season_aired_watched(
            current_user, show_id, season, watched_date=watched_date)

        if progress is None:
            # Nothing aired in this season (or no aired data at all):
            # a zero-state, not an error and not a fabricated write.
            return jsonify({
                'success': True,
                'message': f'Season {season} marked as watched',
                'marked_episodes': 0,
                'aired_episodes': 0,
                'progress': None,
            }), 200

        canonical = canonical_tv_progress(current_user, show_id)
        logger.debug("Season %s of show %s: inserted %s of %s aired",
                     season, show_id, inserted, aired)

        return jsonify({
            'success': True,
            'message': f'Season {season} marked as watched',
            'marked_episodes': inserted,
            'aired_episodes': aired,
            'progress': progress.to_dict(canonical),
        }), 200

    except Exception:
        db.session.rollback()
        logger.error("Unexpected error in tv_tracking", exc_info=True)
        return jsonify({'error': 'An unexpected error occurred'}), 500


@tv_tracking.route('/api/tv/my-shows', methods=['GET'])
@login_required
def get_my_tracked_shows():
    """Get all shows user is tracking"""
    try:
        status_filter = request.args.get('status')  # 'watching', 'completed', 'plan_to_watch', 'dropped'
        
        query = TVShowProgress.query.filter_by(user_id=current_user.id)
        
        if status_filter:
            query = query.filter_by(status=status_filter)
        
        shows = query.order_by(TVShowProgress.last_watched.desc()).all()
        
        # Task F2: every row is serialized through the canonical aired
        # payload (recomputed per request) — a pre-F1 legacy row can no
        # longer leak its stale counters through this endpoint.
        canonical_by_show = canonical_progress_map(
            current_user, [s.show_id for s in shows])

        return jsonify({
            'shows': [show.to_dict(
                # Zeroed canonical for shows with no aired-evidence
                # progress (tracked, not started): to_dict must never
                # fall back to the stored legacy counters.
                canonical_by_show.get(show.show_id)
                or {'watched': 0, 'aired': 0, 'percent': 0})
                      for show in shows],
            'total': len(shows)
        }), 200
        
    except Exception:
        logger.error("Unexpected error in tv_tracking", exc_info=True)
        return jsonify({'error': 'An unexpected error occurred'}), 500


@tv_tracking.route('/api/tv/<int:show_id>/update-status', methods=['POST'])
@login_required
def update_show_status(show_id):
    """Update show tracking status"""
    try:
        data = request.get_json()
        new_status = data.get('status')
        
        if new_status not in ['watching', 'completed', 'plan_to_watch', 'dropped', 'paused']:
            return jsonify({'error': 'Invalid status'}), 400
        
        progress = TVShowProgress.query.filter_by(
            user_id=current_user.id,
            show_id=show_id
        ).first()
        
        if not progress:
            return jsonify({'error': 'Show not being tracked'}), 404
        
        progress.status = new_status

        if new_status == 'completed':
            progress.completed_at = datetime.utcnow()
        elif progress.completed_at:
            progress.completed_at = None

        db.session.commit()
        
        # Task F2: serialize through the canonical aired payload — a
        # pre-F1 legacy row can no longer leak stale counters here.
        canonical = canonical_tv_progress(current_user, show_id) or {
            'watched': 0, 'aired': 0, 'percent': 0}
        return jsonify({
            'success': True,
            'progress': progress.to_dict(canonical)
        }), 200
        
    except Exception:
        db.session.rollback()
        logger.error("Unexpected error in tv_tracking", exc_info=True)
        return jsonify({'error': 'An unexpected error occurred'}), 500


@tv_tracking.route('/api/tv/<int:show_id>/next-episode')
@login_required
def get_next_episode(show_id):
    """First-class Next Episode for a tracked show.

    Returns the earliest unwatched, non-specials episode. Aired state comes
    from the synced UpcomingEpisode table — no extra TMDb request is made.
    """
    try:
        progress = TVShowProgress.query.filter_by(
            user_id=current_user.id, show_id=show_id
        ).first()

        if not progress:
            return jsonify({'tracked': False, 'next_episode': None}), 200

        if progress.status in ('completed', 'dropped'):
            canonical = canonical_tv_progress(current_user, show_id) or {
                'watched': 0, 'aired': 0, 'percent': 0}
            return jsonify({
                'tracked': True,
                'status': progress.status,
                'progress': {
                    'watched': canonical['watched'],
                    'total': canonical['aired'],
                    'percent': canonical['percent'],
                },
                'next_episode': None,
            }), 200

        next_ep = _compute_next_episode_cached(current_user.id, show_id)

        # Task F2: publish the CANONICAL aired-reality payload — never
        # the stored counters (a pre-F1 legacy row can still carry a
        # stale denominator). None (show not started / no aired evidence)
        # reads as zero progress.
        canonical = canonical_tv_progress(current_user, show_id) or {
            'watched': 0, 'aired': 0, 'percent': 0}
        return jsonify({
            'tracked': True,
            'status': progress.status,
            'progress': {
                'watched': canonical['watched'],
                'total': canonical['aired'],
                'percent': canonical['percent'],
            },
            'next_episode': next_ep,
        }), 200

    except Exception:
        logger.error("Unexpected error in get_next_episode", exc_info=True)
        return jsonify({'error': 'An unexpected error occurred'}), 500


@tv_tracking.route('/api/tv/unfinished-shows')
@login_required
def get_unfinished_shows():
    """Unfinished Shows shelf: watching/paused shows that are not complete.

    Sorted by most recently watched. Batched queries — one for the progress
    rows, one for all watched episodes, one batched MediaItem lookup for
    canonical display metadata, plus a bounded cached-TMDb fallback only for
    shows missing from the local cache (result persisted back to MediaItem).
    """
    try:
        limit = min(request.args.get('limit', 20, type=int), 50)
        shows = TVShowProgress.query.filter(
            TVShowProgress.user_id == current_user.id,
            TVShowProgress.status.in_(['watching', 'paused']),
        ).order_by(TVShowProgress.last_watched.desc()).limit(limit).all()

        if not shows:
            return jsonify({'shows': []}), 200

        show_ids = [s.show_id for s in shows]

        # Task F2: CANONICAL aired-reality progress for every tracked
        # show in the bounded shelf — one batched read (two SQL statements
        # total, shared cached-TMDb details), never the stored counters a
        # pre-F1 legacy row may still carry.
        canonical_by_show = canonical_progress_map(current_user, show_ids)

        # One query for every watched episode across all tracked shows.
        watched_rows = (
            db.session.query(
                TVEpisodeWatch.show_id,
                TVEpisodeWatch.season_number,
                TVEpisodeWatch.episode_number,
            )
            .filter(
                TVEpisodeWatch.user_id == current_user.id,
                TVEpisodeWatch.show_id.in_(show_ids),
                TVEpisodeWatch.is_rewatch == False,
            )
            .all()
        )
        watched_by_show = {}
        for sid, sn, en in watched_rows:
            watched_by_show.setdefault(sid, set()).add((sn, en))

        # Canonical display metadata: MediaItem is the local cache of "what
        # this show is" — one batched query for all tracked shows.
        # (UpcomingEpisode is episode air-state, NOT show metadata; a tracked
        # show can legitimately have no upcoming row.)
        media_rows = MediaItem.query.filter(
            MediaItem.tmdb_id.in_(show_ids),
            MediaItem.media_type == 'tv',
        ).all()
        info_by_show = {
            m.tmdb_id: {'name': m.title, 'poster_path': m.poster_path}
            for m in media_rows
        }

        # Missing local cache → bounded cached-TMDb fallback, persisted back
        # to MediaItem so later requests are served locally. Deduplicated per
        # request; a TMDb failure degrades that card only (name stays None →
        # frontend generic fallback) and never breaks the shelf.
        missing = [sid for sid in show_ids if sid not in info_by_show]
        if missing:
            _hydrate_missing_show_metadata(missing, info_by_show)

        result = []
        for s in shows:
            watched = watched_by_show.get(s.show_id, set())
            last_ep = _last_watched_position(watched)
            # Next episode is validated against authoritative TMDb season
            # data (cached) — never naive E+1 arithmetic, which can emit a
            # nonexistent episode / broken watch URL.
            next_ep = _compute_next_episode_cached(current_user.id, s.show_id)
            # An unaired episode must not be offered as playable.
            playable = next_ep is not None and next_ep.get('aired', True)
            info = info_by_show.get(s.show_id, {})
            # Canonical progress for the shelf card: watched/aired/percent
            # from aired evidence only. A show with zero watch rows (or
            # nothing verifiably aired) reads as zero progress — never a
            # stale stored percentage.
            canonical = canonical_by_show.get(s.show_id)
            if canonical:
                canon_watched, canon_total, canon_percent = (
                    canonical['watched'], canonical['aired'],
                    canonical['percent'])
            else:
                canon_watched, canon_total, canon_percent = 0, 0, 0
            result.append({
                'show_id': s.show_id,
                'name': info.get('name'),
                'poster_path': info.get('poster_path'),
                'status': s.status,
                'watched_episodes': canon_watched,
                'total_episodes': canon_total,
                'progress_percent': canon_percent,
                'last_watched': s.last_watched.isoformat() if s.last_watched else None,
                'last_episode': (
                    {'season': last_ep[0], 'episode': last_ep[1]}
                    if last_ep else None
                ),
                'next_episode': (
                    {'season': next_ep['season'], 'episode': next_ep['episode']}
                    if next_ep else None
                ),
                'watch_url': (
                    f"/watch/tv/{s.show_id}/{next_ep['season']}/{next_ep['episode']}"
                    if playable else None
                ),
            })

        return jsonify({'shows': result}), 200

    except Exception:
        logger.error("Unexpected error in get_unfinished_shows", exc_info=True)
        return jsonify({'error': 'An unexpected error occurred'}), 500


def _last_watched_position(watched_set):
    """Highest (season, episode) pair from a set of watched positions."""
    if not watched_set:
        return None
    return max(watched_set, key=lambda p: (p[0], p[1]))


def _tmdb_poster_path(poster):
    """Normalize a TMDb poster reference to the raw path MediaItem stores.

    fetch_tv_show_details returns a full image URL
    (https://image.tmdb.org/t/p/w500/xyz.jpg) while MediaItem.poster_path
    holds the bare '/xyz.jpg' path (convention used by routes/diary.py and
    routes/reviews.py). The frontend accepts either form.
    """
    if not poster:
        return None
    marker = '/t/p/'
    idx = poster.find(marker)
    if idx == -1:
        return poster if poster.startswith('/') else None
    tail = poster[idx + len(marker):]
    slash = tail.find('/')
    return tail[slash:] if slash != -1 else None


def _hydrate_missing_show_metadata(missing_show_ids, info_by_show):
    """Resolve display metadata for shows missing from the local MediaItem
    cache via the existing cached-TMDb helper, persisting the result back so
    subsequent requests are served locally.

    Bounded: deduplicated per request, capped by the endpoint's result limit
    (max 50). A TMDb failure degrades that single card (name stays None →
    frontend generic fallback) and never raises.
    """
    for sid in dict.fromkeys(missing_show_ids):
        try:
            show = fetch_tv_show_details(sid)
        except Exception:
            logger.warning(
                "Could not hydrate show %s metadata", sid, exc_info=True)
            continue
        name = show.get('name')
        if not name:
            continue
        poster = _tmdb_poster_path(show.get('poster_path'))
        try:
            existing = MediaItem.query.filter_by(
                tmdb_id=sid, media_type='tv').first()
            if existing is None:
                db.session.add(MediaItem(
                    tmdb_id=sid, media_type='tv',
                    title=name[:200], poster_path=poster,
                ))
            else:
                existing.title = name[:200]
                existing.poster_path = poster
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.warning(
                "Could not persist hydrated show %s", sid, exc_info=True)
        info_by_show[sid] = {'name': name, 'poster_path': poster}


def _compute_next_episode_cached(user_id, show_id):
    """Earliest unwatched episode using cached TMDb season episode counts.

    Uses the already-cached fetch_tv_show_details; per-episode air state is
    resolved from the UpcomingEpisode table (no per-episode TMDb requests).
    """
    watched = set(
        db.session.query(TVEpisodeWatch.season_number, TVEpisodeWatch.episode_number)
        .filter(
            TVEpisodeWatch.user_id == user_id,
            TVEpisodeWatch.show_id == show_id,
            TVEpisodeWatch.is_rewatch == False,
        )
        .all()
    )

    try:
        show = fetch_tv_show_details(show_id)
    except Exception:
        logger.warning("Could not load show %s for next-episode", show_id, exc_info=True)
        return None

    today = datetime.utcnow().date()
    # One query for all synced rows of this show: air date + title per episode.
    upcoming = {
        (u.season_number, u.episode_number): u
        for u in UpcomingEpisode.query.filter_by(show_id=show_id).all()
    }

    for season in sorted(show.get('seasons', []), key=lambda s: s.get('season_number') or 0):
        sn = season.get('season_number')
        if not sn or sn == 0:  # skip specials
            continue
        for en in range(1, (season.get('episode_count') or 0) + 1):
            if (sn, en) in watched:
                continue
            u = upcoming.get((sn, en))
            air_date = u.air_date if u else None
            aired = air_date is None or air_date <= today
            result = {
                'season': sn,
                'episode': en,
                'aired': aired,
            }
            if air_date:
                result['air_date'] = air_date.isoformat()
            if u and u.episode_name:
                result['title'] = u.episode_name
            return result
    return None


@tv_tracking.route('/api/tv/<int:show_id>/episode/<int:season_number>/<int:episode_number>/update-watch', methods=['POST'])
@login_required
def update_episode_watch(show_id, season_number, episode_number):
    """Update episode watch with rating and notes"""
    try:
        data = request.get_json()
        
        # Find or create episode watch
        watch = TVEpisodeWatch.query.filter_by(
            user_id=current_user.id,
            show_id=show_id,
            season_number=season_number,
            episode_number=episode_number
        ).first()
        
        if not watch:
            watch = TVEpisodeWatch(
                user_id=current_user.id,
                show_id=show_id,
                season_number=season_number,
                episode_number=episode_number,
                watched_at=datetime.utcnow()
            )
            db.session.add(watch)
        
        # Update fields
        watch.rating = data.get('rating')
        watch.notes = data.get('notes')
        watch.is_rewatch = data.get('is_rewatch', False)
        
        db.session.commit()
        
        return jsonify({'success': True})
    except Exception:
        db.session.rollback()
        logger.error("Unexpected error in tv_tracking", exc_info=True)
        return jsonify({'success': False, 'error': 'An unexpected error occurred'}), 500


@tv_tracking.route('/api/tv/<int:show_id>/watched-episodes')
@login_required
def get_watched_episodes(show_id):
    """Get all watched episodes for a show"""
    try:
        episodes = TVEpisodeWatch.query.filter_by(
            user_id=current_user.id,
            show_id=show_id
        ).all()

        return jsonify({
            'success': True,
            'episodes': [{
                'season_number': ep.season_number,
                'episode_number': ep.episode_number,
                'watched_date': ep.watched_date.isoformat() if ep.watched_date else None,
                'rating': ep.rating,
                'notes': ep.notes,
                'is_rewatch': ep.is_rewatch
            } for ep in episodes]
        })
    except Exception as e:
        logger.error("Error in get_watched_episodes: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': 'An unexpected error occurred'}), 500


@tv_tracking.route('/api/tv/<int:show_id>/season/<int:season_number>/unmark-watched', methods=['POST'])
@login_required
def unmark_season_watched(show_id, season_number):
    """Unmark all episodes in a season as unwatched.

    Deletes the season's watch rows (rewatches included — an unmark is an
    explicit removal) and recomputes the show counters from the canonical
    AIRED state, so the published denominator never reverts to a TMDb
    count and rewatch rows of OTHER seasons can never inflate the count.
    """
    try:
        TVEpisodeWatch.query.filter_by(
            user_id=current_user.id,
            show_id=show_id,
            season_number=season_number
        ).delete()

        progress = TVShowProgress.query.filter_by(
            user_id=current_user.id, show_id=show_id
        ).first()
        if progress:
            db.session.flush()
            sync_tv_progress_counters(progress, current_user.id, show_id)
            update_season_progress(progress, show_id)

        db.session.commit()
        return jsonify({
            'success': True,
            'progress': progress.to_dict() if progress else None,
        })
    except Exception:
        db.session.rollback()
        logger.error("Unexpected error in tv_tracking", exc_info=True)
        return jsonify({'success': False, 'error': 'An unexpected error occurred'}), 500


@tv_tracking.route('/api/tv/<int:show_id>/episode/<int:season_number>/<int:episode_number>/unmark-watched', methods=['POST'])
@login_required
def unmark_single_episode(show_id, season_number, episode_number):
    """Unmark a single episode as unwatched (new version).

    Deletes the episode's watch rows (rewatches included) and recomputes
    the show counters from the canonical AIRED state — the deleted rows
    and any surviving rewatch rows can no longer skew the count.
    """
    try:
        TVEpisodeWatch.query.filter_by(
            user_id=current_user.id,
            show_id=show_id,
            season_number=season_number,
            episode_number=episode_number
        ).delete()

        progress = TVShowProgress.query.filter_by(
            user_id=current_user.id, show_id=show_id
        ).first()
        if progress:
            db.session.flush()
            sync_tv_progress_counters(progress, current_user.id, show_id)
            update_season_progress(progress, show_id)

        db.session.commit()
        return jsonify({
            'success': True,
            'progress': progress.to_dict() if progress else None,
        })
    except Exception:
        db.session.rollback()
        logger.error("Unexpected error in tv_tracking", exc_info=True)
        return jsonify({'success': False, 'error': 'An unexpected error occurred'}), 500


@tv_tracking.route('/api/tv/<int:show_id>/mark-all-watched', methods=['POST'])
@login_required
def mark_all_watched(show_id):
    """Mark every AIRED episode in all seasons as watched (Task F1).

    Delegates to the canonical bulk core (``mark_aired_positions_watched``
    → ``aired_positions_for_show``): one cached details resolution, ONE
    existing-watch SELECT, set difference in Python, ONE bulk INSERT.
    The old per-episode ``.first()`` loop (N+1) and its TMDb season
    fetches are gone; future episodes, specials and rewatch inflation are
    excluded by the shared aired rule, and existing rows (ratings, notes,
    dates, rewatches) are never deleted or duplicated.
    """
    try:
        progress, inserted, aired = mark_aired_positions_watched(
            current_user, show_id)

        canonical = canonical_tv_progress(current_user, show_id)
        return jsonify({
            'success': True,
            'message': 'Series completed!',
            'marked_episodes': inserted,
            'aired_episodes': aired,
            'progress': progress.to_dict(canonical) if progress else None,
        }), 200
    except Exception:
        db.session.rollback()
        logger.error("Unexpected error in tv_tracking", exc_info=True)
        return jsonify({'success': False, 'error': 'An unexpected error occurred'}), 500


def update_season_progress(progress, show_id):
    """Recompute ``watched_seasons`` from the canonical AIRED season map.

    Season denominators come from ``season_aired_for_show()`` — a direct
    projection of ``aired_positions_for_show()`` — never from TMDb's raw
    ``episode_count`` (which counts unaired episodes). A season becomes
    complete only when every AIRED episode has a canonical first-watch
    row; a season with nothing aired is never complete.
    """
    try:
        aired_seasons = season_aired_for_show(show_id)
        if not aired_seasons:
            return

        watched = {}
        for season, episode in (
                db.session.query(
                    TVEpisodeWatch.season_number,
                    TVEpisodeWatch.episode_number)
                .filter(
                    TVEpisodeWatch.user_id == progress.user_id,
                    TVEpisodeWatch.show_id == show_id,
                    TVEpisodeWatch.is_rewatch == False,  # noqa: E712
                )
                .all()):
            watched[season] = max(watched.get(season, 0), episode)

        progress.watched_seasons = sum(
            1 for season, count in aired_seasons.items()
            if watched.get(season, 0) >= count)
    except Exception as e:
        logger.warning("Error updating season progress: %s", e)
