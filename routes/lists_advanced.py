"""
Week 2b Lists Features API Routes
Handles collaboration, categories, and analytics
"""
from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user
from models import db, UserList, ListCollaborator, ListCategory, UserListCategory, ListAnalytics, ListView, User
from datetime import datetime, timedelta
from sqlalchemy import func

lists_advanced = Blueprint('lists_advanced', __name__)


# ============================================================================
# LIST COLLABORATION
# ============================================================================

@lists_advanced.route('/api/lists/<int:list_id>/collaborators', methods=['GET'])
def get_collaborators(list_id):
    """Get all collaborators on a list"""
    user_list = UserList.query.get_or_404(list_id)
    
    # Check permissions
    if not user_list.is_public and (not current_user.is_authenticated or current_user.id != user_list.user_id):
        return jsonify({'error': 'Permission denied'}), 403
    
    collaborators = user_list.collaborators.all()
    
    return jsonify({
        'collaborators': [c.to_dict() for c in collaborators],
        'count': len(collaborators)
    }), 200


@lists_advanced.route('/api/lists/<int:list_id>/collaborators', methods=['POST'])
@login_required
def add_collaborator(list_id):
    """Add a collaborator to a list"""
    user_list = UserList.query.get_or_404(list_id)
    
    # Check ownership
    if user_list.user_id != current_user.id:
        return jsonify({'error': 'Only the list owner can add collaborators'}), 403
    
    data = request.get_json()
    username = data.get('username')
    role = data.get('role', 'editor')  # editor or viewer
    
    if not username:
        return jsonify({'error': 'username is required'}), 400
    
    if role not in ['editor', 'viewer']:
        return jsonify({'error': 'role must be "editor" or "viewer"'}), 400
    
    # Find user
    user = User.query.filter_by(username=username).first()
    if not user:
        return jsonify({'error': f'User "{username}" not found'}), 404
    
    # Check if already a collaborator
    existing = ListCollaborator.query.filter_by(
        list_id=list_id,
        user_id=user.id
    ).first()
    
    if existing:
        return jsonify({'error': f'{username} is already a collaborator'}), 400
    
    # Don't add owner as collaborator
    if user.id == user_list.user_id:
        return jsonify({'error': 'List owner is already a collaborator'}), 400
    
    try:
        collaborator = ListCollaborator(
            list_id=list_id,
            user_id=user.id,
            role=role,
            added_by=current_user.id
        )
        
        db.session.add(collaborator)
        db.session.commit()
        
        return jsonify({
            'message': f'Added {username} as {role}',
            'collaborator': collaborator.to_dict()
        }), 201
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@lists_advanced.route('/api/lists/<int:list_id>/collaborators/<int:user_id>', methods=['DELETE'])
@login_required
def remove_collaborator(list_id, user_id):
    """Remove a collaborator from a list"""
    user_list = UserList.query.get_or_404(list_id)
    
    # Check ownership
    if user_list.user_id != current_user.id:
        return jsonify({'error': 'Only the list owner can remove collaborators'}), 403
    
    collaborator = ListCollaborator.query.filter_by(
        list_id=list_id,
        user_id=user_id
    ).first_or_404()
    
    try:
        username = collaborator.user.username
        db.session.delete(collaborator)
        db.session.commit()
        
        return jsonify({'message': f'Removed {username} from collaborators'}), 200
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@lists_advanced.route('/api/lists/<int:list_id>/collaborators/<int:user_id>/role', methods=['PUT'])
@login_required
def update_collaborator_role(list_id, user_id):
    """Update a collaborator's role"""
    user_list = UserList.query.get_or_404(list_id)
    
    # Check ownership
    if user_list.user_id != current_user.id:
        return jsonify({'error': 'Only the list owner can update roles'}), 403
    
    data = request.get_json()
    new_role = data.get('role')
    
    if new_role not in ['editor', 'viewer']:
        return jsonify({'error': 'role must be "editor" or "viewer"'}), 400
    
    collaborator = ListCollaborator.query.filter_by(
        list_id=list_id,
        user_id=user_id
    ).first_or_404()
    
    try:
        collaborator.role = new_role
        db.session.commit()
        
        return jsonify({
            'message': f'Updated role to {new_role}',
            'collaborator': collaborator.to_dict()
        }), 200
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


