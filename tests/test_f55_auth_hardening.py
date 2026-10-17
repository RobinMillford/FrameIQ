"""F55 — authentication and session hardening regression tests.

Covers the F51 findings remediated here:

* Reset tokens were replayable for the whole hour after a successful reset.
* A password reset revoked nothing — neither the user's own sessions nor an
  attacker's.
* The 30-day remember-me cookie carried no ``Secure`` and no ``SameSite``.
* ``SESSION_COOKIE_SECURE`` silently fell back to dev values when a production
  marker was missing or misspelled.
* ``GET /logout`` performed a state change outside CSRF protection.

Offline only: the ordinary ``tests/conftest.py`` SQLite configuration, no
network, no browser, no production-mode process.
"""
import re

import pytest


# ── helpers ───────────────────────────────────────────────────────────────────

def _user(db, name, password='TestPass1'):
    from models import User
    user = User(username=name, email='%s@f55.test' % name, email_verified=True)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return user


def _user_fresh(db, name):
    """Re-read a user by username from the database, immune to stale state.

    Two things are handled here, both consequences of the F56 harness fix
    giving each request its own application context (and therefore its own
    SQLAlchemy session):

    * the request's session and the test's session are now genuinely separate,
      so a row changed by a request is not visible in the identity map the
      test is holding — ``expire_all()`` forces the re-read to hit the DB
      instead of returning the cached instance;
    * a re-query by primary key would return that same cached instance, so
      the session is expired rather than trusted.
    """
    from models import User
    db.session.expire_all()
    return User.query.filter_by(username=name).first()


def _token_for(user):
    from utils.email import generate_reset_token
    return generate_reset_token(user.email, user.password_hash)


def _login(client, username, password='TestPass1'):
    return client.post('/login',
                       data={'username': username, 'password': password},
                       follow_redirects=True)


def _csrf(client, path='/login'):
    """A CSRF token scraped from a rendered form, exactly as a browser would.

    Flask-WTF validates the SIGNED, timestamped form token, not the raw value
    stored in ``session['csrf_token']``. Posting the raw value is rejected with
    400, so the token has to come out of a rendered page.
    """
    body = _get(client, path).get_data(as_text=True)
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', body)
    assert match, 'no rendered CSRF token found on %s' % path
    return match.group(1)


def _post_logout(client):
    """POST logout with a CSRF token, for tests running with CSRF enabled."""
    token = _csrf(client)
    data = {'csrf_token': token} if token else {}
    return _post(client, '/logout', data=data, follow_redirects=False)


# ── one-time use ──────────────────────────────────────────────────────────────

def test_d_replay_of_a_used_reset_token_is_refused(client, db, app):
    """The core finding: a successful reset must invalidate the link.

    F51 showed the token was a signed email plus an expiry with no record of
    use, so it kept working for the remainder of its hour.
    """
    user = _user(db, 'f55replay')
    token = _token_for(user)

    first = client.post('/reset-password/%s' % token, data={
        'password': 'BrandNew1', 'confirm_password': 'BrandNew1'},
        follow_redirects=True)
    assert first.status_code == 200
    assert _user_fresh(db, 'f55replay').check_password('BrandNew1')

    replay = client.post('/reset-password/%s' % token, data={
        'password': 'Attacker99', 'confirm_password': 'Attacker99'},
        follow_redirects=True)
    body = replay.get_data(as_text=True).lower()
    assert 'invalid' in body or 'expired' in body, (
        'the used reset token was accepted a second time')
    # And the password the replay tried to set was NOT applied.
    assert not _user_fresh(db, 'f55replay').check_password('Attacker99'), \
        'a replayed token changed the password'
    assert _user_fresh(db, 'f55replay').check_password('BrandNew1'), \
        'the replay overwrote the password set by the legitimate reset'


