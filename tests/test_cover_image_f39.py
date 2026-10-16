"""F39 — list cover image validation at the server boundary.

These exercise the real route and assert on what actually reaches the
database, rather than on source text. Nothing is sent to a live application:
the Flask test client talks to the in-process app only.
"""
import uuid

import pytest

from utils import cover_image_policy


# ── helpers ────────────────────────────────────────────────────────────────

def _user(db, name):
    from models import User
    username = f'{name}_{uuid.uuid4().hex[:8]}'
    user = User(username=username, email=f'{username}@cover-f39.test',
                email_verified=True)
    user.set_password('TestPass1')
    db.session.add(user)
    db.session.commit()
    return user


def _list(db, user, title='Cover list', **kw):
    from models import UserList
    ul = UserList(user_id=user.id, title=title,
                  slug=f'f39-{uuid.uuid4().hex[:10]}', **kw)
    db.session.add(ul)
    db.session.flush()
    return ul


def _login(client, user):
    client.post('/login', data={'username': user.username,
                                'password': 'TestPass1'},
                follow_redirects=True)


class _Owned:
    """A logged-in owner plus the id of a list it owns.

    The list is committed rather than merely flushed so that reading it back
    through a fresh app context — which is how `_stored` reads — sees it.
    """

    def __init__(self, user, list_id):
        self.user = user
        self.list_id = list_id


def _owned(db, client, name='owner'):
    user = _user(db, name)
    ul = _list(db, user, title=f'{name} list')
    db.session.commit()
    _login(client, user)
    return _Owned(user, ul.id)


@pytest.fixture
def owner(db, client):
    return _owned(db, client)


def _stored(app, list_id):
    from models import UserList
    with app.app_context():
        return UserList.query.get(list_id).cover_image


def _put(client, list_id, value):
    return client.put(f'/api/lists/{list_id}/cover',
                      json={'cover_image': value})


# ── the traced contract: what the application legitimately stores ───────────

@pytest.mark.parametrize('value', [
    '/static/images/x.jpg',                       # root-relative app path
    'https://image.tmdb.org/t/p/w500/abc.jpg',     # approved absolute host
    'https://IMAGE.TMDB.ORG/t/p/w500/abc.jpg',     # host is case-insensitive
    'https://image.tmdb.org:443/t/p/w500/abc.jpg',  # explicit default port
])
def test_accepted_formats_are_stored(client, app, owner, value):
    list_id = owner.list_id
    response = _put(client, list_id, value)
    assert response.status_code == 200, response.get_json()
    assert _stored(app, list_id) == value, 'the value must round-trip verbatim'


# ── everything else is refused, and nothing is written ──────────────────────

REFUSED = [
    ('https://attacker.example/pixel.png', 'arbitrary external host'),
    ('https://image.tmdb.org.evil.example/x.jpg', 'deceptive lookalike host'),
    ('https://tmdb.org.evil.example/x.jpg', 'deceptive suffix host'),
    ('http://image.tmdb.org/t/p/w500/abc.jpg', 'plaintext http'),
    ('//evil.example/pixel.png', 'protocol-relative URL'),
    ('data:image/png;base64,AAAA', 'data URL'),
    ('blob:https://evil.example/uuid', 'blob URL'),
    ('javascript:alert(1)', 'javascript scheme'),
    ('vbscript:msgbox(1)', 'vbscript scheme'),
    ('ftp://image.tmdb.org/x.jpg', 'unsupported scheme'),
    ('https://user:pw@image.tmdb.org/x.jpg', 'embedded credentials'),
    ('https://image.tmdb.org:8443/x.jpg', 'non-default port'),
    ('/../../etc/passwd', 'path traversal'),
    ('/static\\evil.jpg', 'backslash in path'),
    ('/x?y=1', 'query string in path'),
    ('/x#frag', 'fragment in path'),
    ('/static/a b.jpg', 'whitespace in value'),
    ('/static/a\x01b.jpg', 'control character in value'),
    ('image.tmdb.org/x.jpg', 'bare relative path'),
    ('x' * 600, 'over the column length'),
]


@pytest.mark.parametrize('value,reason', REFUSED, ids=[r for _, r in REFUSED])
def test_unsafe_cover_images_are_refused_and_not_persisted(
        client, app, owner, value, reason):
    """The 400 must arrive AND the stored value must be untouched."""
    list_id = owner.list_id

    # Seed a known-good value so the assertion can prove it did not change.
    assert _put(client, list_id, '/static/images/before.jpg').status_code == 200

    response = _put(client, list_id, value)
    assert response.status_code == 400, (
        '%s was accepted: %s' % (reason, response.get_json()))
    body = response.get_json()
    assert body.get('error'), 'the client must get an actionable message'

    assert _stored(app, list_id) == '/static/images/before.jpg', (
        '%s overwrote the stored cover' % reason)


