"""Core main routes: user profiles and stats dashboards.

Browse/discovery endpoints live in routes/browse.py; watchlist/
viewed collection endpoints live in routes/collections.py. Both attach to
the same shared blueprint (routes/_main_bp.main) so all endpoint names
remain stable for url_for() calls across templates.

Importing them here ensures app.py's single `register_blueprint(main)`
picks up every route.

Feature #8 Phase 9 adds the ONE intentionally public statistics surface:
GET /share/year-in-review/<token> — a read-only, unauthenticated recap
page authorized solely by an opaque share token the owner explicitly
created (never automatic). The route validates the share record only,
then rebuilds the recap through the canonical builder; it never queries
the watch-history or media tables and never calls the statistics
service (§36/§37). Deleted/deactivated owners fail closed (§26).
"""
from datetime import datetime

from flask import abort, make_response, render_template
from flask_login import login_required, current_user

from extensions import limiter
import api.year_in_review as year_in_review_service
import api.year_in_review_share as year_in_review_share_service
from models import db, User, UserFollow, Review, YearInReviewShare
from routes._main_bp import main  # noqa: F401 — re-exported for app.py
from routes import browse  # noqa: F401 — registers discovery routes
from routes import collections  # noqa: F401 — registers collection routes

# §16: the public share page is internet-facing and read-only; a
# conservative shared-limit (rate limiter is disabled in tests).
PUBLIC_SHARE_RATE_LIMIT = "30 per minute"


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


@main.route('/share/year-in-review/<token>')
@limiter.limit(PUBLIC_SHARE_RATE_LIMIT)
def year_in_review_share_page(token):
    """Public Year-in-Review recap for one explicitly shared year (Phase 9).

    The opaque token IS the authorization: no authentication, no user/
    year parameters exist (§6/§25), and the token binds exactly one
    (user, year). Generic 404 for unknown, malformed, revoked, or
    deactivated-owner shares — never a distinction (§15/§26).

    Only the share authorization record is queried here; the recap is
    rebuilt through the canonical builder (one build → one statistics
    call → the service's 8 bounded statements). §17: no-store — the
    page reflects revocation instantly; correctness beats caching.
    """
    # Malformed tokens fail before any database work (§15: bounded,
    # fixed work per request).
    try:
        token_hash = year_in_review_share_service.hash_share_token(token)
    except ValueError:
        abort(404)

    share = YearInReviewShare.query.filter_by(token_hash=token_hash).first()
    if share is None or share.revoked_at is not None:
        abort(404)  # exists-but-revoked is indistinguishable from never

    # Authorization read must reflect committed state: select COLUMNS,
    # never entities — column tuples bypass the identity map, so a
    # session holding a cached owner row cannot serve stale active/
    # inactive state. Deleted/deactivated owners fail closed (§26).
    owner_state = db.session.query(User.id, User.is_active).filter(
        User.id == share.user_id).first()
    if owner_state is None or not owner_state.is_active:
        abort(404)

    # Live-share semantics (§5, documented in the model): the public page
    # shows the canonical recap for the token's year as of each view.
    # The builder validates the year again; an invalid stored year or a
    # year that became empty fails closed rather than fabricating a story.
    try:
        recap = year_in_review_service.build_year_in_review(
            share.user_id, share.year)
    except ValueError:
        abort(404)
    if not recap.get('available'):
        abort(404)

    try:
        public_model = year_in_review_share_service.build_public_year_in_review(
            recap)
    except ValueError:
        abort(404)

    response = make_response(render_template(
        'year_in_review_share.html',
        recap=public_model,
        months=year_in_review_service.MONTH_NAMES,
    ))
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Robots-Tag'] = 'noindex, nofollow'
    return response