def test_d_replaying_the_token_on_get_is_refused(client, db, app):
    """Opening a spent link must not even show the reset form."""
    user = _user(db, 'f55replayget')
    token = _token_for(user)
    client.post('/reset-password/%s' % token, data={
        'password': 'Another11', 'confirm_password': 'Another11'},
        follow_redirects=True)

    page = client.get('/reset-password/%s' % token, follow_redirects=True)
    body = page.get_data(as_text=True)
    assert 'new_password' not in body and 'confirm' not in body.lower(), \
        'the spent token still served the reset form'
    assert 'invalid' in body.lower() or 'expired' in body.lower()


def test_d_every_outstanding_token_dies_on_one_use(client, db, app):
    """Nothing outstanding survives a reset, however many were issued.

    A reset token is a deterministic signature over (email, password-hash
    fingerprint), so asking for a second reset link before using the first
    yields the SAME token rather than a second distinct one. That makes the
    guarantee easy to state exactly: the first use changes the hash, and every
    token bound to the old hash is dead from that moment.
    """
    user = _user(db, 'f55multi')
    first_token = _token_for(user)
    second_token = _token_for(_user_fresh(db, 'f55multi'))
    assert first_token == second_token, (
        'precondition: two outstanding links for one account are one token')

    client.post('/reset-password/%s' % first_token, data={
        'password': 'FirstOne1', 'confirm_password': 'FirstOne1'},
        follow_redirects=True)

    for token in (first_token, second_token):
        attempt = client.post('/reset-password/%s' % token, data={
            'password': 'SecondOne1', 'confirm_password': 'SecondOne1'},
            follow_redirects=True)
        assert 'invalid' in attempt.get_data(as_text=True).lower(), \
            'a token outstanding before the reset was still usable'
    assert not _user_fresh(db, 'f55multi').check_password('SecondOne1')


def test_d_token_is_unusable_after_an_unrelated_password_change(client, db,
                                                                app):
    """Changing the password by any other route also invalidates the token."""
    user = _user(db, 'f55other')
    token = _token_for(user)

    fresh = _user_fresh(db, 'f55other')
    fresh.set_password('ChangedElse1')
    db.session.commit()

    attempt = client.post('/reset-password/%s' % token, data={
        'password': 'ResetVia1', 'confirm_password': 'ResetVia1'},
        follow_redirects=True)
    assert 'invalid' in attempt.get_data(as_text=True).lower()
    assert not _user_fresh(db, 'f55other').check_password('ResetVia1')


def test_d_a_fresh_token_still_works_after_a_reset(client, db, app):
    """Invalidation must not be permanent — the user can still reset again."""
    user = _user(db, 'f55again')
    token_one = _token_for(user)
    client.post('/reset-password/%s' % token_one, data={
        'password': 'SecondPass1', 'confirm_password': 'SecondPass1'},
        follow_redirects=True)

    token_two = _token_for(_user_fresh(db, 'f55again'))
    again = client.post('/reset-password/%s' % token_two, data={
        'password': 'ThirdPass1', 'confirm_password': 'ThirdPass1'},
        follow_redirects=True)
    assert 'successful' in again.get_data(as_text=True).lower()
    assert _user_fresh(db, 'f55again').check_password('ThirdPass1')


@pytest.mark.parametrize('token', [
    pytest.param('badtoken', id='garbage'),
    pytest.param('', id='empty'),
    pytest.param('a.b.c', id='dotted-but-not-valid'),
    pytest.param('x' * 500, id='very-long'),
    pytest.param('../../etc/passwd', id='path-shaped'),
])
def test_d_malformed_tokens_are_refused_without_error(client, token):
    """A malformed token is a clean 400-class refusal, never a 500."""
    r = client.get('/reset-password/%s' % token, follow_redirects=True)
    # A token that does not match the route (empty, path-shaped) is refused by
    # routing with 404 before the view runs; one that does is refused by the
    # view. Both are clean refusals -- the property is "never a 500".
    assert r.status_code in (200, 404), (
        'malformed token produced %s; it must never be a 500' % r.status_code)


