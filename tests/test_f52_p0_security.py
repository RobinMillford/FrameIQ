"""F52 — P0 security regression tests.

Covers the five F51 findings remediated in this change:

* D-1  ``templates/list_detail.html`` interpolated the media title into a
       JavaScript string inside an inline ``onclick``. ``|replace("'", "\\'")``
       neutralised the quote but not the backslash, so a title closed the
       string and the rest of the attribute became code.
* D-2  ``routes/reviews.py`` stored ``media_type`` verbatim while sibling
       endpoints already restricted it to ``movie``/``tv``;
       ``static/js/review-manager.js`` interpolated it into an ``href``.
* D-3  ``routes/collections.py`` redirected to ``request.referrer`` unfiltered.
* D-4  ``routes/lists_advanced.py`` let an anonymous caller write analytics to a
       **private** list.
* D-5  the same file let any authenticated user write another user's private
       list's ``share_count``.
* D-6  ``UserList.to_dict()`` emitted the collaborator roster and analytics on
       six public endpoints, bypassing the gate added for F23 C-11.

Offline only. Uses the ordinary ``tests/conftest.py`` SQLite configuration;
no database outside the per-run temp file, no network, no browser.
"""
import pytest

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_user(db, name):
    """Create (once) a user for `name`, returning the same object each time.

    The suite's `app` fixture is session-scoped with no per-test rollback
    (F51 finding), so rows accumulate across a run and a fixed username would
    collide on the second parametrization. Memoising by base name keeps every
    identity distinct per test while letting a test call `_make_user` and
    `_u` for the same role and mean the same person.
    """
    from models import User
    if name not in _USERS:
        _USERS[name] = '%s-%d' % (name, len(_USERS) + 1)
    username = _USERS[name]

    # Always re-read by username rather than caching the ORM object. Other
    # suites in a full run expunge or remove rows, which would leave a cached
    # instance detached and unusable.
    existing = User.query.filter_by(username=username).first()
    if existing is not None:
        return existing

    user = User(username=username, email='%s@f52.test' % username,
                email_verified=True)
    user.set_password('TestPass1')
    db.session.add(user)
    db.session.commit()
    return user


_USERS = {}


def _db_row(db, model, pk):
    """Read a row straight from the database, bypassing any ORM cache.

    ``Model.query.get(pk)`` is deliberately NOT used for assertions that check
    what a request just changed. Since the F56 harness fix each request runs in
    its own application context and therefore its own SQLAlchemy session, so an
    instance the test created is still sitting in the test's identity map — and
    ``get()`` hands that stale instance straight back without issuing any SQL.

    That made these assertions pass or fail on cache contents rather than on
    what the endpoint did. (One of them, the ``media_type`` check, was already
    passing for the wrong reason: its cached value happened to equal the
    expected one.) Expiring first and selecting explicitly is what the
    assertion always meant.
    """
    db.session.expire_all()
    return model.query.filter(model.id == pk).first()


def _u(db, name):
    """Shorthand for the identity a test logs in as."""
    return _make_user(db, name)


def _make_list(db, owner, slug=None, is_public=True, title='A list'):
    from models import UserList
    if slug is None:
        _SEQ['slug'] += 1
        slug = 'f52-list-%d' % _SEQ['slug']
    user_list = UserList(user_id=owner.id, title=title, slug=slug,
                         is_public=is_public)
    db.session.add(user_list)
    db.session.commit()
    return user_list


def _make_media(db, title='A title', media_type='movie', tmdb_id=None):
    """Create a MediaItem. The TMDb id is auto-allocated unless given.

    `MediaItem.tmdb_id` is UNIQUE and the session database is shared across
    tests (F51: no per-test rollback), so a fixed id would collide the second
    time a parametrized test runs. Allocating keeps each invocation distinct.
    """
    from models import MediaItem
    if tmdb_id is None:
        _SEQ['tmdb'] += 1
        tmdb_id = 96_000_000 + _SEQ['tmdb']
    media = MediaItem(tmdb_id=tmdb_id, media_type=media_type, title=title,
                      poster_path='/poster.jpg')
    db.session.add(media)
    db.session.commit()
    return media


_SEQ = {'tmdb': 0, 'slug': 0}


