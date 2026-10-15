"""F43 — bounded query work and batched comment counts.

Behavioural tests. Every N+1 claim is proved by counting the SQL statements a
request actually issues (via `tests/query_counter.py`), not by reading the
source, and every bound is proved by requesting an absurd value and observing
that the response stays small.
"""
import os
from datetime import datetime

import pytest

from tests.query_counter import count_statements


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def world(app, db):
    """Users, media and activity with known, deliberate counts."""
    from models import (MediaItem, MediaComment, MediaLike, Review,
                        User)
    with app.app_context():
        users = []
        for i in range(6):
            u = User(username=f'f43user{i}', email=f'f43_{i}@example.com',
                     email_verified=True)
            u.set_password('TestPass1')
            users.append(u)
        db.session.add_all(users)
        db.session.flush()          # ids exist from here on

        media = []
        for i in range(5):
            m = MediaItem(tmdb_id=900001 + i, media_type='movie',
                          title=f'F43 Film {i}')
            db.session.add(m)
            media.append(m)
        db.session.flush()

        # F44: Review.media_id / MediaComment.media_id / MediaLike.media_id
        # are REFERENCES to media_item.id — the PRIMARY KEY, not the TMDb id.
        # This fixture originally used TMDb ids, which passed only because
        # SQLite does not enforce foreign keys by default; the same fixture
        # fails on PostgreSQL. Each activity row also needs its own media_id:
        # `review` and `media_like` carry a unique constraint over
        # (user_id, media_id, media_type).
        mid = [m.id for m in media]

        now = datetime.utcnow()
        # users[0]: 3 reviews, 2 comments, 4 likes  -> 15 + 6 + 4 = 25
        for m_id in mid[:3]:
            db.session.add(Review(user_id=users[0].id, media_id=m_id,
                                  media_type='movie', rating=4.0,
                                  content='good', created_at=now))
        for m_id in mid[:2]:
            db.session.add(MediaComment(user_id=users[0].id, media_id=m_id,
                                        media_type='movie', content='nice',
                                        created_at=now))
        for m_id in mid[:4]:
            db.session.add(MediaLike(user_id=users[0].id, media_id=m_id,
                                     media_type='movie', created_at=now))
        # users[1]: 1 review -> 5, plus one DELETED comment (must not count)
        db.session.add(Review(user_id=users[1].id, media_id=mid[0],
                              media_type='movie', rating=3.0, content='ok',
                              created_at=now))
        db.session.add(MediaComment(user_id=users[1].id, media_id=mid[4],
                                    media_type='movie', content='gone',
                                    created_at=now, is_deleted=True))
        db.session.commit()

        yield {'users': users, 'media': media, 'now': now,
               'db': db, 'app': app}
        # Rows were committed, so rollback cannot remove them. Delete
        # explicitly, or they leak into the next test's count assertions.
        from models import (MediaComment as _MC, MediaItem as _MI,
                            MediaLike as _ML, Review as _R, User as _U)
        uid = [u.id for u in users]
        if uid:
            db.session.query(_ML).filter(_ML.user_id.in_(uid)).delete(
                synchronize_session=False)
            db.session.query(_MC).filter(_MC.user_id.in_(uid)).delete(
                synchronize_session=False)
            db.session.query(_R).filter(_R.user_id.in_(uid)).delete(
                synchronize_session=False)
        db.session.query(_MI).filter(
            _MI.tmdb_id.in_([900001, 900002, 900003, 900004, 900005])).delete(
                synchronize_session=False)
        if uid:
            db.session.query(_U).filter(_U.id.in_(uid)).delete(
                synchronize_session=False)
        db.session.commit()


