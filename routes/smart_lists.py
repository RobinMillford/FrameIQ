"""Smart Lists API (Feature 05) — CRUD + results, owner-scoped.

Conventions mirror routes/notifications.py / routes/lists.py:

- GET    /api/smart-lists            → the caller's Smart Lists (+ rule chips)
- POST   /api/smart-lists            → create (config validated server-side)
- GET    /api/smart-lists/<id>       → one Smart List (owner only)
- PUT    /api/smart-lists/<id>       → rename / edit filters / change sort
- DELETE /api/smart-lists/<id>       → delete (owner only)
- GET    /api/smart-lists/<id>/results → evaluate the saved query live

Every route is authenticated; every read/write filters by user_id, so another
user's id is indistinguishable from a nonexistent one (404). State-changing
methods are CSRF-protected globally by CSRFProtect. Configuration is never
trusted from the client — validate_config() runs on every write, and the
engine re-validates on every evaluation.
"""
import logging

from flask import Blueprint, jsonify, request, render_template
from flask_login import login_required, current_user

from extensions import limiter
from models import db
from models.smart_lists import SmartList
from api.smart_lists import (validate_config, evaluate_smart_list,
                             rule_summary)

logger = logging.getLogger(__name__)

smart_lists_bp = Blueprint('smart_lists', __name__)

DEFAULT_PER_PAGE = 24
MAX_PER_PAGE = 48
MAX_NAME_LEN = 200
MAX_DESC_LEN = 1000


def _config_from_payload(data):
    """Extract + validate a config from a request body. Raises ValueError."""
    scope = data.get('scope', 'watchlist')
    sort = data.get('sort', 'date_added')
    filters = data.get('filters') or {}
    return validate_config(scope, filters, sort), scope, sort


@smart_lists_bp.route('/api/smart-lists')
@login_required
def api_list_smart_lists():
    """The caller's Smart Lists, newest first, with human-readable rules."""
    rows = (SmartList.query
            .filter_by(user_id=current_user.id)
            .order_by(SmartList.created_at.desc())
            .all())
    return jsonify({
        'smart_lists': [
            {**sl.to_dict(), 'rules': rule_summary(sl)} for sl in rows
        ],
        'count': len(rows),
    })


@smart_lists_bp.route('/api/smart-lists', methods=['POST'])
@login_required
@limiter.limit("30 per minute")
def api_create_smart_list():
    """Create a Smart List. Invalid configuration → 400, never a 500."""
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Name is required'}), 400
    if len(name) > MAX_NAME_LEN:
        return jsonify({'error': f'Name must be ≤ {MAX_NAME_LEN} characters'}), 400
    description = (data.get('description') or '').strip()[:MAX_DESC_LEN]

    try:
        filters, scope, sort = _config_from_payload(data)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    smart_list = SmartList(
        user_id=current_user.id,
        name=name[:MAX_NAME_LEN],
        description=description or None,
        scope=scope,
        sort=sort,
        is_public=bool(data.get('is_public', False)),
    )
    smart_list.filters = filters
    db.session.add(smart_list)
    db.session.commit()

    payload = smart_list.to_dict()
    payload['rules'] = rule_summary(smart_list)
    return jsonify(payload), 201


@smart_lists_bp.route('/api/smart-lists/<int:smart_list_id>')
@login_required
def api_get_smart_list(smart_list_id):
    """One Smart List — owner only (404 for anyone else)."""
    smart_list = SmartList.query.filter_by(
        id=smart_list_id, user_id=current_user.id).first()
    if not smart_list:
        return jsonify({'error': 'Smart List not found'}), 404
    payload = smart_list.to_dict()
    payload['rules'] = rule_summary(smart_list)
    return jsonify(payload)


@smart_lists_bp.route('/api/smart-lists/<int:smart_list_id>', methods=['PUT'])
@login_required
@limiter.limit("30 per minute")
def api_update_smart_list(smart_list_id):
    """Edit name/description/filters/sort — same validation as create."""
    smart_list = SmartList.query.filter_by(
        id=smart_list_id, user_id=current_user.id).first()
    if not smart_list:
        return jsonify({'error': 'Smart List not found'}), 404

    data = request.get_json(silent=True) or {}

    if 'name' in data:
        name = (data.get('name') or '').strip()
        if not name:
            return jsonify({'error': 'Name cannot be empty'}), 400
        smart_list.name = name[:MAX_NAME_LEN]
    if 'description' in data:
        smart_list.description = (data.get('description') or '').strip()[:MAX_DESC_LEN] or None
    if 'is_public' in data:
        smart_list.is_public = bool(data.get('is_public'))

    # Merge edits onto the stored config so a partial PUT cannot wipe filters.
    try:
        scope = data.get('scope', smart_list.scope)
        sort = data.get('sort', smart_list.sort)
        filters_in = data.get('filters', smart_list.filters) or {}
        filters, scope, sort = _config_from_payload(
            {'scope': scope, 'sort': sort, 'filters': filters_in})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    smart_list.scope = scope
    smart_list.sort = sort
    smart_list.filters = filters
    db.session.commit()

    payload = smart_list.to_dict()
    payload['rules'] = rule_summary(smart_list)
    return jsonify(payload)


@smart_lists_bp.route('/api/smart-lists/<int:smart_list_id>',
                      methods=['DELETE'])
@login_required
@limiter.limit("30 per minute")
def api_delete_smart_list(smart_list_id):
    """Delete a Smart List — owner only. Deletes only the saved query."""
    smart_list = SmartList.query.filter_by(
        id=smart_list_id, user_id=current_user.id).first()
    if not smart_list:
        return jsonify({'error': 'Smart List not found'}), 404
    db.session.delete(smart_list)
    db.session.commit()
    return jsonify({'success': True})


@smart_lists_bp.route('/api/smart-lists/<int:smart_list_id>/results')
@login_required
@limiter.limit("60 per minute")
def api_smart_list_results(smart_list_id):
    """Live evaluation of the saved query (paginated). Owner only."""
    smart_list = SmartList.query.filter_by(
        id=smart_list_id, user_id=current_user.id).first()
    if not smart_list:
        return jsonify({'error': 'Smart List not found'}), 404

    try:
        page = int(request.args.get('page', 1))
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = int(request.args.get('per_page', DEFAULT_PER_PAGE))
    except (TypeError, ValueError):
        per_page = DEFAULT_PER_PAGE
    per_page = max(1, min(per_page, MAX_PER_PAGE))

    try:
        result = evaluate_smart_list(smart_list, page=page, per_page=per_page)
    except ValueError as e:
        # A stale/invalid stored config can only come from a data bug; return
        # a clear validation error instead of breaking the page.
        return jsonify({'error': f'Invalid Smart List configuration: {e}'}), 400

    result['rules'] = rule_summary(smart_list)
    result['name'] = smart_list.name
    return jsonify(result)


# ── HTML page ────────────────────────────────────────────────────────────────

@smart_lists_bp.route('/smart-lists/<int:smart_list_id>')
@login_required
def view_smart_list_page(smart_list_id):
    """Smart List detail page — server-rendered shell; results via API."""
    smart_list = SmartList.query.filter_by(
        id=smart_list_id, user_id=current_user.id).first()
    if not smart_list:
        return render_template('error.html', message='Smart List not found'), 404
    return render_template('smart_list_detail.html', smart_list=smart_list,
                           rules=rule_summary(smart_list))