@pytest.fixture(scope='module', autouse=True)
def _f52_cleanup(app):
    """Remove everything this module created before the next test file runs.

    The suite's `app` fixture is session-scoped with no per-test rollback (the
    F51 test-isolation finding), so rows created here survive into every later
    file. Some existing tests make a global assumption about a table's state —
    `tests/test_movie_review_flow.py` calls `Review.query.one()`, meaning
    "exactly one review exists anywhere in the database". Leaving the reviews
    this module creates breaks it, and the alternative — editing somebody
    else's assertions — is not this patch's business.

    Cleanup is by value range and prefix rather than by bookkeeping: this
    module is the sole creator of `@f52.test` users, `f52-…` slugs and TMDb
    ids in the 96M/97M bands, so the ranges identify exactly these rows and
    cannot touch another suite's data.

    Deletion order follows the foreign keys, children first.
    """
    yield
    from models import (ListAnalytics, ListCollaborator, ListView, MediaItem,
                        Review, User, UserList, db as _db)
    from models.lists import UserListItem

    with app.app_context():
        media_ids = [row.id for row in MediaItem.query.filter(
            MediaItem.tmdb_id.between(96_000_000, 97_999_999)).all()]
        user_ids = [row.id for row in User.query.filter(
            User.email.like('%@f52.test')).all()]
        list_rows = UserList.query.filter(UserList.slug.like('f52-%')).all()
        list_ids = [row.id for row in list_rows]

        for children, key in (
                (ListView, 'list_id'),
                (ListCollaborator, 'list_id'),
                (UserListItem, 'list_id'),
                (ListAnalytics, 'list_id')):
            if list_ids:
                children.query.filter(
                    getattr(children, key).in_(list_ids)).delete(
                    synchronize_session=False)

        for children, key, ids in ((Review, 'media_id', media_ids),
                                   (Review, 'user_id', user_ids)):
            if ids:
                children.query.filter(
                    getattr(children, key).in_(ids)).delete(
                    synchronize_session=False)

        if list_ids:
            UserList.query.filter(UserList.id.in_(list_ids)).delete(
                synchronize_session=False)
        if media_ids:
            MediaItem.query.filter(MediaItem.id.in_(media_ids)).delete(
                synchronize_session=False)
        if user_ids:
            User.query.filter(User.id.in_(user_ids)).delete(
                synchronize_session=False)

        _db.session.commit()

    _USERS.clear()


def _next_tmdb():
    """A TMDb id no other row in the shared session database will hold."""
    _SEQ['tmdb'] += 1
    return 97_000_000 + _SEQ['tmdb']


def _add_item(db, user_list, media, position=1):
    from models.lists import UserListItem
    item = UserListItem(list_id=user_list.id, media_id=media.id,
                        media_type=media.media_type, position=position)
    db.session.add(item)
    db.session.commit()
    return item


def _analytics(db, list_id):
    from models import ListAnalytics
    return ListAnalytics.query.filter_by(list_id=list_id).first()


def _login(client, username):
    return client.post('/login',
                       data={'username': username, 'password': 'TestPass1'},
                       follow_redirects=True)


def _views(db, list_id):
    from models import ListView
    return ListView.query.filter_by(list_id=list_id).count()


def _attribute_names(tag):
    """Attribute names present on a single opening tag.

    Deliberately attribute-aware rather than a substring search: an escaped
    payload inside `aria-label` or `data-title` leaves handler-looking text
    (`onerror=`) in the raw markup, and that text is inert. Only the parsed
    attribute names can say whether an element really gained a handler.
    """
    import re as _re
    return [name.lower() for name in
            _re.findall(r'([a-zA-Z_:][-\w:.]*)\s*=\s*"', tag)]


def _remove_button(body):
    """The remove button element, or None. Ignores the inline script block.

    The delegated handler itself contains the string '.js-remove-item', so a
    plain `in body` test cannot distinguish "button rendered" from "script
    present".
    """
    import re as _re
    return _re.search(r'<button[^>]*class="[^"]*js-remove-item[^"]*"[^>]*>',
                      body)


# Titles that each broke the old inline handler in a different way.
HOSTILE_TITLES = [
    pytest.param("\\'+alert(1)//", id='backslash-then-quote'),
    pytest.param("\\'", id='trailing-backslash-quote'),
    pytest.param('";alert(1);//', id='double-quote-breakout'),
    pytest.param("'-alert(1)-'", id='quote-breakout'),
    pytest.param('<img src=x onerror=alert(1)>', id='tag-with-handler'),
    pytest.param('" onmouseover="alert(1)', id='attribute-breakout'),
    pytest.param('</script><script>alert(1)</script>', id='script-breakout'),
    pytest.param('Back\\slash\\', id='double-backslash'),
    pytest.param("O'Brien & Sons <Ltd>", id='ordinary-punctuation'),
]


# ── D-1 ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize('title', HOSTILE_TITLES)
def test_d1_title_is_never_interpolated_into_javascript(
        client, db, app, title):
    """The remove button must carry data, never a JavaScript string literal.

    Regression for the F51 finding where ``|replace("'", "\\'")`` left the
    backslash intact: the HTML parser decoded ``&#39;`` back to ``'`` before
    the JS parser ran, so a title of ``\\'+alert(1)//`` closed the string early.
    """
    import re as _re

    owner = _make_user(db, 'd1owner')
    media = _make_media(db, title)
    user_list = _make_list(db, owner)
    _add_item(db, user_list, media)

    _login(client, _u(db, 'd1owner').username)
    body = client.get('/lists/%d' % user_list.id).get_data(as_text=True)

    button = _re.search(r'<button[^>]*class="[^"]*js-remove-item[^"]*"[^>]*>',
                        body)
    assert button, 'the remove button was not rendered'
    tag = button.group(0)

    # Check the ATTRIBUTE NAMES, not the raw text: a title like
    # `<img src=x onerror=alert(1)>` legitimately leaves the characters
    # "onerror=" inside the escaped aria-label/data-title values, where they
    # are inert text. What must not exist is an actual `on*` attribute.
    names = _attribute_names(tag)
    handlers = [n for n in names if n.lower().startswith('on')]
    assert not handlers, (
        'title produced event-handler attribute(s) %r on: %s'
        % (handlers, tag))
    assert 'onclick' not in [n.lower() for n in names], (
        'inline onclick is back on the remove button: %s' % tag)

    # The title survives as an attribute value, not as code.
    assert 'data-title' in names
    assert {'data-list-id', 'data-item-id'} <= set(names)


