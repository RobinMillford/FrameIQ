"""F56 — application-context and authentication isolation between requests.

The defect
----------
``pytest-flask`` installs an **autouse** fixture, ``_push_request_context``,
that pushes an ``app.test_request_context()`` for the duration of every test
that touches the ``app`` fixture — directly or transitively, which includes
``db``, ``client``, ``sample_user`` and ``auth_client``. So for most tests an
application context is already bound before the test body runs.

Flask's ``RequestContext.push`` reuses it rather than creating a new one::

    app_ctx = _cv_app.get(None)
    if app_ctx is None or app_ctx.app is not self.app:
        app_ctx = self.app.app_context()
        app_ctx.push()

Because ``app_ctx.app is self.app``, the ``app`` context is *shared*. ``g`` is
therefore one object for the whole test, across every client.

Flask-Login then caches the resolved identity in exactly that shared ``g``::

    def _get_user():
        if has_request_context():
            if "_login_user" not in g:
                current_app.login_manager._load_user()
            return g._login_user

``_load_user()`` runs only when ``g`` has no ``_login_user`` yet. The first
request that authenticates populates it, and every later request — including
one from a brand-new, never-used client — reads the cached value instead of
consulting its own cookies. One login in a process therefore authenticates
every subsequent request, and assertions about "anonymous" become
order-dependent.

On a real server this cannot happen: every request pushes and pops its own
context. The bug is entirely in how the test harness held one context open.

What these tests assert
-----------------------
Every assertion is behavioural, against a ``@login_required`` endpoint, so it
fails if the identity is wrong rather than if some source text moved.
"""
import pytest


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_user(db, username, password='TestPass1'):
    from models import User
    user = User(username=username,
                email='%s@f56.test' % username,
                email_verified=True)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return user


def _second_client(app):
    """An independent client: its own cookie jar, no shared session state."""
    return app.test_client()


def _login(client, username, password='TestPass1'):
    return client.post('/login',
                       data={'username': username, 'password': password},
                       follow_redirects=True)


def _profile(client):
    """GET the protected profile page. 302 = anonymous, 200 = authenticated."""
    return client.get('/profile', follow_redirects=False)


def _is_authenticated(client):
    return _profile(client).status_code == 200


def _identity_on_page(client):
    """The username ``/profile`` rendered, or None when anonymous."""
    response = client.get('/profile', follow_redirects=False)
    if response.status_code != 200:
        return None
    body = response.get_data(as_text=True)
    # The avatar renders current_user.username's initial in upper case and the
    # alt text carries the full username; the heading is the simplest stable
    # marker, so assert on membership of the exact usernames used here.
    return body


def _cleanup(app, usernames):
    """Remove users this module created, in FK-safe order (children first)."""
    from models import db as _db
    with app.app_context():
        from models import User
        for username in usernames:
            _db.session.query(User).filter_by(username=username).delete()
        _db.session.commit()


@pytest.fixture(autouse=True)
def _f56_users(app):
    """Remove every ``f56*`` user after each test, pass or fail.

    The suite's shared temp-file SQLite has no per-test rollback, so a test
    that fails before its own cleanup would leave rows behind and make an
    unrelated later run fail on a UNIQUE(email) collision — obscuring the real
    result. Username-scoped cleanup is unique to this file: it cannot delete
    rows any other test created.
    """
    yield
    from models import db as _db
    with app.app_context():
        from models import User
        _db.session.query(User).filter(
            User.username.like('f56%')).delete(synchronize_session=False)
        _db.session.commit()


# ── 1. an authenticated request must not authenticate a separate client ──────

def test_a_login_does_not_authenticate_an_independent_client(app, client, db):
    """The headline defect: one client logging in poisons the next."""
    _make_user(db, 'f56poison')

    other = _second_client(app)
    assert _is_authenticated(other) is False, \
        'precondition: the second client starts anonymous'

    _login(client, 'f56poison')
    assert _is_authenticated(client) is True, \
        'precondition: the first client is logged in'

    assert _is_authenticated(other) is False, (
        'a client that never sent credentials was authenticated by another '
        'client\'s login — Flask-Login\'s cached identity leaked across '
        'requests')

    _cleanup(app, ['f56poison'])


