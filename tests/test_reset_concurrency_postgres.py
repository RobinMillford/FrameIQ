"""PostgreSQL proof for the concurrent password-reset guarantee (Feature F61).

Why this cannot be asserted on SQLite
-------------------------------------
``routes/auth.py`` re-reads the user under ``with_for_update()`` immediately
before writing, and re-checks the token's password-hash fingerprint. That check
is only atomic if the re-read actually returns the row as it is NOW. Two things
are engine-specific:

* **ORM refresh.** A re-query of an already-loaded primary key returns the
  object from the identity map. ``with_for_update()`` on its own does NOT
  refresh its attributes, which is why the production query pairs it with
  ``populate_existing()``. Verified directly below, on real PostgreSQL, with
  two independent connections — including the negative control, so a future
  removal of ``populate_existing()`` fails here.
* **Lock and snapshot semantics.** SQLite serialises writers and treats
  ``FOR UPDATE`` as a no-op, so it can demonstrate neither that the lock
  serialises two resets nor that a committed concurrent change becomes visible
  to the waiting transaction. Only PostgreSQL establishes that.

The route-level test drives the REAL ``/reset-password/<token>`` view — the real
fingerprint helper, the real locked query, the real
``bump_session_version`` — against a real application bound to PostgreSQL. The
race is made deterministic by injecting the competing change at the pre-flight
binding check, which is exactly the window between the view's initial read and
its locked re-read. No sleeps and no thread scheduling are involved.
"""
import os
import sys

import pytest
from sqlalchemy import text

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'migrates'))

# Hard precondition for the whole module: this suite DROPs and recreates the
# public schema. `utils.db_target.classify` refuses anything that is not a
# local host, so pointing it at a real database skips the module rather than
# running. This is the same guard tests/test_migration_postgres.py uses, and it
# is deliberately kept rather than reimplemented.


def _pg_url():
    url = os.environ.get('FRAMEIQ_TEST_POSTGRES_URL', '').strip()
    if not url:
        return None
    from utils.db_target import classify
    target = classify(url)
    if not target.is_local:
        pytest.skip('FRAMEIQ_TEST_POSTGRES_URL is not a local host; refusing '
                    'to run destructive tests against %r'
                    % target.safe_identity())
    return url


PG_URL = _pg_url()

# A CI run exists precisely to execute these tests, so a missing
# FRAMEIQ_TEST_POSTGRES_URL there must FAIL rather than skip. Without this the
# job would report success having run nothing, and a skipped postgres test
# would read as coverage it never provided. Locally there is no server, so the
# suite still skips and the offline gate is unaffected.
_IN_CI = (os.environ.get('GITHUB_ACTIONS', '').strip().lower() in ('1', 'true')
          or os.environ.get('CI', '').strip().lower() in ('1', 'true'))

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(PG_URL is None and not _IN_CI,
                       reason='needs FRAMEIQ_TEST_POSTGRES_URL pointing at a '
                              'throwaway PostgreSQL server'),
]


# ── harness ──────────────────────────────────────────────────────────────────

@pytest.fixture
def pg_app():
    """A real application bound to a throwaway PostgreSQL database.

    A second Flask app is needed because the shared one is bound to the offline
    SQLite file. `db` is a module-level singleton, but Flask-SQLAlchemy keeps
    per-app extension state, so `create_app()` can be called again and the
    module-level app is left untouched. Verified offline on SQLite.
    """
    import app as app_module
    from models.base import db as _db

    previous_url = os.environ.get('DATABASE_URL')
    previous_skip = os.environ.get('SKIP_SCHEMA_GUARD')
    os.environ['DATABASE_URL'] = PG_URL
    os.environ['SKIP_SCHEMA_GUARD'] = '1'
    try:
        app = app_module.create_app()
    finally:
        if previous_url is None:
            os.environ.pop('DATABASE_URL', None)
        else:
            os.environ['DATABASE_URL'] = previous_url
        if previous_skip is None:
            os.environ.pop('SKIP_SCHEMA_GUARD', None)
        else:
            os.environ['SKIP_SCHEMA_GUARD'] = previous_skip

    # The same overrides tests/conftest.py applies to the shared app. Without
    # WTF_CSRF_ENABLED=False every form POST answers 400.
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False,
                      SERVER_NAME='localhost', MAIL_SERVER='',
                      RATELIMIT_ENABLED=False)

    with app.app_context():
        engine = _db.engine
        with engine.begin() as connection:
            connection.execute(text('DROP SCHEMA public CASCADE'))
            connection.execute(text('CREATE SCHEMA public'))
        _db.create_all()

    try:
        yield app, engine
    finally:
        with app.app_context():
            _db.session.remove()
            _db.engine.dispose()