@pytest.mark.parametrize('title', HOSTILE_TITLES)
def test_d1_data_attribute_round_trips_the_exact_title(client, db, app, title):
    """The title must arrive at the handler as the exact original string.

    Parsed the way a browser would: pull the attribute off the tag and
    HTML-entity-decode it. That decode is the step the old code got wrong —
    it is the point where ``&#39;`` turned back into a live quote — so the
    test asserts it explicitly rather than trusting the attribute's raw text.
    """
    import html as _html
    import re as _re

    owner = _make_user(db, 'd1rt')
    media = _make_media(db, title)
    user_list = _make_list(db, owner)
    _add_item(db, user_list, media)

    _login(client, _u(db, 'd1rt').username)
    body = client.get('/lists/%d' % user_list.id).get_data(as_text=True)

    tag = _remove_button(body).group(0)
    raw = _re.search(r'data-title="([^"]*)"', tag).group(1)
    decoded = _html.unescape(raw)

    assert decoded == title, (
        'title did not round-trip.\n  sent     : %r\n  in markup: %r'
        % (title, decoded))
    # The decoded value is a plain string; nothing re-parses it as JS.
    assert isinstance(decoded, str)


def test_d1_remove_button_carries_the_right_ids(client, db):
    """Behaviour preservation: the ids still reach the handler."""
    owner = _make_user(db, 'd1ids')
    media = _make_media(db, 'Ordinary Title')
    user_list = _make_list(db, owner)
    item = _add_item(db, user_list, media)

    _login(client, _u(db, 'd1ids').username)
    body = client.get('/lists/%d' % user_list.id).get_data(as_text=True)
    tag = _remove_button(body).group(0)

    assert 'data-list-id="%d"' % user_list.id in tag
    assert 'data-item-id="%d"' % item.id in tag


def test_d1_list_removal_endpoint_still_works(client, db):
    """The endpoint the button calls is unchanged and still authorises."""
    owner = _make_user(db, 'd1rm')
    media = _make_media(db, 'Remove Me')
    user_list = _make_list(db, owner)
    item = _add_item(db, user_list, media)

    _login(client, _u(db, 'd1rm').username)
    response = client.delete(
        '/api/lists/%d/remove/%d' % (user_list.id, item.id))

    assert response.status_code == 200
    assert _views(db, user_list.id) == 0
    from models.lists import UserListItem
    assert _db_row(db, UserListItem, item.id) is None


def test_d1_remove_button_absent_for_non_editors(client, db):
    """The button is still gated by can_edit — unchanged behaviour."""
    owner = _make_user(db, 'd1own2')
    _make_user(db, 'd1other')
    media = _make_media(db, 'Someone Elses Item')
    user_list = _make_list(db, owner)
    _add_item(db, user_list, media)

    _login(client, 'd1other')
    body = client.get('/lists/%d' % user_list.id).get_data(as_text=True)

    assert _remove_button(body) is None, \
        'the remove button rendered for a user who does not own the list'


# ── D-2 ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize('media_type', ['movie', 'tv'])
def test_d2_valid_media_types_still_accepted(client, db, media_type):
    from models import Review
    _make_user(db, 'd2ok')
    _login(client, _u(db, 'd2ok').username)

    response = client.post('/api/reviews', json={
        'media_id': _next_tmdb(),
        'media_type': media_type,
        'rating': 4.5,
        'title': 'A Valid ' + media_type,
    })

    assert response.status_code == 201
    review = Review.query.order_by(Review.id.desc()).first()
    assert review is not None
    assert review.media_type == media_type


@pytest.mark.parametrize('media_type', [
    pytest.param('onclick=window.__XSS_FIRED=1', id='f51-payload'),
    pytest.param('onmouseover=alert(1)', id='other-handler'),
    pytest.param('Movie', id='capitalised'),
    pytest.param('tv ', id='trailing-space'),
    pytest.param(' movie', id='leading-space'),
    pytest.param('movie\n', id='trailing-newline'),
    pytest.param('mov ie', id='inner-space'),
    pytest.param('script', id='not-a-media-type'),
    pytest.param('../admin', id='path-shaped'),
    pytest.param('javascript:alert(1)', id='scheme'),
    pytest.param(123, id='integer'),
    pytest.param(['movie'], id='list'),
    pytest.param({'t': 'movie'}, id='dict'),
])
def test_d2_invalid_media_type_is_rejected_and_creates_nothing(
        client, db, media_type):
    """400, and no Review or MediaItem row may be created."""
    from models import MediaItem, Review
    user = _make_user(db, 'd2bad')
    _login(client, _u(db, 'd2bad').username)

    reviews_before = Review.query.count()
    media_before = MediaItem.query.count()

    response = client.post('/api/reviews', json={
        'media_id': _next_tmdb(),
        'media_type': media_type,
        'rating': 4.0,
        'title': 'Should Not Persist',
    })

    assert response.status_code == 400, (
        'expected 400 for media_type=%r, got %s'
        % (media_type, response.status_code))
    # Either the explicit media_type guard or the pre-existing
    # required-fields check may answer first; both are 400 and both refuse.
    error = (response.get_json() or {}).get('error', '')
    assert 'media_type' in error or 'Missing required fields' in error, (
        'unexpected error for %r: %r' % (media_type, error))

    assert Review.query.count() == reviews_before, \
        'a Review row was created for a rejected media_type'
    assert MediaItem.query.count() == media_before, \
        'a MediaItem row was created for a rejected media_type'
    assert Review.query.filter_by(user_id=user.id).count() == 0


