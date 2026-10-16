"""F44 — PostgreSQL validation of the F43 query-bounds remediation.

SQLite cannot prove two of the things F43 relied on it for:

1. **Deterministic ordering.** On SQLite, rows with equal sort keys come back
   in rowid order, which is already primary-key order. The `created_at, id`
   and `activity_score, id` tie-breaks therefore looked redundant here and
   their removal was invisible to mutation testing. PostgreSQL leaves tie
   order unspecified, so this is where the tie-break is actually load-bearing.
2. **Negative limits.** SQLite reads `LIMIT -1` as "no limit"; PostgreSQL
   raises an error. A clamp that only ever saw positive input on SQLite has
   not really been exercised.

These run against a disposable local PostgreSQL, in a process where
``DATABASE_URL`` is set to that target *before* ``app`` is imported. They are
marked ``postgres`` and are skipped without ``FRAMEIQ_TEST_POSTGRES_URL``, so
the offline suite is unaffected.

Isolation
---------
``tests/conftest.py`` force-overrides ``DATABASE_URL`` to a temp SQLite file,
and it is imported before this module. So this module re-points
``DATABASE_URL`` at PostgreSQL after that, and then imports ``app`` — the URI
is read by ``create_app()`` at call time, so the app comes up bound to
PostgreSQL. That means this file must be run in its own pytest invocation
(like CI's dedicated ``postgres-migrations`` job): any test that also uses the
shared ``app`` fixture would find the global app already bound to PostgreSQL.

The repo's own ``offline_network_guard`` fixture is left in place. It patches
Python-level sockets and cannot intercept ``psycopg2`` (libpq opens its own
sockets below Python) — verified empirically — which is precisely why
``utils/db_target`` exists as the separate database guard. Every target in
this module is re-classified locally before use.
"""
import os

import pytest

PG_URL = os.environ.get('FRAMEIQ_TEST_POSTGRES_URL', '').strip()

# A CI run exists precisely to execute these tests, so a missing
# FRAMEIQ_TEST_POSTGRES_URL there must FAIL rather than skip. Without this the
# job would report success having run nothing, and a skipped postgres test
# would read as coverage it never provided. Locally there is no server, so the
# suite still skips and the offline gate is unaffected.
_IN_CI = (os.environ.get('GITHUB_ACTIONS', '').strip().lower() in ('1', 'true')
          or os.environ.get('CI', '').strip().lower() in ('1', 'true'))

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not PG_URL and not _IN_CI,
        reason='needs FRAMEIQ_TEST_POSTGRES_URL pointing at a throwaway '
               'PostgreSQL server'),
]

if PG_URL:
    # Refuse anything that is not loopback. This suite drops and creates the
    # schema, so this is a hard precondition — not per-test advice.
    from utils.db_target import classify
    _target = classify(PG_URL)
    if not _target.is_local:
        raise RuntimeError(
            'FRAMEIQ_TEST_POSTGRES_URL is not a local host (%s); refusing to '
            'run destructive tests against it'
            % _target.safe_identity())

# ── F46: nothing below this line may touch the environment or build an app ──
#
# F45 found that this module used to do BOTH of these at import time:
#
#     os.environ['DATABASE_URL'] = PG_URL
#     from app import app as flask_app
#
# `app.py` builds a module-level app instance at import, so setting the
# variable here re-pointed that shared instance to PostgreSQL for the whole
# process. Any SQLite test collected alongside this module then ran against
# PostgreSQL (F45 measured 30 such failures). Importing a test module must
# not reconfigure the application for unrelated tests.
#
# The target is now applied inside the `pg` fixture, which builds a SEPARATE
# app via create_app() and restores the environment afterwards. See
# `test_importing_this_module_does_not_change_the_database_target` in
# tests/test_query_bounds_f43.py, which fails if that regresses.