def test_d_legacy_bare_email_token_is_refused(client, db, app):
    """Tokens in the pre-F55 payload shape must fail closed.

    Accepting the old bare-email payload would leave every already-issued link
    replayable for whatever is left of its hour — the exact defect being fixed.
    """
    from utils.email import _serializer, _RESET_SALT
    user = _user(db, 'f55legacy')
    with app.app_context():
        legacy = _serializer().dumps(user.email, salt=_RESET_SALT)

    r = client.post('/reset-password/%s' % legacy, data={
        'password': 'Legacy11', 'confirm_password': 'Legacy11'},
        follow_redirects=True)
    assert 'invalid' in r.get_data(as_text=True).lower()
    assert not _user_fresh(db, 'f55legacy').check_password('Legacy11')


def test_d_expired_token_is_refused(client, db, app, monkeypatch):
    """An expired token is refused by the age check, independent of binding."""
    import utils.email as email_utils
    user = _user(db, 'f55expired')
    token = _token_for(user)

    monkeypatch.setattr(email_utils, '_TOKEN_MAX_AGE', -1)
    r = client.post('/reset-password/%s' % token, data={
        'password': 'Expired11', 'confirm_password': 'Expired11'},
        follow_redirects=True)
    assert 'invalid' in r.get_data(as_text=True).lower() \
        or 'expired' in r.get_data(as_text=True).lower()
    assert not _user_fresh(db, 'f55expired').check_password('Expired11')


def test_d_password_policy_still_applies(client, db, app):
    """Refusing a bad password must NOT consume the token.

    The binding check has to survive a rejected submission, otherwise a typo
    would burn the user's only reset link.
    """
    user = _user(db, 'f55policy')
    token = _token_for(user)

    weak = client.post('/reset-password/%s' % token, data={
        'password': 'short', 'confirm_password': 'short'})
    assert weak.status_code == 200
    mismatch = client.post('/reset-password/%s' % token, data={
        'password': 'GoodPass1', 'confirm_password': 'Different1'})
    assert mismatch.status_code == 200

    good = client.post('/reset-password/%s' % token, data={
        'password': 'GoodPass1', 'confirm_password': 'GoodPass1'},
        follow_redirects=True)
    assert 'successful' in good.get_data(as_text=True).lower()
    assert _user_fresh(db, 'f55policy').check_password('GoodPass1')


# ── session and remember-me invalidation ──────────────────────────────────────

def test_d_reset_bumps_the_session_version(client, db, app):
    user = _user(db, 'f55version')
    before = _user_fresh(db, 'f55version').session_version
    client.post('/reset-password/%s' % _token_for(user), data={
        'password': 'Versioned1', 'confirm_password': 'Versioned1'},
        follow_redirects=True)
    after = _user_fresh(db, 'f55version').session_version
    assert after == before + 1, (
        'session_version did not advance, so no credential is revoked')


def test_d_reset_revokes_another_browsers_session(client, db, app):
    """The finding: reset revoked nothing.

    A second client logs in and holds a valid session cookie. After the reset
    that cookie must stop authenticating.
    """
    user = _user(db, 'f55victim')

    attacker = _second_client()
    _login(attacker, 'f55victim')
    assert _get(attacker, '/profile').status_code == 200, \
        'precondition: the second client is logged in'
    stolen_identity = _identity_of(attacker)
    assert stolen_identity is not None

    client.post('/reset-password/%s' % _token_for(user), data={
        'password': 'Revoked01', 'confirm_password': 'Revoked01'},
        follow_redirects=True)

    after = _get(attacker, '/profile')
    assert after.status_code in (302, 401, 403), (
        'the pre-reset session cookie still authenticates after a reset '
        '(status %s)' % after.status_code)


def test_d_reset_revokes_another_browsers_remember_cookie(client, db, app):
    """The same must hold for the 30-day remember-me cookie."""
    user = _user(db, 'f55remember')

    attacker = _second_client()
    _login_with_remember(attacker, 'f55remember')

    # Drop the session cookie so only the remember-me cookie remains, which is
    # exactly the state after the user closes the browser.
    _clear_session_cookie(attacker)
    assert _identity_of(attacker, session_only=True) is None, \
        'precondition: no session cookie remains'
    assert _get(attacker, '/profile').status_code == 200, \
        'precondition: the remember cookie still authenticates'

    client.post('/reset-password/%s' % _token_for(user), data={
        'password': 'Revoked02', 'confirm_password': 'Revoked02'},
        follow_redirects=True)

    after = _get(attacker, '/profile')
    assert after.status_code in (302, 401, 403), (
        'the pre-reset remember-me cookie still authenticates after a reset '
        '(status %s)' % after.status_code)


