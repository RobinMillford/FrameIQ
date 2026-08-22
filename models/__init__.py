"""
FrameIQ models package.

All models are re-exported here so existing imports keep working unchanged:
    from models import db, User, MediaItem, WatchProgress, user_watchlist, ...

Submodules:
    base          — shared SQLAlchemy instance
    associations  — watchlist / wishlist / viewed junction tables
    user          — User, UserFollow
    media         — MediaItem
    reviews       — Review, ReviewLike, ReviewComment, ReviewHelpful
    lists         — UserList, ListCollaborator, ListCategory, UserListCategory,
                    ListAnalytics, ListView, UserListItem
    social        — DiaryEntry, Tag, UserMediaTag, MediaLike, MediaComment
    tv            — TVShowProgress, TVEpisodeWatch, UpcomingEpisode
    watch         — WatchProgress
"""

# db must be imported first so all modules share the same instance
from models.base import db                                    # noqa: F401

from models.associations import (                             # noqa: F401
    user_watchlist,
    user_wishlist,
    user_viewed,
)
from models.user import User, UserFollow                      # noqa: F401
from models.media import MediaItem                            # noqa: F401
from models.reviews import (                                  # noqa: F401
    Review,
    ReviewLike,
    ReviewComment,
    ReviewHelpful,
)
from models.lists import (                                    # noqa: F401
    UserList,
    ListCollaborator,
    ListCategory,
    UserListCategory,
    ListAnalytics,
    ListView,
    UserListItem,
)
from models.social import (                                   # noqa: F401
    DiaryEntry,
    Tag,
    UserMediaTag,
    MediaLike,
    MediaComment,
)
from models.tv import (                                       # noqa: F401
    TVShowProgress,
    TVEpisodeWatch,
    UpcomingEpisode,
)
from models.watch import WatchProgress                        # noqa: F401

__all__ = [
    'db',
    'user_watchlist', 'user_wishlist', 'user_viewed',
    'User', 'UserFollow',
    'MediaItem',
    'Review', 'ReviewLike', 'ReviewComment', 'ReviewHelpful',
    'UserList', 'ListCollaborator', 'ListCategory', 'UserListCategory',
    'ListAnalytics', 'ListView', 'UserListItem',
    'DiaryEntry', 'Tag', 'UserMediaTag', 'MediaLike', 'MediaComment',
    'TVShowProgress', 'TVEpisodeWatch', 'UpcomingEpisode',
    'WatchProgress',
]