def can_edit_list(user_list, user):
    """Helper: Check if user can edit a list"""
    if not user.is_authenticated:
        return False
    
    # Owner can always edit
    if user_list.user_id == user.id:
        return True
    
    # Check if user is an editor collaborator
    collaborator = ListCollaborator.query.filter_by(
        list_id=user_list.id,
        user_id=user.id,
        role='editor'
    ).first()
    
    return collaborator is not None


# ============================================================================
# LIST CATEGORIES
# ============================================================================

@lists_advanced.route('/api/categories', methods=['GET'])
def get_all_categories():
    """Get all available list categories"""
    categories = ListCategory.query.order_by(ListCategory.usage_count.desc()).all()
    
    return jsonify({
        'categories': [c.to_dict() for c in categories],
        'count': len(categories)
    }), 200


@lists_advanced.route('/api/lists/<int:list_id>/categories', methods=['GET'])
def get_list_categories(list_id):
    """Get categories for a specific list"""
    user_list = UserList.query.get_or_404(list_id)
    
    # Check permissions
    if not user_list.is_public and (not current_user.is_authenticated or current_user.id != user_list.user_id):
        return jsonify({'error': 'Permission denied'}), 403
    
    list_cats = user_list.list_categories.all()
    categories = [lc.category.to_dict() for lc in list_cats]
    
    return jsonify({
        'categories': categories,
        'count': len(categories)
    }), 200


@lists_advanced.route('/api/lists/<int:list_id>/categories', methods=['POST'])
@login_required
def add_category_to_list(list_id):
    """Add a category to a list"""
    user_list = UserList.query.get_or_404(list_id)
    
    # Check edit permissions
    if not can_edit_list(user_list, current_user):
        return jsonify({'error': 'You do not have permission to edit this list'}), 403
    
    data = request.get_json()
    category_id = data.get('category_id')
    
    if not category_id:
        return jsonify({'error': 'category_id is required'}), 400
    
    category = ListCategory.query.get_or_404(category_id)
    
    # Check if already added
    existing = UserListCategory.query.filter_by(
        list_id=list_id,
        category_id=category_id
    ).first()
    
    if existing:
        return jsonify({'error': 'Category already added to this list'}), 400
    
    try:
        list_category = UserListCategory(
            list_id=list_id,
            category_id=category_id
        )
        
        # Update usage count
        category.usage_count += 1
        
        db.session.add(list_category)
        db.session.commit()
        
        return jsonify({
            'message': f'Added category "{category.name}"',
            'category': category.to_dict()
        }), 201
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@lists_advanced.route('/api/lists/<int:list_id>/categories/<int:category_id>', methods=['DELETE'])
@login_required
def remove_category_from_list(list_id, category_id):
    """Remove a category from a list"""
    user_list = UserList.query.get_or_404(list_id)
    
    # Check edit permissions
    if not can_edit_list(user_list, current_user):
        return jsonify({'error': 'You do not have permission to edit this list'}), 403
    
    list_category = UserListCategory.query.filter_by(
        list_id=list_id,
        category_id=category_id
    ).first_or_404()
    
    try:
        category = list_category.category
        
        # Update usage count
        if category.usage_count > 0:
            category.usage_count -= 1
        
        db.session.delete(list_category)
        db.session.commit()
        
        return jsonify({'message': f'Removed category "{category.name}"'}), 200
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@lists_advanced.route('/api/categories/search', methods=['GET'])
def search_categories():
    """Search categories by name"""
    query = request.args.get('q', '').strip()
    
    if not query:
        return jsonify({'categories': []}), 200
    
    categories = ListCategory.query.filter(
        ListCategory.name.ilike(f'%{query}%')
    ).order_by(ListCategory.usage_count.desc()).limit(10).all()
    
    return jsonify({
        'categories': [c.to_dict() for c in categories]
    }), 200


# ============================================================================
# LIST ANALYTICS
# ============================================================================

