"""User collection routes: watchlist, viewed — pages + add/remove/priority.

Legacy /wishlist and /add_to_wishlist//remove_from_wishlist routes are
kept as redirects to the canonical Watchlist equivalents (Wishlist was
consolidated into Watchlist).
"""
import logging

from flask import (abort, flash, jsonify, redirect, render_template, request,
                   url_for)
from flask_login import login_required, current_user
from sqlalchemy import select

from extensions import limiter
from models import db, MediaItem, user_watchlist, user_viewed
from routes._main_bp import main
from routes.helpers import get_user_collection_ids
from utils.collections import (canonical_tv_viewed_keys,
                               get_or_create_media_item)

logger = logging.getLogger(__name__)

_PRIORITY_LABELS = {'high': '🔥 High', 'medium': '📌 Medium', 'low': '💤 Low'}

# Task F4 — media_type is a PATH SEGMENT, so it reaches a code branch
# (`if media_type == 'tv'`) that decides whether canonical TV state is
# synchronised. An unvalidated value therefore selects code paths and
# writes a user_viewed row with an arbitrary type. Only the two types the
# application actually models are accepted.
VALID_MEDIA_TYPES = frozenset({'movie', 'tv'})

# One budget for the collection mutations. `routes/smart_lists.py` uses
# 30/min for item writes; bunching a show or a movie is a handful of
# clicks per person, so this matches the established policy rather than
# inventing a stricter one that would make normal use annoying.
COLLECTION_WRITE_LIMIT = "30 per minute"


def _valid_media_type(media_type):
    """True when the path segment is a media type this app models.

    Every caller turns False into ``abort(404)`` — the same answer
    routes/main.py gives for a resource that does not exist. A
    flash-and-redirect would answer 200/302 and read as a successful write.
    """
    return media_type in VALID_MEDIA_TYPES


def _collection_page(table, template, list_key):
    """Render a prioritized collection page (watchlist)."""
    stmt = select(
        table.c.media_id,
        table.c.media_type,
        table.c.date_added,
        table.c.priority,
    ).where(
        table.c.user_id == current_user.id
    ).order_by(table.c.date_added.desc())

    rows = db.session.execute(stmt).all()

    # Batch-load all MediaItems in one IN query instead of one SELECT per row.
    media_by_id = {}
    if rows:
        media_ids = [row.media_id for row in rows]
        media_items = MediaItem.query.filter(MediaItem.id.in_(media_ids)).all()
        media_by_id = {m.id: m for m in media_items}

    items_with_priority = []
    for row in rows:
        media_item = media_by_id.get(row.media_id)
        if media_item:
            items_with_priority.append({
                'item': media_item,
                'priority': row.priority or 'medium',
                'date_added': row.date_added,
            })

    watchlist_ids, viewed_ids = get_user_collection_ids(current_user)
    # The page's own collection ids must reflect the queried rows above
    own_ids = {(i['item'].tmdb_id, i['item'].media_type) for i in items_with_priority}
    ids_by_key = {
        'watchlist': watchlist_ids, 'viewed': viewed_ids,
    }
    ids_by_key[list_key] = own_ids

    # Task F4: canonical TV Viewed for the page's TV cards (one batched
    # call — see utils.collections.canonical_tv_viewed_keys). Movies keep
    # the user_viewed mirror, which is canonical for them.
    tv_ids = [i['item'].tmdb_id for i in items_with_priority
              if i['item'].media_type == 'tv']

    return render_template(
        template,
        **{list_key: items_with_priority},
        user_watchlist_ids=ids_by_key['watchlist'],
        user_viewed_ids=ids_by_key['viewed'],
        user_tv_viewed_ids=canonical_tv_viewed_keys(current_user, tv_ids),
    )


@main.route('/watchlist')
@login_required
def watchlist():
    """Display user's watchlist with priorities"""
    return _collection_page(user_watchlist, 'watchlist.html', 'watchlist')