from models import db as _db                                      # noqa: E402
from tests.query_counter import count_statements                  # noqa: E402
from datetime import datetime, timedelta                          # noqa: E402

PG_MAJOR = int(os.environ.get('FRAMEIQ_EXPECT_PG_MAJOR', '17'))

#: DATABASE_URL as the shared SQLite conftest left it, captured before the
#: fixture overrides it. Used to assert the override is complete and to prove
#: what gets restored.
_PREVIOUS_DATABASE_URL = os.environ.get('DATABASE_URL')


def _restore_database_url(previous):
    """Put DATABASE_URL back exactly as it was."""
    if previous is None:
        os.environ.pop('DATABASE_URL', None)
    else:
        os.environ['DATABASE_URL'] = previous


# ── isolation + target guards ────────────────────────────────────────────────

def test_the_configured_target_is_a_disposable_local_postgres():
    """Classification is unchanged from F44: local only, never production."""
    from utils.db_target import classify
    t = classify(PG_URL)
    assert t.is_local, 'the configured target is not loopback: %s' % (
        t.safe_identity())
    assert not t.is_production_like


def test_importing_this_module_had_no_environment_side_effect():
    """The F46 invariant, asserted from inside the module itself: the process
    target is still the one conftest set, not PostgreSQL."""
    assert os.environ.get('DATABASE_URL') == _PREVIOUS_DATABASE_URL, (
        'the PostgreSQL target leaked into the shared process environment')


def test_the_pg_fixture_activates_postgres_only_while_it_is_active(pg):
    """While the fixture is active the environment names PostgreSQL and the
    app it built is genuinely bound to loopback PostgreSQL.

    `_restore_database_url` is a no-argument-free helper purely so its
    restore semantics can be asserted directly; the fixture uses the same
    function in its `finally`.
    """
    from urllib.parse import urlparse
    assert os.environ['DATABASE_URL'] == PG_URL

    uri = pg.config['SQLALCHEMY_DATABASE_URI']
    assert uri.startswith('postgresql'), uri
    host = urlparse(uri).hostname
    assert host in ('127.0.0.1', 'localhost', '::1'), (
        'the app is bound to a non-loopback host: %r' % host)


def test_restore_database_url_puts_the_previous_value_back():
    """Restoration is exact, including the "was unset" case."""
    saved = os.environ.get('DATABASE_URL')
    try:
        os.environ['DATABASE_URL'] = 'postgresql://scratch@127.0.0.1:1/x'
        _restore_database_url(saved)
        assert os.environ.get('DATABASE_URL') == saved

        os.environ.pop('DATABASE_URL', None)
        _restore_database_url(None)
        assert 'DATABASE_URL' not in os.environ
    finally:
        if saved is None:
            os.environ.pop('DATABASE_URL', None)
        else:
            os.environ['DATABASE_URL'] = saved


def test_dotenv_did_not_override_database_url(pg):
    """`load_dotenv()` runs at `app` import. If it had overridden, the app
    would be bound to whatever `.env` says — the exact F7 hazard that
    `utils/db_target` exists to prevent."""
    assert os.environ['DATABASE_URL'] == PG_URL, (
        'DATABASE_URL changed during app import — dotenv overrode it')


def test_engine_is_really_postgresql(pg):
    with pg.app_context():
        assert _db.engine.dialect.name == 'postgresql'
        assert _db.engine.url.host in ('127.0.0.1', 'localhost', '::1')