@lists_advanced.route('/api/lists/<int:list_id>/analytics', methods=['GET'])
@login_required
def get_list_analytics(list_id):
    """Get analytics for a list"""
    user_list = UserList.query.get_or_404(list_id)
    
    # Only owner and collaborators can see analytics
    is_owner = user_list.user_id == current_user.id
    is_collaborator = ListCollaborator.query.filter_by(
        list_id=list_id,
        user_id=current_user.id
    ).first() is not None
    
    if not (is_owner or is_collaborator):
        return jsonify({'error': 'Permission denied'}), 403
    
    # Get or create analytics
    analytics = user_list.analytics
    if not analytics:
        analytics = ListAnalytics(list_id=list_id)
        db.session.add(analytics)
        db.session.commit()
    
    # Get recent views
    recent_views = ListView.query.filter_by(list_id=list_id)\
        .order_by(ListView.viewed_at.desc())\
        .limit(10)\
        .all()
    
    recent_viewers = []
    for view in recent_views:
        if view.user:
            recent_viewers.append({
                'username': view.user.username,
                'viewed_at': view.viewed_at.isoformat()
            })
        else:
            recent_viewers.append({
                'username': 'Anonymous',
                'viewed_at': view.viewed_at.isoformat()
            })
    
    return jsonify({
        'analytics': analytics.to_dict(),
        'recent_viewers': recent_viewers
    }), 200


@lists_advanced.route('/api/lists/<int:list_id>/view', methods=['POST'])
def track_list_view(list_id):
    """Track a view on a list (called when someone views the list)"""
    user_list = UserList.query.get_or_404(list_id)
    
    # Get or create analytics
    analytics = user_list.analytics
    if not analytics:
        analytics = ListAnalytics(list_id=list_id)
        db.session.add(analytics)
    
    try:
        # Create view record
        user_id = current_user.id if current_user.is_authenticated else None
        ip_address = request.remote_addr
        
        # Check if this user/IP already viewed recently (within 1 hour)
        one_hour_ago = datetime.utcnow() - timedelta(hours=1)
        
        recent_view = ListView.query.filter(
            ListView.list_id == list_id,
            ListView.viewed_at > one_hour_ago
        )
        
        if user_id:
            recent_view = recent_view.filter(ListView.user_id == user_id)
        else:
            recent_view = recent_view.filter(ListView.ip_address == ip_address)
        
        recent_view = recent_view.first()
        
        if not recent_view:
            # New view
            view = ListView(
                list_id=list_id,
                user_id=user_id,
                ip_address=ip_address
            )
            db.session.add(view)
            
            # Update analytics
            analytics.view_count += 1
            analytics.last_viewed = datetime.utcnow()
            
            # Update unique viewers count
            if user_id:
                unique_count = db.session.query(func.count(func.distinct(ListView.user_id)))\
                    .filter(ListView.list_id == list_id, ListView.user_id.isnot(None))\
                    .scalar()
                analytics.unique_viewers = unique_count
        
        db.session.commit()
        
        return jsonify({'message': 'View tracked'}), 200
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@lists_advanced.route('/api/lists/<int:list_id>/share', methods=['POST'])
@login_required
def track_list_share(list_id):
    """Track when a list is shared"""
    user_list = UserList.query.get_or_404(list_id)
    
    # Get or create analytics
    analytics = user_list.analytics
    if not analytics:
        analytics = ListAnalytics(list_id=list_id)
        db.session.add(analytics)
    
    try:
        analytics.share_count += 1
        db.session.commit()
        
        return jsonify({
            'message': 'Share tracked',
            'share_count': analytics.share_count
        }), 200
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


# ============================================================================
# LIST DISCOVERY WITH CATEGORIES
# ============================================================================

@lists_advanced.route('/api/lists/discover/category/<int:category_id>', methods=['GET'])
def discover_by_category(category_id):
    """Discover lists by category"""
    category = ListCategory.query.get_or_404(category_id)
    
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    
    # Get lists with this category
    lists_query = db.session.query(UserList).join(UserListCategory).filter(
        UserListCategory.category_id == category_id,
        UserList.is_public
    ).order_by(UserList.created_at.desc())
    
    # Paginate
    pagination = lists_query.paginate(page=page, per_page=per_page, error_out=False)  # type: ignore
    
    return jsonify({
        'category': category.to_dict(),
        'lists': [user_list.to_dict() for user_list in pagination.items],
        'total': pagination.total,
        'pages': pagination.pages,
        'current_page': page,
        'has_next': pagination.has_next,
        'has_prev': pagination.has_prev
    }), 200
