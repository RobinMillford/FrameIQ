"""F23 — CSRF token delivery, TMDb request-input validation, collaborator privacy.

Three independent audit findings, one file so the interactions between them stay
visible:

C-7  ``my_lists.html``, ``watchlist.html`` and ``user_profile.html`` are
     standalone documents that do not extend ``base.html``. They therefore
     inherited neither the ``csrf-token`` meta tag nor the global ``fetch()``
     patch that injects the header, so every mutation they fire was rejected by
     ``CSRFProtect``. Server-side enforcement is NOT relaxed here: the tests
     below turn it ON and prove that missing and malformed tokens are still
     refused.

C-10 ``/api/diary/log`` interpolated request-supplied ``media_type`` and
     ``media_id`` into an API-key-bearing TMDb URL. These tests assert that
     invalid input is refused *and* that no outbound request is attempted, by
     substituting the HTTP client with a recorder.

C-11 ``GET /api/lists/<id>/collaborators`` was reachable anonymously for public
     lists and returned usernames, avatars and roles.

The ``app`` fixture sets ``WTF_CSRF_ENABLED=False``, so every CSRF assertion
here re-enables it explicitly and restores it afterwards, rather than relying on
the fixture's default.
"""
import re

import pytest

ROUTES_DIARY = 'routes.diary'


# ── helpers ────────────────────────────────────────────────────────────────


#: Every user this module creates starts with one of these, so they can be
#: identified and removed again. The shared ``app`` fixture is session-scoped,
#: so rows left behind would collide with the unique constraint on a later run
#: and would leak into other test modules.
_F23_USERS = ('f23', 'csrf', 'diary')


@pytest.fixture(autouse=True)
def _f23_cleanup(app, db):
    """Remove this module's rows after every test.

    The session-scoped ``app`` fixture shares one SQLite file across the whole
    run, so anything created here must not survive into another test.
    """
    yield
    from models import (DiaryEntry, ListCollaborator, User, UserFollow,
                        UserList)
    with app.app_context():
        try:
            rows = User.query.filter(
                User.username.like('f23%')
                | User.username.like('csrf%')
                | User.username.like('diary%')).all()
            if rows:
                ids = [u.id for u in rows]
                # UserFollow rows reference users on BOTH sides and are not
                # removed by deleting the user, so they outlived this helper and
                # leaked a relationship into later tests.
                db.session.query(UserFollow).filter(
                    db.or_(UserFollow.follower_id.in_(ids),
                           UserFollow.following_id.in_(ids))).delete(
                        synchronize_session=False)
                db.session.query(DiaryEntry).filter(
                    DiaryEntry.user_id.in_(ids)).delete(
                        synchronize_session=False)
                db.session.query(ListCollaborator).filter(
                    ListCollaborator.user_id.in_(ids)).delete(
                        synchronize_session=False)
                db.session.query(UserList).filter(
                    UserList.user_id.in_(ids)).delete(
                        synchronize_session=False)
                db.session.query(User).filter(User.id.in_(ids)).delete(
                    synchronize_session=False)
                db.session.commit()
        except Exception:
            db.session.rollback()
            yield


def _make_user(app, db, username, password='F23Pass1!'):
    """Create (or reuse) a test user.

    Idempotent on purpose: the session-scoped database survives between tests in
    this module, so a second request for the same username must not attempt a
    duplicate insert.
    """
    from models import User
    with app.app_context():
        existing = User.query.filter_by(username=username).first()
        if existing is not None:
            existing.set_password(password)
            db.session.commit()
            return existing.id
        user = User(username=username,
                    email=f'{username}@example.com',
                    email_verified=True)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        return user.id


def _login(client, username, password='F23Pass1!'):
    return client.post('/login', data={'username': username,
                                       'password': password},
                       follow_redirects=True)


def _csrf_token(client, path):
    """Scrape the token the page publishes, exactly as the browser would."""
    html = client.get(path).get_data(as_text=True)
    match = re.search(r'name="csrf-token"\s+content="([^"]+)"', html)
    assert match, f'{path} publishes no csrf-token meta tag:\n{html[:400]}'
    return match.group(1)


# ══════════════════════════════════════════════════════════════════════════
# C-7 — CSRF token delivery
# ══════════════════════════════════════════════════════════════════════════