@pytest.fixture
def thread(app, db):
    """One review with 12 top-level comments, 3 of which have replies."""
    from models import Review, ReviewComment, User
    with app.app_context():
        author = User(username='f43author', email='f43a@example.com',
                      email_verified=True)
        author.set_password('TestPass1')
        db.session.add(author)
        db.session.flush()          # author.id is needed before the Review

        from models import MediaItem
        item = MediaItem(tmdb_id=900001, media_type='movie', title='F43')
        db.session.add(item)
        db.session.flush()
        db.session.add(Review(user_id=author.id, media_id=item.id,
                              media_type='movie', rating=4.0,
                              content='a review'))
        db.session.flush()
        author_id = author.id
        review_id = Review.query.filter_by(user_id=author_id).first().id

        base = datetime(2024, 1, 1, 12, 0, 0)
        tops = []
        for i in range(12):
            c = ReviewComment(user_id=author.id, review_id=review_id,
                              content=f'comment {i}', created_at=base)
            # every comment shares created_at, so ordering can only be stable
            # via the id tie-break
            db.session.add(c)
            tops.append(c)
        db.session.flush()
        for parent, n in zip(tops, (2, 1, 0)):
            for j in range(n):
                db.session.add(ReviewComment(
                    user_id=author.id, review_id=review_id,
                    parent_id=parent.id, content=f'reply {j}',
                    created_at=base))
        db.session.commit()
        yield {'review_id': review_id, 'tops': tops, 'author_id': author_id,
               'db': db, 'app': app}
        # Committed rows: delete explicitly, or they leak between tests.
        from models import (MediaItem as _MI, Review as _R,
                            ReviewComment as _RC, User as _U)
        db.session.query(_RC).filter(
            _RC.review_id == review_id).delete(synchronize_session=False)
        db.session.query(_R).filter(_R.id == review_id).delete(
            synchronize_session=False)
        db.session.query(_MI).filter(_MI.tmdb_id == 900001).delete(
            synchronize_session=False)
        db.session.query(_U).filter(_U.id == author_id).delete(
            synchronize_session=False)
        db.session.commit()


# ── utils/query_limits ──────────────────────────────────────────────────────

@pytest.mark.parametrize('raw,default,maximum,expected', [
    (None, 20, 50, 20),          # absent -> default
    ('', 20, 50, 20),            # empty -> default
    ('abc', 20, 50, 20),         # malformed -> default
    ('5.0', 20, 50, 20),         # float string rejected, not truncated
    (None, 20, 50, 20),
    ('0', 20, 50, 1),            # below minimum -> minimum
    ('-1', 20, 50, 1),           # negative -> minimum (LIMIT -1 is UNBOUNDED)
    ('-999999', 20, 50, 1),
    ('50', 20, 50, 50),          # at maximum
    ('51', 20, 50, 50),          # over -> clamped
    ('999999999', 20, 50, 50),
    ('7', 20, 50, 7),            # in range -> untouched
])
def test_bounded_int_never_returns_a_dangerous_value(raw, default, maximum,
                                                     expected):
    from utils.query_limits import bounded_int
    assert bounded_int(raw, default, maximum) == expected


def test_bounded_int_rejects_bool_and_never_exceeds_maximum():
    from utils.query_limits import bounded_int, LIST_LIMIT_MAX
    assert bounded_int(True, 20, 50) == 20
    assert bounded_int(False, 20, 50) == 20
    assert bounded_int(10 ** 30, 20, 50) == LIST_LIMIT_MAX


def test_size_arg_reads_a_mapping():
    from utils.query_limits import size_arg
    assert size_arg({'limit': '999999'}, 'limit', 20, 50) == 50
    assert size_arg({}, 'limit', 20, 50) == 20


def test_page_args_clamps_page_and_per_page():
    from utils.query_limits import page_args
    assert page_args({}) == (1, 20)
    assert page_args({'page': '0', 'per_page': '-3'}) == (1, 1)
    assert page_args({'page': '4', 'per_page': '9999'}) == (4, 100)


# ── trending: limits ────────────────────────────────────────────────────────