def test_b_a_brand_new_client_is_anonymous_after_another_login(app, client,
                                                               db):
    """Even a client created after the login must be anonymous."""
    _make_user(db, 'f56brandnew')

    _login(client, 'f56brandnew')
    assert _is_authenticated(client) is True

    fresh = _second_client(app)
    assert _is_authenticated(fresh) is False, (
        'a client constructed after the login was already authenticated')

    _cleanup(app, ['f56brandnew'])


# ── 2. User B must not inherit User A's identity ─────────────────────────────

def test_c_second_user_does_not_inherit_the_first_identity(app, client, db):
    """B logs in as B and must see B, never A."""
    _make_user(db, 'f56alice')
    _make_user(db, 'f56bob')

    alice = _second_client(app)
    _login(alice, 'f56alice')
    assert 'f56alice' in _identity_on_page(alice)

    bob = _second_client(app)
    _login(bob, 'f56bob')

    alice_body = _identity_on_page(alice)
    assert 'f56alice' in alice_body, 'precondition: A is still A'
    assert 'f56bob' not in alice_body, \
        'the first client adopted the second client\'s identity'

    bob_body = _identity_on_page(bob)
    assert 'f56bob' in bob_body, 'B is not B'
    assert 'f56alice' not in bob_body, \
        'B inherited A\'s identity'

    _cleanup(app, ['f56alice', 'f56bob'])


def test_d_identity_is_not_sticky_across_interleaved_clients(app, client, db):
    """Interleaved requests must each resolve their own cookies."""
    _make_user(db, 'f56carol')
    _make_user(db, 'f56dave')

    carol = _second_client(app)
    dave = _second_client(app)
    _login(carol, 'f56carol')
    _login(dave, 'f56dave')

    for _ in range(3):
        assert 'f56carol' in _identity_on_page(carol)
        assert 'f56dave' in _identity_on_page(dave)
        assert 'f56dave' not in _identity_on_page(carol)
        assert 'f56carol' not in _identity_on_page(dave)

    _cleanup(app, ['f56carol', 'f56dave'])


# ── 3. logout in one client must not disturb another ─────────────────────────

def test_e_logout_in_one_client_leaves_the_other_alone(app, client, db):
    _make_user(db, 'f56erin')
    _make_user(db, 'f56fiona')

    erin = _second_client(app)
    fiona = _second_client(app)
    _login(erin, 'f56erin')
    _login(fiona, 'f56fiona')
    assert _is_authenticated(erin) and _is_authenticated(fiona), \
        'precondition: both are logged in'

    # Log out is POST-only (F55).
    erin.post('/logout', follow_redirects=True)

    assert _is_authenticated(erin) is False, 'logout did not end that session'
    assert _is_authenticated(fiona) is True, (
        'logging out one client ended an unrelated client\'s session')
    assert 'f56fiona' in _identity_on_page(fiona), \
        'the surviving client lost its identity'

    _cleanup(app, ['f56erin', 'f56fiona'])


def test_f_logout_by_a_leaked_identity_does_not_log_out_b(app, client, db):
    """A's logout must not revoke B, and vice versa."""
    _make_user(db, 'f56gina')
    _make_user(db, 'f56hank')

    gina = _second_client(app)
    hank = _second_client(app)
    _login(gina, 'f56gina')
    _login(hank, 'f56hank')

    gina.post('/logout', follow_redirects=True)

    assert _is_authenticated(hank) is True, (
        'A\'s logout revoked B\'s session')
    assert 'f56hank' in _identity_on_page(hank)

    _cleanup(app, ['f56gina', 'f56hank'])


# ── 4. legitimate per-client session behaviour is preserved ──────────────────

def test_g_successive_requests_from_one_client_stay_authenticated(app, client,
                                                                  db):
    """The fix must not break normal session continuity."""
    _make_user(db, 'f56iris')
    _login(client, 'f56iris')

    for attempt in range(5):
        response = _profile(client)
        assert response.status_code == 200, (
            'request %d of a valid session was not authenticated' % (attempt + 1))
        assert 'f56iris' in response.get_data(as_text=True)

    _cleanup(app, ['f56iris'])