def test_d_reset_clears_the_resetting_browsers_own_credentials(
        client, db, app):
    """The browser that performed the reset is logged out too."""
    user = _user(db, 'f55selfreset')
    _login(client, 'f55selfreset')

    client.post('/reset-password/%s' % _token_for(user), data={
        'password': 'SelfReset1', 'confirm_password': 'SelfReset1'},
        follow_redirects=True)

    assert _identity_of(client, session_only=True) is None, \
        'the resetting browser kept a live session cookie'
    assert _get(client, '/profile').status_code in (302, 401, 403)


def test_d_legacy_unversioned_identity_is_rejected(client, db, app):
    """A cookie issued before F55 must not authenticate.

    The identity is now versioned; accepting the bare legacy form would keep
    every pre-deploy cookie valid and defeat the point of the counter.

    The client's cookies are dropped first, because Flask-Login mints a
    remember-me cookie on every response (``REMEMBER_COOKIE_REFRESH_EACH_REQUEST``
    defaults on). That cookie is separately valid and would otherwise keep the
    request authenticated, hiding what this test is about: the session identity
    alone.
    """
    user = _user(db, 'f55legacyid')
    _login(client, 'f55legacyid')
    _forget_all_cookies(client)

    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)          # the pre-F55 shape
    assert _get(client, '/profile').status_code in (302, 401, 403), \
        'an unversioned identity string still authenticated'


def test_d_version_mismatch_is_rejected(client, db, app):
    """A wrong version in an otherwise well-formed identity is refused."""
    user = _user(db, 'f55mismatch')
    _login(client, 'f55mismatch')
    _forget_all_cookies(client)
    with client.session_transaction() as sess:
        sess['_user_id'] = '%d|%d' % (user.id, (user.session_version or 0) + 99)
    assert _get(client, '/profile').status_code in (302, 401, 403)


def test_d_identity_is_versioned_at_login(client, db, app):
    """A fresh login must carry the current version."""
    from utils.session_auth import session_identity
    _user(db, 'f55identity')
    _login(client, 'f55identity')
    with client.session_transaction() as sess:
        assert sess['_user_id'] == session_identity(_user_fresh(db, 'f55identity'))


@pytest.mark.parametrize('identity,expected', [
    ('42|3', (42, 3)),
    ('0|0', (0, 0)),
    ('42', None),          # legacy, unversioned
    ('42|', None),
    ('|3', None),
    ('42|x', None),
    ('42|3|4', None),        # ambiguous: rejected, never partially parsed
    ('', None),
    (None, None),
    (42, None),
])
def test_d_identity_parser_is_strict(identity, expected):
    from utils.session_auth import parse_session_identity
    assert parse_session_identity(identity) == expected