@main.route('/wishlist')
@login_required
def wishlist():
    """Legacy bookmark compatibility: /wishlist was consolidated into the
    canonical Watchlist (Wishlist→Watchlist consolidation)."""
    flash('Wishlist has been merged into your Watchlist.')
    return redirect(url_for('main.watchlist'))


@main.route('/viewed')
@login_required
def viewed():
    """Display user's viewing history.

    This surface is a WATCHED-TITLE COLLECTION (not an event history —
    the chronological journal is the Diary). Its movie half comes from
    the canonical ``user_viewed`` state; its TV half is derived at read
    time from ``TVEpisodeWatch`` — distinct shows with >= 1 watched
    episode (Phases 1/4 semantics: titles, not episodes; tracking
    without episode data contributes nothing). This is why TV watches
    previously never appeared here: ``user_viewed`` only receives
    movie quick-log/diary writes (routes/diary.py), never TV writes.
    """
    from api.tv_watch_titles import tv_watch_titles

    def _hydrate(missing_show_ids):
        # Reuse the existing cached-TMDb hydrator (network only for
        # shows missing from the local MediaItem cache; results are
        # persisted so later requests are served locally).
        from routes.tv_tracking import _hydrate_missing_show_metadata

        _hydrate_missing_show_metadata(missing_show_ids, {})

    viewed_items = list(current_user.viewed_media)
    tv_titles = tv_watch_titles(current_user.id, hydrate=_hydrate)

    watchlist_ids, viewed_ids = \
        get_user_collection_ids(current_user)

    # Task F4: the TV half of the collection badges comes from the canonical
    # model, not from the user_viewed mirror — one batched call for the whole
    # page, so a card never becomes an N+1. Movies keep the mirror, which is
    # canonical for them.
    tv_ids = [t["tmdb_id"] for t in tv_titles if t.get("media_type") == "tv"]
    tv_viewed_keys = canonical_tv_viewed_keys(current_user, tv_ids)

    return render_template('viewed.html', viewed=viewed_items + tv_titles,
                           user_watchlist_ids=watchlist_ids,
                           user_viewed_ids=viewed_ids,
                           user_tv_viewed_ids=tv_viewed_keys)


def _add_to_collection(table, media_id, media_type, label):
    """Shared add-to-collection logic for watchlist/viewed.

    Task F4: ``priority`` is read from the form body first and the query
    string second. The controls are POST forms now, so the value travels
    as a hidden field; the query-string fallback keeps the legacy
    wishlist redirect working, which forwards ``priority`` through
    ``url_for`` rather than re-rendering a form.
    """
    priority = request.form.get('priority') or request.args.get(
        'priority', 'medium')
    if priority not in ['high', 'medium', 'low']:
        priority = 'medium'

    media_item = get_or_create_media_item(media_id, media_type)
    if not media_item:
        flash('Could not find that item!')
        return redirect(request.referrer or url_for('main.index'))

    exists = db.session.execute(
        select(table.c.user_id).where(
            table.c.user_id == current_user.id,
            table.c.media_id == media_item.id,
            table.c.media_type == media_type,
        )
    ).fetchone()

    if table is user_viewed:
        added_msg = f'Marked {media_item.title} as viewed!'
        exists_msg = f'{media_item.title} is already marked as viewed!'
    else:
        added_msg = (f'Added {media_item.title} to your {label} '
                     f'with {_PRIORITY_LABELS.get(priority)} priority!')
        exists_msg = f'{media_item.title} is already in your {label}!'

    if exists:
        flash(exists_msg)
    else:
        values = dict(
            user_id=current_user.id,
            media_id=media_item.id,
            media_type=media_type,
        )
        if table is not user_viewed:
            values['priority'] = priority
        db.session.execute(table.insert().values(**values))
        db.session.commit()
        flash(added_msg)

    return redirect(request.referrer or url_for('main.index'))