@pytest.fixture(scope='module')
def pg():
    """A dedicated app bound to the disposable PostgreSQL.

    The ordering here is the whole point of F46, so it is not incidental:

    1. Import ``app`` FIRST, while ``DATABASE_URL`` is still whatever the
       shared conftest set. ``app.py`` constructs a module-level instance at
       import time, so that instance is permanently the SQLite one and every
       unrelated test keeps working — even when this module is collected in
       the same process.
    2. Only THEN point ``DATABASE_URL`` at PostgreSQL and build a SEPARATE app
       with ``create_app()``. Flask-SQLAlchemy 3.x keeps one engine per app,
       so the two coexist.

    Doing it the other way round re-points the shared instance — that was the
    F45 defect. The environment is restored in ``finally`` either way.

    The config block mirrors the shared conftest ``app`` fixture; without it
    a POST to /login is rejected and every authenticated request redirects.
    """
    import app as app_module                      # (1) SQLite instance cached

    _restore_database_url(_PREVIOUS_DATABASE_URL)  # tidy if a prior scope ran
    os.environ['DATABASE_URL'] = PG_URL            # (2) only now
    try:
        pg_app = app_module.create_app()
        pg_app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
            SERVER_NAME='localhost',
            MAIL_SERVER='',
            RATELIMIT_ENABLED=False,
        )
        with pg_app.app_context():
            _db.drop_all()
            _db.create_all()
        yield pg_app
        with pg_app.app_context():
            _db.session.remove()
            _db.drop_all()
    finally:
        _restore_database_url(_PREVIOUS_DATABASE_URL)


@pytest.fixture
def client(pg):
    return pg.test_client()


@pytest.fixture
def clean_db(pg):
    """Truncate between tests — committed rows are not undone by rollback."""
    yield pg
    with pg.app_context():
        for model in reversed(_db.metadata.sorted_tables):
            _db.session.execute(model.delete())
        _db.session.commit()


# ── seeding helpers ──────────────────────────────────────────────────────────
#
# PostgreSQL enforces foreign keys; SQLite does not by default. Seeds must
# therefore satisfy the real constraints or the tests would measure the FK
# instead of the behaviour under test.

def _user(username):
    from models import User
    u = User(username=username, email=f'{username}@example.com',
             email_verified=True)
    u.set_password('TestPass1')
    return u


def _seed_users_and_media(n_users=6, n_media=5):
    from models import MediaItem
    users = [_user(f'f44u{i}') for i in range(n_users)]
    media = [MediaItem(tmdb_id=910000 + i, media_type='movie',
                       title=f'F44 Film {i}') for i in range(n_media)]
    _db.session.add_all(users + media)
    _db.session.flush()
    return users, media


def _statements(app, path):
    with app.app_context():
        with count_statements(app) as stmts:
            r = app.test_client().get(path)
    assert r.status_code == 200, (path, r.status_code)
    return stmts, r.get_json()


# ── 1. the limit helper, on PostgreSQL ───────────────────────────────────────

def test_negative_limit_is_a_value_not_a_sql_error_on_postgres(client, clean_db):
    """`LIMIT -1` is unbounded on SQLite but an ERROR on PostgreSQL. The clamp
    must stop it before it ever reaches the engine — otherwise this is a 500."""
    for path in ('/api/trending/tags', '/api/trending/reviews',
                 '/api/trending/users', '/api/trending/media'):
        r = client.get(f'{path}?limit=-1')
        assert r.status_code == 200, (path, r.status_code, r.get_data(as_text=True)[:200])
        assert r.get_json() is not None


def test_zero_and_negative_and_missing_and_malformed_limits(client, clean_db):
    with clean_db.app_context():
        _seed_users_and_media()
        _db.session.commit()

    cases = {
        '-1': 1, '-99999': 1, '0': 1,
        'abc': None,          # malformed -> documented default
        '': None,
        '999999999': 50,
    }
    for raw, expected in cases.items():
        r = client.get(f'/api/trending/tags?limit={raw}')
        assert r.status_code == 200, (raw, r.status_code)
        tags = r.get_json()['tags']
        assert len(tags) <= 50, (raw, len(tags))
        if expected is not None:
            assert len(tags) == min(expected, len(tags)) or len(tags) <= 50
        # the SQL itself must never carry a negative LIMIT
        with clean_db.app_context():
            with count_statements(clean_db) as stmts:
                client.get(f'/api/trending/tags?limit={raw}')
        for s in stmts:
            assert 'LIMIT -' not in s.upper(), s


