"""
Lists API Routes
Handles user-created custom lists of movies/TV shows
"""
import logging

from flask import Blueprint, request, jsonify, render_template
from flask_login import login_required, current_user
from models import db, User, UserList, UserListItem
from models.lists import (prefetch_list_data, ListLike, ListComment,
                          ListAnalytics, ListView)
from models.associations import user_viewed, user_watchlist
from models.tv import TVShowProgress
from sqlalchemy.orm import joinedload
from sqlalchemy.exc import IntegrityError
from datetime import datetime
import re
from utils.collections import get_or_create_media_item

logger = logging.getLogger(__name__)

lists = Blueprint('lists', __name__)


def generate_slug(title, list_id=None):
    """Generate a URL-friendly slug from list title"""
    # Convert to lowercase and replace spaces/special chars with hyphens
    slug = re.sub(r'[^\w\s-]', '', title.lower())
    slug = re.sub(r'[-\s]+', '-', slug)
    slug = slug.strip('-')

    # Add list ID if provided to ensure uniqueness
    if list_id:
        slug = f"{slug}-{list_id}"

    return slug


@lists.route('/api/lists', methods=['GET'])
def get_public_lists():
    """Get all public lists"""
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 20, type=int), 100)

    # Query public lists
    lists_query = UserList.query.filter_by(is_public=True).order_by(UserList.created_at.desc())

    # Paginate
    pagination = lists_query.paginate(page=page, per_page=per_page, error_out=False)

    prefetch_list_data(pagination.items)

    return jsonify({
        'lists': [user_list.to_dict() for user_list in pagination.items],
        'total': pagination.total,
        'pages': pagination.pages,
        'current_page': page,
        'has_next': pagination.has_next,
        'has_prev': pagination.has_prev
    }), 200


@lists.route('/api/users/<int:user_id>/lists', methods=['GET'])
def get_user_lists(user_id):
    """Get lists for a specific user"""
    user = User.query.get_or_404(user_id)

    # If viewing own lists, show all. Otherwise, only public
    if current_user.is_authenticated and current_user.id == user_id:
        user_lists = user.lists.order_by(UserList.created_at.desc()).all()
    else:
        user_lists = user.lists.filter_by(is_public=True).order_by(UserList.created_at.desc()).all()

    prefetch_list_data(user_lists)

    return jsonify({
        'lists': [user_list.to_dict() for user_list in user_lists],
        'count': len(user_lists)
    }), 200


@lists.route('/api/lists/<int:list_id>', methods=['GET'])
def get_list_details(list_id):
    """Get details of a specific list including all items.

    Lists V2: additive per-viewer watched-state + liked-state enrichment
    (three bounded batch queries) and paginated comments. No N+1: one
    viewed-join scan, one watchlist scan, one TV-progress scan per request
    regardless of item count.
    """
    user_list = UserList.query.get_or_404(list_id)

    # Check permissions
    if not user_list.is_public and (not current_user.is_authenticated or current_user.id != user_list.user_id):
        return jsonify({'error': 'This list is private'}), 403

    # Get all items in the list with their media details (eager-loaded to
    # avoid one lazy query per item in the template)
    items = user_list.items.options(
        joinedload(UserListItem.media)
    ).order_by(UserListItem.position, UserListItem.added_at).all()

    list_data = user_list.to_dict()
    list_data['items'] = [item.to_dict() for item in items]

    _enrich_items_with_watched_state(list_data['items'])
    if current_user.is_authenticated:
        list_data['liked_by_me'] = db.session.query(ListLike.id).filter_by(
            user_id=current_user.id, list_id=user_list.id).first() is not None

    list_data['comments'] = _serialize_comments_page(user_list)
    list_data['comments_page'] = 1
    list_data['comments_pages'] = _comments_pages(user_list)

    return jsonify(list_data), 200