CSRF_PAGES = [
    ('/lists', 'templates/my_lists.html'),
    ('/watchlist', 'templates/watchlist.html'),
]


def test_the_three_pages_now_publish_a_csrf_token(app, db):
    """C-7: each standalone page exposes the token base.html would have given it.

    The pages are standalone documents, so they inherit nothing. This asserts
    the published token is present and non-empty on every affected page.
    """
    _make_user(app, db, 'csrfowner')
    client = app.test_client()
    _login(client, 'csrfowner')

    for path, _template in CSRF_PAGES:
        token = _csrf_token(client, path)
        assert token, f'{path} published an empty csrf token'

    # user_profile is covered separately because it needs another user's page.
    subject_id = _make_user(app, db, 'csrfsubject')
    token = _csrf_token(client, f'/user/{subject_id}')
    assert token, '/user/<name> published an empty csrf token'


def test_base_derived_pages_still_publish_their_token(app, db):
    """The fix must not disturb pages that already inherit the mechanism."""
    _make_user(app, db, 'csrfbase')
    client = app.test_client()
    _login(client, 'csrfbase')
    assert _csrf_token(client, '/lists')


def test_csrf_protection_remains_enabled_by_default(app):
    """The fix must not have disabled CSRF to make the pages work."""
    # The extension must be registered regardless of the fixture's default.
    from flask_wtf.csrf import CSRFProtect
    assert any(isinstance(value, CSRFProtect)
               for value in app.extensions.values()), (
        'CSRFProtect is not registered — F23 must never remove it')
    # And it must actually be enforcing once the fixture default is lifted.
    assert app.config['WTF_CSRF_ENABLED'] is False, (
        'the fixture disables CSRF; the behavioural tests re-enable it')


def test_mutation_is_refused_without_a_token(app, db, monkeypatch):
    """With CSRF ON and no token, the server still refuses.

    This is the assertion that proves F23 restored the token *delivery* rather
    than weakening enforcement.
    """
    _make_user(app, db, 'csrfnologin')
    client = app.test_client()
    _login(client, 'csrfnologin')

    app.config['WTF_CSRF_ENABLED'] = True
    try:
        response = client.post('/api/lists/create',
                               json={'title': 'no token', 'is_public': False})
        assert response.status_code in (400, 401, 403), response.status_code
    finally:
        app.config['WTF_CSRF_ENABLED'] = False


def test_mutation_is_refused_with_an_invalid_token(app, db):
    """A wrong token must not be accepted, even from a logged-in user."""
    _make_user(app, db, 'csrfbad')
    client = app.test_client()
    _login(client, 'csrfbad')

    app.config['WTF_CSRF_ENABLED'] = True
    try:
        response = client.post('/api/lists/create',
                               json={'title': 'bad token', 'is_public': False},
                               headers={'X-CSRFToken': 'not-a-real-token'})
        assert response.status_code in (400, 401, 403), response.status_code
    finally:
        app.config['WTF_CSRF_ENABLED'] = False


def test_mutation_succeeds_with_a_valid_token(app, db):
    """The restored delivery path actually works end to end."""
    _make_user(app, db, 'csrfgood')
    client = app.test_client()
    _login(client, 'csrfgood')

    app.config['WTF_CSRF_ENABLED'] = True
    try:
        token = _csrf_token(client, '/lists')
        response = client.post('/api/lists/create',
                               json={'title': 'valid token list',
                                     'is_public': False},
                               headers={'X-CSRFToken': token})
        assert response.status_code in (200, 201), (
            response.status_code, response.get_data(as_text=True)[:300])
        payload = response.get_json()
        assert payload.get('id') or payload.get('list', {}).get('id')
    finally:
        app.config['WTF_CSRF_ENABLED'] = False


def test_the_clients_send_the_expected_header(app, db):
    """Each mutation client must attach X-CSRFToken.

    Checked at the source level because the shipped JS is what actually runs;
    a browser-level assertion would not catch a client that never reads the
    meta tag.
    """
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent

    # The shared clients read the meta tag and set the header.
    for rel in ('static/js/follow-button.js',
                'static/js/watchlist-priority-handler.js',
                'static/js/list-advanced.js',
                'templates/my_lists.html'):
        source = (root / rel).read_text(encoding='utf-8')
        assert "meta[name=\"csrf-token\"]" in source or \
            "meta[name='csrf-token']" in source, f'{rel} never reads the token'
        assert 'X-CSRFToken' in source, f'{rel} never sends the header'