def test_d_locked_recheck_sees_a_concurrent_password_change(client, db, app,
                                                              monkeypatch):
    """The pre-commit re-check must RE-READ the column, not the cached object.

    A re-query of an already-loaded primary key returns the identity-mapped
    instance, and ``with_for_update()`` alone does not refresh its attributes.
    So the "atomic" re-check could be comparing the token against the very same
    stale hash the request had already accepted. With that, two concurrent
    resets carrying one token would both be applied and the second would
    silently overwrite the first — which is exactly the race the row lock was
    added to close.

    The competing change is injected at the FIRST binding check, which is the
    window between the view's initial read and the locked re-read. The check
    helper is the seam because that window is otherwise unreachable from a
    single-threaded test client.
    """
    import routes.auth as auth_mod

    user = _user(db, 'f55atomic')
    token = _token_for(user)

    original = auth_mod._token_still_matches
    seen = {'n': 0}

    def _racing_check(payload, candidate):
        seen['n'] += 1
        if seen['n'] == 1:
            # A competing reset commits while this request holds its read.
            with app.app_context():
                from models import db as _db
                from models import User as _User
                row = _db.session.get(_User, candidate.id)
                row.set_password('Competing01')
                _db.session.commit()
        return original(payload, candidate)

    monkeypatch.setattr(auth_mod, '_token_still_matches', _racing_check)

    response = client.post('/reset-password/%s' % token, data={
        'password': 'RaceWinner1', 'confirm_password': 'RaceWinner1'},
        follow_redirects=True)

    body = response.get_data(as_text=True).lower()
    assert 'invalid' in body or 'expired' in body, (
        'the locked re-check did not see the competing change, so a lost race '
        'would still overwrite the winner\'s password')
    assert seen['n'] == 2, (
        'expected the binding check to run twice (pre-flight and pre-commit), '
        'saw %d' % seen['n'])
    assert not _user_fresh(db, 'f55atomic').check_password('RaceWinner1'),         'the losing request applied its password'
    assert _user_fresh(db, 'f55atomic').check_password('Competing01'),         'the competing reset\'s password was clobbered'


def test_d_concurrent_resets_only_one_wins(client, db, app):
    """Two submissions with the same token: the second must not apply.

    The binding is re-checked under a row lock immediately before the write, so
    the read-modify-write is not a TOCTOU window.
    """
    user = _user(db, 'f55race')
    token = _token_for(user)

    first = _second_client()
    second = _second_client()

    first.post('/reset-password/%s' % token, data={
        'password': 'Winner111', 'confirm_password': 'Winner111'},
        follow_redirects=True)
    r2 = second.post('/reset-password/%s' % token, data={
        'password': 'Loser222', 'confirm_password': 'Loser222'},
        follow_redirects=True)

    body2 = r2.get_data(as_text=True).lower()
    assert 'invalid' in body2 or 'expired' in body2, (
        'the second concurrent submission was accepted: %s' % body2[:120])
    assert not _user_fresh(db, 'f55race').check_password('Loser222')


# ── cookie flags ──────────────────────────────────────────────────────────────

_COOKIE_FLAG_KEYS = (
    'SESSION_COOKIE_SECURE', 'REMEMBER_COOKIE_SECURE',
    'REMEMBER_COOKIE_HTTPONLY', 'REMEMBER_COOKIE_SAMESITE',
    'SESSION_COOKIE_HTTPONLY', 'SESSION_COOKIE_SAMESITE',
)


def _cookie_flags_under(monkeypatch, env):
    """Build a real app with an explicit environment; return its cookie flags.

    `create_app()` reads the mode markers ONCE, at call time, and bakes
    SESSION_COOKIE_SECURE / REMEMBER_COOKIE_SECURE into the config it returns.
    The only way to assert those flags is therefore to build an app per mode.

    The previous version of this test asserted a literal `True` on the SHARED
    suite app after monkeypatching `APP_ENV` — but that app had already been
    constructed, so the env change could not reach its config. The assertion
    was really testing whichever `.env` the machine happened to load. On a
    developer box `.env` sets `RENDER`, so `_is_production_deployment()` was
    True and the test passed; in CI there is no `.env`, so it was False and the
    test failed. A test that passes or fails according to the shell it runs in
    is not a test of the code.

    Every marker is cleared first so the surrounding environment — the suite's
    own, or the runner's — cannot leak into the result.
    """
    import app as app_module
    for key in ('APP_ENV', 'RENDER', 'K_SERVICE', 'DATABASE_URL'):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    built = app_module.create_app()
    return {key: built.config[key] for key in _COOKIE_FLAG_KEYS}


# A local file DSN for both modes: the point of each case is the MODE marker,
# not the database. `_has_remote_database()` would otherwise decide the answer
# and make the case indistinguishable from the detection tests below.
_PRODUCTION_MODE = {'APP_ENV': 'production',
                    'DATABASE_URL': 'sqlite:////tmp/f55prod.db'}