def _make_user(app, username, password='TestPass1'):
    from models import User
    from models.base import db as _db
    with app.app_context():
        user = User(username=username, email='%s@f61.test' % username,
                    email_verified=True)
        user.set_password(password)
        _db.session.add(user)
        _db.session.commit()
        return user.id


def _mint_token(app, user_id):
    from models import User
    from models.base import db as _db
    from utils.email import generate_reset_token
    with app.app_context():
        _db.session.remove()
        row = _db.session.get(User, user_id)
        return generate_reset_token(row.email, row.password_hash)


# ── 1. the mechanism: a locked re-read must refresh the ORM object ───────────

def test_locked_reread_refreshes_the_password_hash_on_postgres(pg_app):
    """`with_for_update()` alone is stale; the pair the route uses is not.

    The negative control is the point. Both queries below run against the *same*
    primed session with nothing in between — deliberately no rollback, because
    ``rollback()`` expires every instance in the identity map, and the second
    query would then re-load the row from scratch and look correct even if
    ``populate_existing()`` had been deleted. With the session left untouched,
    the only thing that can move the second result is ``populate_existing()``
    itself, so removing it fails here.
    """
    from models import User
    from models.base import db as _db
    from werkzeug.security import generate_password_hash

    app, engine = pg_app
    user_id = _make_user(app, 'f61refresh')
    competing = generate_password_hash('Competing01')

    # Connection 1 primes the identity map with the OLD hash and holds its
    # transaction open, exactly as the request does before its locked re-read.
    with app.app_context():
        session = _db.session
        original = session.get(User, user_id)

        # Connection 2, genuinely independent, commits a competing change.
        with engine.begin() as competing_connection:
            competing_connection.execute(text(
                'UPDATE "user" SET password_hash = :h WHERE id = :i'),
                {'h': competing, 'i': user_id})

        # The negative control: a FOR UPDATE re-read on its own.
        stale = (session.query(User)
                 .filter(User.id == user_id)
                 .with_for_update().one())
        stale_seen = stale.password_hash

        # What the route does. Same identity map, same transaction, one statement.
        fresh = (session.query(User)
                 .filter(User.id == user_id)
                 .with_for_update()
                 .populate_existing()
                 .one())
        fresh_seen = fresh.password_hash

        assert stale is original, (
            'precondition: the re-query returned the identity-mapped object')
        assert stale_seen != competing, (
            'precondition: a FOR UPDATE re-read was expected to be stale '
            'without populate_existing()')
        assert fresh_seen == competing, (
            'with_for_update().populate_existing() did not refresh the '
            'password hash, so the reset re-check would compare against a '
            'stale value: got %r' % (fresh_seen,))
        session.rollback()


# ── 2. the route: a lost race must not overwrite the winner ──────────────────

