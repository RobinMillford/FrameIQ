# File: routes/watchlist_priorities.py
from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user
from models import db, user_watchlist
from sqlalchemy import text, select, update

priorities_bp = Blueprint('priorities', __name__)


@priorities_bp.route('/api/watchlist/<int:media_id>/priority', methods=['GET'])
@login_required
def get_watchlist_priority(media_id):
    """Get priority for a watchlist item"""
    media_type = request.args.get('media_type', 'movie')
    
    # Query the association table directly
    stmt = select(user_watchlist.c.priority).where(
        user_watchlist.c.user_id == current_user.id,
        user_watchlist.c.media_id == media_id,
        user_watchlist.c.media_type == media_type
    )
    
    result = db.session.execute(stmt).first()
    
    if not result:
        return jsonify({'error': 'Item not in watchlist'}), 404
    
    return jsonify({
        'media_id': media_id,
        'media_type': media_type,
        'priority': result[0] or 'medium'
    }), 200


@priorities_bp.route('/api/watchlist/<int:media_id>/priority', methods=['PUT'])
@login_required
def update_watchlist_priority(media_id):
    """Update priority for a watchlist item"""
    if not current_user.is_authenticated:
        return jsonify({'error': 'Authentication required'}), 401
    
    data = request.get_json()
    
    if not data or 'priority' not in data:
        return jsonify({'error': 'priority is required'}), 400
    
    priority = data['priority']
    media_type = data.get('media_type', 'movie')
    
    # Validate priority value
    if priority not in ['high', 'medium', 'low']:
        return jsonify({'error': 'priority must be high, medium, or low'}), 400
    
    # Check if item is in watchlist
    stmt = select(user_watchlist.c.user_id).where(
        user_watchlist.c.user_id == current_user.id,
        user_watchlist.c.media_id == media_id,
        user_watchlist.c.media_type == media_type
    )
    
    result = db.session.execute(stmt).first()
    
    if not result:
        return jsonify({'error': 'Item not in watchlist'}), 404
    
    # Update priority
    update_stmt = update(user_watchlist).where(
        user_watchlist.c.user_id == current_user.id,
        user_watchlist.c.media_id == media_id,
        user_watchlist.c.media_type == media_type
    ).values(priority=priority)
    
    db.session.execute(update_stmt)
    db.session.commit()
    
    return jsonify({
        'message': 'Priority updated successfully',
        'media_id': media_id,
        'media_type': media_type,
        'priority': priority
    }), 200


@priorities_bp.route('/api/watchlist', methods=['GET'])
@login_required
def get_watchlist_with_priorities():
    """Get user's watchlist with priorities"""
    priority_filter = request.args.get('priority')  # Optional: 'high', 'medium', 'low'
    sort_by = request.args.get('sort', 'date_added')  # 'date_added', 'priority'
    
    # Build query
    stmt = select(
        user_watchlist.c.media_id,
        user_watchlist.c.media_type,
        user_watchlist.c.date_added,
        user_watchlist.c.priority
    ).where(
        user_watchlist.c.user_id == current_user.id
    )
    
    # Apply priority filter if specified
    if priority_filter and priority_filter in ['high', 'medium', 'low']:
        stmt = stmt.where(user_watchlist.c.priority == priority_filter)
    
    # Apply sorting
    if sort_by == 'priority':
        # Custom priority order: high > medium > low
        priority_order = text("CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END")
        stmt = stmt.order_by(priority_order, user_watchlist.c.date_added.desc())
    else:
        stmt = stmt.order_by(user_watchlist.c.date_added.desc())
    
    results = db.session.execute(stmt).all()
    
    watchlist_items = []
    for row in results:
        watchlist_items.append({
            'media_id': row.media_id,
            'media_type': row.media_type,
            'date_added': row.date_added.isoformat() if row.date_added else None,
            'priority': row.priority or 'medium'
        })
    
    return jsonify({
        'watchlist': watchlist_items,
        'count': len(watchlist_items)
    }), 200


@priorities_bp.route('/api/watchlist/stats', methods=['GET'])
@login_required
def get_watchlist_stats():
    """Get watchlist statistics by priority"""
    # Count items by priority
    stmt = text("""
        SELECT 
            COALESCE(priority, 'medium') as priority,
            COUNT(*) as count
        FROM user_watchlist
        WHERE user_id = :user_id
        GROUP BY COALESCE(priority, 'medium')
    """)
    
    results = db.session.execute(stmt, {'user_id': current_user.id}).all()
    
    stats = {
        'high': 0,
        'medium': 0,
        'low': 0,
        'total': 0
    }
    
    for row in results:
        priority = row[0]  # First column (priority)
        count = row[1]     # Second column (count)
        if priority in stats:
            stats[priority] = count
            stats['total'] += count
    
    return jsonify(stats), 200