_DEVELOPMENT_MODE = {'APP_ENV': 'development',
                     'DATABASE_URL': 'sqlite:////tmp/f55dev.db'}


def test_d_remember_cookie_flags_follow_production_mode(monkeypatch):
    """REMEMBER_COOKIE_* must track SESSION_COOKIE_SECURE, not the defaults.

    Flask-Login's defaults are Secure=False and NO SameSite attribute, which is
    what F51 flagged.
    """
    config = _cookie_flags_under(monkeypatch, _PRODUCTION_MODE)

    assert config['SESSION_COOKIE_SECURE'] is True
    assert config['REMEMBER_COOKIE_SECURE'] is True
    assert config['REMEMBER_COOKIE_SECURE'] == config['SESSION_COOKIE_SECURE'], \
        'the remember cookie does not follow the session cookie'
    assert config['REMEMBER_COOKIE_HTTPONLY'] is True
    assert config['REMEMBER_COOKIE_SAMESITE'] == 'Lax'
    assert config['SESSION_COOKIE_HTTPONLY'] is True
    assert config['SESSION_COOKIE_SAMESITE'] == 'Lax'


def test_d_cookie_secure_is_off_in_development_mode(monkeypatch):
    """The other direction: development must not issue Secure cookies.

    Asserting only the production case would leave a hardcoded
    `SESSION_COOKIE_SECURE=True` — which breaks plain-HTTP local development
    and the test client — completely undetected. Both directions are what makes
    "follows the session cookie" a real invariant rather than a constant.
    """
    config = _cookie_flags_under(monkeypatch, _DEVELOPMENT_MODE)

    assert config['SESSION_COOKIE_SECURE'] is False, (
        'Secure cookies in development break plain-HTTP local use')
    assert config['REMEMBER_COOKIE_SECURE'] == config['SESSION_COOKIE_SECURE'], \
        'the remember cookie does not follow the session cookie'
    # The flags that are not mode-dependent stay fixed in both modes.
    assert config['REMEMBER_COOKIE_HTTPONLY'] is True
    assert config['REMEMBER_COOKIE_SAMESITE'] == 'Lax'


def test_d_cookie_defaults_are_not_relied_upon(app):
    """Guard: the insecure Flask-Login defaults must be overridden explicitly.

    Source-level, but it is the right level for it: these are CONFIG KEYS, and
    the property is that the application sets them rather than inheriting
    `False`/absent. The behavioural half is asserted by the test above and by
    the full offline suite running over HTTP.
    """
    import pathlib
    source = pathlib.Path('app.py').read_text(encoding='utf-8')
    for key in ('REMEMBER_COOKIE_SECURE', 'REMEMBER_COOKIE_HTTPONLY',
                'REMEMBER_COOKIE_SAMESITE'):
        assert '%s=' % key in source, (
            '%s is not set explicitly; Flask-Login defaults to Secure=False '
            'and no SameSite' % key)


def test_d_remember_cookie_always_tracks_the_session_cookie(app):
    """The invariant that matters, independent of the ambient environment.

    Which mode the suite happens to run in is decided by RENDER/APP_ENV/
    DATABASE_URL in the surrounding environment, so asserting a literal True or
    False here would be a test of the shell, not of the code. What must always
    hold is that the remember cookie inherits the session cookie's Secure
    decision — the F51 finding was precisely that it did not.
    """
    assert app.config['REMEMBER_COOKIE_SECURE'] == \
        app.config['SESSION_COOKIE_SECURE']
    assert app.config['REMEMBER_COOKIE_HTTPONLY'] is True
    assert app.config['REMEMBER_COOKIE_SAMESITE'] == \
        app.config['SESSION_COOKIE_SAMESITE'] == 'Lax'
    # Plain-HTTP dev/CI: login works end to end over the test client.
    assert app.config['SERVER_NAME'] == 'localhost'


