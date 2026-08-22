"""Core main routes: user profiles and stats dashboards.

Browse/discovery endpoints live in routes/browse.py; watchlist/wishlist/
viewed collection endpoints live in routes/collections.py. Both attach to
the same shared blueprint (routes/_main_bp.main) so all endpoint names
remain stable for url_for() calls across templates.

Importing them here ensures app.py's single `register_blueprint(main)`
picks up every route.
"""
from datetime import datetime

from flask import render_template
from flask_login import login_required, current_user

from models import db, User, UserFollow, Review
from routes._main_bp import main  # noqa: F401 — re-exported for app.py
from routes import browse  # noqa: F401 — registers discovery routes
from routes import collections  # noqa: F401 — registers collection routes


@main.route('/user/<int:user_id>')
@login_required
def user_profile(user_id):
    """View another user's public profile"""
    user = User.query.get_or_404(user_id)

    # Check if current user follows this user
    is_following = False
    if current_user.is_authenticated:
        follow = UserFollow.query.filter_by(
            follower_id=current_user.id,
            following_id=user_id,
            is_active=True
        ).first()
        is_following = follow is not None

    # Get user reviews
    recent_reviews = user.user_reviews.order_by(db.desc(Review.created_at)).limit(5).all()

    return render_template('user_profile.html',
                           user=user,
                           is_following=is_following,
                           reviews=recent_reviews)


@main.route('/stats')
@login_required
def stats_dashboard():
    """View personal statistics dashboard"""
    return render_template('stats_dashboard.html')


@main.route('/stats/year-in-review')
@main.route('/stats/year-in-review/<int:year>')
@login_required
def year_in_review(year=None):
    """View Year in Review for a specific year"""
    if year is None:
        year = datetime.now().year
    return render_template('year_in_review.html', year=year)