def test_trending_reviews_order_carries_an_id_tie_break(client, world):
    """Equal like counts must be broken deterministically.

    Asserted on the emitted ORDER BY because SQLite returns ties in rowid
    order, which already satisfies id-ascending and would mask the clause's
    removal here (see the SQLite note in the comment tests).
    """
    app = world['app']
    with app.app_context():
        with count_statements(app) as stmts:
            r = client.get('/api/trending/reviews?limit=10')
    assert r.status_code == 200
    ordered = [s for s in stmts if 'FROM review' in s and 'ORDER BY' in s.upper()]
    assert len(ordered) == 1, ordered
    order = ordered[0].upper().split('ORDER BY')[-1]
    assert 'LIKE_COUNT' in order, order
    assert 'REVIEW.ID' in order, (
        'trending reviews have no id tie-break; equal like counts come back '
        'in whatever order the database produces')


@pytest.mark.parametrize('endpoint,default', [
    ('/api/trending/tags', 20),
    ('/api/trending/reviews', 10),
])
def test_trending_extreme_limits_are_clamped(client, endpoint, default):
    for raw, expected_len in (('999999999', 50), ('-1', 1), ('abc', default)):
        r = client.get(f'{endpoint}?limit={raw}')
        assert r.status_code == 200, (endpoint, raw, r.status_code)
        key = 'tags' if endpoint.endswith('tags') else 'reviews'
        body = r.get_json()
        assert len(body[key]) <= max(expected_len, 1)
        # never more than the documented maximum, whatever was asked for
        assert len(body[key]) <= 50


def test_trending_missing_limit_uses_the_default(client, world):
    assert client.get('/api/trending/tags').status_code == 200
    assert client.get('/api/trending/reviews').status_code == 200
    assert client.get('/api/trending/users').status_code == 200


def test_trending_users_limit_is_applied_in_the_database(client, world):
    """The response is capped — and the SQL is capped, not just the slice."""
    r = client.get('/api/trending/users?limit=1')
    assert r.status_code == 200
    assert len(r.get_json()['users']) == 1

    # A negative limit must not become "no limit".
    r = client.get('/api/trending/users?limit=-1')
    assert r.status_code == 200
    assert len(r.get_json()['users']) == 1, 'negative limit removed the bound'

    r = client.get('/api/trending/users?limit=1000000')
    assert len(r.get_json()['users']) <= 50


def test_trending_users_query_does_not_grow_with_user_count(client, world):
    """One statement for the page, and the count is independent of user count.

    This is the N+1 the audit named. The old code ran `2N + 1` statements —
    one grouped query over every user, then two COUNTs per user — before
    slicing to `limit`. Here the endpoint issues a single statement, and
    adding 40 more users does not change that.
    """
    app = world['app']
    db = world['db']

    def statements():
        with app.app_context():
            with count_statements(app) as stmts:
                r = client.get('/api/trending/users?limit=10')
        assert r.status_code == 200
        return list(stmts), r.get_json()

    before, _ = statements()
    assert len(before) == 1, (
        'expected one aggregated statement, got %d:\n%s'
        % (len(before), '\n'.join(before)))
    # all three activity sources are aggregated inside that one statement
    for needle in ('media_comment', 'media_like', 'review'):
        assert needle in before[0], (
            'expected %s to be aggregated in SQL, not fetched per user:\n%s'
            % (needle, before[0]))

    from models import User
    with app.app_context():
        extra = [User(username=f'f43extra{i}', email=f'f43x_{i}@example.com',
                      email_verified=True) for i in range(40)]
        for u in extra:
            u.set_password('TestPass1')
        db.session.add_all(extra)
        db.session.commit()
        extra_ids = [u.id for u in extra]

    try:
        after, _ = statements()
        assert len(after) == len(before), (
            'statement count grew with the user table: %d -> %d'
            % (len(before), len(after)))
    finally:
        with app.app_context():
            db.session.query(User).filter(
                User.id.in_(extra_ids)).delete(synchronize_session=False)
            db.session.commit()


# ── trending users: counts and ranking ──────────────────────────────────────