@pytest.mark.parametrize('label,env,expected', [
    ('compose default', {'APP_ENV': 'production',
                         'DATABASE_URL': 'postgresql://u:p@db.host/db'}, True),
    ('misspelled as PRODUCTION', {'APP_ENV': 'PRODUCTION',
                                  'DATABASE_URL': 'postgresql://u:p@h/db'}, True),
    ('mixed case Production', {'APP_ENV': 'Production',
                               'DATABASE_URL': 'postgresql://u:p@h/db'}, True),
    ('Render marker', {'RENDER': 'true', 'DATABASE_URL': 'sqlite:///x'}, True),
    ('Cloud Run marker', {'K_SERVICE': 'web', 'DATABASE_URL': 'sqlite:///x'},
     True),
    ('marker missing, remote db',
     {'DATABASE_URL': 'postgresql://u:p@some.remote.host/db'}, True),
    ('local development',
     {'APP_ENV': 'development', 'DATABASE_URL': 'sqlite:////tmp/dev.db'}, False),
    ('local sqlite', {'DATABASE_URL': 'sqlite:////tmp/t.db'}, False),
    ('compose db service name',
     {'DATABASE_URL': 'postgresql://u:p@db:5432/frameiq'}, False),
    ('localhost postgres',
     {'DATABASE_URL': 'postgresql://u:p@localhost:5432/x'}, False),
    ('no database url', {'DATABASE_URL': ''}, False),
])
def test_d_production_detection(monkeypatch, label, env, expected):
    """A missing or misspelled marker must not silently disable protection."""
    import app as app_module
    for key in ('APP_ENV', 'RENDER', 'K_SERVICE', 'DATABASE_URL'):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert app_module._is_production_deployment() is expected, label


def test_d_no_new_production_mode_variable():
    """Guard: F55 must not invent a mode variable the deploy never sets.

    An unrecognised name is precisely the silent-failure mode being closed.
    """
    import pathlib
    source = pathlib.Path('app.py').read_text(encoding='utf-8')
    markers = re.findall(r'os\.getenv\("([A-Z0-9_]+)"\)', source)
    new_modes = [m for m in markers
                 if m in ('APP_MODE', 'IS_PRODUCTION', 'PRODUCTION',
                          'PRODUCTION_MODE', 'ENVIRONMENT', 'FLASK_ENV')]
    assert not new_modes, (
        'a new production-mode variable was introduced: %s' % new_modes)


# ── logout ────────────────────────────────────────────────────────────────────

def test_d_logout_rejects_get(client, db):
    """GET /logout must not perform the state change.

    This is the CSRF vector: a cross-origin <img src="/logout"> would otherwise
    terminate a signed-in user's session.
    """
    _user(db, 'f55getlogout')
    _login(client, 'f55getlogout')

    r = _get(client, '/logout')
    assert r.status_code == 405, (
        'GET /logout returned %s; it must be 405 Method Not Allowed' % r.status_code)
    # And the user is still logged in.
    assert _get(client, '/profile').status_code == 200, \
        'GET /logout logged the user out anyway'


def test_d_logout_post_works(client, db):
    user = _user(db, 'f55postlogout')
    _login(client, 'f55postlogout')
    assert _get(client, '/profile').status_code == 200

    r = _post(client, '/logout', follow_redirects=True)

    assert r.status_code == 200
    assert _get(client, '/profile').status_code in (302, 401, 403), \
        'POST /logout did not end the session'
    assert user.check_password('TestPass1'), \
        'logout must not disturb the password'


def test_d_logout_requires_a_session(client, db):
    """An anonymous POST is refused, and must not error."""
    r = _post(client, '/logout')
    assert r.status_code in (302, 401, 403)


