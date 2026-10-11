"""Notification API (Feature 04) — minimal, user-scoped, finite.

- GET  /api/notifications            → newest-first list + unread count
- POST /api/notifications/<id>/read  → mark one read (owner only)
- POST /api/notifications/read-all   → mark all read

Feature F12 adds per-user notification preferences:

- GET  /api/notification-preferences        → effective preferences + catalogue
- POST /api/notification-preferences        → validate + save (owner only)
- GET  /api/notification-preferences/categories → supported categories

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
from api.notification_preferences import (
    PreferenceError, describe_categories, get_preferences, save_preferences,
)
from models.notification_preferences import UserNotificationPreference

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


@notifications_bp.route('/api/notification-preferences')
@login_required
def api_get_notification_preferences():
    """The caller's effective preferences + the supported category catalogue.

    A user with no stored record gets the documented defaults; reading never
    creates a row, so simply visiting the settings cannot fork state.
    """
    return jsonify({
        'preferences': get_preferences(current_user.id),
        'categories': describe_categories(),
        'saved': UserNotificationPreference.query.filter_by(
            user_id=current_user.id).first() is not None,
    })


@notifications_bp.route('/api/notification-preferences', methods=['POST'])
@login_required
@limiter.limit("30 per minute")
def api_save_notification_preferences():
    """Validate and persist the caller's preferences.

    Recipient isolation is structural: there is no user id in the payload, so
    one user cannot read or write another's preferences. CSRF is enforced
    globally by CSRFProtect on this unsafe verb.
    """
    data = request.get_json(silent=True) or {}
    payload = data.get('preferences', data)
    try:
        stored = save_preferences(current_user.id, payload)
    except PreferenceError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'ok': True, 'preferences': stored, 'saved': True})


@notifications_bp.route('/api/notification-preferences/categories')
@login_required
def api_notification_preference_categories():
    """The supported categories (keys, labels, explanations)."""
    return jsonify({'categories': describe_categories()})