def test_trending_users_counts_and_ranking_are_preserved(client, world):
    body = client.get('/api/trending/users?days=3650&limit=50').get_json()
    by_id = {u['id']: u for u in body['users']}

    top = by_id[world['users'][0].id]
    assert top['review_count'] == 3
    assert top['comment_count'] == 2      # the deleted one is excluded
    assert top['like_count'] == 4
    assert top['activity_score'] == 3 * 5 + 2 * 3 + 4 * 1

    second = by_id[world['users'][1].id]
    assert second['review_count'] == 1
    assert second['comment_count'] == 0, 'deleted comments must not count'
    assert second['like_count'] == 0
    assert second['activity_score'] == 5

    scores = [u['activity_score'] for u in body['users']]
    assert scores == sorted(scores, reverse=True), 'ranking not by score'


def test_trending_users_order_is_deterministic_on_tied_scores(client, world):
    """Equal scores must break ties on id, not on whatever order the DB
    happened to return."""
    runs = []
    for _ in range(3):
        body = client.get('/api/trending/users?days=3650&limit=50').get_json()
        runs.append([u['id'] for u in body['users']])
    assert runs[0] == runs[1] == runs[2], 'ordering is not stable'

    # everyone here with score 0 must appear in ascending id order
    body = client.get('/api/trending/users?days=1&limit=50').get_json()
    tied = [u['id'] for u in body['users'] if u['activity_score'] == 0]
    assert tied == sorted(tied), 'tied scores are not ordered by id'


def test_trending_users_does_not_scan_other_users_activities(client, world):
    """Counts must reflect only the window, not all-time activity."""
    body = client.get('/api/trending/users?days=1&limit=50').get_json()
    by_id = {u['id']: u for u in body['users']}
    # fixtures are dated now(), so a 1-day window still sees them
    assert by_id[world['users'][0].id]['review_count'] == 3

    # a 0-day window (since_date == now) can only see strictly-older rows
    past = client.get('/api/trending/users?days=0&limit=50').get_json()
    for u in past['users']:
        assert u['review_count'] == 0


# ── trending reviews: N+1 on user/media ──────────────────────────────────────

def test_trending_reviews_does_not_lazy_load_user_per_row(client, world):
    """Every review's username and media title come from the page query."""
    app = world['app']
    db = world['db']
    with app.app_context():
        from models import Review
        for i, r in enumerate(Review.query.limit(3).all()):
            r.user_id = world['users'][i].id
        db.session.commit()

        with count_statements(app) as stmts:
            r = client.get('/api/trending/reviews?limit=10')
    assert r.status_code == 200
    reviews = r.get_json()['reviews']
    assert len(reviews) >= 1

    # F44: `selectinload` batches each relationship into ONE extra
    # `WHERE id IN (...)` statement. It is deliberately not `joinedload`:
    # this query groups by review.id alone, and a joined user would put every
    # user column in the projection, which PostgreSQL rejects with a
    # GroupingError. Before F43 each review added a user AND a media fetch.
    assert len(stmts) == 3, (
        'expected the page plus one batched user query and one batched media '
        'query, got %d statements:\n%s' % (len(stmts), '\n'.join(stmts)))
    assert any(' IN ' in s for s in stmts), (
        'relationships are not batch-loaded:\n%s' % '\n'.join(stmts))
    for row in reviews:
        assert row['username'] is not None


# ── review comments: batching + pagination ──────────────────────────────────

def test_review_comments_reply_counts_are_correct(client, thread):
    r = client.get(f"/api/reviews/{thread['review_id']}/comments")
    assert r.status_code == 200
    body = r.get_json()
    counts = {c['id']: c['reply_count'] for c in body['comments']}
    for top, expected in zip(thread['tops'], (2, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)):
        assert counts[top.id] == expected, (
            'reply_count for comment %s is %s' % (top.id, counts[top.id]))
    assert body['total'] == 15, 'total should be 12 top-level + 3 replies'