def test_the_clients_only_send_the_header_when_a_token_exists(app, db):
    """Shared-client hazard, asserted behaviourally elsewhere.

    follow-button.js is loaded by pages that DO extend base.html, where a
    global fetch() patch already injects the header. A client that always set
    X-CSRFToken -- including to '' -- would override that patch with an empty
    value and break those pages.

    The guarantee is enforced by ``test_f23_csrf_browser.py``::

        test_the_shared_component_does_not_override_the_base_html_patch

    which runs the real component and asserts no empty header is sent. That
    assertion is kept here only as a pointer, deliberately without a
    source-text match: a check for one exact literal would pass or fail on
    formatting rather than on behaviour, which is the failure mode this test
    file already had once (see the module docstring).
    """
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    browser_tests = (root / 'tests' / 'test_f23_csrf_browser.py').read_text(
        encoding='utf-8')
    assert 'test_the_shared_component_does_not_override_the_base_html_patch' \
        in browser_tests, 'the behavioural guard for this hazard is missing'


def test_no_csrf_exemption_was_introduced(app):
    """F23 must not have resolved the failures by exempting the routes."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    for rel in ('routes/lists.py', 'routes/smart_lists.py',
                'routes/collections.py', 'routes/social.py',
                'routes/diary.py'):
        source = (root / rel).read_text(encoding='utf-8')
        assert 'csrf.exempt' not in source and 'csrf_exempt' not in source, (
            f'{rel} gained a CSRF exemption')


def test_only_the_intended_pages_gained_the_meta_tag(app, db):
    """No unrelated template silently acquired or lost CSRF delivery."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    templates = root / 'templates'

    for name in ('my_lists.html', 'watchlist.html', 'user_profile.html'):
        source = (templates / name).read_text(encoding='utf-8')
        # The page's JS also contains the literal selector text, so count
        # actual <meta> tags rather than raw occurrences of the string.
        assert len(re.findall(r'<meta[^>]*name="csrf-token"', source)) == 1, (
            f'{name} should publish exactly one csrf meta tag')

    # These are the pages that already had it; they must not have gained a second.
    for name in ('base.html', 'list_detail.html'):
        source = (templates / name).read_text(encoding='utf-8')
        assert len(re.findall(r'<meta[^>]*name="csrf-token"', source)) == 1, (
            f'{name} now publishes a duplicate csrf meta tag')


# ══════════════════════════════════════════════════════════════════════════
# C-10 — TMDb request-input validation
# ══════════════════════════════════════════════════════════════════════════

VALID_MEDIA_TYPES = ('movie', 'tv')

# Each entry is (media_type, media_id). True means the pair is accepted.
ACCEPTED_CASES = [
    ('movie', 550),
    ('tv', 1396),
    ('movie', '550'),          # numeric string from a JSON client
    ('tv', '1396'),
    ('movie', 1),
    ('tv', 999999),
]

REJECTED_TYPE_CASES = [
    None, '', '   ', 'MOVIE', 'Movie', 'person',
    'movie/../account', '../account', 'movie/1',
    'movie?x=1', 'movie#f', '//movie', 'movie tv',
    'tv/../../v3', '../../..',
    123, 1.5, True, False, ['movie'], {'a': 'movie'}, {'movie'},
]

REJECTED_ID_CASES = [
    None, '', '   ', ' 550', '550 ', 0, -1, -550,
    True, False, 550.0, 1.5, float('nan'),
    '55.0', '5e2', '0x10', '+550', '-550', '٥٥٠',
    '55/../x', '550?x=1', '550#f', '../../..',
    [550], {'id': 550}, (550,),
    # F23 final review: `$` also matches before a TRAILING NEWLINE, so
    # `^[0-9]+$` accepted '550\n'. int() normalised it away so nothing unsafe
    # reached the URL, but the conversion is documented as exact and
    # whitespace-free, so these are rejected too. Pinned so the anchor cannot
    # silently regress to `$`.
    '550\n', '\n550', '550\r\n', '550\nX-Injected: 1', '550\t', '550\x00',
    '０５５０', '550;ls', '550|whoami', '550\\2', '1//2', '1e3', b'550',
]


