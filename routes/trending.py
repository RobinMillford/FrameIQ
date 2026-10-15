"""F43: bound the trending queries and replace the per-user N+1 counts.

F43.

WHAT WAS WRONG
--------------
* ``get_trending_tags`` and ``get_trending_reviews`` passed a client-supplied
  ``limit`` straight into ``.limit()``. A large value asked for the whole
  table; a *negative* value was worse, because SQLite reads ``LIMIT -1`` as
  "no limit". ``get_trending_media`` already clamped, which is why only these
  three were exposed.
* ``get_trending_users`` loaded **every** user with ``.all()`` and then ran two
  COUNT queries per user in a Python loop — an N+1 over the whole user table,
  before the requested limit was applied at all. The limit was a slice of an
  already-computed list, so it bounded the response but not the work.
* Both list endpoints sorted only by a score, so equal scores came back in
  whatever order the database happened to produce.

WHAT CHANGED
------------
* Every limit goes through :func:`query_limits.size_arg` and is applied in the
  database, before rows are materialised.
* ``get_trending_users`` computes reviews, comments and likes in three
  grouped subqueries and ranks in SQL: one query instead of ``2N + 1``, and the
  limit is a SQL ``LIMIT``. The score formula and each count's filter (reviews
  and likes are not filtered on ``is_deleted``; comments are) are preserved
  exactly, so the numbers a client sees do not change.
* Equal scores now break ties on ascending user id, so the response is
  deterministic across calls and databases.
"""
from flask import Blueprint, request, jsonify
from models import db, MediaItem, Tag, User, Review, ReviewLike, MediaLike, MediaComment, UserMediaTag
from sqlalchemy import func, desc, and_
from datetime import datetime, timedelta

from utils.query_limits import LIST_LIMIT_MAX, size_arg

trending = Blueprint('trending', __name__)


def _limit(default: int) -> int:
    """A client-supplied limit, clamped to a sane, positive maximum."""
    return size_arg(request.args, 'limit', default, LIST_LIMIT_MAX)


@trending.route('/api/trending/media', methods=['GET'])
def get_trending_media():
    """Get trending movies and TV shows based on recent activity"""
    days = request.args.get('days', 7, type=int)
    media_type = request.args.get('type', None)  # 'movie', 'tv', or None for both
    # Cap the public limit so callers can't request arbitrarily large pages.
    limit = _limit(20)

    since_date = datetime.utcnow() - timedelta(days=days)

    # Score based on multiple factors:
    # - Number of likes
    # - Number of comments
    # - Number of reviews
    # - Number of tags added
    # - Recency bonus

    # Query to calculate trending scores
    base_query = db.session.query(
        MediaItem.id,
        MediaItem.tmdb_id,
        MediaItem.media_type,
        MediaItem.title,
        MediaItem.poster_path,
        func.count(MediaLike.id).label('like_count'),
    ).outerjoin(MediaLike, and_(
        MediaLike.media_id == MediaItem.tmdb_id,
        MediaLike.media_type == MediaItem.media_type,
        MediaLike.created_at >= since_date
    ))

    if media_type:
        base_query = base_query.filter(MediaItem.media_type == media_type)

    base_query = base_query.group_by(
        MediaItem.id, MediaItem.tmdb_id, MediaItem.media_type,
        MediaItem.title, MediaItem.poster_path
    )

    results = base_query.order_by(desc('like_count')).limit(limit).all()

    # Batch-count comments and reviews for all result items in two grouped
    # queries instead of one COUNT per item (N+1).
    item_ids = [item.id for item in results]
    comment_counts = {}
    review_counts = {}
    if item_ids:
        comment_rows = db.session.query(
            MediaComment.media_id,
            MediaComment.media_type,
            func.count(MediaComment.id).label('cnt'),
        ).filter(
            MediaComment.media_id.in_(item_ids),
            MediaComment.created_at >= since_date,
            MediaComment.is_deleted == False
        ).group_by(
            MediaComment.media_id, MediaComment.media_type
        ).all()
        comment_counts = {
            (row.media_id, row.media_type): row.cnt for row in comment_rows
        }

        review_rows = db.session.query(
            Review.media_id,
            Review.media_type,
            func.count(Review.id).label('cnt'),
        ).filter(
            Review.media_id.in_(item_ids),
            Review.is_deleted == False,
            Review.created_at >= since_date
        ).group_by(
            Review.media_id, Review.media_type
        ).all()
        review_counts = {
            (row.media_id, row.media_type): row.cnt for row in review_rows
        }

    # Enhance with additional data
    trending_items = []
    for item in results:
        comment_count = comment_counts.get((item.id, item.media_type), 0)
        review_count = review_counts.get((item.id, item.media_type), 0)

        # Calculate trending score
        score = (item.like_count * 2) + (comment_count * 3) + (review_count * 5)

        trending_items.append({
            'id': item.id,
            'tmdb_id': item.tmdb_id,
            'media_type': item.media_type,
            'title': item.title,
            'poster_path': item.poster_path,
            'score': score,
            'like_count': item.like_count,
            'comment_count': comment_count,
            'review_count': review_count
        })

    # Re-sort by calculated score. The tie-break on id keeps the order stable
    # across calls; the DB `limit` above already bounds the set, so the slice
    # below can only ever drop rows that ranked below the cutoff.
    trending_items.sort(key=lambda x: (-x['score'], x['id']))

    return jsonify({
        'success': True,
        'period_days': days,
        'media_type': media_type or 'all',
        'items': trending_items[:limit]
    })