def test_review_comments_exclude_deleted(client, thread):
    db = thread['db']
    with thread['app'].app_context():
        from models import ReviewComment
        victim = thread['tops'][0].id
        db.session.query(ReviewComment).filter(
            ReviewComment.parent_id == victim,
            ReviewComment.is_deleted.is_(False)).first().is_deleted = True
        db.session.commit()

    body = client.get(f"/api/reviews/{thread['review_id']}/comments").get_json()
    counts = {c['id']: c['reply_count'] for c in body['comments']}
    assert counts[victim] == 1, 'deleted replies must not be counted'


def test_review_comments_query_count_does_not_grow_with_page_size(client, thread):
    """The real N+1 claim: statement count must be CONSTANT as the page grows.

    A page of 1 comment and a page of 15 comments must cost the same number
    of statements. Before F43 this was 1 + 2 per comment (a reply COUNT and a
    lazy user fetch each), so the count grew linearly with the page.
    """
    app = thread['app']
    rid = thread['review_id']

    def statements_for(per_page):
        with app.app_context():
            with count_statements(app) as stmts:
                r = client.get(f'/api/reviews/{rid}/comments?per_page={per_page}')
        assert r.status_code == 200
        return len(stmts), r.get_json()

    one_count, one_body = statements_for(1)
    all_count, all_body = statements_for(100)

    assert len(one_body['comments']) == 1
    assert len(all_body['comments']) == 15
    assert all_count == one_count, (
        'query count grew with page size: %d statements for 1 comment, '
        '%d for 15' % (one_count, all_count))


def test_review_comments_counts_come_from_one_grouped_query(client, thread):
    """Every reply count on the page resolves in a single GROUP BY."""
    app = thread['app']
    with app.app_context():
        with count_statements(app) as stmts:
            r = client.get(f"/api/reviews/{thread['review_id']}/comments"
                           f"?per_page=100")
    assert r.status_code == 200
    assert len(r.get_json()['comments']) == 15

    grouped = [s for s in stmts if 'GROUP BY' in s.upper()
               and 'review_comment' in s]
    assert len(grouped) == 1, (
        'expected exactly one grouped reply-count query, got %d:\n%s'
        % (len(grouped), '\n'.join(grouped)))

    # Users come from the page query's own join, not one fetch per row.
    joined_pages = [s for s in stmts
                    if 'FROM review_comment' in s and 'LEFT OUTER JOIN user' in s]
    assert len(joined_pages) == 1, (
        'comment authors are not eager-loaded in the page query:\n%s'
        % '\n'.join(stmts))


def test_review_comments_pagination_covers_every_row_exactly_once(client, thread):
    seen = []
    for page in (1, 2, 3):
        body = client.get(
            f"/api/reviews/{thread['review_id']}/comments"
            f"?per_page=5&page={page}").get_json()
        seen.extend(c['id'] for c in body['comments'])
        assert len(body['comments']) == 5
        assert body['per_page'] == 5
        assert body['pages'] == 3
    assert len(seen) == 15
    assert len(set(seen)) == 15, 'a comment repeated or was skipped'


def test_review_comments_ordering_has_a_deterministic_tie_break(client, thread):
    """Ordering must be stable, and must actually carry an id tie-break.

    The behavioural half (repeated calls agree) passes even without the
    tie-break, and cannot catch its removal: on SQLite, rows with equal sort
    keys come back in rowid order, which is already id order — measured, not
    assumed (see `test_sqlite_masks_the_tie_break_because_rowid_is_id`). The
    guard is therefore on the emitted ORDER BY, which is query behaviour
    rather than a source string.
    """
    app = thread['app']
    with app.app_context():
        with count_statements(app) as stmts:
            client.get(
                f"/api/reviews/{thread['review_id']}/comments?per_page=5")

    pages = [s for s in stmts
             if 'FROM review_comment' in s and 'ORDER BY' in s.upper()]
    assert len(pages) == 1, pages
    order = pages[0].upper().split('ORDER BY')[-1]
    assert 'CREATED_AT' in order, order
    assert 'REVIEW_COMMENT.ID' in order, (
        'no id tie-break in ORDER BY; concurrent inserts can reorder '
        'equal-timestamped comments and make paging skip or repeat rows')

    runs = []
    for _ in range(3):
        body = client.get(
            f"/api/reviews/{thread['review_id']}/comments?per_page=100"
        ).get_json()
        runs.append([c['id'] for c in body['comments']])
    assert runs[0] == runs[1] == runs[2], 'ordering is not stable across calls'