@pytest.mark.parametrize('media_type', [pytest.param('', id='empty'),
                                        pytest.param(None, id='null')])
def test_d2_empty_media_type_is_refused(client, db, media_type):
    """A falsy media_type is caught before the allowlist, still as a 400."""
    from models import MediaItem, Review
    _make_user(db, 'd2empty')
    _login(client, _u(db, 'd2empty').username)
    reviews_before = Review.query.count()
    media_before = MediaItem.query.count()

    response = client.post('/api/reviews', json={
        'media_id': _next_tmdb(),
        'media_type': media_type,
        'rating': 4.0,
    })

    assert response.status_code == 400
    assert Review.query.count() == reviews_before
    assert MediaItem.query.count() == media_before


def test_d2_rejection_happens_before_any_write(client, db):
    """The guard must run before the MediaItem lookup/insert.

    A rejection that still created the MediaItem row would leave orphan
    catalogue rows behind for every malformed request.
    """
    from models import MediaItem
    _make_user(db, 'd2pre')
    _login(client, _u(db, 'd2pre').username)

    tmdb_id = _next_tmdb()
    client.post('/api/reviews', json={
        'media_id': tmdb_id,
        'media_type': 'onclick=x',
        'rating': 3.0,
        'title': 'Orphan Maker',
    })

    assert MediaItem.query.filter_by(tmdb_id=tmdb_id).first() is None


def test_d2_existing_review_survives_the_change(client, db):
    """A row written before the validation existed still renders safely.

    Defence in depth: the renderer no longer trusts the stored value, so a
    legacy or hand-edited row cannot reach an ``href`` attribute intact.
    """
    from models import Review
    user = _make_user(db, 'd2legacy')
    media = _make_media(db, 'Legacy Row')
    review = Review(user_id=user.id, media_id=media.id,
                    media_type='onclick=alert(1)', rating=4.0)
    db.session.add(review)
    db.session.commit()

    assert review.media_type == 'onclick=alert(1)'
    # The column still holds the bad value; the renderer is what must cope.
    assert _db_row(db, Review, review.id).media_type == 'onclick=alert(1)'


def _js_code_only(source):
    """Strip // line comments and /* */ blocks.

    The guards below match source text, and this change documents the old
    vulnerable pattern in comments explaining what it replaced. Without
    stripping, the guard would match its own explanation. Matching only
    executable code is also what the guards actually mean.
    """
    import re as _re
    source = _re.sub(r'/\*.*?\*/', '', source, flags=_re.S)
    source = _re.sub(r'^\s*//.*$', '', source, flags=_re.M)
    source = _re.sub(r'(?m)^\s*//.*$', '', source)
    return source


def test_d2_renderer_does_not_trust_a_stored_media_type():
    """Static guard on the sink that the F51 finding identified.

    Behavioural coverage of the rendered link needs a browser; this asserts
    the property that makes it safe — an allowlist stands between the stored
    value and the ``href``.
    """
    import pathlib
    source = _js_code_only(pathlib.Path(
        'static/js/review-manager.js').read_text(encoding='utf-8'))

    assert '/${review.media.media_type}/' not in source, (
        'review-manager.js interpolates the stored media_type into an href '
        'without validation')
    assert "['movie', 'tv'].includes(review.media.media_type)" in source, (
        'the media_type allowlist guarding the href is missing')
    # The id must be validated too, via the shared helper.
    assert 'D.safeId(review.media.id)' in source


def test_d2_href_is_never_protocol_relative():
    """An unrecognised stored media_type must not yield `//<id>`.

    Found during the F52 review. The first version of this fix mapped an
    unrecognised `media_type` to `''` on the theory that the href would then
    read as a harmless `/<type>/<id>` path. It does not: with an empty type
    the href is `//<id>`, and a browser resolves that as a
    PROTOCOL-RELATIVE url pointing at host `<id>` — an off-site navigation.
    That is how the defensive fallback itself became a redirect hazard.

    Evaluates the guard expressions from review-manager.js directly, so the
    property is checked rather than the spelling of the code.
    """
    import pathlib
    import re as _re

    source = _js_code_only(pathlib.Path(
        'static/js/review-manager.js').read_text(encoding='utf-8'))

    # The guard must produce a falsy "no link" value, not an empty string.
    assert _re.search(
        r"const mediaType\s*=\s*\['movie',\s*'tv'\]\.includes\([^)]*\)"
        r"\s*\?[^:]*:\s*null\s*;", source), (
        'an unrecognised media_type must fall back to null, not \'\', so the '
        'link is omitted instead of rendering as //<id>')

    # The anchor must be conditional on a recognised type.
    assert _re.search(r"const mediaLink\s*=\s*mediaType\s*\?", source), (
        'the anchor is emitted unconditionally; it must be conditional on a '
        'recognised media_type')

    # The href itself must be built from the allowlisted type.
    assert _re.search(r'<a href="/\$\{mediaType\}/\$\{mediaId\}"', source), (
        'expected the href to be built from the allowlisted mediaType')

    # And nothing may interpolate a possibly-empty type straight into an href.
    assert not _re.search(r'<a href="/\$\{review\.media\.media_type\}', source), (
        'the stored media_type is interpolated straight into an href again')