def test_h_anonymous_client_stays_anonymous_across_many_requests(app, client,
                                                                 db):
    """Repeated anonymous requests must never latch onto an identity."""
    anonymous = _second_client(app)
    _make_user(db, 'f56jack')
    _login(client, 'f56jack')

    for _ in range(3):
        assert _is_authenticated(anonymous) is False, \
            'an anonymous client became authenticated by repetition alone'

    _cleanup(app, ['f56jack'])


def test_i_logout_then_anonymous_stays_anonymous(app, client, db):
    _make_user(db, 'f56kate')
    _login(client, 'f56kate')
    client.post('/logout', follow_redirects=True)

    for _ in range(3):
        assert _is_authenticated(client) is False

    _cleanup(app, ['f56kate'])


# ── 5. order independence ────────────────────────────────────────────────────

@pytest.mark.parametrize('which', ['first', 'second'])
def test_j_authentication_does_not_depend_on_which_client_runs_first(
        app, client, db, which):
    """The same two-step sequence must pass with either client leading."""
    _make_user(db, 'f56liam')
    _make_user(db, 'f56maya')

    one = _second_client(app)
    two = _second_client(app)

    if which == 'first':
        _login(one, 'f56liam')
        assert _is_authenticated(two) is False
        _login(two, 'f56maya')
        assert 'f56liam' in _identity_on_page(one)
        assert 'f56maya' in _identity_on_page(two)
    else:
        _login(two, 'f56maya')
        assert _is_authenticated(one) is False
        _login(one, 'f56liam')
        assert 'f56maya' in _identity_on_page(two)
        assert 'f56liam' in _identity_on_page(one)

    _cleanup(app, ['f56liam', 'f56maya'])


def test_k_sequential_single_client_tests_are_independent(app, client, db):
    """Two logins in sequence, one client: the second must win cleanly."""
    _make_user(db, 'f56nina')
    _make_user(db, 'f56oscar')

    _login(client, 'f56nina')
    assert 'f56nina' in _identity_on_page(client)

    client.post('/logout', follow_redirects=True)
    _login(client, 'f56oscar')
    assert 'f56oscar' in _identity_on_page(client)
    assert 'f56nina' not in _identity_on_page(client)

    _cleanup(app, ['f56nina', 'f56oscar'])


# ── the mechanism, asserted directly ─────────────────────────────────────────

def _observe_per_request(app, paths, client=None):
    """Record ``id(g)`` and the identity seen by ``current_user`` INSIDE each request.

    Reading ``g`` from the test body is meaningless here: the harness context is
    restored after every request, so the test body always sees that one object.
    The question is whether each REQUEST gets its own, which can only be
    observed from within the request, so a temporary ``before_request`` hook
    does the recording and is removed again afterwards.

    Two Flask details make this easy to get wrong, and both produced a
    false "shared" reading first:

    * ``flask.g`` is a ``LocalProxy`` — one proxy object is created at import
      and reused for every context, so ``id(g)`` is constant. The namespace
      behind it is what has to be compared, via ``g._get_current_object()``.
    * A context popped at the end of a request is freed, so the next request's
      context is very likely allocated at the same address. The objects are
      therefore retained in ``seen`` so the ids below cannot be recycled.
    """
    from flask import g
    from flask_login import current_user

    seen = []
    caller = client if client is not None else app.test_client()

    def _record():
        seen.append({
            # The `g` OBJECT, not its id. A context popped at the end of a
            # request is freed, so the next request's context is very likely
            # allocated at the same address — comparing `id(g)` across requests
            # reports a false collision. Retaining the objects keeps every
            # allocation alive, so the ids below are genuinely distinct.
            'g': g._get_current_object(),
            'auth': bool(current_user.is_authenticated),
            'user': getattr(current_user, 'username', None),
        })

    hooks = app.before_request_funcs
    hooks.setdefault(None, []).append(_record)
    try:
        for path in paths:
            caller.get(path, follow_redirects=False)
    finally:
        try:
            hooks[None].remove(_record)
        except ValueError:  # pragma: no cover - defensive
            pass
    return seen


