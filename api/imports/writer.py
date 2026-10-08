"""Import-safe writers over the canonical FrameIQ write paths (Task F6).

The single most important rule in this module: **an importer never inserts a
row directly.** Both writers delegate to the same functions the UI and API use,
so every guarantee those paths provide applies to imported data too:

  * movies → ``DiaryEntry`` (the canonical movie history; F5 §7)
  * TV episodes → ``mark_episode_watched_core`` (F4's canonical episode write,
    which owns eligibility gating, counter sync, season completion and
    completion gating)

Direct ``TVEpisodeWatch(...)`` inserts would bypass F4's eligibility gate — that
is exactly how a future or special episode would become watched history and
inflate the canonical counters.

Idempotency
-----------
Both writers are keyed on *domain* identity rather than on remembering which
source rows were seen:

  * a movie is "already present" when a ``DiaryEntry`` exists for this user,
    this media, this watched date;
  * an episode is "already present" when a ``TVEpisodeWatch`` exists for this
    user, this show, this season/episode.

That needs no bookkeeping table, survives a re-import of the same file, and
cannot drift from the data it describes. It also means importing the same
film genuinely watched twice (two distinct dates) keeps both events — which is
correct: those are two real watch events, not duplicates.
"""
from datetime import date, datetime

from sqlalchemy import select

from api.user_view_state import (EpisodeNotAired, aired_positions_for_show,
                                 episode_eligibility, memoized_details_loader)
from models import DiaryEntry, TVEpisodeWatch, TVShowProgress, db


class ImportWriteResult:
    """Outcome of one write attempt."""

    def __init__(self, status, detail=None, episode_rejected=None):
        self.status = status  # 'imported' | 'already_present'
        self.detail = detail
        self.episode_rejected = episode_rejected


def existing_movie_watch_keys(user_id, media_ids):
    """``{(media_id, watched_date_iso)}`` the user already has.

    One query for the whole import (not one per row).
    """
    if not media_ids:
        return set()
    rows = db.session.execute(
        select(DiaryEntry.media_id, DiaryEntry.watched_date)
        .where(DiaryEntry.user_id == user_id,
               DiaryEntry.media_type == 'movie',
               DiaryEntry.media_id.in_(sorted(set(media_ids))))
    ).all()
    return {(media_id, watched.isoformat()) for media_id, watched in rows}


def watched_counts_by_media(user_id, media_ids):
    """``{media_id: count}`` — needed to derive ``is_rewatch`` correctly."""
    if not media_ids:
        return {}
    rows = db.session.execute(
        select(DiaryEntry.media_id, db.func.count(DiaryEntry.id))
        .where(DiaryEntry.user_id == user_id,
               DiaryEntry.media_type == 'movie',
               DiaryEntry.media_id.in_(sorted(set(media_ids))))
        .group_by(DiaryEntry.media_id)
    ).all()
    return {media_id: count for media_id, count in rows}


def write_movie_watch(user_id, media_item, watched_at, rating=None):
    """Create a movie watch event through the canonical ``DiaryEntry`` model.

    ``is_rewatch`` is DERIVED from how many watch events already exist for this
    user and media — the same rule ``routes/diary.py`` uses — rather than being
    trusted from the source. A source cannot prove a rewatch any better than we
    can, and inferring it keeps the counter consistent.
    """
    existing = existing_movie_watch_keys(user_id, [media_item.id])
    if (media_item.id, watched_at.isoformat()) in existing:
        return ImportWriteResult('already_present')

    prior = watched_counts_by_media(user_id, [media_item.id]).get(
        media_item.id, 0)

    db.session.add(DiaryEntry(
        user_id=user_id,
        media_id=media_item.id,
        media_type='movie',
        watched_date=watched_at,
        rating=rating,
        is_rewatch=prior > 0,
    ))
    return ImportWriteResult('imported')


def existing_episode_keys(user_id, show_ids):
    """``{(show_id, season, episode)}`` already watched — one query."""
    if not show_ids:
        return set()
    rows = db.session.execute(
        select(TVEpisodeWatch.show_id, TVEpisodeWatch.season_number,
               TVEpisodeWatch.episode_number)
        .where(TVEpisodeWatch.user_id == user_id,
               TVEpisodeWatch.show_id.in_(sorted(set(show_ids))))
    ).all()
    return {(show_id, season, episode)
            for show_id, season, episode in rows}


def write_episode_watch(user_id, show_id, season, episode, watched_at,
                        rating=None, episode_name=None, is_rewatch=None):
    """Delegate to F4's canonical episode write.

    Everything the UI gets, an import gets:

      * ``episode_eligibility`` refuses a future episode or an ineligible
        special, raising ``EpisodeNotAired`` before anything is written;
      * ``sync_tv_progress_counters`` and ``apply_completion_gating`` run, so
        Viewed and progress stay canonical;
      * metadata keys are only passed when the source actually has them, so an
        absent rating never clears an existing one.

    ``is_rewatch`` is forwarded only when the source states it; otherwise the
    canonical path's own default applies. ``watched_at`` is forwarded only when
    known, because a missing date must not overwrite a stored one.
    """
    from routes.tv_tracking import mark_episode_watched_core

    data = {}
    if watched_at is not None:
        data['watched_date'] = watched_at
    if rating is not None:
        data['rating'] = rating
    if episode_name:
        data['episode_name'] = episode_name
    if is_rewatch is not None:
        data['is_rewatch'] = bool(is_rewatch)

    mark_episode_watched_core(user_id, show_id, season, episode, data=data)
    return ImportWriteResult('imported')


def preflight_episode(show_id, season, episode, details_loader=None):
    """Ask F4's gate whether an episode is importable, without writing.

    Used by PREVIEW so the user sees "12 episodes are not aired yet" BEFORE
    confirming, rather than discovering it as a skipped row afterwards.
    """
    loader = details_loader or memoized_details_loader()
    aired = aired_positions_for_show(show_id, details_loader=loader)
    reason = episode_eligibility(show_id, season, episode, aired=aired,
                                 details_loader=loader)
    return reason


def ensure_tv_progress(user_id, show_id):
    """Make sure a tracked show exists for imported episodes.

    The canonical write path creates progress itself; this exists only so an
    imported-but-then-unmarked show keeps a progress row, matching what the UI
    does when a user starts tracking.
    """
    progress = db.session.execute(
        select(TVShowProgress).where(
            TVShowProgress.user_id == user_id,
            TVShowProgress.show_id == show_id)
    ).scalars().first()
    if progress is not None:
        return progress
    progress = TVShowProgress(user_id=user_id, show_id=show_id,
                              status='watching')
    db.session.add(progress)
    db.session.flush()
    return progress


__all__ = [
    'EpisodeNotAired', 'ImportWriteResult', 'date', 'datetime',
    'ensure_tv_progress', 'existing_episode_keys', 'existing_movie_watch_keys',
    'preflight_episode', 'watched_counts_by_media', 'write_episode_watch',
    'write_movie_watch',
]