def test_a_lost_reset_race_cannot_overwrite_the_winner_on_postgres(pg_app,
                                                                  monkeypatch):
    """Two submissions, one token, one real PostgreSQL database.

    The competing reset commits on an independent connection inside the window
    between the view's initial read and its locked re-read, so the second
    submission really does re-read a row that changed underneath it.
    """
    from werkzeug.security import check_password_hash, generate_password_hash
    import routes.auth as auth_mod

    app, engine = pg_app
    user_id = _make_user(app, 'f61race')
    token = _mint_token(app, user_id)
    competing = generate_password_hash('Competing01')

    original = auth_mod._token_still_matches
    calls = {'n': 0}

    def _racing_check(payload, candidate):
        calls['n'] += 1
        if calls['n'] == 1:
            # The winning reset, committing on its own connection, mid-flight.
            with engine.begin() as competing_connection:
                competing_connection.execute(text(
                    'UPDATE "user" SET password_hash = :h WHERE id = :i'),
                    {'h': competing, 'i': user_id})
        return original(payload, candidate)

    monkeypatch.setattr(auth_mod, '_token_still_matches', _racing_check)

    client = app.test_client()
    response = client.post('/reset-password/%s' % token, data={
        'password': 'ResetVia1', 'confirm_password': 'ResetVia1'},
        follow_redirects=True)

    body = response.get_data(as_text=True).lower()
    assert 'invalid' in body or 'expired' in body, (
        'the losing submission was accepted; it would have overwritten the '
        'password the winner had already set')
    assert calls['n'] == 2, (
        'expected the binding check twice (pre-flight and pre-commit), saw %d'
        % calls['n'])

    with app.app_context():
        from models import User
        from models.base import db as _db
        _db.session.remove()
        row = _db.session.get(User, user_id)
        assert not check_password_hash(row.password_hash, 'ResetVia1'), \
            'the losing request overwrote the winner\'s password'
        assert check_password_hash(row.password_hash, 'Competing01'), \
            'the winning password was not preserved'
        # A refused submission must mutate nothing at all — including the
        # revocation counter, which is the thing sessions are keyed on.
        assert row.session_version == 0, (
            'a refused reset bumped session_version to %r; a lost race must '
            'leave the row untouched' % row.session_version)


def test_a_winning_reset_bumps_the_session_version_on_postgres(pg_app):
    """The control: an uncontested reset still revokes, exactly once."""
    from models import User
    from models.base import db as _db
    from werkzeug.security import check_password_hash

    app, _engine = pg_app
    user_id = _make_user(app, 'f61winner')
    token = _mint_token(app, user_id)

    client = app.test_client()
    response = client.post('/reset-password/%s' % token, data={
        'password': 'ResetVia1', 'confirm_password': 'ResetVia1'},
        follow_redirects=True)

    assert b'successful' in response.data.lower()

    with app.app_context():
        _db.session.remove()
        row = _db.session.get(User, user_id)
        assert check_password_hash(row.password_hash, 'ResetVia1')
        assert row.session_version == 1, (
            'an uncontested reset must revoke exactly once, got %r'
            % row.session_version)


# ── 3. no leaked connections or transactions ─────────────────────────────────

def test_the_race_leaves_no_connection_or_transaction_behind(pg_app):
    """Connections and transactions must be closed on success AND on refusal.

    Both paths are exercised deliberately — a successful reset commits, the
    replay is refused and rolls back — because a leak in only one of them would
    otherwise stay invisible.
    """
    app, engine = pg_app
    user_id = _make_user(app, 'f61cleanup')
    token = _mint_token(app, user_id)

    client = app.test_client()
    accepted = client.post('/reset-password/%s' % token, data={
        'password': 'ResetVia1', 'confirm_password': 'ResetVia1'},
        follow_redirects=True)
    refused = client.post('/reset-password/%s' % token, data={
        'password': 'ResetVia2', 'confirm_password': 'ResetVia2'},
        follow_redirects=True)

    # Precondition: both branches really were taken, so the cleanup assertions
    # below describe a committed write and a rolled-back one.
    assert b'successful' in accepted.data.lower(), (
        'the first reset should have been accepted; the replay path would '
        'never have been exercised')
    refused_body = refused.get_data(as_text=True).lower()
    assert 'invalid' in refused_body or 'expired' in refused_body, (
        'the replay should have been refused; the rollback path would never '
        'have been exercised')

    with app.app_context():
        from models.base import db as _db
        # `db.session` is a scoped_session and does not proxy the Session API,
        # so unwrap it first. A fresh app context must come up with no
        # transaction pending: a leaked BEGIN would hold a snapshot and a row
        # lock against every later request in the process.
        session = _db.session()
        assert session.get_transaction() is None, (
            'a transaction was left open on the application session')
        _db.session.remove()

    assert engine.pool.checkedout() == 0, (
        '%d connection(s) still checked out after the reset flow'
        % engine.pool.checkedout())