def test_d_logout_is_csrf_protected(app, client, db):
    """With CSRF enabled, a POST without a token is rejected.

    The offline suite disables CSRF globally (tests/conftest.py), so this
    re-enables it for the duration of the check — otherwise the assertion would
    pass for the wrong reason.
    """
    _user(db, 'f55csrf')
    _login(client, 'f55csrf')
    app.config['WTF_CSRF_ENABLED'] = True
    try:
        # Prime a session so a CSRF token exists, then post without it.
        _get(client, '/profile')
        r = _post(client, '/logout')
        assert r.status_code == 400, (
            'POST /logout without a CSRF token returned %s, expected 400'
            % r.status_code)
        assert _get(client, '/profile').status_code == 200, \
            'the rejected POST still ended the session'

        # With a valid token the same request succeeds.
        ok = _post_logout(client)
        assert ok.status_code in (302, 303), \
            'POST /logout with a CSRF token returned %s' % ok.status_code
    finally:
        app.config['WTF_CSRF_ENABLED'] = False


def test_d_logout_links_are_forms_not_anchors():
    """Every logout control must POST, so CSRFProtect can cover it."""
    import pathlib
    for name in ('base.html', 'edit_profile.html',
                 'partials/profile_dropdown.html'):
        text = pathlib.Path('templates', name).read_text(encoding='utf-8')
        assert 'auth.logout' not in re.sub(
            r'<form[^>]*auth\.logout.*?</form>', '', text, flags=re.S), \
            '%s still reaches logout outside a form' % name


def test_d_no_get_logout_links_remain():
    """Guard against reintroducing a GET logout control."""
    import pathlib
    offenders = []
    for path in pathlib.Path('templates').rglob('*.html'):
        text = path.read_text(encoding='utf-8')
        for match in re.finditer(r'<a[^>]*auth\.logout[^>]*>', text):
            offenders.append('%s: %s' % (path.name, match.group(0)[:80]))
    assert not offenders, 'GET logout links remain: %s' % offenders


# ── local client helpers ──────────────────────────────────────────────────────

def _request(method, client, path, **kwargs):
    """Issue a request through the ordinary client API.

    This used to clear ``g._login_user`` first. That was a per-test workaround
    for the harness defect F56 fixed at its source: an application context held
    open for the whole test made every client request reuse it, so Flask-Login
    cached one identity for the entire test and a brand-new client inherited
    it. ``_RequestScopedClient`` in tests/conftest.py now gives each request
    its own context, so the clearing is gone — and these tests passing without
    it is the evidence that the harness, not the workaround, does the work.
    """
    return client.open(path, method=method, **kwargs)


def _get(client, path, **kwargs):
    return _request('GET', client, path, **kwargs)


def _post(client, path, **kwargs):
    return _request('POST', client, path, **kwargs)


def _forget_all_cookies(client):
    """Drop every cookie the client holds.

    Werkzeug's test client keeps them in ``client._cookies`` keyed by
    ``(domain, path, name)`` and offers no bulk-delete helper.
    """
    client._cookies.clear()


def _second_client():
    """A second, independent test client sharing the same app and database."""
    from app import app as flask_app
    return flask_app.test_client()


def _identity_of(client, session_only=False):
    with client.session_transaction() as sess:
        identity = sess.get('_user_id')
    if identity:
        return identity
    if session_only:
        return None
    # Fall back to decoding the remember-me cookie the client is holding.
    for cookie in client._cookies.values():
        for name, value in cookie.items():
            if name != flask_login_cookie_name():
                continue
            payload = _decode(value)
            if payload:
                return payload
    return None


def flask_login_cookie_name():
    from flask_login.utils import COOKIE_NAME
    return COOKIE_NAME


def _decode(value):
    try:
        from flask_login.utils import decode_cookie
        return decode_cookie(value)
    except Exception:
        return None


def _login_with_remember(client, username):
    """Log in with the remember-me box ticked.

    The field is ``remember_me`` — routes/auth.py tests ``'remember_me' in
    request.form`` — so posting ``remember`` silently logs in WITHOUT a
    remember-me cookie and any test about revocation would prove nothing.
    """
    return _post(client, '/login', data={
        'username': username, 'password': 'TestPass1', 'remember_me': 'y'},
        follow_redirects=True)


def _clear_session_cookie(client):
    """Forget the session cookie but keep the remember-me cookie.

    Models what happens when a user closes their browser: the session cookie
    would be gone while the 30-day remember cookie remains.
    """
    with client.session_transaction() as sess:
        sess.clear()