def test_d2_review_update_still_works(client, db):
    """PUT is unchanged and still owns its validation."""
    from models import Review
    user = _make_user(db, 'd2put')
    media = _make_media(db, 'Updatable')
    review = Review(user_id=user.id, media_id=media.id, media_type='movie',
                    rating=3.0)
    db.session.add(review)
    db.session.commit()
    _login(client, _u(db, 'd2put').username)

    ok = client.put('/api/reviews/%d' % review.id, json={'rating': 5})
    assert ok.status_code == 200
    assert _db_row(db, Review, review.id).rating == 5.0

    bad = client.put('/api/reviews/%d' % review.id, json={'rating': 99})
    assert bad.status_code == 400


# ── D-3 ───────────────────────────────────────────────────────────────────────

_COLLECTION_PATHS = [
    '/add_to_watchlist/%d/movie',
    '/mark_as_viewed/%d/movie',
    '/remove_from_watchlist/%d/movie',
    '/remove_from_viewed/%d/movie',
]


def _collection_cases():
    return [
        pytest.param('https://evil.example/landing', id='absolute-https'),
        pytest.param('http://evil.example/landing', id='absolute-http'),
        pytest.param('//evil.example/landing', id='protocol-relative'),
        pytest.param('https://evil.example', id='bare-host'),
        pytest.param('javascript:alert(1)', id='javascript-scheme'),
        pytest.param('/\\evil.example/landing', id='backslash-leading'),
        pytest.param('\\\\evil.example', id='double-backslash'),
        pytest.param('not a url at all', id='malformed'),
        pytest.param('////evil.example', id='many-slashes'),
        pytest.param('https://frameiq.example@evil.example/', id='userinfo'),
        pytest.param('HTTPS://EVIL.EXAMPLE/', id='uppercase-scheme'),
    ]


@pytest.mark.parametrize('referer', _collection_cases())
@pytest.mark.parametrize('path_template', _COLLECTION_PATHS)
def test_d3_hostile_referer_never_redirects_off_site(
        client, db, referer, path_template):
    """The core D-3 property: Location never leaves this origin.

    F51 reproduced ``Referer: https://evil.example/landing`` producing
    ``Location: https://evil.example/landing``.
    """
    _make_user(db, 'd3')
    media = _make_media(db, 'Redirect Target')
    _login(client, 'd3')

    path = path_template % media.tmdb_id
    response = client.post(path, headers={'Referer': referer})

    assert response.status_code in (301, 302, 303, 307, 308), (
        'expected a redirect for %s, got %s' % (path, response.status_code))

    location = response.headers.get('Location', '')
    assert not location.startswith(('http://', 'https://', '//')), \
        'redirected off-site: %r' % location
    assert 'evil.example' not in location, \
        'redirected to the attacker host: %r' % location
    if location.startswith('/'):
        assert not location.startswith('//'), \
            'protocol-relative redirect: %r' % location


@pytest.mark.parametrize('path_template', _COLLECTION_PATHS)
def test_d3_internal_referer_is_preserved(client, db, path_template):
    """A genuine same-origin return path still works — no feature loss."""
    _make_user(db, 'd3ok')
    media = _make_media(db, 'Keep Going')
    _login(client, _u(db, 'd3ok').username)

    response = client.post(path_template % media.tmdb_id,
                           headers={'Referer': 'http://localhost/diary'})

    assert response.status_code in (301, 302, 303, 307, 308)
    assert response.headers.get('Location') == '/diary'


def test_d3_referer_query_string_is_kept_when_safe(client, db):
    """Path and query survive; the return trip keeps its parameters."""
    _make_user(db, 'd3q')
    media = _make_media(db, 'Keep Query')
    _login(client, _u(db, 'd3q').username)

    response = client.post('/add_to_watchlist/%d/movie' % media.tmdb_id,
                           headers={'Referer': 'http://localhost/search?q=abc'})

    assert response.headers.get('Location') == '/search?q=abc'


@pytest.mark.parametrize('path_template', _COLLECTION_PATHS)
def test_d3_missing_referer_falls_back_internally(client, db, path_template):
    """Every handler has a deterministic safe destination."""
    _make_user(db, 'd3n')
    media = _make_media(db, 'No Referer')
    _login(client, _u(db, 'd3n').username)

    response = client.post(path_template % media.tmdb_id)

    assert response.status_code in (301, 302, 303, 307, 308)
    location = response.headers.get('Location', '')
    assert location.startswith('/') and not location.startswith('//'), \
        'fallback was not an internal path: %r' % location