def test_missing_cover_is_refused(client, app, owner):
    list_id = owner.list_id
    response = client.put(f'/api/lists/{list_id}/cover', json={})
    assert response.status_code == 400
    assert _stored(app, list_id) is None


def test_non_string_cover_is_refused(client, app, owner):
    list_id = owner.list_id
    response = _put(client, list_id, 12345)
    assert response.status_code == 400
    assert _stored(app, list_id) is None


# ── authorization and existing behaviour are untouched ──────────────────────

def test_a_non_owner_cannot_set_the_cover(client, app, db):
    victim = _owned(db, client, 'coverowner')

    intruder = _user(db, 'intruder')
    db.session.commit()
    _login(client, intruder)

    response = _put(client, victim.list_id, '/static/images/hijack.jpg')
    assert response.status_code == 403, response.get_json()
    assert _stored(app, victim.list_id) is None, 'a refused write persisted'


def test_anonymous_cannot_set_the_cover(client, app, db):
    victim = _owned(db, client, 'anonowner')
    client.post('/logout', follow_redirects=True)

    response = client.put(f'/api/lists/{victim.list_id}/cover',
                          json={'cover_image': '/static/images/x.jpg'})
    assert response.status_code in (302, 401, 403), response.status_code
    assert _stored(app, victim.list_id) is None


# ── the policy module itself ────────────────────────────────────────────────

@pytest.mark.parametrize('value', [
    '/static/images/x.jpg',
    'https://image.tmdb.org/t/p/w500/abc.jpg',
])
def test_policy_accepts(value):
    assert cover_image_policy.is_valid_cover_image(value)
    assert cover_image_policy.normalize_cover_image(value) == value


@pytest.mark.parametrize('value,reason', REFUSED, ids=[r for _, r in REFUSED])
def test_policy_refuses(value, reason):
    assert not cover_image_policy.is_valid_cover_image(value), reason
    with pytest.raises(cover_image_policy.InvalidCoverImage):
        cover_image_policy.normalize_cover_image(value)


def test_policy_trims_surrounding_whitespace():
    assert cover_image_policy.normalize_cover_image(
        '  /static/images/x.jpg  ') == '/static/images/x.jpg'


def test_policy_error_names_the_problem():
    problems = cover_image_policy.cover_image_problems(
        'https://attacker.example/x.png')
    assert problems and 'host is not allowed' in problems[0]


def test_render_filter_returns_empty_for_unusable_values():
    """The Jinja filter is the render-time half; legacy rows depend on it."""
    assert cover_image_policy.cover_image_url(
        '/static/images/x.jpg') == '/static/images/x.jpg'
    assert cover_image_policy.cover_image_url('https://evil.example/x.png') == ''
    assert cover_image_policy.cover_image_url(None) == ''
    assert cover_image_policy.cover_image_url('data:image/png;base64,AA') == ''


def test_allowed_hosts_match_the_browser_allowlist():
    """Server and renderer must not drift apart.

    The browser restricts to the same host through
    `FrameIQDOM.safeImageUrlFromHosts(['image.tmdb.org'])` in
    templates/lists_discover.html. If one side gains a host the other lacks,
    the server and the renderer disagree about what is renderable.
    """
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    discover = (root / 'templates' / 'lists_discover.html').read_text()
    assert "'image.tmdb.org'" in discover, \
        'the browser allowlist changed; reconcile it with ALLOWED_HOSTS'
    assert cover_image_policy.ALLOWED_HOSTS == ('image.tmdb.org',)


def test_no_migration_or_column_change_was_introduced():
    """F39 must not rewrite stored data."""
    from models import UserList
    assert UserList.__table__.c.cover_image.type.length == 500
    root = __import__('pathlib').Path(__file__).resolve().parent.parent
    for name in (root / 'migrates').iterdir():
        if name.suffix != '.py':
            continue
        body = name.read_text(encoding='utf-8')
        assert 'ALLOWED_HOSTS' not in body, (
            'a migration references the cover policy; F39 validates at the '
            'boundary and must not migrate stored data')


# ── every rendering consumer still validates ───────────────────────────────