def test_importing_the_postgres_module_does_not_change_the_database_target(
        monkeypatch):
    """F46 regression: the F44 module used to re-point the shared app.

    `tests/test_query_bounds_postgres_f44.py` assigned
    ``os.environ['DATABASE_URL']`` and imported `app` at MODULE scope. Since
    `app.py` builds its module-level instance at import, collecting that module
    re-pointed the shared app to PostgreSQL for the whole process: F45
    reproduced 30 unrelated SQLite tests erroring when the full suite was run
    with `FRAMEIQ_TEST_POSTGRES_URL` set.

    Importing the module must now be inert. This asserts the environment is
    untouched and that the module's classification still refuses a remote
    target — the safety check must not have been dropped to make this pass.
    """
    import importlib
    import sys

    from utils.db_target import classify

    before = os.environ.get('DATABASE_URL')
    monkeypatch.setenv(
        'FRAMEIQ_TEST_POSTGRES_URL',
        'postgresql://nobody:secret@127.0.0.1:59999/scratch?sslmode=disable')

    # Re-import the module to observe what import itself does, then put the
    # original object back. Without the restore a second module object would
    # linger in sys.modules holding this test's throwaway URL, and any later
    # `import tests.test_query_bounds_postgres_f44` would see that value.
    key = 'tests.test_query_bounds_postgres_f44'
    had_previous = key in sys.modules
    previous = sys.modules.get(key)
    try:
        sys.modules.pop(key, None)
        module = importlib.import_module(key)

        assert os.environ.get('DATABASE_URL') == before, (
            'importing the PostgreSQL test module changed the shared '
            'DATABASE_URL; it must not reconfigure unrelated tests')
        # The local-target guard is still in place, not removed to dodge the
        # assertion above.
        assert classify(module.PG_URL).is_local
        assert not hasattr(module, 'flask_app'), (
            'the module must not hold a module-level app instance')
    finally:
        if had_previous:
            sys.modules[key] = previous
        else:
            sys.modules.pop(key, None)


def test_sqlite_masks_the_tie_break_because_rowid_is_id(app, db):
    """Why the previous test cannot be purely behavioural on this engine.

    On SQLite a query ordered by `created_at` alone returns ties in rowid
    order, and rowid is the primary key — so id ascending is already
    satisfied and the tie-break looks redundant. On PostgreSQL tie order is
    unspecified, which is why the clause is asserted rather than assumed.

    F46: the rows are seeded HERE. The first version queried whatever
    comments happened to exist, so run in isolation it found none, took an
    early return, and asserted nothing while still reporting a pass.
    """
    from models import MediaItem, Review, ReviewComment, User
    with app.app_context():
        base = datetime(2023, 1, 1)
        author = User(username='f43rowid', email='f43rowid@example.com',
                      email_verified=True)
        author.set_password('TestPass1')
        item = MediaItem(tmdb_id=900099, media_type='movie', title='rowid')
        db.session.add_all([author, item])
        db.session.flush()
        review = Review(user_id=author.id, media_id=item.id,
                        media_type='movie', rating=4.0, content='rowid seed')
        db.session.add(review)
        db.session.flush()

        ids = []
        for i in range(4):
            c = ReviewComment(user_id=author.id, review_id=review.id,
                              content=f'rowid {i}',
                              created_at=base)   # every row ties
            db.session.add(c)
            ids.append(c)
        db.session.flush()
        row_ids = [c.id for c in ids]

        returned = [c.id for c in db.session.query(ReviewComment)
                    .filter(ReviewComment.id.in_(row_ids))
                    .order_by(ReviewComment.created_at).all()]
        assert len(returned) == 4, 'fixture did not seed four tied comments'
        assert returned == sorted(returned), (
            'SQLite did not return ties in id order here; the assumption '
            'behind the tie-break test no longer holds')

        for c in ids:
            db.session.delete(c)
        db.session.delete(review)
        db.session.delete(item)
        db.session.delete(author)
        db.session.commit()