def _remove_from_collection(table, media_id, media_type, label, fallback):
    """Shared remove-from-collection logic."""
    media_item = MediaItem.query.filter_by(
        tmdb_id=media_id, media_type=media_type).first()
    if media_item:
        result = db.session.execute(
            select(table.c.user_id).where(
                table.c.user_id == current_user.id,
                table.c.media_id == media_item.id,
                table.c.media_type == media_type,
            )
        ).fetchone()
        if result:
            db.session.execute(table.delete().where(
                table.c.user_id == current_user.id,
                table.c.media_id == media_item.id,
                table.c.media_type == media_type,
            ))
            db.session.commit()
            flash(f'Removed {media_item.title} from your {label}!')
        else:
            flash(f'Item not found in your {label}!')
    else:
        flash('Item not found!')

    return redirect(request.referrer or url_for(fallback))


@main.route('/add_to_watchlist/<int:media_id>/<media_type>', methods=['POST'])
@login_required
@limiter.limit(COLLECTION_WRITE_LIMIT)
def add_to_watchlist(media_id, media_type):
    """Add a movie or TV show to the user's watchlist.

    Task F4: POST, like the other collection mutations — this is reached
    from the same TV surfaces and had the identical unprotected-GET write.
    """
    if not _valid_media_type(media_type):
        logger.info("Rejected add_to_watchlist for unknown media_type=%r",
                    media_type)
        abort(404)
    return _add_to_collection(user_watchlist, media_id, media_type, 'watchlist')


@main.route('/add_to_wishlist/<int:media_id>/<media_type>', methods=['POST'])
@login_required
def add_to_wishlist(media_id, media_type):
    """Legacy route: the Wishlist was consolidated into the canonical
    Watchlist. Redirect to the equivalent Watchlist action, preserving
    the requested priority.

    Task F4 — 307, not 302. Both endpoints are POST-only now, and a 302
    tells the browser to re-issue the follow-up as GET, which would land on
    405 (and would have silently dropped the CSRF token). 307 Temporary
    Redirect preserves the method and the body, so the request that
    actually performs the write is the same authenticated, CSRF-protected
    POST the caller made. The priority is mirrored into the query string
    as well so the hop is still correct if it is ever followed manually.
    """
    return redirect(url_for(
        'main.add_to_watchlist',
        media_id=media_id,
        media_type=media_type,
        priority=request.values.get('priority', 'medium')), code=307)


@main.route('/mark_as_viewed/<int:media_id>/<media_type>', methods=['POST'])
@login_required
@limiter.limit(COLLECTION_WRITE_LIMIT)
def mark_as_viewed(media_id, media_type):
    """Mark a movie or TV show as viewed.

    For TV (Task D) this is STATE SYNCHRONIZATION: alongside the canonical
    ``user_viewed`` entry, every currently AIRED valid episode becomes a
    TVEpisodeWatch row via the bounded bulk helper — so the season cards
    and overall progress line agree with the hero's Viewed state. Future
    episodes are never manufactured, and repeated clicks are idempotent
    (no rewatch generation, no diary entries).

    Task F4 — this was a GET. Two problems with that: a state-changing
    GET is not cacheable, prefetchable or link-safe, and Flask-WTF's
    CSRFProtect only guards unsafe methods, so the write was reachable
    cross-site from any <img src>. It is now POST, which brings it under
    the app's existing CSRF protection with no new CSRF implementation,
    and ``media_type`` is validated against the modelled types.
    """
    if not _valid_media_type(media_type):
        logger.info("Rejected mark_as_viewed for unknown media_type=%r",
                    media_type)
        abort(404)

    if media_type == 'tv':
        try:
            from routes.tv_tracking import mark_show_aired_watched_core
            mark_show_aired_watched_core(current_user.id, media_id)
        except Exception:
            db.session.rollback()
            logger.error("TV bulk mark-as-viewed failed for show %s",
                         media_id, exc_info=True)
            flash('Could not mark that show as viewed!')
            return redirect(request.referrer or url_for('main.index'))
    return _add_to_collection(user_viewed, media_id, media_type, 'viewing history')