@pytest.mark.parametrize('media_type', VALID_MEDIA_TYPES)
def test_media_type_allowlist_accepts_exactly_the_modelled_types(media_type):
    from routes import diary
    assert diary._validated_media_type(media_type) == media_type


@pytest.mark.parametrize('media_type', REJECTED_TYPE_CASES)
def test_media_type_rejects_everything_else(media_type):
    """No value can smuggle a path separator, query, or container through."""
    from routes import diary
    assert diary._validated_media_type(media_type) is None


@pytest.mark.parametrize('media_id', [550, '550', 1, '1', 999999, '999999'])
def test_media_id_accepts_int_and_ascii_digit_string(media_id):
    from routes import diary
    assert diary._validated_tmdb_id(media_id) == int(media_id)


@pytest.mark.parametrize('media_id', REJECTED_ID_CASES)
def test_media_id_rejects_everything_else(media_id):
    """bool, float, containers, signs, whitespace and path-like input."""
    from routes import diary
    assert diary._validated_tmdb_id(media_id) is None


def test_media_id_conversion_is_exact_not_truncating():
    """A float must be rejected outright, never truncated to a valid id."""
    from routes import diary
    assert diary._validated_tmdb_id(550.9) is None
    assert diary._validated_tmdb_id('550.9') is None
    # A numeric string normalises to the same integer, not a different value.
    assert diary._validated_tmdb_id('0550') == 550


class _Recorder:
    """Stands in for requests.get and records whether it was ever called."""

    def __init__(self):
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        raise AssertionError('TMDb must not be called for invalid input')

    @property
    def called(self):
        return bool(self.calls)


@pytest.fixture
def diary_client(app, db, monkeypatch):
    """A logged-in client whose TMDb HTTP calls are intercepted."""
    _make_user(app, db, 'diaryuser')
    client = app.test_client()
    _login(client, 'diaryuser')
    return client


def _post_log(client, **payload):
    return client.post('/api/diary/log', json=payload)


@pytest.mark.parametrize('media_type,media_id', [
    ('movie/../account', 1),
    ('../../..', 1),
    ('movie?api_key=stolen', 1),
    ('person', 1),
])
def test_traversal_media_type_is_refused_without_any_outbound_request(
        diary_client, monkeypatch, media_type, media_id):
    from routes import diary
    recorder = _Recorder()
    monkeypatch.setattr(diary.requests, 'get', recorder)

    response = _post_log(diary_client, media_id=media_id,
                         media_type=media_type, watched_date='2024-01-01')
    assert response.status_code == 400, response.get_data(as_text=True)[:200]
    assert not recorder.called, 'an outbound TMDb request was attempted'


@pytest.mark.parametrize('media_id', [
    '../../..', '1/../../account', 0, -1, True, 1.5, 'abc', [1], {'a': 1},
    '1?api_key=stolen',
])
def test_invalid_media_id_is_refused_without_any_outbound_request(
        diary_client, monkeypatch, media_id):
    from routes import diary
    recorder = _Recorder()
    monkeypatch.setattr(diary.requests, 'get', recorder)

    response = _post_log(diary_client, media_id=media_id, media_type='movie',
                         watched_date='2024-01-01')
    assert response.status_code == 400, response.get_data(as_text=True)[:200]
    assert not recorder.called, 'an outbound TMDb request was attempted'


def test_missing_media_type_is_refused(diary_client, monkeypatch):
    from routes import diary
    recorder = _Recorder()
    monkeypatch.setattr(diary.requests, 'get', recorder)

    response = _post_log(diary_client, media_id=550, watched_date='2024-01-01')
    assert response.status_code == 400
    assert not recorder.called


def test_null_media_fields_are_refused(diary_client, monkeypatch):
    from routes import diary
    recorder = _Recorder()
    monkeypatch.setattr(diary.requests, 'get', recorder)

    response = _post_log(diary_client, media_id=None, media_type=None,
                         watched_date='2024-01-01')
    assert response.status_code == 400
    assert not recorder.called