@trending.route('/api/trending/tags', methods=['GET'])
def get_trending_tags():
    """Get trending tags based on recent usage"""
    days = request.args.get('days', 7, type=int)
    limit = _limit(20)

    since_date = datetime.utcnow() - timedelta(days=days)

    # Count recent tag usage
    trending_tags = db.session.query(
        Tag.id,
        Tag.name,
        Tag.usage_count,
        func.count(UserMediaTag.id).label('recent_usage')
    ).join(
        UserMediaTag, UserMediaTag.tag_id == Tag.id
    ).filter(
        UserMediaTag.created_at >= since_date
    ).group_by(
        Tag.id, Tag.name, Tag.usage_count
    ).order_by(
        desc('recent_usage'), Tag.id.asc()
    ).limit(limit).all()

    return jsonify({
        'success': True,
        'period_days': days,
        'tags': [{
            'id': tag.id,
            'name': tag.name,
            'total_usage': tag.usage_count,
            'recent_usage': tag.recent_usage
        } for tag in trending_tags]
    })


@trending.route('/api/trending/users', methods=['GET'])
def get_trending_users():
    """Get most active users based on recent activity.

    F43: the three activity counts are aggregated in grouped subqueries and
    the score is computed in SQL, so this is a single query with a SQL LIMIT
    instead of `2N + 1` queries over every user in the table.
    """
    days = request.args.get('days', 7, type=int)
    limit = _limit(20)

    since_date = datetime.utcnow() - timedelta(days=days)

    # Count activities: reviews, comments, likes. Each becomes one grouped
    # subquery. The filters match the previous per-user COUNTs exactly —
    # reviews and likes are not filtered on `is_deleted`, comments are — so
    # the counts a client receives are unchanged.
    review_counts = (
        db.session.query(
            Review.user_id.label('user_id'),
            func.count(Review.id).label('review_count'),
        ).filter(
            Review.created_at >= since_date
        ).group_by(
            Review.user_id
        ).subquery()
    )

    comment_counts = (
        db.session.query(
            MediaComment.user_id.label('user_id'),
            func.count(MediaComment.id).label('comment_count'),
        ).filter(
            MediaComment.created_at >= since_date,
            MediaComment.is_deleted == False
        ).group_by(
            MediaComment.user_id
        ).subquery()
    )

    like_counts = (
        db.session.query(
            MediaLike.user_id.label('user_id'),
            func.count(MediaLike.id).label('like_count'),
        ).filter(
            MediaLike.created_at >= since_date
        ).group_by(
            MediaLike.user_id
        ).subquery()
    )

    review_count = func.coalesce(review_counts.c.review_count, 0)
    comment_count = func.coalesce(comment_counts.c.comment_count, 0)
    like_count = func.coalesce(like_counts.c.like_count, 0)
    activity_score = review_count * 5 + comment_count * 3 + like_count * 1

    rows = db.session.query(
        User.id,
        User.username,
        User.profile_picture,
        User.followers_count,
        review_count.label('review_count'),
        comment_count.label('comment_count'),
        like_count.label('like_count'),
        activity_score.label('activity_score'),
    ).outerjoin(
        review_counts, review_counts.c.user_id == User.id
    ).outerjoin(
        comment_counts, comment_counts.c.user_id == User.id
    ).outerjoin(
        like_counts, like_counts.c.user_id == User.id
    ).order_by(
        desc(activity_score), User.id.asc()
    ).limit(limit).all()

    return jsonify({
        'success': True,
        'period_days': days,
        'users': [{
            'id': row.id,
            'username': row.username,
            'profile_picture': row.profile_picture,
            'followers_count': row.followers_count or 0,
            'activity_score': row.activity_score,
            'review_count': row.review_count,
            'comment_count': row.comment_count,
            'like_count': row.like_count
        } for row in rows]
    })