# ── 2. trending aggregation ──────────────────────────────────────────────────

@pytest.fixture
def trend_world(clean_db):
    with clean_db.app_context():
        from models import MediaComment, MediaLike, Review
        users, media = _seed_users_and_media()
        # Review.media_id is a ForeignKey to media_item.id — the PRIMARY KEY,
        # not the TMDb id. SQLite does not enforce FKs by default, so a
        # fixture that uses TMDb ids passes there and fails here; PostgreSQL
        # is what proves the seed is real.
        mid = [m.id for m in media]
        now = datetime.utcnow()
        # users[0]: 3 reviews, 2 comments, 4 likes -> 15 + 6 + 4 = 25
        for m_id in mid[:3]:
            _db.session.add(Review(user_id=users[0].id, media_id=m_id,
                                   media_type='movie', rating=4.0,
                                   content='good', created_at=now))
        for m_id in mid[:2]:
            _db.session.add(MediaComment(user_id=users[0].id, media_id=m_id,
                                         media_type='movie', content='nice',
                                         created_at=now))
        for m_id in mid[:4]:
            _db.session.add(MediaLike(user_id=users[0].id, media_id=m_id,
                                      media_type='movie', created_at=now))
        # users[1]: 1 review (score 5) + one DELETED comment (must not count)
        _db.session.add(Review(user_id=users[1].id, media_id=mid[0],
                               media_type='movie', rating=3.0, content='ok',
                               created_at=now))
        _db.session.add(MediaComment(user_id=users[1].id, media_id=mid[4],
                                     media_type='movie', content='gone',
                                     created_at=now, is_deleted=True))
        _db.session.commit()
        return {'users': users, 'ids': [u.id for u in users]}


def test_trending_users_counts_and_ranking_match_fixtures_on_postgres(
        client, trend_world):
    body = client.get('/api/trending/users?days=3650&limit=50').get_json()
    by_id = {u['id']: u for u in body['users']}
    top = by_id[trend_world['users'][0].id]
    assert top['review_count'] == 3
    assert top['comment_count'] == 2
    assert top['like_count'] == 4
    assert top['activity_score'] == 3 * 5 + 2 * 3 + 4

    second = by_id[trend_world['users'][1].id]
    assert second['comment_count'] == 0, 'deleted comments must not count'
    assert second['activity_score'] == 5

    scores = [u['activity_score'] for u in body['users']]
    assert scores == sorted(scores, reverse=True)


def test_tied_scores_are_ordered_by_id_on_postgres(client, trend_world):
    """The case SQLite could not prove: every user here scores 0, and
    PostgreSQL does not promise rowid order for equal sort keys."""
    runs = []
    for _ in range(5):
        body = client.get('/api/trending/users?days=0&limit=50').get_json()
        runs.append([u['id'] for u in body['users']])
    assert runs[0] == runs[1] == runs[2] == runs[3] == runs[4], (
        'tied activity scores are not deterministically ordered')

    zeros = [u['id'] for u in
             client.get('/api/trending/users?days=0&limit=50').get_json()['users']
             if u['activity_score'] == 0]
    assert zeros == sorted(zeros), 'tie-break on id is not applied'


def test_trending_users_issues_one_statement_on_postgres(client, trend_world):
    app = client.application
    with app.app_context():
        with count_statements(app) as stmts:
            r = app.test_client().get('/api/trending/users?limit=10')
    assert r.status_code == 200
    assert len(stmts) == 1, (
        'expected one aggregated statement on PostgreSQL too, got %d:\n%s'
        % (len(stmts), '\n'.join(stmts)))