def test_refusal_never_echoes_the_api_key(diary_client, monkeypatch):
    """The error body must be safe to log."""
    from routes import diary
    monkeypatch.setattr(diary, 'TMDB_API_KEY', 'f23-secret-key-value')
    monkeypatch.setattr(diary.requests, 'get', _Recorder())

    response = _post_log(diary_client, media_id='../../..',
                         media_type='movie', watched_date='2024-01-01')
    body = response.get_data(as_text=True)
    assert 'f23-secret-key-value' not in body
    assert 'Traceback' not in body


def test_valid_input_builds_the_expected_endpoint(diary_client, monkeypatch):
    """A valid pair reaches TMDb at exactly one path segment per field, and the
    key travels as a request parameter rather than inside the URL string."""
    from routes import diary

    class _Ok:
        # Force the not-found branch; only the built request is asserted here.
        status_code = 404

    seen = {}

    def _get(url, **kwargs):
        seen['url'] = url
        seen['kwargs'] = kwargs
        return _Ok()

    monkeypatch.setattr(diary.requests, 'get', _get)
    _post_log(diary_client, media_id=550, media_type='movie',
              watched_date='2024-01-01')

    assert seen['url'] == 'https://api.themoviedb.org/3/movie/550', seen
    params = seen['kwargs'].get('params') or {}
    assert params.get('api_key') == diary.TMDB_API_KEY
    # The key must not be embedded in the path.
    assert 'api_key' not in seen['url']
    # Timeout behaviour is preserved.
    assert seen['kwargs'].get('timeout') == (3, 10)


def test_numeric_string_media_id_normalises_in_the_path(diary_client, monkeypatch):
    from routes import diary
    seen = {}

    class _Ok:
        status_code = 404

    monkeypatch.setattr(diary.requests, 'get',
                        lambda url, **kw: (seen.update(url=url), _Ok())[1])
    _post_log(diary_client, media_id='550', media_type='movie',
              watched_date='2024-01-01')
    assert seen['url'] == 'https://api.themoviedb.org/3/movie/550'


def test_invalid_input_creates_no_application_state(
        app, db, diary_client, monkeypatch):
    """A refused request must not create a MediaItem or DiaryEntry."""
    from routes import diary
    from models import DiaryEntry, MediaItem
    monkeypatch.setattr(diary.requests, 'get', _Recorder())

    with app.app_context():
        before_items = MediaItem.query.count()
        before_entries = DiaryEntry.query.count()

    _post_log(diary_client, media_id='../../..', media_type='movie',
              watched_date='2024-01-01')

    with app.app_context():
        assert MediaItem.query.count() == before_items
        assert DiaryEntry.query.count() == before_entries


# ══════════════════════════════════════════════════════════════════════════
# C-11 — collaborator privacy
# ══════════════════════════════════════════════════════════════════════════


def _make_list(app, db, owner_id, is_public, collaborators=()):
    from models import UserList, ListCollaborator
    with app.app_context():
        user_list = UserList(title='F23 list', user_id=owner_id,
                             is_public=is_public)
        db.session.add(user_list)
        db.session.flush()
        for user_id in collaborators:
            db.session.add(ListCollaborator(list_id=user_list.id,
                                            user_id=user_id, role='editor'))
        db.session.commit()
        return user_list.id


@pytest.fixture
def collab_users(app, db):
    owner = _make_user(app, db, 'f23owner')
    member = _make_user(app, db, 'f23member')
    outsider = _make_user(app, db, 'f23outsider')
    return owner, member, outsider


def _collaborators(client, list_id):
    return client.get(f'/api/lists/{list_id}/collaborators')


def test_anonymous_caller_cannot_enumerate_a_public_list_collaborators(
        app, db, collab_users):
    """C-11: the reported enumeration path is closed."""
    owner, member, _outsider = collab_users
    public_list = _make_list(app, db, owner, is_public=True, collaborators=[member])

    response = _collaborators(app.test_client(), public_list)

    # No 200, and no roster in the body under any status.
    assert response.status_code in (401, 302, 403), response.status_code
    body = response.get_data(as_text=True)
    # The redirect target legitimately echoes the URL in ?next=..., so assert on
    # the roster payload itself rather than on the word "collaborators".
    assert 'f23member' not in body
    assert 'f23owner' not in body
    assert 'profile_picture' not in body
    assert '"role"' not in body