@trending.route('/api/trending/reviews', methods=['GET'])
def get_trending_reviews():
    """Get trending/popular reviews based on likes"""
    days = request.args.get('days', 30, type=int)
    limit = _limit(10)

    since_date = datetime.utcnow() - timedelta(days=days)

    # F43: `user` and `media` are batch-loaded rather than lazy-loaded per row
    # (two extra queries per review before).
    #
    # F44: `selectinload`, NOT `joinedload`. This query aggregates and groups by
    # `review.id` alone; `joinedload` folds `user` into this same SELECT, so
    # every `user` column lands in the projection while only `review.id` is
    # grouped. SQLite accepts that, PostgreSQL rejects it:
    #
    #     psycopg2.errors.GroupingError: column "user_1.id" must appear in
    #     the GROUP BY clause or be used in an aggregate function
    #
    # i.e. a 500 in production, which only the PostgreSQL run could find.
    # `selectinload` issues one extra `WHERE id IN (...)` statement for the
    # whole page instead, so the GROUP BY stays valid and the load is still
    # batched rather than per-row.
    trending_reviews = db.session.query(
        Review,
        func.count(ReviewLike.id).label('like_count')
    ).outerjoin(
        ReviewLike, ReviewLike.review_id == Review.id
    ).options(
        db.selectinload(Review.user),
        db.selectinload(Review.media),
    ).filter(
        Review.created_at >= since_date
    ).group_by(Review.id).order_by(
        desc('like_count'), Review.id.asc()
    ).limit(limit).all()

    reviews_data = []
    for review, like_count in trending_reviews:
        reviews_data.append({
            'id': review.id,
            'user_id': review.user_id,
            'username': review.user.username,
            'media_id': review.media_id,
            'media_type': review.media_type,
            'media_title': review.media.title if review.media else None,
            'rating': review.rating,
            'content': review.content,
            'created_at': review.created_at.isoformat(),
            'like_count': like_count
        })

    return jsonify({
        'success': True,
        'period_days': days,
        'reviews': reviews_data
    })


@trending.route('/api/trending/summary', methods=['GET'])
def get_trending_summary():
    """Get a summary of all trending data"""
    days = request.args.get('days', 7, type=int)

    # Get top 5 of each category
    trending_media_resp = get_trending_media()
    trending_tags_resp = get_trending_tags()
    trending_users_resp = get_trending_users()

    media_data = trending_media_resp.get_json()
    tags_data = trending_tags_resp.get_json()
    users_data = trending_users_resp.get_json()

    return jsonify({
        'success': True,
        'period_days': days,
        'summary': {
            'top_media': media_data['items'][:5],
            'top_tags': tags_data['tags'][:5],
            'top_users': users_data['users'][:5]
        }
    })