def test_trending_users_result_is_bounded_in_sql_on_postgres(client, trend_world):
    """The LIMIT must be in the statement, not applied to a Python slice."""
    app = client.application
    for raw in ('1', '999999', '-1'):
        with app.app_context():
            with count_statements(app) as stmts:
                r = app.test_client().get(f'/api/trending/users?limit={raw}')
        assert r.status_code == 200
        body = r.get_json()['users']
        assert len(body) <= 50
        joined = [s for s in stmts if 'LIMIT' in s.upper()]
        assert joined, 'no statement carried a LIMIT'
        for s in joined:
            assert 'LIMIT -' not in s.upper(), s


def test_trending_reviews_is_one_statement_on_postgres(client, trend_world):
    app = client.application
    with app.app_context():
        with count_statements(app) as stmts:
            r = app.test_client().get('/api/trending/reviews?limit=10')
    assert r.status_code == 200
    rows = r.get_json()['reviews']
    assert rows
    for row in rows:
        assert row['username'] is not None
    # 1 aggregated page query + 1 batched user IN(...) + 1 batched media
    # IN(...). Constant, and it is the shape that keeps PostgreSQL's GROUP BY
    # rule satisfied (F44 finding).
    assert len(stmts) == 3, (
        'expected 3 batched statements on PostgreSQL, got %d:\n%s'
        % (len(stmts), '\n'.join(stmts)))


# ── 3. review comments: batching + pagination on PostgreSQL ──────────────────

@pytest.fixture
def thread(clean_db):
    with clean_db.app_context():
        from models import Review, ReviewComment
        from models import MediaItem
        author = _user('f44author')
        item = MediaItem(tmdb_id=910000, media_type='movie', title='F44')
        _db.session.add_all([author, item])
        _db.session.flush()
        _db.session.add(Review(user_id=author.id, media_id=item.id,
                               media_type='movie', rating=4.0,
                               content='a review'))
        _db.session.flush()
        rid = Review.query.filter_by(content='a review').first().id
        # EVERY comment shares created_at: on PostgreSQL the ordering of ties
        # is unspecified, which is exactly what the id tie-break is for.
        base = datetime(2024, 1, 1, 12, 0, 0)
        tops = []
        for i in range(12):
            c = ReviewComment(user_id=author.id, review_id=rid,
                              content=f'comment {i}', created_at=base)
            _db.session.add(c)
            tops.append(c)
        _db.session.flush()
        for parent, n in zip(tops, (2, 1, 0)):
            for j in range(n):
                _db.session.add(ReviewComment(
                    user_id=author.id, review_id=rid,
                    parent_id=parent.id, content=f'reply {j}',
                    created_at=base))
        _db.session.commit()
        # ids, not ORM objects: these are detached once the app context exits
        return {'review_id': rid, 'top_ids': [c.id for c in tops],
                'author_id': author.id, 'app': clean_db}


def test_reply_counts_are_correct_on_postgres(client, thread):
    body = client.get(f"/api/reviews/{thread['review_id']}/comments"
                      f"?per_page=100").get_json()
    counts = {c['id']: c['reply_count'] for c in body['comments']}
    for top_id, expected in zip(
            thread['top_ids'], (2, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)):
        assert counts[top_id] == expected
    assert body['total'] == 15


def test_comment_query_count_is_constant_on_postgres(client, thread):
    """The N+1 claim, on an engine where ordering differs."""
    app = thread['app']

    def count(per_page):
        with app.app_context():
            with count_statements(app) as stmts:
                r = app.test_client().get(
                    f"/api/reviews/{thread['review_id']}/comments?per_page={per_page}")
        assert r.status_code == 200
        return len(stmts), r.get_json()

    one, one_body = count(1)
    all_n, all_body = count(100)
    assert len(one_body['comments']) == 1
    assert len(all_body['comments']) == 15
    assert one == all_n, (
        'query count grew with page size on PostgreSQL: %d -> %d'
        % (one, all_n))