@lists.route('/api/lists/create', methods=['POST'])
@login_required
def create_list():
    """Create a new list"""
    data = request.get_json()

    if not data or not data.get('title'):
        return jsonify({'error': 'List title is required'}), 400

    # Lists V2: optional ranked/unranked mode (default preserves the
    # pre-V2 unranked UX).
    list_type = data.get('list_type', UserList.TYPE_UNRANKED)
    if list_type not in (UserList.TYPE_RANKED, UserList.TYPE_UNRANKED):
        return jsonify({'error': "list_type must be 'ranked' or 'unranked'"}), 400

    try:
        new_list = UserList(
            user_id=current_user.id,
            title=data['title'],
            description=data.get('description', ''),
            is_public=data.get('is_public', True),
            list_type=list_type,
        )

        db.session.add(new_list)
        db.session.flush()  # Get the ID before committing

        # Generate and set slug (Week 2)
        new_list.slug = generate_slug(new_list.title, new_list.id)

        db.session.commit()

        return jsonify({
            'message': 'List created successfully',
            'list': new_list.to_dict()
        }), 201

    except Exception:
        db.session.rollback()
        logger.error("List creation error", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500


@lists.route('/api/lists/<int:list_id>/update', methods=['PUT'])
@login_required
def update_list(list_id):
    """Update list details"""
    user_list = UserList.query.get_or_404(list_id)

    # Check ownership
    if user_list.user_id != current_user.id:
        return jsonify({'error': 'You can only edit your own lists'}), 403

    data = request.get_json()
    if not data:
        return jsonify({'error': 'JSON body required'}), 400

    try:
        if 'title' in data:
            user_list.title = data['title']
        if 'description' in data:
            user_list.description = data['description']
        if 'is_public' in data:
            user_list.is_public = data['is_public']

        user_list.updated_at = datetime.utcnow()
        db.session.commit()

        return jsonify({
            'message': 'List updated successfully',
            'list': user_list.to_dict()
        }), 200

    except Exception:
        db.session.rollback()
        logger.error("Failed to update list", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500


@lists.route('/api/lists/<int:list_id>/delete', methods=['DELETE'])
@login_required
def delete_list(list_id):
    """Delete a list"""
    user_list = UserList.query.get_or_404(list_id)

    # Check ownership
    if user_list.user_id != current_user.id:
        return jsonify({'error': 'You can only delete your own lists'}), 403

    try:
        db.session.delete(user_list)
        db.session.commit()

        return jsonify({'message': 'List deleted successfully'}), 200

    except Exception:
        db.session.rollback()
        logger.error("Failed to delete list", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500


@lists.route('/api/lists/<int:list_id>/add', methods=['POST'])
@login_required
def add_to_list(list_id):
    """Add a media item to a list"""
    user_list = UserList.query.get_or_404(list_id)

    # Check ownership
    if user_list.user_id != current_user.id:
        return jsonify({'error': 'You can only add items to your own lists'}), 403

    data = request.get_json()
    media_id = data.get('media_id')
    media_type = data.get('media_type')

    if not media_id or not media_type:
        return jsonify({'error': 'media_id and media_type are required'}), 400

    try:
        # Check if media item exists in our database, if not create it
        media_item = get_or_create_media_item(media_id, media_type)
        if not media_item:
            return jsonify({'error': 'Media item not found'}), 404

        # Get the next position
        max_position = db.session.query(db.func.max(UserListItem.position)).filter_by(list_id=list_id).scalar()
        next_position = (max_position or 0) + 1

        # Add to list
        list_item = UserListItem(
            list_id=list_id,
            media_id=media_item.id,
            media_type=media_type,
            position=next_position,
            note=data.get('note', '')
        )

        db.session.add(list_item)
        user_list.updated_at = datetime.utcnow()
        db.session.commit()

        return jsonify({
            'message': f'Added {media_item.title} to list',
            'item': list_item.to_dict()
        }), 201

    except IntegrityError:
        db.session.rollback()
        return jsonify({'error': 'This item is already in the list'}), 400
    except Exception:
        db.session.rollback()
        logger.error("Failed to add item to list", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500


@lists.route('/api/lists/<int:list_id>/remove/<int:item_id>', methods=['DELETE'])
@login_required
def remove_from_list(list_id, item_id):
    """Remove an item from a list"""
    user_list = UserList.query.get_or_404(list_id)

    # Check ownership
    if user_list.user_id != current_user.id:
        return jsonify({'error': 'You can only remove items from your own lists'}), 403

    list_item = UserListItem.query.get_or_404(item_id)

    # Verify item belongs to this list
    if list_item.list_id != list_id:
        return jsonify({'error': 'Item not found in this list'}), 404

    try:
        db.session.delete(list_item)
        user_list.updated_at = datetime.utcnow()
        db.session.commit()

        return jsonify({'message': 'Item removed from list'}), 200

    except Exception:
        db.session.rollback()
        logger.error("Failed to remove item from list", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500


# ============================================================================
# WEEK 2: Enhanced Lists Features
# ============================================================================

@lists.route('/api/lists/<int:list_id>/cover', methods=['PUT'])
@login_required
def update_list_cover(list_id):
    """Update list cover image"""
    user_list = UserList.query.get_or_404(list_id)

    # Check ownership
    if user_list.user_id != current_user.id:
        return jsonify({'error': 'You can only edit your own lists'}), 403

    data = request.get_json()
    cover_url = data.get('cover_image')

    if not cover_url:
        return jsonify({'error': 'cover_image URL is required'}), 400

    try:
        user_list.cover_image = cover_url
        user_list.updated_at = datetime.utcnow()
        db.session.commit()

        return jsonify({
            'message': 'Cover image updated successfully',
            'list': user_list.to_dict()
        }), 200

    except Exception:
        db.session.rollback()
        logger.error("Failed to update list cover", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500


def _parse_items_payload(items_payload):
    """Validate the V2 "items" shape; returns (positions, error_response)."""
    if not isinstance(items_payload, list) or not items_payload:
        return None, (jsonify({'error': 'items must be a non-empty array'}), 400)
    positions = {}
    for entry in items_payload:
        if (not isinstance(entry, dict)
                or not isinstance(entry.get('item_id'), int)
                or not isinstance(entry.get('position'), int)
                or entry['position'] < 1):
            return None, (jsonify(
                {'error': 'each item needs integer item_id and position >= 1'}), 400)
        if entry['item_id'] in positions:
            return None, (jsonify(
                {'error': 'duplicate item_id in payload'}), 400)
        positions[entry['item_id']] = entry['position']
    ordered = [item_id for item_id, _ in
               sorted(positions.items(), key=lambda kv: kv[1])]
    return (ordered, positions), None


def _validate_item_order(item_order):
    """Validate the PUT item_order shape; returns error_response or None.

    Accepts ints or numeric strings (legacy drag-UI payloads send dataset
    strings); everything is coerced to int before any DB work.
    """
    if not item_order or not isinstance(item_order, list):
        return jsonify({'error': 'item_order must be an array of item IDs'}), 400
    normalized = []
    for raw in item_order:
        if isinstance(raw, bool):
            return jsonify({'error': 'item_order must be an array of item IDs'}), 400
        if isinstance(raw, int):
            normalized.append(raw)
        elif isinstance(raw, str) and raw.isdigit():
            normalized.append(int(raw))
        else:
            return jsonify({'error': 'item_order must be an array of item IDs'}), 400
    item_order[:] = normalized
    if len(set(item_order)) != len(item_order):
        return jsonify({'error': 'item_order must not contain duplicates'}), 400
    return None


def _parse_reorder_payload(data):
    """Normalize the two accepted reorder shapes.

    Returns (item_order, items_payload, error_response). Exactly one of
    item_order/items_payload is set; error_response is None on success.
    """
    item_order = data.get('item_order')  # Array of item IDs in new order
    items_payload = data.get('items')  # V2 alternative shape

    if items_payload is not None:
        parsed, error = _parse_items_payload(items_payload)
        if error is not None:
            return None, None, error
        ordered, positions = parsed
        return ordered, positions, None

    error = _validate_item_order(item_order)
    if error is not None:
        return None, None, error
    return item_order, None, None


def _apply_sparse_positions(user_list, items_payload):
    """Move claimed items to their requested 1-indexed slots.

    Move-to-slot semantics: claimed items are lifted out of the current
    canonical order, then re-inserted at their requested slots in ascending
    request order; untouched items keep their relative order and shift
    around them. Slots beyond the list end clamp to the last position.
    The caller renumbers the final arrangement gaplessly.
    """
    all_items = (UserListItem.query
                 .filter_by(list_id=user_list.id)
                 .order_by(UserListItem.position, UserListItem.id)
                 .all())
    by_id = {item.id: item for item in all_items}
    claimed = sorted(items_payload.items(), key=lambda kv: kv[1])
    claimed_ids = {item_id for item_id, _ in claimed}
    final = [item for item in all_items if item.id not in claimed_ids]
    for item_id, pos in claimed:
        index = min(max(int(pos) - 1, 0), len(final))
        final.insert(index, by_id[item_id])
    for index, item in enumerate(final):
        item.position = index + 1


def _apply_reorder(user_list, item_order, items_payload):
    """Mutate positions for a validated reorder payload.

    Two-phase strategy: park touched rows at large negative offsets
    (collision-free intermediate state), stamp target positions, then
    normalize the whole list to gapless 1..N in canonical order
    (position, id). Returns an error response or None.
    """
    list_id = user_list.id
    touched = (UserListItem.query
               .filter(UserListItem.id.in_(item_order),
                       UserListItem.list_id == list_id)
               .all())
    if len(touched) != len(item_order):
        return jsonify({'error': 'all items must belong to this list'}), 400

    # The item_order shape is a full-order contract: the drag UI always
    # renders every item, so a partial payload would silently collide
    # with untouched positions. Reject instead of guessing.
    total_items = user_list.items.count()
    if items_payload is None and len(item_order) != total_items:
        return jsonify({'error': 'item_order must include every item in the list; '
                                 'use the items shape for partial updates'}), 400

    by_id = {item.id: item for item in touched}
    offset = -(total_items + 1) * 1_000_000

    # Full reorder -> park, stamp 1..N in the requested order.
    if items_payload is not None:
        # Sparse explicit positions -> move-to-slot semantics, then the
        # gapless normalization below renumbers the final arrangement.
        _apply_sparse_positions(user_list, items_payload)
    else:
        # Full reorder -> park, stamp 1..N in the requested order.
        for index, item_id in enumerate(item_order):
            by_id[item_id].position = offset - index
        for index, item_id in enumerate(item_order):
            by_id[item_id].position = index + 1

    # Gapless normalization also repairs legacy position gaps in the same
    # transaction.
    all_items = (UserListItem.query
                 .filter_by(list_id=list_id)
                 .order_by(UserListItem.position, UserListItem.id)
                 .all())
    for index, item in enumerate(all_items):
        item.position = index + 1
    return None


@lists.route('/api/lists/<int:list_id>/reorder', methods=['PUT'])
@login_required
def reorder_list_items(list_id):
    """Reorder items in a list (drag and drop, keyboard moves).

    Lists V2 hardening: two-phase position update so each intermediate DB
    state is collision-free — concurrent drags can never violate the
    per-list position order even without a hard UNIQUE constraint.

    Accepts the drag-UI shape ``{"item_order": [id, id, ...]}`` (full
    order) and the finer-grained V2 shape
    ``{"items": [{"item_id": 123, "position": 1}, ...]}``.
    """
    user_list = UserList.query.get_or_404(list_id)

    # Owner or editor collaborator; viewers can never reorder.
    if not _can_edit(user_list):
        return jsonify({'error': 'You can only reorder your own lists'}), 403

    data = request.get_json(silent=True) or {}
    item_order, items_payload, error = _parse_reorder_payload(data)
    if error is not None:
        return error

    try:
        error = _apply_reorder(user_list, item_order, items_payload)
        if error is not None:
            return error

        user_list.updated_at = datetime.utcnow()
        db.session.commit()

        return jsonify({
            'message': 'List items reordered successfully',
            'list': user_list.to_dict()
        }), 200

    except Exception:
        db.session.rollback()
        logger.error("Failed to reorder list items", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500


@lists.route('/api/lists/slug/<slug>')
def get_list_by_slug(slug):
    """Get list by shareable slug (Lists V2: + watched/liked/comments)."""
    user_list = UserList.query.filter_by(slug=slug).first_or_404()

    # Check permissions
    if not user_list.is_public and (not current_user.is_authenticated or current_user.id != user_list.user_id):
        return jsonify({'error': 'This list is private'}), 403

    # Get all items in the list with their media details (eager-loaded to
    # avoid one lazy query per item in the template)
    items = user_list.items.options(
        joinedload(UserListItem.media)
    ).order_by(UserListItem.position, UserListItem.added_at).all()

    list_data = user_list.to_dict()
    list_data['items'] = [item.to_dict() for item in items]

    _enrich_items_with_watched_state(list_data['items'])
    if current_user.is_authenticated:
        list_data['liked_by_me'] = db.session.query(ListLike.id).filter_by(
            user_id=current_user.id, list_id=user_list.id).first() is not None

    list_data['comments'] = _serialize_comments_page(user_list)
    list_data['comments_page'] = 1
    list_data['comments_pages'] = _comments_pages(user_list)

    return jsonify(list_data), 200


@lists.route('/api/lists/discover')
def discover_lists():
    """Discover popular and trending lists"""
    sort = request.args.get('sort', 'recent')  # recent, popular, trending
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 20, type=int), 100)

    # Base query - only public lists
    query = UserList.query.filter_by(is_public=True)

    # Apply sorting (Lists V2 adds liked + viewed; all deterministic)
    if sort == 'popular':
        # Sort by item count (lists with more items are "popular")
        query = query.outerjoin(UserListItem).group_by(UserList.id).order_by(
            db.func.count(UserListItem.id).desc(), UserList.id.desc()
        )
    elif sort == 'trending':
        # Sort by recently updated lists
        query = query.order_by(UserList.updated_at.desc(), UserList.id.desc())
    elif sort == 'liked':
        query = query.outerjoin(ListLike).group_by(UserList.id).order_by(
            db.func.count(ListLike.id).desc(), UserList.id.desc())
    elif sort == 'viewed':
        query = query.outerjoin(ListView).group_by(UserList.id).order_by(
            db.func.count(ListView.id).desc(), UserList.id.desc())
    else:  # recent
        query = query.order_by(UserList.created_at.desc(), UserList.id.desc())

    # Paginate
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    prefetch_list_data(pagination.items)

    return jsonify({
        'lists': [user_list.to_dict() for user_list in pagination.items],
        'total': pagination.total,
        'pages': pagination.pages,
        'current_page': page,
        'has_next': pagination.has_next,
        'has_prev': pagination.has_prev,
        'sort': sort
    }), 200


# HTML Template Routes
@lists.route('/lists')
@login_required
def my_lists():
    """View all user's lists (normal + Smart Lists)"""
    from models.smart_lists import SmartList
    from api.smart_lists import rule_summary

    user_lists = current_user.lists.order_by(UserList.created_at.desc()).all()
    smart_lists = (SmartList.query
                   .filter_by(user_id=current_user.id)
                   .order_by(SmartList.created_at.desc())
                   .all())
    smart_data = [{**sl.to_dict(), 'rules': rule_summary(sl)}
                  for sl in smart_lists]
    return render_template('my_lists.html', user_lists=user_lists,
                           smart_lists=smart_data)


@lists.route('/lists/<int:list_id>')
def view_list(list_id):
    """View a specific list with all its items"""
    user_list = UserList.query.get_or_404(list_id)

    # Check permissions
    if not user_list.is_public and (not current_user.is_authenticated or current_user.id != user_list.user_id):
        return render_template('error.html', message='This list is private'), 403

    # Get all items in the list with their media details (eager-loaded to
    # avoid one lazy query per item in the template)
    items = user_list.items.options(
        joinedload(UserListItem.media)
    ).order_by(UserListItem.position, UserListItem.added_at).all()

    serialized = [item.to_dict() for item in items]
    _enrich_items_with_watched_state(serialized)
    watched_map = {i['id']: i.get('watched') for i in serialized}
    progress_map = {i['id']: i.get('watch_progress') for i in serialized
                    if i.get('watch_progress')}
    liked_by_me = (current_user.is_authenticated and db.session.query(
        ListLike.id).filter_by(user_id=current_user.id,
                               list_id=user_list.id).first() is not None)

    return render_template('list_detail.html', user_list=user_list,
                           items=items, watched_map=watched_map,
                           progress_map=progress_map,
                           liked_by_me=liked_by_me, can_edit=_can_edit(user_list),
                           comments=_serialize_comments_page(user_list),
                           liked_count=ListLike.query.filter_by(
                               list_id=user_list.id).count())


@lists.route('/lists/l/<slug>')
def view_list_by_slug(slug):
    """View a list by its shareable slug - Week 2 feature"""
    user_list = UserList.query.filter_by(slug=slug).first_or_404()

    # Check permissions
    if not user_list.is_public and (not current_user.is_authenticated or current_user.id != user_list.user_id):
        return render_template('error.html', message='This list is private'), 403

    # Get all items in the list with their media details (eager-loaded to
    # avoid one lazy query per item in the template)
    items = user_list.items.options(
        joinedload(UserListItem.media)
    ).order_by(UserListItem.position, UserListItem.added_at).all()

    serialized = [item.to_dict() for item in items]
    _enrich_items_with_watched_state(serialized)
    watched_map = {i['id']: i.get('watched') for i in serialized}
    progress_map = {i['id']: i.get('watch_progress') for i in serialized
                    if i.get('watch_progress')}
    liked_by_me = (current_user.is_authenticated and db.session.query(
        ListLike.id).filter_by(user_id=current_user.id,
                               list_id=user_list.id).first() is not None)

    return render_template('list_detail.html', user_list=user_list,
                           items=items, watched_map=watched_map,
                           progress_map=progress_map,
                           liked_by_me=liked_by_me, can_edit=_can_edit(user_list),
                           comments=_serialize_comments_page(user_list),
                           liked_count=ListLike.query.filter_by(
                               list_id=user_list.id).count())


@lists.route('/discover')
def discover():
    """List discovery page - Week 2 feature"""
    return render_template('lists_discover.html')


# ============================================================================
# LISTS V2: ordering, ranking mode, cloning, social engagement, bulk ops
# ============================================================================

LIST_COMMENT_MAX_LEN = 2000
COMMENTS_PER_PAGE = 20


def _can_edit(user_list):
    """Owner or editor collaborator (viewer can never mutate)."""
    from routes.lists_advanced import can_edit_list
    return can_edit_list(user_list, current_user)


def _load_internal_ids(items):
    """Return {item.id: (internal media_id, media_type)} for serialized items.

    Items arrive as serialized dicts (see all call sites); the junction
    tables key on the INTERNAL media_item.id (see routes/diary.py insert
    sites), exposed additively as media.internal_id. No TMDb lookups, ever.
    """
    out = {}
    for item in items:
        media = item.get('media') or {}
        internal = media.get('internal_id')
        if internal is not None:
            out[item['id']] = (internal, item.get('media_type', 'movie'))
    return out


def _load_tmdb_keys(items):
    """{item.id: (tmdb_id, media_type)} — TVShowProgress.show_id is TMDb-keyed.

    Works on serialized dicts; media.id in the dict payload is the TMDb id.
    """
    out = {}
    for item in items:
        media = item.get('media') or {}
        tmdb_id = media.get('id')
        if tmdb_id is not None and item.get('media_type') == 'tv':
            out[item['id']] = (tmdb_id, 'tv')
    return out


def _apply_tv_progress(items, tmdb_by_item, progress_rows):
    """Stamp watched state + progress for TV items (helper, keeps caller flat)."""
    progress_by_show = {p.show_id: p for p in progress_rows}
    for item in items:
        key = tmdb_by_item.get(item['id'])
        if key and key[0] in progress_by_show:
            p = progress_by_show[key[0]]
            item['watched'] = ('watched' if p.status == 'completed'
                               else 'watching')
            total = p.total_episodes or 0
            if total > 0:
                # Half-up rounding (62.5% -> 63%): deterministic UI math,
                # independent of Python's banker's rounding.
                item['watch_progress'] = {
                    'watched': p.watched_episodes or 0,
                    'total': total,
                    'percent': min(100,
                                   int((p.watched_episodes or 0) * 100
                                       / total + 0.5)),
                }


def _apply_movie_states(items, internal_by_item, viewed_movies, watchlisted):
    """Stamp watched/watching state for movie items (internal-id keyed)."""
    for item in items:
        key = internal_by_item.get(item['id'])
        if key and key[1] == 'movie':
            if key[0] in viewed_movies:
                item['watched'] = 'watched'
            elif key[0] in watchlisted:
                item['watched'] = 'watching'


def _enrich_items_with_watched_state(items):
    """Add watched/unwatched state per viewer in three bounded queries.

    Mutates the serialized item dicts in place (additive keys only):
        watched: 'watched' | 'watching' | 'unwatched' | None (anonymous)
        watch_progress: {"watched": int, "total": int, "percent": int}
                       (TV only, when progress data exists)

    Movie watched := (user_id, media_id) present in user_viewed.
    TV: 'watched' when TVShowProgress.status == 'completed', 'watching'
    otherwise; progress percentages from watched/total_episodes.
    """
    if not items:
        return
    viewer_id = current_user.get_id() if current_user.is_authenticated else None
    if viewer_id is None:
        return

    internal_by_item = _load_internal_ids(items)
    if not internal_by_item:
        return

    for item in items:
        item['watched'] = 'unwatched'

    movie_ids = {mid for (mid, mtype) in internal_by_item.values()
                 if mtype == 'movie'}
    tmdb_show_ids = {key[0] for key in _load_tmdb_keys(items).values()}

    # Batch 1: viewed movies (one join of the junction table).
    viewed_movies = set()
    if movie_ids:
        viewed_rows = db.session.query(user_viewed.c.media_id).filter(
            user_viewed.c.user_id == viewer_id,
            user_viewed.c.media_id.in_(movie_ids)).all()
        viewed_movies = {row[0] for row in viewed_rows}

    # Batch 2: watchlisted movies = 'watching' intent signal.
    watchlisted = set()
    if movie_ids:
        wl_rows = db.session.query(user_watchlist.c.media_id).filter(
            user_watchlist.c.user_id == viewer_id,
            user_watchlist.c.media_id.in_(movie_ids)).all()
        watchlisted = {row[0] for row in wl_rows}

    _apply_movie_states(items, internal_by_item, viewed_movies, watchlisted)

    # Batch 3: TV progress rows (one scan; TVShowProgress.show_id is the
    # TMDb show id, so TV items resolve through the serialized media.id).
    if tmdb_show_ids:
        progress_rows = (TVShowProgress.query
                         .filter(TVShowProgress.user_id == viewer_id,
                                 TVShowProgress.show_id.in_(tmdb_show_ids))
                         .all())
        _apply_tv_progress(items, _load_tmdb_keys(items), progress_rows)


def _comments_pages(user_list):
    total = user_list.comments.filter_by(is_deleted=False).count()
    return max(1, (total + COMMENTS_PER_PAGE - 1) // COMMENTS_PER_PAGE)


def _serialize_comments_page(user_list, page=1):
    """Newest-first paginated comments (bounded: COMMENTS_PER_PAGE rows)."""
    page = max(1, page)
    rows = (user_list.comments.filter_by(is_deleted=False)
            .order_by(ListComment.created_at.desc(), ListComment.id.desc())
            .offset((page - 1) * COMMENTS_PER_PAGE)
            .limit(COMMENTS_PER_PAGE)
            .all())
    return [c.to_dict() for c in rows]


def _remaining_likers(user_list, liker_ids):
    """Display strings for other users who liked this list (bounded)."""
    if not liker_ids:
        return []
    users = User.query.filter(User.id.in_(liker_ids)).all()
    by_id = {u.id: u.username for u in users}
    return [by_id.get(uid, 'A FrameIQ user') for uid in liker_ids
            if uid in by_id][:5]


def _require_public_or_owner(user_list):
    """Likes/comments surface only on public lists (or to the owner)."""
    if user_list.is_public:
        return None
    if current_user.is_authenticated and current_user.id == user_list.user_id:
        return None
    return jsonify({'error': 'This list is private'}), 403


def _next_position(list_id):
    max_pos = db.session.query(db.func.max(UserListItem.position)).filter_by(
        list_id=list_id).scalar()
    return (max_pos or 0) + 1


def _serialize_items(user_list):
    items = user_list.items.options(
        joinedload(UserListItem.media)
    ).order_by(UserListItem.position, UserListItem.added_at).all()
    return [item.to_dict() for item in items]


@lists.route('/api/lists/<int:list_id>/mode', methods=['PUT'])
@login_required
def set_list_mode(list_id):
    """Switch ranked/unranked mode.

    Mode switching NEVER mutates item positions: unranked -> ranked assigns
    ranks from the current canonical order; ranked -> unranked keeps the
    same order and only changes the presentation flag.
    """
    user_list = UserList.query.get_or_404(list_id)
    if not _can_edit(user_list):
        return jsonify({'error': 'You can only edit your own lists'}), 403

    data = request.get_json(silent=True) or {}
    list_type = data.get('list_type')
    if list_type not in (UserList.TYPE_RANKED, UserList.TYPE_UNRANKED):
        return jsonify({'error': "list_type must be 'ranked' or 'unranked'"}), 400

    user_list.list_type = list_type
    user_list.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'message': 'List mode updated',
                    'list_type': user_list.list_type}), 200


@lists.route('/api/lists/<int:list_id>/clone', methods=['POST'])
@login_required
def clone_list(list_id):
    """Clone a list into a private copy owned by the current user.

    Copies: metadata, items, positions, ranked/unranked mode.
    Never copies: owner, collaborators, views, analytics, likes, comments.
    Provenance: 'Cloned from <title>' is stored in the description only when
    the description is empty (does not destroy the source's own text).
    """
    source = UserList.query.get_or_404(list_id)

    # Only public lists (or your own) may be cloned.
    if not source.is_public and source.user_id != current_user.id:
        return jsonify({'error': 'This list is private'}), 403

    try:
        clone = UserList(
            user_id=current_user.id,
            title=source.title,
            description=source.description,
            is_public=False,  # clones are private by default
            cover_image=source.cover_image,
            list_type=source.list_type,  # mode copied per spec
        )
        db.session.add(clone)
        db.session.flush()
        clone.slug = generate_slug(clone.title, clone.id)

        if not (clone.description or '').strip():
            clone.description = f"Cloned from {source.title}"

        # Copy items with positions in canonical source order (one INSERT
        # batch via bulk_save_objects; avoids per-item flushes).
        source_items = (source.items
                        .order_by(UserListItem.position, UserListItem.added_at)
                        .all())
        cloned_items = []
        for src_item in source_items:
            cloned_items.append(UserListItem(
                list_id=clone.id,
                media_id=src_item.media_id,
                media_type=src_item.media_type,
                position=src_item.position,
                note=src_item.note,
                added_at=datetime.utcnow(),
            ))
        if cloned_items:
            db.session.bulk_save_objects(cloned_items)

        # Increment fork_count on the source analytics row (best-effort).
        analytics = ListAnalytics.query.filter_by(list_id=source.id).first()
        if analytics is not None:
            analytics.fork_count = (analytics.fork_count or 0) + 1

        db.session.commit()
        return jsonify({
            'message': 'List cloned successfully',
            'list_id': clone.id,
            'slug': clone.slug,
            'list_type': clone.list_type,
        }), 201

    except Exception:
        db.session.rollback()
        logger.error("Failed to clone list", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500


@lists.route('/api/lists/<int:list_id>/like', methods=['POST'])
@login_required
def like_list(list_id):
    """Like a public list (one per user per list)."""
    user_list = UserList.query.get_or_404(list_id)
    denied = _require_public_or_owner(user_list)
    if denied is not None:
        return denied

    existing = ListLike.query.filter_by(
        user_id=current_user.id, list_id=list_id).first()
    if existing is None:
        db.session.add(ListLike(user_id=current_user.id, list_id=list_id))
        db.session.commit()

    count = ListLike.query.filter_by(list_id=list_id).count()
    return jsonify({'liked': True, 'like_count': count}), 200


@lists.route('/api/lists/<int:list_id>/like', methods=['DELETE'])
@login_required
def unlike_list(list_id):
    """Remove the current user's like from a list."""
    UserList.query.get_or_404(list_id)
    row = ListLike.query.filter_by(
        user_id=current_user.id, list_id=list_id).first()
    if row is not None:
        db.session.delete(row)
        db.session.commit()

    count = ListLike.query.filter_by(list_id=list_id).count()
    return jsonify({'liked': False, 'like_count': count}), 200


@lists.route('/api/lists/<int:list_id>/like/status', methods=['GET'])
@login_required
def like_status(list_id):
    """Current user's like state + total count for a list."""
    user_list = UserList.query.get_or_404(list_id)
    denied = _require_public_or_owner(user_list)
    if denied is not None:
        return denied
    liked = ListLike.query.filter_by(
        user_id=current_user.id, list_id=list_id).first() is not None
    count = ListLike.query.filter_by(list_id=list_id).count()
    return jsonify({'liked': liked, 'like_count': count}), 200


@lists.route('/api/lists/<int:list_id>/comments', methods=['GET'])
def get_list_comments(list_id):
    """Paginated comments on a list (newest first)."""
    user_list = UserList.query.get_or_404(list_id)
    denied = _require_public_or_owner(user_list)
    if denied is not None:
        return denied
    page = max(1, request.args.get('page', 1, type=int))
    return jsonify({
        'comments': _serialize_comments_page(user_list, page),
        'page': page,
        'pages': _comments_pages(user_list),
    }), 200


@lists.route('/api/lists/<int:list_id>/comments', methods=['POST'])
@login_required
def add_list_comment(list_id):
    """Post a comment on a public list."""
    user_list = UserList.query.get_or_404(list_id)
    denied = _require_public_or_owner(user_list)
    if denied is not None:
        return denied

    data = request.get_json(silent=True) or {}
    content = (data.get('content') or '').strip()
    if not content:
        return jsonify({'error': 'Comment cannot be empty'}), 400
    if len(content) > LIST_COMMENT_MAX_LEN:
        return jsonify({'error': 'Comment too long'}), 400

    comment = ListComment(
        user_id=current_user.id,
        list_id=list_id,
        content=content,
    )
    db.session.add(comment)
    db.session.commit()
    return jsonify({'message': 'Comment added',
                    'comment': comment.to_dict()}), 201


@lists.route('/api/lists/<int:list_id>/comments/<int:comment_id>',
             methods=['DELETE'])
@login_required
def delete_list_comment(list_id, comment_id):
    """Soft-delete a comment (author or list owner only)."""
    user_list = UserList.query.get_or_404(list_id)
    comment = ListComment.query.filter_by(
        id=comment_id, list_id=list_id, is_deleted=False).first_or_404()

    if comment.user_id != current_user.id and \
            user_list.user_id != current_user.id:
        return jsonify({'error': 'You can only delete your own comments'}), 403

    comment.is_deleted = True
    db.session.commit()
    return jsonify({'message': 'Comment deleted'}), 200


def _bulk_move(user_list, rows, data):
    """Move rows to another owned list (dedupe by (media_id, media_type)).

    Returns an error response or None. Never mutates the session on error.
    """
    target_id = data.get('target_list_id')
    if not isinstance(target_id, int):
        return jsonify({'error': 'target_list_id is required for move'}), 400
    target = UserList.query.get(target_id)
    if target is None or target.user_id != current_user.id:
        return jsonify({'error': 'Target list not found or not owned by you'}), 403
    if target.id == user_list.id:
        return jsonify({'error': 'Cannot move items to the same list'}), 400

    existing_keys = {
        (i.media_id, i.media_type)
        for i in target.items.with_entities(
            UserListItem.media_id, UserListItem.media_type).all()
    }
    for row in rows:
        if (row.media_id, row.media_type) in existing_keys:
            db.session.delete(row)  # dedupe: drop from source
            continue
        row.list_id = target.id
        row.position = _next_position(target.id)
        existing_keys.add((row.media_id, row.media_type))

    _renumber(user_list.id)
    _renumber(target.id)
    return None


def _bulk_validate(data):
    """Validate the bulk payload; returns (action, item_ids, error)."""
    action = data.get('action')
    item_ids = data.get('item_ids')

    if action not in ('remove', 'move'):
        return None, None, (jsonify(
            {'error': "action must be 'remove' or 'move'"}), 400)
    if not isinstance(item_ids, list) or not item_ids or \
            not all(isinstance(i, int) for i in item_ids):
        return None, None, (jsonify(
            {'error': 'item_ids must be a non-empty array of IDs'}), 400)
    if len(set(item_ids)) != len(item_ids):
        return None, None, (jsonify(
            {'error': 'item_ids must not contain duplicates'}), 400)
    return action, item_ids, None


@lists.route('/api/lists/<int:list_id>/items/bulk', methods=['POST'])
@login_required
def bulk_items(list_id):
    """Bulk remove items or move them to another list (owner/editor only).

    Body: {"action": "remove"|"move", "item_ids": [..],
           "target_list_id": int (move only)}
    Cross-list item references are rejected; invalid ids return 400.
    """
    user_list = UserList.query.get_or_404(list_id)
    if not _can_edit(user_list):
        return jsonify({'error': 'You can only edit your own lists'}), 403

    data = request.get_json(silent=True) or {}
    action, item_ids, error = _bulk_validate(data)
    if error is not None:
        return error

    rows = (UserListItem.query
            .filter(UserListItem.id.in_(item_ids),
                    UserListItem.list_id == list_id)
            .all())
    if len(rows) != len(item_ids):
        return jsonify({'error': 'all items must belong to this list'}), 400

    try:
        if action == 'remove':
            for row in rows:
                db.session.delete(row)
            # Keep positions gapless after deletion.
            db.session.flush()
            _renumber(list_id)
        else:  # move
            error = _bulk_move(user_list, rows, data)
            if error is not None:
                return error

        user_list.updated_at = datetime.utcnow()
        db.session.commit()
        return jsonify({'message': f'Bulk {action} complete'}), 200

    except Exception:
        db.session.rollback()
        logger.error("Bulk list item operation failed", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500


def _renumber(list_id):
    """Renumber a list's positions to gapless 1..N (canonical order)."""
    items = (UserListItem.query.filter_by(list_id=list_id)
             .order_by(UserListItem.position, UserListItem.id).all())
    for index, item in enumerate(items):
        item.position = index + 1


@lists.route('/api/lists/<int:list_id>/watched-state', methods=['GET'])
@login_required
def get_watched_state(list_id):
    """Per-viewer watched state for every item in a list (batch queries)."""
    user_list = UserList.query.get_or_404(list_id)
    if not user_list.is_public and user_list.user_id != current_user.id:
        return jsonify({'error': 'This list is private'}), 403

    items = user_list.items.options(
        joinedload(UserListItem.media)
    ).order_by(UserListItem.position, UserListItem.added_at).all()
    serialized = [item.to_dict() for item in items]
    _enrich_items_with_watched_state(serialized)

    watched_count = sum(1 for i in serialized if i.get('watched') == 'watched')
    return jsonify({
        'items': serialized,
        'watched_count': watched_count,
        'total': len(serialized),
    }), 200