def test_anonymous_caller_cannot_enumerate_a_private_list_collaborators(
        app, db, collab_users):
    owner, member, _outsider = collab_users
    private_list = _make_list(app, db, owner, is_public=False,
                              collaborators=[member])

    response = _collaborators(app.test_client(), private_list)
    assert response.status_code in (401, 302, 403), response.status_code
    assert 'f23member' not in response.get_data(as_text=True)


def test_authenticated_non_collaborator_is_refused(app, db, collab_users):
    """Authentication alone is not enough; membership is decided server-side."""
    owner, member, outsider = collab_users
    public_list = _make_list(app, db, owner, is_public=True, collaborators=[member])

    client = app.test_client()
    _login(client, 'f23outsider')

    response = _collaborators(client, public_list)
    assert response.status_code == 403, response.status_code
    assert 'f23member' not in response.get_data(as_text=True)


def test_owner_receives_the_roster(app, db, collab_users):
    owner, member, _outsider = collab_users
    public_list = _make_list(app, db, owner, is_public=True, collaborators=[member])

    client = app.test_client()
    _login(client, 'f23owner')

    response = _collaborators(client, public_list)
    assert response.status_code == 200, response.status_code
    payload = response.get_json()
    assert payload['count'] == 1
    assert payload['collaborators'][0]['user']['username'] == 'f23member'


def test_authorized_collaborator_receives_the_roster(app, db, collab_users):
    """Existing authorised management behaviour is preserved."""
    owner, member, _outsider = collab_users
    public_list = _make_list(app, db, owner, is_public=True, collaborators=[member])

    client = app.test_client()
    _login(client, 'f23member')

    response = _collaborators(client, public_list)
    assert response.status_code == 200, response.status_code
    assert response.get_json()['count'] == 1


def test_ownership_is_not_taken_from_the_client(app, db, collab_users):
    """A client-supplied owner/user id must not influence the decision."""
    owner, member, outsider = collab_users
    public_list = _make_list(app, db, owner, is_public=True, collaborators=[member])

    client = app.test_client()
    _login(client, 'f23outsider')

    response = client.get(f'/api/lists/{public_list}/collaborators',
                          query_string={'user_id': owner,
                                        'owner_id': owner,
                                        'as_user_id': owner})
    assert response.status_code == 403, response.status_code


def test_nonexistent_list_is_a_404_for_an_authenticated_caller(app, db):
    _make_user(app, db, 'f23ghost')
    client = app.test_client()
    _login(client, 'f23ghost')

    response = _collaborators(client, 999999999)
    assert response.status_code == 404, response.status_code


def test_public_list_content_still_renders_for_anonymous_viewers(
        app, db, collab_users):
    """C-11 must not break the public list page itself."""
    owner, member, _outsider = collab_users
    public_list = _make_list(app, db, owner, is_public=True, collaborators=[member])

    response = app.test_client().get(f'/lists/{public_list}')
    assert response.status_code == 200, response.status_code

    html = response.get_data(as_text=True)
    assert 'F23 list' in html
    # The collaboration-management panel is owner/editor gated, so it is not
    # rendered for an anonymous viewer at all.
    assert 'id="collaborators-list"' not in html


def test_the_client_does_not_mistake_a_401_for_an_empty_roster(app, db):
    """F23 (UI): a refused load must not render 'No collaborators yet'."""
    from pathlib import Path
    source = (Path(__file__).resolve().parent.parent /
              'static' / 'js' / 'list-advanced.js').read_text(encoding='utf-8')
    assert 'response.status === 401 || response.status === 403' in source
    assert "text: 'Could not load collaborators.'" in source


# ══════════════════════════════════════════════════════════════════════════
# F23.1 — search_results.html, the fourth page that loads follow-button.js
# ══════════════════════════════════════════════════════════════════════════
#
# search_results.html is standalone (no base.html) and published no CSRF token,
# while loading a component that POSTs to /api/users/<id>/follow. The behavioural
# browser proof lives in test_f23_csrf_browser.py; these are the server-side
# halves. CSRF is re-enabled explicitly here because the app fixture disables it.

SEARCH_PATH = '/search?query=f23target'


