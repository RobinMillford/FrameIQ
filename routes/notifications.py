"""Notification API (Feature 04) — minimal, user-scoped, finite.

- GET  /api/notifications            → newest-first list + unread count
- POST /api/notifications/<id>/read  → mark one read (owner only)
- POST /api/notifications/read-all   → mark all read

All endpoints are authenticated and strictly user-scoped: a notification id
belonging to another user is indistinguishable from a nonexistent one (404),
and ownership is enforced by user_id filters — never by trusting the client.

Notifications are created ONLY by the episode-sync job, so no endpoint here
can create them and no page render fans them out.
"""
import logging
from datetime import datetime

from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user

from extensions import limiter
from models import db
from models.notification import Notification
from api.notifications import unread_count, list_notifications

logger = logging.getLogger(__name__)

notifications_bp = Blueprint('notifications', __name__)

DEFAULT_PAGE_SIZE = 30
MAX_PAGE_SIZE = 100


@notifications_bp.route('/api/notifications')
@login_required
def api_list_notifications():
    """The caller's notifications, newest first, with unread count."""
    try:
        limit = int(request.args.get('limit', DEFAULT_PAGE_SIZE))
    except (TypeError, ValueError):
        limit = DEFAULT_PAGE_SIZE
    limit = max(1, min(limit, MAX_PAGE_SIZE))

    notifications = list_notifications(current_user.id, limit=limit)
    return jsonify({
        'unread_count': unread_count(current_user.id),
        'notifications': [n.to_dict() for n in notifications],
    })


@notifications_bp.route('/api/notifications/<int:notification_id>/read',
                        methods=['POST'])
@login_required
@limiter.limit("60 per minute")
def api_mark_read(notification_id):
    """Mark one of the caller's notifications read (idempotent).

    Ownership: the UPDATE is filtered by user_id — another user's id 404s.
    """
    n = (Notification.query
         .filter_by(id=notification_id, user_id=current_user.id)
         .first())
    if n is None:
        return jsonify({'error': 'Notification not found'}), 404

    if n.read_at is None:
        n.read_at = datetime.utcnow()
        db.session.commit()

    return jsonify({'ok': True, 'id': n.id,
                    'unread_count': unread_count(current_user.id)})


@notifications_bp.route('/api/notifications/read-all', methods=['POST'])
@login_required
@limiter.limit("10 per minute")
def api_mark_all_read():
    """Mark every unread notification of the caller as read."""
    updated = (Notification.query
               .filter_by(user_id=current_user.id)
               .filter(Notification.read_at.is_(None))
               .update({'read_at': datetime.utcnow()},
                       synchronize_session=False))
    db.session.commit()
    return jsonify({'ok': True, 'marked': updated, 'unread_count': 0})
