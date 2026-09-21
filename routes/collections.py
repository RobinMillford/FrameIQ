"""User collection routes: watchlist, viewed — pages + add/remove/priority.

Legacy /wishlist and /add_to_wishlist//remove_from_wishlist routes are
kept as redirects to the canonical Watchlist equivalents (Wishlist was
consolidated into Watchlist).
"""
from flask import render_template, request, jsonify, redirect, url_for, flash
from flask_login import login_required, current_user
from sqlalchemy import select

from models import db, MediaItem, user_watchlist, user_viewed
from routes._main_bp import main
from routes.helpers import get_user_collection_ids
from utils.collections import get_or_create_media_item

_PRIORITY_LABELS = {'high': '🔥 High', 'medium': '📌 Medium', 'low': '💤 Low'}


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

    return render_template(
        template,
        **{list_key: items_with_priority},
        user_watchlist_ids=ids_by_key['watchlist'],
        user_viewed_ids=ids_by_key['viewed'],
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

    return render_template('viewed.html', viewed=viewed_items + tv_titles,
                           user_watchlist_ids=watchlist_ids,
                           user_viewed_ids=viewed_ids)


def _add_to_collection(table, media_id, media_type, label):
    """Shared add-to-collection logic for watchlist/viewed."""
    priority = request.args.get('priority', 'medium')
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


@main.route('/add_to_watchlist/<int:media_id>/<media_type>', methods=['GET'])
@login_required
def add_to_watchlist(media_id, media_type):
    """Add a movie or TV show to the user's watchlist"""
    return _add_to_collection(user_watchlist, media_id, media_type, 'watchlist')


@main.route('/add_to_wishlist/<int:media_id>/<media_type>', methods=['GET'])
@login_required
def add_to_wishlist(media_id, media_type):
    """Legacy route: the Wishlist was consolidated into the canonical
    Watchlist. Redirect to the equivalent Watchlist action, preserving
    the requested priority."""
    return redirect(url_for(
        'main.add_to_watchlist',
        media_id=media_id,
        media_type=media_type,
        priority=request.args.get('priority', 'medium')))


@main.route('/mark_as_viewed/<int:media_id>/<media_type>', methods=['GET'])
@login_required
def mark_as_viewed(media_id, media_type):
    """Mark a movie or TV show as viewed"""
    return _add_to_collection(user_viewed, media_id, media_type, 'viewing history')


@main.route('/remove_from_watchlist/<int:media_id>/<media_type>', methods=['GET'])
@login_required
def remove_from_watchlist(media_id, media_type):
    """Remove a movie or TV show from the user's watchlist"""
    return _remove_from_collection(
        user_watchlist, media_id, media_type, 'watchlist', 'main.watchlist')


@main.route('/remove_from_wishlist/<int:media_id>/<media_type>', methods=['GET'])
@login_required
def remove_from_wishlist(media_id, media_type):
    """Legacy route: the Wishlist was consolidated into the canonical
    Watchlist. Redirect to the equivalent Watchlist removal."""
    return redirect(url_for(
        'main.remove_from_watchlist', media_id=media_id, media_type=media_type))


@main.route('/remove_from_viewed/<int:media_id>/<media_type>', methods=['GET'])
@login_required
def remove_from_viewed(media_id, media_type):
    """Remove a movie or TV show from the user's viewing history"""
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