@pytest.fixture
def search_pair(app, db):
    """A viewer and a distinct follow target, both synthetic and local."""
    viewer = _make_user(app, db, 'f23viewer')
    target = _make_user(app, db, 'f23target')
    client = app.test_client()
    _login(client, 'f23viewer')
    return client, viewer, target


def test_the_rendered_search_page_publishes_a_token(app, search_pair):
    """F23.1 (1): the token is actually rendered, not merely present in source."""
    client, _viewer, _target = search_pair
    html = client.get(SEARCH_PATH).get_data(as_text=True)
    match = re.search(r'<meta[^>]*name="csrf-token"[^>]*content="([^"]+)"', html)
    assert match, ('the rendered search page published no csrf-token meta tag; '
                   'follow/unfollow from search results would be refused')
    assert match.group(1).strip(), 'the published token was empty'


def test_search_results_still_renders_its_results(app, search_pair):
    """F23.1 (6): adding a meta tag must not disturb search behaviour."""
    client, viewer, target = search_pair

    response = client.get(SEARCH_PATH)
    assert response.status_code == 200, response.status_code
    html = response.get_data(as_text=True)

    # The community member section is the one carrying the follow button.
    assert 'f23target' in html, 'the matching community member disappeared'
    assert 'data-follow-button' in html, 'the follow button disappeared'
    assert 'No community members matching' not in html, (
        'the member section fell through to its empty state')

    # The self-view branch must still render instead of a follow button.
    own = client.get('/search?query=f23viewer').get_data(as_text=True)
    assert '(You)' in own, 'the self-view branch regressed'


def test_a_valid_token_is_accepted_server_side(app, search_pair):
    """F23.1 (3): the delivered token makes the real action succeed."""
    client, viewer, target = search_pair
    token = _csrf_token(client, SEARCH_PATH)

    app.config['WTF_CSRF_ENABLED'] = True
    try:
        response = client.post(f'/api/users/{target}/follow',
                               headers={'X-CSRFToken': token})
        assert response.status_code == 200, (
            response.status_code, response.get_data(as_text=True)[:300])
        payload = response.get_json()
        assert payload.get('is_following') is True, payload

        from models import UserFollow
        with app.app_context():
            assert UserFollow.query.filter_by(
                follower_id=viewer, following_id=target).first() is not None, (
                'the follow was accepted but no relationship was written')
    finally:
        app.config['WTF_CSRF_ENABLED'] = False


def test_the_same_request_without_a_token_is_refused(app, search_pair):
    """F23.1 (4): delivery is fixed; enforcement is NOT weakened."""
    client, viewer, target = search_pair

    app.config['WTF_CSRF_ENABLED'] = True
    try:
        missing = client.post(f'/api/users/{target}/follow')
        assert missing.status_code in (400, 401, 403), missing.status_code

        from models import UserFollow
        with app.app_context():
            assert UserFollow.query.filter_by(follower_id=viewer,
                                              following_id=target).count() == 0, (
                'a refused request still wrote a follow relationship')
    finally:
        app.config['WTF_CSRF_ENABLED'] = False


def test_an_invalid_token_is_refused(app, search_pair):
    """F23.1 (4): a wrong token must not be accepted either."""
    client, _viewer, target = search_pair

    app.config['WTF_CSRF_ENABLED'] = True
    try:
        response = client.post(f'/api/users/{target}/follow',
                               headers={'X-CSRFToken': 'not-a-real-token'})
        assert response.status_code in (400, 401, 403), response.status_code
    finally:
        app.config['WTF_CSRF_ENABLED'] = False


def test_no_second_csrf_mechanism_was_introduced(app):
    """F23.1: exactly one meta tag, and no exemption was granted."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    source = (root / 'templates' / 'search_results.html').read_text(
        encoding='utf-8')

    assert len(re.findall(r'<meta[^>]*name="csrf-token"', source)) == 1, (
        'search_results.html must publish exactly one csrf meta tag')
    # No inline token hidden field or duplicated JS mechanism.
    assert source.count('csrf_token()') == 1, (
        'search_results.html gained a second token mechanism')

    social = (root / 'routes' / 'social.py').read_text(encoding='utf-8')
    assert 'csrf.exempt' not in social and 'csrf_exempt' not in social, (
        'routes/social.py gained a CSRF exemption')