def test_every_cover_image_render_site_validates():
    """A consumer must validate the URL, not merely escape it.

    Escaping alone writes a safe ATTRIBUTE holding an off-host URL, and the
    browser still fetches it.
    """
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent

    # templates/lists_discover.html — JS render path
    discover = (root / 'templates' / 'lists_discover.html').read_text()
    assert 'safeImageUrlFromHosts' in discover, \
        'lists_discover must validate cover_image as a URL'
    assert "src=\"${list.cover_image}\"" not in discover, \
        'lists_discover interpolates the raw value again'

    # templates/list_detail.html — Jinja render path
    detail = (root / 'templates' / 'list_detail.html').read_text()
    assert 'cover_src' in detail, (
        'list_detail must render the route-validated cover_src')
    assert 'src="{{ user_list.cover_image }}"' not in detail, \
        'list_detail renders the raw stored value again'

    # The validation must reach the template. It is done in the route rather
    # than as a Jinja filter because app.py is off-limits: F20 asserts that
    # `utils/schema_guard.py` and `app.py` are untouched, and that guard is not
    # ours to edit. Editing it to unblock this change would be exactly the
    # "weaken a guard to go green" failure mode.
    route_src = (root / 'routes' / 'lists.py').read_text(encoding='utf-8')
    assert 'cover_src=cover_image_url(user_list.cover_image)' in route_src, \
        'the route must hand the template a validated cover_src'
    assert route_src.count('cover_src=cover_image_url(user_list.cover_image)') == 2, \
        'both list_detail render sites must validate the cover'
    assert '{{ user_list.cover_image }}' not in detail, \
        'the template must not read the raw stored value'
    # app.py must remain untouched for the F20 guard.
    app_src = (root / 'app.py').read_text(encoding='utf-8')
    assert 'cover_image_url' not in app_src, (
        'F39 must not register a Jinja filter in app.py')


# ── the render path: evidence that an off-host URL cannot reach the page ─────
#
# A row written before this policy existed still holds whatever was stored, so
# the evidence has to come from the rendered page, not from the write path.
# The valid cases are written through the real PUT route, which also proves the
# route persists what the policy accepts.

LEGACY_OFF_HOST = 'https://attacker.example/pixel.png'


def _page(client, list_id):
    return client.get(f'/lists/{list_id}',
                      follow_redirects=True).get_data(as_text=True)


def _force_store(db, list_id, value):
    """Write past the route, as a pre-F39 row would already be stored."""
    from models import UserList
    row = db.session.query(UserList).get(list_id)
    row.is_public = True
    row.cover_image = value
    db.session.commit()


@pytest.mark.parametrize('value', [
    'https://image.tmdb.org/t/p/w500/abc.jpg',
    '/static/images/mine.jpg',
])
def test_a_cover_set_through_the_route_is_rendered(client, db, owner, value):
    """End to end: the route accepts it, and the page shows it."""
    assert _put(client, owner.list_id, value).status_code == 200
    assert value in _page(client, owner.list_id), (
        'an accepted cover is not rendered on the detail page')


def test_a_legacy_off_host_cover_is_not_rendered(client, db, owner):
    """A row that predates the policy must not make the page fetch a host.

    The route would refuse this value today, so it is written directly to
    simulate a row stored before F39.
    """
    _force_store(db, owner.list_id, LEGACY_OFF_HOST)
    body = _page(client, owner.list_id)
    assert LEGACY_OFF_HOST not in body, 'an off-host cover reached the page'
    assert 'id="list-cover"' not in body, (
        'the cover block rendered for a value the policy rejects')


def test_a_legacy_valid_cover_is_still_rendered(client, db, owner):
    """The render filter must not blank out a legitimate legacy cover."""
    for value in ('https://image.tmdb.org/t/p/w500/legacy.jpg',
                  '/static/images/legacy.jpg'):
        _force_store(db, owner.list_id, value)
        body = _page(client, owner.list_id)
        assert value in body, 'a valid legacy cover stopped rendering: %r' % value


def test_a_list_with_no_cover_renders_no_cover_block(client, db, owner):
    from models import UserList
    row = db.session.query(UserList).get(owner.list_id)
    row.is_public = True
    row.cover_image = None
    db.session.commit()
    assert 'id="list-cover"' not in _page(client, owner.list_id)


def test_credentials_are_refused_for_the_credential_reason(client, db, owner):
    """Pin the specific reason, not merely the refusal.

    With the credential check removed, the host comparison still refuses the
    value — `user:pw@image.tmdb.org` parses to host `user` — so the request is
    still a 400 and a status-only assertion passes. That made a mutation of the
    credential check look caught when it was only caught by accident. Asserting
    the message pins the control itself.
    """
    assert _put(client, owner.list_id,
                'https://user:pw@image.tmdb.org/x.jpg').status_code == 400
    body = _put(client, owner.list_id,
                'https://user:pw@image.tmdb.org/x.jpg').get_json()
    assert 'credential' in body['error'], body


def test_each_refusal_names_its_own_reason():
    """The reason must be specific, not a generic 'invalid'."""
    expected = {
        'https://user:pw@image.tmdb.org/x.jpg': 'credential',
        'https://attacker.example/x.png': 'host is not allowed',
        '//evil.example/x.png': 'protocol-relative',
        'http://image.tmdb.org/x.jpg': 'must be https',
        '/../../etc/passwd': '".."',
        '/static/a b.jpg': 'whitespace',
    }
    for value, fragment in expected.items():
        problems = cover_image_policy.cover_image_problems(value)
        assert problems, '%r was accepted' % value
        assert any(fragment in p for p in problems), (
            '%r refused for the wrong reason: %s' % (value, problems))