def test_l_each_request_gets_its_own_application_context(app, client, db):
    """``g`` must not be one shared object across client requests.

    This is the underlying cause rather than a symptom: with a shared ``g``,
    Flask-Login's ``"_login_user" not in g`` short-circuit can return a stale
    identity. Asserting the context lifecycle directly keeps the harness honest
    even if a future endpoint is more forgiving than ``/profile``.
    """
    _make_user(db, 'f56percy')
    _login(client, 'f56percy')

    seen = _observe_per_request(app, ['/profile', '/profile', '/profile'], client)

    distinct = len({id(entry['g']) for entry in seen})
    assert len(seen) == 3, 'the probe did not observe every request'
    assert distinct == 3, (
        'requests shared one `g` object (%d distinct over %d requests) — the '
        'application context is being reused, so any per-request cached state '
        'leaks' % (distinct, len(seen)))
    # And every request still resolved the same user from its own cookie,
    # i.e. the isolation did not break legitimate sessions.
    assert {entry['user'] for entry in seen} == {'f56percy'}


def test_m_flask_login_identity_is_recomputed_per_request(app, client, db):
    """An anonymous request must not inherit the previous request's identity."""
    _make_user(db, 'f56quinn')
    other = _second_client(app)

    _login(client, 'f56quinn')

    # The authenticated request first, then the anonymous one — the ordering
    # that used to leak.
    seen = _observe_per_request(app, ['/profile'], client)
    seen += _observe_per_request(app, ['/profile'], other)

    assert seen[0]['auth'] is True, 'precondition: the first client is logged in'
    assert seen[0]['user'] == 'f56quinn'
    assert seen[1]['auth'] is False, (
        'the anonymous request was authenticated as %r — Flask-Login\'s cached '
        'identity crossed requests' % (seen[1]['user'],))
    assert seen[1]['user'] is None

# ── the framework contract the harness depends on (F57) ──────────────────────
#
# `_RequestScopedClient` unbinds `flask.globals._cv_app` around each request.
# That is a PRIVATE Flask internal, and the whole fix rests on two framework
# behaviours:
#
#   1. `RequestContext.push` creates a FRESH app context when `_cv_app` is
#      unbound (`if app_ctx is None or app_ctx.app is not self.app:`).
#   2. It REUSES the bound one otherwise — which is the bug being fixed.
#
# Flask 3.1.3 is pinned in requirements.txt and satisfies both. These tests
# assert the behaviours rather than reading Flask's source, so a future
# upgrade that changes the reuse rule fails HERE, pointing at the harness,
# instead of silently re-enabling the identity leak everywhere.


def test_the_unbinding_assumption_holds_flask_binds_a_fresh_context(app):
    """With `_cv_app` unbound, Flask must create the request's own context."""
    from flask.globals import _cv_app

    outer = _cv_app.get(None)
    assert outer is not None, 'precondition: a harness context is bound'

    token = _cv_app.set(None)
    try:
        with app.test_request_context('/'):
            inner = _cv_app.get(None)
            assert inner is not outer, (
                'Flask reused the ambient application context even with '
                '_cv_app unbound — the harness fix would no longer isolate '
                'requests')
            assert inner.app is app
    finally:
        _cv_app.reset(token)

    assert _cv_app.get(None) is outer, 'the probe did not restore the binding'


def test_the_reuse_assumption_holds_flask_binds_the_ambient_context(app):
    """With `_cv_app` bound, Flask reuses it — this is the leak itself.

    Asserted so the two halves cannot drift apart: if a future Flask stopped
    reusing, the first test would still pass and this one would fail, showing
    the harness comment no longer describes the framework.
    """
    from flask.globals import _cv_app

    outer = _cv_app.get(None)
    assert outer is not None, 'precondition: a harness context is bound'

    with app.test_request_context('/'):
        assert _cv_app.get(None) is outer, (
            'Flask no longer reuses the ambient application context; the '
            'F56 harness fix exists to work around that reuse and should be '
            're-evaluated')