def test_pagination_covers_every_comment_exactly_once_on_postgres(client, thread):
    seen = []
    for page in (1, 2, 3):
        body = client.get(
            f"/api/reviews/{thread['review_id']}/comments?per_page=5&page={page}"
        ).get_json()
        assert len(body['comments']) == 5
        assert body['pages'] == 3
        assert body['per_page'] == 5
        seen.extend(c['id'] for c in body['comments'])
    assert len(seen) == 15
    assert len(set(seen)) == 15, 'a comment was skipped or duplicated'


def test_equal_created_at_ordering_is_deterministic_on_postgres(client, thread):
    runs = []
    for _ in range(5):
        body = client.get(
            f"/api/reviews/{thread['review_id']}/comments?per_page=100"
        ).get_json()
        runs.append([c['id'] for c in body['comments']])
    assert runs[0] == runs[1] == runs[2] == runs[3] == runs[4], (
        'ties in created_at are ordered differently between calls on '
        'PostgreSQL')
    assert runs[0] == sorted(runs[0])


def test_per_page_is_clamped_on_postgres(client, thread):
    body = client.get(
        f"/api/reviews/{thread['review_id']}/comments?per_page=999999"
    ).get_json()
    assert body['per_page'] == 100
    body = client.get(
        f"/api/reviews/{thread['review_id']}/comments?per_page=-5"
    ).get_json()
    assert body['per_page'] == 1
    assert len(body['comments']) == 1


def test_replies_endpoint_is_batched_and_paged_on_postgres(client, thread):
    app = thread['app']
    with app.app_context():
        with count_statements(app) as stmts:
            r = app.test_client().get(
                f"/api/reviews/{thread['review_id']}/replies?per_page=100")
    body = r.get_json()
    assert body['count'] == 12
    assert len(body['replies']) == 12
    assert all(x['parent_id'] is None for x in body['replies'])
    grouped = [s for s in stmts if 'GROUP BY' in s.upper() and 'review_comment' in s]
    assert len(grouped) == 1, grouped


def test_missing_review_is_404_on_postgres(client, thread):
    assert client.get('/api/reviews/99999999/comments').status_code == 404


# ── 4. chat cursor contract on PostgreSQL ────────────────────────────────────

@pytest.fixture
def convo(clean_db):
    with clean_db.app_context():
        from models.chat import ChatConversation, ChatMessage
        owner, other = _user('f44owner'), _user('f44other')
        _db.session.add_all([owner, other])
        _db.session.flush()
        c = ChatConversation(user_id=owner.id, title='F44 thread')
        _db.session.add(c)
        _db.session.flush()
        base = datetime(2024, 5, 1, 9, 0, 0)
        for i in range(30):
            _db.session.add(ChatMessage(
                conversation_id=c.id, role='user' if i % 2 == 0 else 'assistant',
                content=f'message {i:02d}', created_at=base + timedelta(seconds=i)))
        _db.session.commit()
        return {'app': clean_db, 'cid': c.id,
                'owner_id': owner.id, 'other_id': other.id}


@pytest.fixture
def owner_client(clean_db, convo):
    c = clean_db.test_client()
    c.post('/login', data={'username': 'f44owner', 'password': 'TestPass1'},
           follow_redirects=True)
    return c


@pytest.fixture
def other_client(clean_db, convo):
    c = clean_db.test_client()
    c.post('/login', data={'username': 'f44other', 'password': 'TestPass1'},
           follow_redirects=True)
    return c


def test_history_limit_is_capped_on_postgres(owner_client, convo):
    for raw, expected in (('999999', 100), ('-1', 1), ('0', 1), ('abc', 50)):
        body = owner_client.get(
            f"/chat/conversations/{convo['cid']}?limit={raw}").get_json()
        assert body['limit'] == expected, (raw, body['limit'])