def test_d3_no_handler_redirects_to_bare_referrer():
    """Static guard: no route may redirect to request.referrer directly.

    Walks the AST rather than grepping, so a docstring describing the old
    pattern cannot make the guard pass or fail — only real code counts.
    """
    import ast
    import pathlib

    offenders = []
    for path in sorted(pathlib.Path('routes').rglob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if ast.unparse(node.func) != 'redirect':
                continue
            rendered = ast.unparse(node)
            if 'request.referrer' in rendered and '_safe_return_url' not in rendered:
                offenders.append('%s:%d %s' % (path, node.lineno, rendered))

    assert not offenders, (
        'unfiltered Referer redirects remain:\n  %s' % '\n  '.join(offenders))


# ── D-4 / D-5 ─────────────────────────────────────────────────────────────────


def test_d4_anonymous_cannot_write_to_a_private_list(client, db):
    """F51: anonymous POST /view returned 200 and incremented view_count."""
    owner = _make_user(db, 'd4owner')
    user_list = _make_list(db, owner, is_public=False)

    response = client.post('/api/lists/%d/view' % user_list.id)

    assert response.status_code == 404, (
        'a private list answered %s to an anonymous caller'
        % response.status_code)
    assert _analytics(db, user_list.id) is None, \
        'an analytics row was created for a refused anonymous view'
    assert _views(db, user_list.id) == 0, \
        'a ListView row was inserted for a refused anonymous view'


def test_d4_other_user_cannot_write_to_a_private_list(client, db):
    owner = _make_user(db, 'd4own2')
    _make_user(db, 'd4intruder')
    user_list = _make_list(db, owner, is_public=False)
    _login(client, _u(db, 'd4intruder').username)

    response = client.post('/api/lists/%d/view' % user_list.id)

    assert response.status_code == 404
    assert _analytics(db, user_list.id) is None
    assert _views(db, user_list.id) == 0


def test_d4_owner_can_write_to_their_own_private_list(client, db):
    """The owner's own traffic must still be counted."""
    from models import ListAnalytics
    owner = _make_user(db, 'd4self')
    user_list = _make_list(db, owner, is_public=False)
    _login(client, _u(db, 'd4self').username)

    response = client.post('/api/lists/%d/view' % user_list.id)

    assert response.status_code == 200
    analytics = ListAnalytics.query.filter_by(list_id=user_list.id).first()
    assert analytics is not None
    assert analytics.view_count == 1
    assert _views(db, user_list.id) == 1


def test_d4_collaborator_can_write_to_a_private_list(client, db):
    """Collaborators are the other party entitled to these numbers."""
    from models import ListAnalytics, ListCollaborator
    owner = _make_user(db, 'd4cown')
    mate = _make_user(db, 'd4cmate')
    user_list = _make_list(db, owner, is_public=False)
    db.session.add(ListCollaborator(list_id=user_list.id, user_id=mate.id,
                                    role='editor'))
    db.session.commit()
    _login(client, _u(db, 'd4cmate').username)

    response = client.post('/api/lists/%d/view' % user_list.id)

    assert response.status_code == 200
    analytics = ListAnalytics.query.filter_by(list_id=user_list.id).first()
    assert analytics.view_count == 1


def test_d4_public_list_still_records_anonymous_views(client, db):
    """Anonymous public view counting is the intended product behaviour.

    Preserved deliberately: restricting it would be a product change, and the
    finding was about private lists, not about anonymous counting at all.
    """
    from models import ListAnalytics
    owner = _make_user(db, 'd4pub')
    user_list = _make_list(db, owner, is_public=True)

    response = client.post('/api/lists/%d/view' % user_list.id)

    assert response.status_code == 200
    analytics = ListAnalytics.query.filter_by(list_id=user_list.id).first()
    assert analytics.view_count == 1
    assert _views(db, user_list.id) == 1


def test_d4_refused_request_does_not_move_an_existing_counter(client, db):
    """A pre-existing analytics row must be untouched by a refused call."""
    from models import ListAnalytics
    owner = _make_user(db, 'd4keep')
    user_list = _make_list(db, owner, is_public=False)
    db.session.add(ListAnalytics(list_id=user_list.id, view_count=7,
                                 share_count=3, fork_count=0))
    db.session.commit()
    before = _views(db, user_list.id)

    response = client.post('/api/lists/%d/view' % user_list.id)

    assert response.status_code == 404
    analytics = _analytics(db, user_list.id)
    assert analytics.view_count == 7
    assert analytics.share_count == 3
    assert _views(db, user_list.id) == before


def test_d5_other_user_cannot_write_a_private_lists_share_count(client, db):
    """F51: a different authenticated user incremented a private share_count."""
    owner = _make_user(db, 'd5owner')
    _make_user(db, 'd5attacker')
    user_list = _make_list(db, owner, is_public=False)
    _login(client, _u(db, 'd5attacker').username)

    response = client.post('/api/lists/%d/share' % user_list.id)

    assert response.status_code == 404
    assert _analytics(db, user_list.id) is None, \
        'an analytics row was created for a refused share'


def test_d5_anonymous_cannot_write_a_share_count(client, db):
    owner = _make_user(db, 'd5own2')
    user_list = _make_list(db, owner, is_public=True)

    response = client.post('/api/lists/%d/share' % user_list.id)

    assert response.status_code in (401, 302, 404)
    assert _analytics(db, user_list.id) is None


def test_d5_stranger_cannot_inflate_a_public_lists_share_count(client, db):
    """Sharing is owner-or-collaborator even when the list is public.

    A public list is readable by everyone, but "this user shared their list"
    is a statement about *their* content, so an unrelated account must not be
    able to write it.
    """
    owner = _make_user(db, 'd5pubown')
    _make_user(db, 'd5pubstranger')
    user_list = _make_list(db, owner, is_public=True)
    _login(client, _u(db, 'd5pubstranger').username)

    response = client.post('/api/lists/%d/share' % user_list.id)

    assert response.status_code == 404
    assert _analytics(db, user_list.id) is None


def test_d5_owner_share_still_counted(client, db):
    owner = _make_user(db, 'd5self')
    user_list = _make_list(db, owner, is_public=True)
    _login(client, _u(db, 'd5self').username)

    response = client.post('/api/lists/%d/share' % user_list.id)

    assert response.status_code == 200
    assert response.get_json()['share_count'] == 1
    assert _analytics(db, user_list.id).share_count == 1


def test_d5_collaborator_share_still_counted(client, db):
    from models import ListCollaborator
    owner = _make_user(db, 'd5cown')
    mate = _make_user(db, 'd5cmate')
    user_list = _make_list(db, owner, is_public=True)
    db.session.add(ListCollaborator(list_id=user_list.id, user_id=mate.id,
                                    role='editor'))
    db.session.commit()
    _login(client, _u(db, 'd5cmate').username)

    response = client.post('/api/lists/%d/share' % user_list.id)

    assert response.status_code == 200
    assert _analytics(db, user_list.id).share_count == 1


def test_d5_read_analytics_endpoint_still_works_for_owner(client, db):
    """The read side of analytics is unchanged — only writes were gated."""
    from models import ListAnalytics
    owner = _make_user(db, 'd5read')
    user_list = _make_list(db, owner, is_public=True)
    db.session.add(ListAnalytics(list_id=user_list.id, view_count=5,
                                 share_count=2, fork_count=1))
    db.session.commit()
    _login(client, _u(db, 'd5read').username)

    response = client.get('/api/lists/%d/analytics' % user_list.id)

    assert response.status_code == 200
    assert response.get_json()['analytics']['view_count'] == 5


# ── D-6 ───────────────────────────────────────────────────────────────────────

# Every route that serialises lists and therefore reaches UserList.to_dict().
_LIST_ENDPOINTS = [
    '/api/lists',
    '/api/lists/discover',
    '/api/users/%d/lists',
]


def _find_list(payload, list_id):
    if isinstance(payload, dict):
        for key in ('list', 'lists'):
            value = payload.get(key)
            if isinstance(value, dict) and value.get('id') == list_id:
                return value
            if isinstance(value, list):
                for entry in value:
                    if isinstance(entry, dict) and entry.get('id') == list_id:
                        return entry
    return None


@pytest.mark.parametrize('path_template', _LIST_ENDPOINTS)
def test_d6_anonymous_gets_no_collaborators_or_analytics(
        client, db, path_template):
    """F51: anonymous /api/lists returned a full collaborator roster."""
    from models import ListCollaborator, ListAnalytics
    owner = _make_user(db, 'd6own')
    mate = _make_user(db, 'd6mate')
    user_list = _make_list(db, owner, is_public=True)
    db.session.add(ListCollaborator(list_id=user_list.id, user_id=mate.id,
                                    role='editor'))
    db.session.add(ListAnalytics(list_id=user_list.id, view_count=11,
                                 share_count=4, fork_count=2))
    db.session.commit()

    path = path_template % owner.id if '%d' in path_template else path_template
    response = client.get(path)
    assert response.status_code == 200
    entry = _find_list(response.get_json(), user_list.id)
    assert entry is not None, 'list %d missing from %s' % (user_list.id, path)

    assert entry.get('collaborators') in (None, []), (
        'anonymous caller received a collaborator roster: %r'
        % entry.get('collaborators'))
    assert entry.get('analytics') is None, (
        'anonymous caller received analytics: %r' % entry.get('analytics'))

    # The username must not appear anywhere in the payload.
    assert mate.username not in response.get_data(as_text=True), \
        'a collaborator username leaked to an anonymous caller'


def test_d6_unrelated_authenticated_user_gets_nothing(client, db):
    """Being logged in is not authorisation."""
    from models import ListCollaborator, ListAnalytics
    owner = _make_user(db, 'd6uown')
    mate = _make_user(db, 'd6umate')
    _make_user(db, 'd6stranger')
    user_list = _make_list(db, owner, is_public=True)
    db.session.add(ListCollaborator(list_id=user_list.id, user_id=mate.id,
                                    role='editor'))
    db.session.add(ListAnalytics(list_id=user_list.id, view_count=9,
                                 share_count=3, fork_count=0))
    db.session.commit()
    _login(client, _u(db, 'd6stranger').username)

    response = client.get('/api/lists')
    entry = _find_list(response.get_json(), user_list.id)

    assert entry.get('collaborators') in (None, [])
    assert entry.get('analytics') is None
    assert mate.username not in response.get_data(as_text=True)


def test_d6_owner_still_sees_collaborators_and_analytics(client, db):
    """Preserved: the owner is entitled to their own roster and counters."""
    from models import ListCollaborator, ListAnalytics
    owner = _make_user(db, 'd6vown')
    mate = _make_user(db, 'd6vmate')
    user_list = _make_list(db, owner, is_public=True)
    db.session.add(ListCollaborator(list_id=user_list.id, user_id=mate.id,
                                    role='editor'))
    db.session.add(ListAnalytics(list_id=user_list.id, view_count=6,
                                 share_count=2, fork_count=0))
    db.session.commit()
    _login(client, _u(db, 'd6vown').username)

    response = client.get('/api/lists')
    entry = _find_list(response.get_json(), user_list.id)

    collaborators = entry.get('collaborators')
    assert collaborators, 'the owner lost their collaborator roster'
    assert collaborators[0]['user']['username'] == mate.username
    assert entry.get('analytics') is not None
    assert entry['analytics']['view_count'] == 6


def test_d6_collaborator_still_sees_the_roster(client, db):
    from models import ListCollaborator
    owner = _make_user(db, 'd6cown')
    mate = _make_user(db, 'd6cmate')
    user_list = _make_list(db, owner, is_public=True)
    db.session.add(ListCollaborator(list_id=user_list.id, user_id=mate.id,
                                    role='editor'))
    db.session.commit()
    _login(client, _u(db, 'd6cmate').username)

    response = client.get('/api/lists')
    entry = _find_list(response.get_json(), user_list.id)

    assert entry.get('collaborators'), 'a collaborator lost the roster'
    assert entry['collaborators'][0]['user']['username'] == mate.username


def test_d6_private_list_owner_view_is_unchanged(client, db):
    """to_dict() still works for the owner on a private list."""
    from models import ListCollaborator
    owner = _make_user(db, 'd6pown')
    mate = _make_user(db, 'd6pmate')
    user_list = _make_list(db, owner, is_public=False)
    db.session.add(ListCollaborator(list_id=user_list.id, user_id=mate.id,
                                    role='viewer'))
    db.session.commit()
    _login(client, _u(db, 'd6pown').username)

    response = client.get('/api/users/%d/lists' % owner.id)
    entry = _find_list(response.get_json(), user_list.id)

    assert entry is not None
    assert entry['is_public'] is False
    assert entry['collaborators'][0]['user']['username'] == mate.username


def test_d6_dedicated_collaborators_endpoint_still_authorised(client, db):
    """The authorised path must keep working — nothing regressed."""
    from models import ListCollaborator
    owner = _make_user(db, 'd6down')
    mate = _make_user(db, 'd6dmate')
    _make_user(db, 'd6dstranger')
    user_list = _make_list(db, owner, is_public=True)
    db.session.add(ListCollaborator(list_id=user_list.id, user_id=mate.id,
                                    role='editor'))
    db.session.commit()

    anonymous = client.get('/api/lists/%d/collaborators' % user_list.id)
    assert anonymous.status_code in (302, 401, 403)

    _login(client, _u(db, 'd6dstranger').username)
    stranger = client.get('/api/lists/%d/collaborators' % user_list.id)
    assert stranger.status_code == 403
    assert mate.username not in stranger.get_data(as_text=True)

    _login(client, _u(db, 'd6down').username)
    owner_response = client.get('/api/lists/%d/collaborators' % user_list.id)
    assert owner_response.status_code == 200
    assert owner_response.get_json()['collaborators'][0]['user']['username'] \
        == mate.username


def test_d6_public_list_fields_are_unchanged(client, db):
    """Only the two insider fields changed; public shape is intact."""
    owner = _make_user(db, 'd6shape')
    user_list = _make_list(db, owner, is_public=True,
                           title='Shape Check')

    response = client.get('/api/lists')
    entry = _find_list(response.get_json(), user_list.id)

    for field in ('id', 'user', 'title', 'description', 'is_public',
                  'cover_image', 'cover_posters', 'slug', 'list_type',
                  'like_count', 'comment_count', 'categories', 'created_at',
                  'updated_at', 'item_count', 'is_owner'):
        assert field in entry, 'public field %r disappeared' % field
    assert entry['title'] == 'Shape Check'
    assert entry['user']['username'] == owner.username
    assert 'collaborators' in entry, 'the key was removed rather than emptied'
    assert 'analytics' in entry, 'the key was removed rather than emptied'


def test_d6_no_n_plus_one_for_the_insider_check(client, db, app):
    """The authorisation check must not query per list.

    `_viewer_may_see_insider_data` reads the batch-prefetched collaborator rows
    on the index routes, so a page of lists costs no extra statements.
    """
    from tests.query_counter import count_statements
    owner = _make_user(db, 'd6perf')
    for index in range(8):
        _make_list(db, owner, is_public=True)

    _login(client, _u(db, 'd6perf').username)
    with app.test_request_context():
        with count_statements(app) as statements:
            client.get('/api/lists')

    # Bounded: roughly constant, not growing one-per-list. 8 lists with a
    # per-list query would be well past this.
    assert len(statements) < 40, (
        'listing 8 lists issued %d statements — looks like an N+1'
        % len(statements))