@main.route('/remove_from_watchlist/<int:media_id>/<media_type>', methods=['POST'])
@login_required
@limiter.limit(COLLECTION_WRITE_LIMIT)
def remove_from_watchlist(media_id, media_type):
    """Remove a movie or TV show from the user's watchlist.

    Task F4: POST + validated media_type, as above.
    """
    if not _valid_media_type(media_type):
        logger.info("Rejected remove_from_watchlist for unknown media_type=%r",
                    media_type)
        abort(404)
    return _remove_from_collection(
        user_watchlist, media_id, media_type, 'watchlist', 'main.watchlist')


@main.route('/remove_from_wishlist/<int:media_id>/<media_type>', methods=['POST'])
@login_required
def remove_from_wishlist(media_id, media_type):
    """Legacy route: the Wishlist was consolidated into the canonical
    Watchlist. Redirect to the equivalent Watchlist removal.

    Task F4 — POST, and 307 rather than 302 so the destructive write keeps
    its method and CSRF token across the redirect (a 302 would turn it into
    an unauthenticated-method GET, which the POST-only destination rejects).
    """
    return redirect(url_for(
        'main.remove_from_watchlist', media_id=media_id, media_type=media_type),
        code=307)


@main.route('/remove_from_viewed/<int:media_id>/<media_type>', methods=['POST'])
@login_required
@limiter.limit(COLLECTION_WRITE_LIMIT)
def remove_from_viewed(media_id, media_type):
    """Remove a movie or TV show from the user's viewing history.

    Task F2 — SYMMETRIC UNMARK (spec §43, option A): for TV this clears
    the show's canonical watched state (every TVEpisodeWatch row, rewatches
    included — the same explicit-removal contract as unmark-season and
    unmark-episode) before removing the compatibility ``user_viewed`` row,
    so Viewed OFF can never coexist with 100% progress. No duplicate-row
    safety valve: no legitimate duplicate can exist (unique identity), so
    a leftover would mean state was already cleared. Movies keep their
    exact semantics (junction row only; DiaryEntry events are history and
    are never touched).

    Task F4 — POST, for the same reason as ``mark_as_viewed``: this is a
    destructive write (for TV it deletes the whole episode ledger) and a
    GET would perform it without CSRF protection.
    """
    if not _valid_media_type(media_type):
        logger.info("Rejected remove_from_viewed for unknown media_type=%r",
                    media_type)
        abort(404)

    if media_type == 'tv':
        try:
            from routes.tv_tracking import unmark_show_watched_core
            unmark_show_watched_core(current_user.id, media_id)
        except Exception:
            db.session.rollback()
            logger.error("TV unmark-as-viewed failed for show %s",
                         media_id, exc_info=True)
            flash('Could not remove that show from your viewing history!')
            return redirect(request.referrer or url_for('main.index'))
    return _remove_from_collection(
        user_viewed, media_id, media_type, 'viewing history', 'main.viewed')


@main.route('/api/update_priority/<list_type>/<int:media_id>/<media_type>',
            methods=['POST'])
@login_required
def update_priority(list_type, media_id, media_type):
    """Update priority for a watchlist item"""
    from sqlalchemy import update as sql_update

    priority = request.json.get('priority')
    if priority not in ['high', 'medium', 'low']:
        return jsonify({'success': False, 'error': 'Invalid priority'}), 400

    media_item = MediaItem.query.filter_by(
        tmdb_id=media_id, media_type=media_type).first()
    if not media_item:
        return jsonify({'success': False, 'error': 'Media item not found'}), 404

    if list_type not in ('watchlist',):
        return jsonify({'success': False, 'error': 'Invalid list type'}), 400

    # Determine which table to update
    table = user_watchlist

    result = db.session.execute(
        sql_update(table).where(
            table.c.user_id == current_user.id,
            table.c.media_id == media_item.id,
            table.c.media_type == media_type,
        ).values(priority=priority))
    db.session.commit()

    if result.rowcount == 0:
        return jsonify({'success': False, 'error': 'Item not found in list'}), 404

    return jsonify({'success': True, 'priority': priority})