# ── the harness's own restore guarantees ─────────────────────────────────────


def test_the_harness_context_is_restored_after_a_request(app, client, db):
    """By identity, not merely "something is bound"."""
    from flask.globals import _cv_app

    outer = _cv_app.get(None)
    client.get('/login')
    assert _cv_app.get(None) is outer, (
        'the request left a different application context bound for the test '
        'body — `db.session` and `url_for` would break')


def test_the_harness_context_is_restored_when_a_request_raises(app, client,
                                                                db, monkeypatch):
    """The restore is in a `finally`; an exploding request must not skip it.

    Patching the base class rather than the subclass is deliberate: it makes
    `super().open()` raise from inside `_RequestScopedClient.open`, which is
    the exact path the `finally` has to cover.
    """
    from flask.globals import _cv_app
    from flask.testing import FlaskClient

    outer = _cv_app.get(None)

    def boom(self, *args, **kwargs):
        raise RuntimeError('dispatch exploded')

    monkeypatch.setattr(FlaskClient, 'open', boom)

    with pytest.raises(RuntimeError, match='dispatch exploded'):
        client.get('/login')

    assert _cv_app.get(None) is outer, (
        'a failing request left the application context unbound, corrupting '
        'every later step of this test')


def test_a_request_does_not_run_the_harness_context_teardown(app, client, db):
    """Restoring must not have run the harness context's teardown.

    `AppContext.pop()` calls `do_teardown_appcontext` BEFORE unbinding, which
    makes Flask-SQLAlchemy call `db.session.remove()` for the harness context
    and detach every instance the test is still holding. That is exactly the
    bug the `set(None)` approach avoids, so it needs a test.
    """
    from models import User

    user = User(username='f57attached', email='f57attached@f56.test',
                email_verified=True)
    user.set_password('TestPass1')
    db.session.add(user)
    db.session.commit()

    client.get('/login')

    # Still attached — not detached by a stray session.remove().
    assert user.username == 'f57attached'

    # And uncommitted work is still pending, not rolled back or committed.
    pending = User(username='f57pending', email='f57pending@f56.test',
                   email_verified=True)
    pending.set_password('TestPass1')
    db.session.add(pending)
    db.session.flush()
    client.get('/login')
    try:
        assert User.query.filter_by(username='f57pending').first() is not None
    finally:
        db.session.rollback()


def test_requests_never_cache_an_identity_in_the_harness_context(app, client,
                                                                 db):
    """Isolation is structural, not simulated by clearing `g._login_user`.

    The F55 workaround popped `_login_user` off `g` before every request. If
    the harness were still doing that, the identity cache would never appear on
    the harness context either — so the proof that the fix is structural is
    that the *request's* context holds the identity and the harness's does not,
    and that this is achieved by giving the request its own `g` at all.
    """
    from flask import g
    from flask_login import current_user

    _make_user(db, 'f57structural')
    _login(client, 'f57structural')

    harness_g = g._get_current_object()
    before_keys = set(harness_g)

    client.get('/profile')

    # The request must not have written ANYTHING into the harness context, not
    # even the one key this defect was about. That is what makes the isolation
    # structural: a request that shared the harness `g` would add `_login_user`
    # to it, and a harness that merely cleared `_login_user` before each
    # request would leave other per-request state behind too.
    assert set(harness_g) == before_keys, (
        'the request wrote %r into the harness context; requests must not '
        'touch it at all'
        % (set(harness_g) - before_keys))
    assert '_login_user' not in harness_g, (
        'the harness context accumulated a login identity, which means '
        'isolation is still being simulated rather than structural')

    # And the request that did authenticate saw it in ITS OWN context.
    seen = []
    hook = app.before_request_funcs.setdefault(None, [])

    def _record():
        seen.append((id(g._get_current_object()),
                     bool(current_user.is_authenticated)))

    hook.append(_record)
    try:
        client.get('/profile')
    finally:
        hook.remove(_record)

    assert seen and seen[-1][1] is True, (
        'the authenticated request did not resolve a user in its own context')
    assert seen[-1][0] != id(harness_g), (
        'the request reused the harness context after all')