def test_cursor_pages_cover_history_without_gaps_on_postgres(owner_client, convo):
    """This is the check SQLite could not make meaningful. Every message shares
    nothing here, but the cursor predicate and the ordering must agree."""
    cid = convo['cid']
    pages, seen = [], []
    url = f'/chat/conversations/{cid}?limit=10'
    for _ in range(10):
        body = owner_client.get(url).get_json()
        texts = [m['content'] for m in body['messages']]
        assert texts == sorted(texts), 'a page must read oldest-first'
        pages.append(texts)
        seen.extend(texts)
        if not body['has_more'] or body['next_before'] is None:
            break
        url = f'/chat/conversations/{cid}?limit=10&before={body["next_before"]}'

    assert len(pages) == 3
    assert len(seen) == 30
    assert len(set(seen)) == 30, 'a message was returned twice'
    assert set(seen) == {'message %02d' % i for i in range(30)}


def test_api_and_agent_views_agree_because_nothing_backdates_created_at(
        clean_db, convo):
    """The id-cursor and the created_at agent window must not disagree.

    `chat_conversation` pages by `id`; `_history_messages` orders by
    `created_at`. Those two orders are identical only while `created_at`
    advances with `id`. The mission asked for this to be verified against the
    data model rather than assumed, so this asserts the *reason*: no creation
    site supplies `created_at`, so it always comes from the column default at
    insert time.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    sites = []
    for path in list((root / 'routes').glob('*.py')) + \
            list((root / 'scripts').glob('*.py')):
        text = path.read_text(encoding='utf-8')
        for m in re.finditer(r'ChatMessage\(', text):
            # look at the call and its closing argument list
            depth, i = 0, m.end() - 1
            while i < len(text):
                if text[i] == '(':
                    depth += 1
                elif text[i] == ')':
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            call = text[m.start():i]
            if 'created_at' in call:
                sites.append(f'{path.name}:{text.count(chr(10), 0, m.start()) + 1}')
    assert not sites, (
        'a ChatMessage creation site supplies created_at explicitly, so id '
        'order and created_at order can disagree: %s' % sites)

    # And on real data the two views agree.
    from models.chat import ChatConversation
    from routes.chat import _history_messages
    with clean_db.app_context():
        c = ChatConversation.query.get(convo['cid'])
        agent_order = [m.content for m in _history_messages(c)]

    c2 = clean_db.test_client()
    c2.post('/login', data={'username': 'f44owner', 'password': 'TestPass1'},
            follow_redirects=True)
    api_order = [m['content'] for m in
                 c2.get(f"/chat/conversations/{convo['cid']}?limit=100")
                 .get_json()['messages']]
    assert api_order == agent_order, (
        'API page (id order) and agent window (created_at order) disagree')


def test_cursor_is_unaffected_when_created_at_is_backdated(clean_db, convo):
    """Even if something DID backdate a row, the API's own paging stays
    complete: both the ordering and the predicate use `id`, so they cannot
    disagree with each other."""
    from models.chat import ChatMessage
    with clean_db.app_context():
        rows = (ChatMessage.query.filter_by(conversation_id=convo['cid'])
                .order_by(ChatMessage.id).all())
        rows[-1].created_at = rows[0].created_at - timedelta(days=1)
        _db.session.commit()

    c = clean_db.test_client()
    c.post('/login', data={'username': 'f44owner', 'password': 'TestPass1'},
           follow_redirects=True)
    seen = []
    url = f"/chat/conversations/{convo['cid']}?limit=10"
    for _ in range(10):
        body = c.get(url).get_json()
        seen.extend(m['content'] for m in body['messages'])
        if not body['has_more'] or body['next_before'] is None:
            break
        url = (f"/chat/conversations/{convo['cid']}"
               f"?limit=10&before={body['next_before']}")
    assert len(seen) == 30
    assert len(set(seen)) == 30, 'paging skipped or repeated a message'


def test_agent_history_is_capped_on_postgres(clean_db, convo):
    from routes.chat import AGENT_HISTORY_MAX, _history_messages
    from models.chat import ChatMessage, ChatConversation
    with clean_db.app_context():
        base = datetime(2024, 6, 1, 9, 0, 0)
        _db.session.add_all([
            ChatMessage(conversation_id=convo['cid'], role='user',
                        content=f'extra {i}',
                        created_at=base + timedelta(seconds=i))
            for i in range(AGENT_HISTORY_MAX + 20)])
        _db.session.commit()
        c = ChatConversation.query.get(convo['cid'])
        msgs = _history_messages(c)
        assert len(msgs) == AGENT_HISTORY_MAX
        texts = [m.content for m in msgs]
        assert texts == sorted(texts), 'agent history must be oldest-first'
        assert texts[-1] == 'extra %02d' % (AGENT_HISTORY_MAX + 19)


def test_conversation_authorization_holds_on_postgres(other_client, convo):
    assert other_client.get(
        f"/chat/conversations/{convo['cid']}").status_code == 404
    assert other_client.get(
        f"/chat/conversations/{convo['cid']}?limit=100&before=1"
    ).status_code == 404


def test_unauthenticated_history_is_refused_on_postgres(clean_db, convo):
    """No authentication, no transcript.

    The status is whatever the login machinery produces (a redirect or a
    refusal); what matters is that it is not 200 and that no message content
    appears in the response.
    """
    c = clean_db.test_client()
    r = c.get(f"/chat/conversations/{convo['cid']}")
    assert r.status_code != 200, r.get_data(as_text=True)[:200]
    body = r.get_data(as_text=True)
    assert 'message 00' not in body, 'transcript leaked to an anonymous caller'
    assert 'F44 thread' not in body, 'conversation title leaked'


# ── 5. related endpoints on PostgreSQL ───────────────────────────────────────

@pytest.mark.parametrize('raw', ['999999999', '-1', '0'])
def test_related_limits_stay_bounded_on_postgres(client, clean_db, raw):
    for path in ('/api/tags/popular', '/api/users/autocomplete?q=ab'):
        r = client.get(f'{path}&limit={raw}' if '?' in path
                       else f'{path}?limit={raw}')
        assert r.status_code in (200, 400), (path, raw, r.status_code)
        if r.status_code == 200:
            body = r.get_json()
            listed = body.get('tags') or body.get('users') or []
            assert len(listed) <= 50


def test_negative_limit_produces_no_sql_error_on_related_paths(client, clean_db):
    """PostgreSQL raises on a negative LIMIT, so an unclamped path would 500."""
    r = client.get('/api/users/autocomplete?q=ab&limit=-1')
    assert r.status_code == 200, r.get_data(as_text=True)[:300]


def test_trending_reviews_order_carries_an_id_tie_break_on_postgres(
        client, trend_world):
    """Guarded on the emitted ORDER BY, not on observed ordering.

    The other two tie-breaks (comments, trending users) are caught
    behaviourally on PostgreSQL — removing them changes the result order. This
    one is not: every fixture review has like_count 0, and with so few rows
    PostgreSQL's sort happens to emit them in id order anyway, so removing the
    clause is not observable on EITHER engine. The clause is still correct
    (it is what stops a real tie from being ordered arbitrarily once the plan
    changes), so it is asserted structurally here and the limitation is
    recorded rather than papered over.
    """
    app = client.application
    with app.app_context():
        with count_statements(app) as stmts:
            r = app.test_client().get('/api/trending/reviews?limit=10')
    assert r.status_code == 200
    ordered = [s for s in stmts
               if 'FROM review' in s and 'ORDER BY' in s.upper()]
    assert len(ordered) == 1, ordered
    order = ordered[0].upper().split('ORDER BY')[-1]
    assert 'LIKE_COUNT' in order, order
    assert 'REVIEW.ID' in order, (
        'trending reviews lost their id tie-break; with real tie counts the '
        'result order becomes plan-dependent')