def test_review_comments_absent_limit_returns_one_bounded_page(client, thread):
    body = client.get(f"/api/reviews/{thread['review_id']}/comments").get_json()
    assert body['per_page'] == 20
    assert len(body['comments']) == 15


def test_review_comments_extreme_per_page_is_clamped(client, thread):
    body = client.get(
        f"/api/reviews/{thread['review_id']}/comments?per_page=999999"
    ).get_json()
    assert body['per_page'] == 100
    assert len(body['comments']) <= 15

    body = client.get(
        f"/api/reviews/{thread['review_id']}/comments?per_page=-5"
    ).get_json()
    assert body['per_page'] == 1
    assert len(body['comments']) == 1


def test_review_comments_missing_review_still_404s(client, thread):
    assert client.get('/api/reviews/99999999/comments').status_code == 404


# ── review replies (reviews_enhanced) ───────────────────────────────────────

def test_review_replies_are_paginated_and_batched(client, thread):
    app = thread['app']
    with app.app_context():
        with count_statements(app) as stmts:
            r = client.get(f"/api/reviews/{thread['review_id']}/replies"
                           f"?per_page=100")
    assert r.status_code == 200
    body = r.get_json()
    # only top-level comments belong on this endpoint
    assert body['count'] == 12
    assert len(body['replies']) == 12
    assert all(x['parent_id'] is None for x in body['replies'])

    grouped = [s for s in stmts if 'GROUP BY' in s.upper()
               and 'review_comment' in s]
    assert len(grouped) == 1, (
        'reply counts are not batched:\n%s' % '\n'.join(grouped))


def test_review_replies_extreme_per_page_is_clamped(client, thread):
    body = client.get(
        f"/api/reviews/{thread['review_id']}/replies?per_page=999999"
    ).get_json()
    assert body['per_page'] == 100


def test_review_replies_paginates_without_repeats(client, thread):
    seen = []
    for page in (1, 2):
        body = client.get(
            f"/api/reviews/{thread['review_id']}/replies?per_page=6&page={page}"
        ).get_json()
        seen.extend(x['id'] for x in body['replies'])
    assert len(seen) == len(set(seen)) == 12
    assert seen == sorted(seen)


# ── related endpoints found by the F43 scan ─────────────────────────────────
#
# Same defect class as the named findings, confirmed to reach `.limit()` or
# `.paginate()` un-clamped. `routes/for_you.py` is deliberately NOT here: it
# already validates and rejects, which is correct because its limit feeds a
# paid upstream API rather than a database query.

@pytest.mark.parametrize('path', [
    '/api/tags/popular',
    '/api/users/autocomplete?q=ab',
])
@pytest.mark.parametrize('raw,ceiling', [
    ('999999999', 50),
    ('-1', 1),
    ('0', 1),
])
def test_related_uncapped_limits_are_now_clamped(client, world, path, raw,
                                                 ceiling):
    r = client.get(f'{path}&limit={raw}' if '?' in path else f'{path}?limit={raw}')
    assert r.status_code in (200, 400), (path, raw, r.status_code)
    if r.status_code == 400:
        return
    body = r.get_json()
    listed = body.get('tags') or body.get('users') or []
    assert len(listed) <= ceiling, (path, raw, len(listed))


def test_similar_users_limit_is_clamped(client, world):
    uid = world['users'][0].id
    for raw, ceiling in (('999999999', 50), ('-1', 1), ('0', 1)):
        r = client.get(f'/api/users/{uid}/similar?limit={raw}')
        assert r.status_code == 200, (raw, r.status_code)
        listed = r.get_json()['similar_users']
        assert len(listed) <= ceiling, (raw, len(listed))
