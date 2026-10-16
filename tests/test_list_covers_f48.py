"""F48 — automatic list-cover artwork.

The cover for a list is derived from **that list's own first ten items, in the
list's own order**. These tests pin the selection and ordering rule, the empty
and incomplete cases, that the collage costs one query and not one per list,
and that the broken data-URI upload flow is gone.

Nothing here reimplements the browser renderer. Where the collage itself is
asserted on, ``static/js/list-cover.js`` and ``static/js/dom-safety.js`` are
executed under node (the pattern F41 established) so a green test means the
real code agrees.
"""
import itertools
import json
import os
import pathlib
import re
import uuid

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
LIST_COVER_JS = ROOT / 'static' / 'js' / 'list-cover.js'
DOM_SAFETY_JS = ROOT / 'static' / 'js' / 'dom-safety.js'
LIST_ENHANCEMENTS_JS = ROOT / 'static' / 'js' / 'list-enhancements.js'

import sys  # noqa: E402

sys.path.insert(0, str(ROOT))
from utils import list_covers  # noqa: E402
from tests.query_counter import count_statements  # noqa: E402
from utils.db_target import classify  # noqa: E402


# ── helpers ────────────────────────────────────────────────────────────────

def _strip_js_comments(source):
    """Remove /* */ and // comments so assertions test code, not prose."""
    without_block = re.sub(r'/\*.*?\*/', '', source, flags=re.DOTALL)
    return re.sub(r'//[^\n]*', '', without_block)


def _user(db, name='owner'):
    from models import User
    username = f'{name}_{uuid.uuid4().hex[:8]}'
    user = User(username=username, email=f'{username}@cover-f48.test',
                email_verified=True)
    user.set_password('TestPass1')
    db.session.add(user)
    db.session.commit()
    return user


def _list(db, user=None, title='Cover list', **kw):
    """A list owned by `user`, or by a fresh user when none is given.

    Each test owns its user rather than using `sample_user`: that fixture
    deletes the user at teardown, and `user_list.user_id` is NOT NULL with no
    cascade, so a list created during a test blocks its own teardown. This is
    the arrangement `tests/test_cover_image_f39.py` already uses.
    """
    from models import UserList
    if user is None:
        user = _user(db)
    kw.setdefault('slug', 'f48-%s' % uuid.uuid4().hex[:10])
    ul = UserList(user_id=user.id, title=title, **kw)
    db.session.add(ul)
    db.session.commit()
    return ul


#: `tmdb_id` is UNIQUE, so this module must mint ids no other suite uses.
#:
#: Three constraints, learned the hard way:
#:
#: 1. NOT A FIXED BASE. The first version used `900000 + counter` and
#:    `tests/test_migrate_remove_wishlist.py` hard-codes 900001-900003. Those
#:    six tests passed alone, then errored in the full suite with
#:    `UNIQUE constraint failed: media_item.tmdb_id` — a failure that pointed
#:    at the migration tests rather than at this module.
#: 2. ABOVE EVERY FIXTURE ID. The largest tmdb_id literal anywhere in the suite
#:    is 999999999, so anything at or above 1e9 is free.
#: 3. **BELOW 2**147**483**647.** `MediaItem.tmdb_id` is `db.Integer`, which
#:    PostgreSQL compiles to `integer` / `numeric_precision = 32` — a signed
#:    32-bit column. SQLite's `INTEGER` is 64-bit, so the offline suite cannot
#:    detect an overflow here at all; it only appears against PostgreSQL, as
#:    `ERROR: integer out of range`. The second version used
#:    `1_500_000_000 + uuid % 900_000_000`, whose maximum is 2,399,999,999 —
#:    252,516,352 past the column's limit. The offline suite was green and
#:    any PostgreSQL run would have failed.
#:
#: The span below leaves ~47 million of headroom, and the counter (a few
#: thousand at most) cannot approach it.
_TMDB_MIN = 1_100_000_000
_TMDB_SPAN = 1_000_000_000
INT32_MAX = 2_147_483_647

_TMDB_BASE = _TMDB_MIN + (uuid.uuid4().int % _TMDB_SPAN)
_TMDB_SEQ = itertools.count(0)


def _media(db, n, poster=True, media_type='movie'):
    """A distinct MediaItem with poster_path '/p{n}.jpg' (or None).

    The counter guarantees uniqueness within this module (a hash-derived id
    collided inside the 200-item test); the randomised base guarantees it
    against every other suite, and stays inside the signed 32-bit column.
    """
    from models import MediaItem
    m = MediaItem(tmdb_id=_TMDB_BASE + next(_TMDB_SEQ),
                  media_type=media_type, title=f'Movie {n}',
                  poster_path=f'/p{n}.jpg' if poster else None)
    db.session.add(m)
    db.session.commit()
    return m


def _add(db, ul, media, position, media_type='movie', poster_override=None):
    from models.lists import UserListItem
    if poster_override is not None:
        media.poster_path = poster_override
        db.session.commit()
    li = UserListItem(list_id=ul.id, media_id=media.id,
                      media_type=media_type, position=position)
    db.session.add(li)
    db.session.commit()
    return li


def _fill(db, ul, count, start=1, with_posters=True):
    """Give `ul` `count` items at position 1..count."""
    media = []
    for n in range(start, start + count):
        m = _media(db, n, poster=with_posters)
        _add(db, ul, m, n)
        media.append(m)
    return media


# ── 1. selection and ordering ──────────────────────────────────────────────

def test_ten_or_more_items_yields_exactly_the_first_ten(db):
    """A list with more than ten items contributes only its first ten."""
    ul = _list(db)
    _fill(db, ul, 15)

    posters = list_covers.cover_posters(ul)

    assert posters == ['/p%d.jpg' % n for n in range(1, 11)]


def test_cover_follows_position_order_not_insertion_order(db):
    """The drag handle is `position`, so a later-inserted item can lead."""
    ul = _list(db)
    # Insert 1..5 in order, then move item 5 to the front.
    media = _fill(db, ul, 5)
    from models.lists import UserListItem
    last = UserListItem.query.filter_by(
        list_id=ul.id, media_id=media[-1].id).one()
    last.position = 0
    db.session.commit()

    assert list_covers.cover_posters(ul)[0] == '/p5.jpg'


def test_cover_is_list_specific_not_global(db):
    """Two lists must not share posters, and neither sees the other's items."""
    a = _list(db, title='A')
    b = _list(db, title='B')
    _fill(db, a, 3, start=1)
    _fill(db, b, 3, start=101)

    assert list_covers.cover_posters(a) == ['/p1.jpg', '/p2.jpg', '/p3.jpg']
    assert list_covers.cover_posters(b) == ['/p101.jpg', '/p102.jpg',
                                            '/p103.jpg']


def test_batched_prefetch_matches_per_list_queries(db):
    """The batch and the single-list path must agree, or cards and details
    would draw different covers from the same list."""
    lists = [_list(db, title='L%d' % i) for i in range(3)]
    _fill(db, lists[0], 12)
    _fill(db, lists[1], 2)
    # lists[2] intentionally left empty

    singly = [list_covers.cover_posters(ul) for ul in lists]
    list_covers.prefetch_list_covers(lists)
    batched = [ul._prefetched_cover_posters for ul in lists]

    assert batched == singly


def test_ordering_is_total_even_when_position_ties(db):
    """`position` is deliberately not unique, so `position, added_at` can tie.

    The `id` tiebreak is what makes the cover deterministic; without it the
    same list could render a different order on two reads.
    """
    ul = _list(db)
    from models.lists import UserListItem
    ids = []
    for n in (3, 1, 2):          # same position, inserted out of order
        m = _media(db, n)
        _add(db, ul, m, 5)
        ids.append(UserListItem.query.filter_by(list_id=ul.id,
                                                media_id=m.id).one().id)

    first = list_covers.cover_posters(ul)
    db.session.expunge_all()
    second = list_covers.cover_posters(ul)

    assert first == second
    # insertion order (id asc) is the documented final tiebreak
    by_id = sorted(zip(ids, ['/p3.jpg', '/p1.jpg', '/p2.jpg']))
    assert first == [p for _, p in by_id]


# ── 2. fewer than ten, empty, missing posters ───────────────────────────────

@pytest.mark.parametrize('count', [1, 2, 3, 4, 5, 6, 7, 8, 9])
def test_fewer_than_ten_uses_all_available(db, count):
    ul = _list(db)
    _fill(db, ul, count)
    assert list_covers.cover_posters(ul) == ['/p%d.jpg' % n
                                             for n in range(1, count + 1)]


def test_empty_list_yields_no_posters(db):
    ul = _list(db)
    assert list_covers.cover_posters(ul) == []


def test_missing_poster_keeps_its_slot_rather_than_shifting(db):
    """A title with no poster still occupies a position.

    Dropping it would slide every later poster forward, so the cover would
    show movies 2,3,4... and quietly misrepresent the list.
    """
    ul = _list(db)
    no_poster = _media(db, 2, poster=False)
    _add(db, ul, _media(db, 1), 1)
    _add(db, ul, no_poster, 2)
    _add(db, ul, _media(db, 3), 3)

    assert list_covers.cover_posters(ul) == ['/p1.jpg', None, '/p3.jpg']


def test_all_missing_posters_yields_all_none_not_empty(db):
    """Ten slots, no posters — distinct from an empty list, and still ten."""
    ul = _list(db)
    for n in range(1, 11):
        _add(db, ul, _media(db, n, poster=False), n)

    posters = list_covers.cover_posters(ul)
    assert len(posters) == 10
    assert all(p is None for p in posters)


def test_tv_items_are_included(db):
    """A mixed list covers what it actually contains.

    Filtering to movies only would leave a TV-only list with no cover at all,
    which is the placeholder showing where real posters were one filter away.
    """
    ul = _list(db)
    _add(db, ul, _media(db, 1, media_type='movie'), 1, media_type='movie')
    _add(db, ul, _media(db, 2, media_type='tv'), 2, media_type='tv')

    assert list_covers.cover_posters(ul) == ['/p1.jpg', '/p2.jpg']


def test_dangling_media_id_does_not_shift_the_cover(db, app):
    """An item whose media row is gone still holds its position.

    SQLite runs with `PRAGMA foreign_keys = 0` here (no pragma anywhere in the
    project), so `user_list_item.media_id` can dangle for real. That is why the
    query outer-joins `media_item`: with an inner join the row disappears and
    every later poster slides forward, so the cover would show movies 2,3,4...
    and quietly misrepresent the list.
    """
    ul = _list(db, title='Dangling')
    _add(db, ul, _media(db, 1), 1)
    from models.lists import UserListItem
    ghost = UserListItem(list_id=ul.id, media_id=999999999,
                         media_type='movie', position=2)
    db.session.add(ghost)
    _add(db, ul, _media(db, 3), 3)
    db.session.commit()

    assert list_covers.cover_posters(ul) == ['/p1.jpg', None, '/p3.jpg']


def test_the_ordering_tiebreak_is_in_the_emitted_sql(db, app):
    """Structural guard for the `id` tiebreak.

    A behavioural test cannot catch its removal: `position, added_at` can tie,
    and SQLite then happens to return insertion order, which is what the
    behavioural test asserts anyway. The mutant survives because the two
    orderings agree on this data — not because the tiebreak is unnecessary.
    Without `id` the cover is only *probably* stable, and it would stop being
    so the moment a different plan or backend returned ties the other way.
    """
    ul = _list(db, title='Sql')
    _fill(db, ul, 3)

    with app.test_request_context():
        with count_statements(app) as stmts:
            list_covers.cover_posters(ul)
    sql = ' '.join(stmts).upper()

    assert 'ROW_NUMBER()' in sql

    # Scope to the WINDOW clause. A plain search of the whole statement is not
    # enough: `user_list_item.id` also appears in the subquery's SELECT list
    # (`AS item_id`), so an index-based search would still find it after the
    # tiebreak was deleted and the test would pass on a broken query.
    window = re.search(r'OVER\s*\(([^)]*ORDER BY[^)]*)\)', sql)
    assert window, sql
    clause = window.group(1)

    ordered = [c.strip() for c in clause.split('ORDER BY', 1)[1].split(',')]
    assert ordered == ['USER_LIST_ITEM.POSITION ASC',
                       'USER_LIST_ITEM.ADDED_AT ASC',
                       'USER_LIST_ITEM.ID ASC'], ordered

    # The join must be a LEFT/OUTER join, or a dangling media_id drops a tile.
    assert 'LEFT OUTER JOIN' in sql or 'LEFT JOIN' in sql

    # The window filter is what bounds the read.
    assert re.search(r'WHERE ANON_1\.RN <= \?', sql), sql


# ── 3. API surface + authorization ─────────────────────────────────────────

def test_to_dict_exposes_cover_posters(db):
    ul = _list(db)
    _fill(db, ul, 3)

    payload = ul.to_dict()

    assert payload['cover_posters'] == ['/p1.jpg', '/p2.jpg', '/p3.jpg']


def test_to_dict_keeps_cover_image_contract(db):
    """F48 is additive: an already-saved external cover must still render."""
    ul = _list(db,
               cover_image='https://image.tmdb.org/t/p/w500/x.jpg')
    _fill(db, ul, 2)

    payload = ul.to_dict()

    assert payload['cover_image'] == 'https://image.tmdb.org/t/p/w500/x.jpg'
    assert payload['cover_posters'] == ['/p1.jpg', '/p2.jpg']


def test_cover_from_already_loaded_items_matches_the_query(db):
    """The detail pages pass the items they already loaded.

    If this diverged from the SQL path, a list's detail page and its card would
    show different covers.
    """
    from sqlalchemy.orm import joinedload
    from models.lists import UserListItem
    ul = _list(db)
    _fill(db, ul, 12)

    items = ul.items.options(joinedload(UserListItem.media)).order_by(
        UserListItem.position, UserListItem.added_at).all()

    assert (list_covers.cover_posters_from_items(items)
            == list_covers.cover_posters(ul))


def test_cover_from_items_sorts_defensively(db):
    """A caller handing over mis-ordered items must not change the cover."""
    ul = _list(db)
    _fill(db, ul, 4)
    from models.lists import UserListItem
    items = UserListItem.query.filter_by(list_id=ul.id).all()
    items.reverse()

    assert (list_covers.cover_posters_from_items(items)
            == ['/p1.jpg', '/p2.jpg', '/p3.jpg', '/p4.jpg'])


def test_discover_api_only_returns_public_lists(client, db):
    """F48 must not widen visibility: covers ride the existing filter."""
    public = _list(db, title='Public', is_public=True)
    private = _list(db, title='Private', is_public=False)
    _fill(db, public, 2)

    response = client.get('/api/lists/discover?per_page=50')
    assert response.status_code == 200
    ids = {entry['id'] for entry in response.get_json()['lists']}

    assert public.id in ids
    assert private.id not in ids


def test_discover_api_carries_cover_posters(client, db):
    ul = _list(db, title='WithPosters', is_public=True)
    _fill(db, ul, 4)

    payload = client.get('/api/lists/discover?per_page=50').get_json()
    entry = next(e for e in payload['lists'] if e['id'] == ul.id)

    assert entry['cover_posters'] == ['/p1.jpg', '/p2.jpg', '/p3.jpg', '/p4.jpg']


def test_private_list_detail_still_403s(client, db):
    """Unchanged by F48, asserted so the collage cannot become a leak."""
    private = _list(db, title='Private', is_public=False)
    _fill(db, private, 3)

    assert client.get(f'/lists/{private.id}').status_code == 403


# ── 3b. the pages actually render ────────────────────────────────────────────
#
# These exist because nothing else rendered `/lists` with a list present, so a
# `{{ list.cover_posters | tojson }}` against an ORM object (where
# `cover_posters` does not exist) raised
# `TypeError: Object of type Undefined is not JSON serializable` and returned
# a 500 with a completely green suite. The template is not the unit under test
# here; the page returning 200 is.


def _owned_list_with_items(db, slug, count=3, user=None, **kw):
    # `user` matters for `/lists`, which renders `current_user.lists`: a list
    # owned by somebody else is simply not on the page.
    ul = _list(db, user, title='Rendered', is_public=True, slug=slug, **kw)
    _fill(db, ul, count)
    return ul


def _login_as(client, db, username):
    from models import User
    user = User(username=username, email=f'{username}@f48-render.test',
                email_verified=True)
    user.set_password('TestPass1')
    db.session.add(user)
    db.session.commit()
    client.post('/login', data={'username': username, 'password': 'TestPass1'},
                follow_redirects=True)
    return user


def test_my_lists_page_renders_the_collage(client, db):
    """`/lists` renders a mount carrying the posters, and does not 500."""
    owner = _login_as(client, db, 'renderowner')
    _owned_list_with_items(db, 'f48-render-mine-1', user=owner)

    response = client.get('/lists')
    body = response.get_data(as_text=True)

    assert response.status_code == 200, body[:500]
    assert 'class="fq-cover"' in body
    assert 'js/list-cover.js' in body
    assert 'css/list-cover.css' in body
    assert 'data-cover-posters=' in body
    for n in (1, 2, 3):
        assert '/p%d.jpg' % n in body


def test_list_detail_page_renders_the_collage(client, db):
    """The detail hero renders for a list whose owner saved no external cover."""
    _login_as(client, db, 'renderowner2')
    ul = _owned_list_with_items(db, 'f48-render-detail-1', count=5)

    response = client.get(f'/lists/{ul.id}')
    body = response.get_data(as_text=True)

    assert response.status_code == 200, body[:500]
    assert 'class="fq-cover"' in body
    assert 'data-cover-aspect="21 / 9"' in body
    # The upload control is gone from the rendered page, not just the template.
    assert 'upload-cover-btn' not in body
    assert 'type="file"' not in body


def test_list_detail_prefers_a_saved_external_cover(client, db):
    """A valid saved cover still wins, so pre-F48 lists look unchanged."""
    from utils.cover_image_policy import ALLOWED_HOSTS
    _login_as(client, db, 'renderowner3')  # noqa: F841
    ul = _owned_list_with_items(db, 'f48-render-cover-1', count=3)
    ul.cover_image = 'https://%s/t/p/w500/x.jpg' % ALLOWED_HOSTS[0]
    db.session.commit()

    body = client.get(f'/lists/{ul.id}').get_data(as_text=True)

    assert 'id="list-cover"' in body
    assert 'class="fq-cover"' not in body


def test_empty_list_page_still_renders(client, db):
    """Zero items is a normal list, not an error path."""
    owner = _login_as(client, db, 'renderowner4')
    _list(db, owner, title='Nothing here', is_public=True,
          slug='f48-render-empty-1')

    response = client.get('/lists')
    body = response.get_data(as_text=True)

    assert response.status_code == 200, body[:500]
    # An empty list still emits a mount; the renderer paints the placeholder.
    assert "data-cover-posters='[]'" in body or \
        'data-cover-posters=\'[]\'' in body


def test_cover_posters_property_is_prefetch_only(db):
    """The template-facing property must never issue a query.

    It is the difference between a grid of lists costing ONE extra statement
    and costing one per card.
    """
    ul = _list(db, title='Property')
    _fill(db, ul, 3)

    assert ul.cover_posters == [], 'not prefetched yet'

    list_covers.prefetch_list_covers([ul])
    assert ul.cover_posters == ['/p1.jpg', '/p2.jpg', '/p3.jpg']


# ── 4. performance ──


def test_prefetch_covers_many_lists_in_one_query(db, app):
    """No N+1: 12 lists resolve their posters in a single extra statement."""
    from tests.query_counter import count_statements

    lists = [_list(db, title='P%d' % i) for i in range(12)]
    for ul in lists:
        _fill(db, ul, 10)

    with app.test_request_context():
        with count_statements(app) as stmts:
            list_covers.prefetch_list_covers(lists)

    poster_sql = [s for s in stmts if 'media_item' in s.lower()]
    assert len(poster_sql) == 1, poster_sql


def test_prefetch_covers_a_long_list_costs_no_more_than_a_short_one(
        db, app):
    """The window filter means a 200-item list costs one bounded statement."""
    from tests.query_counter import count_statements
    short = _list(db, title='Short')
    long_ = _list(db, title='Long')
    _fill(db, short, 3)
    _fill(db, long_, 200, start=1000)

    with app.test_request_context():
        with count_statements(app) as stmts:
            list_covers.prefetch_list_covers([short, long_])

    poster_sql = [s for s in stmts if 'media_item' in s.lower()]
    assert len(poster_sql) == 1, poster_sql
    # The window filter is what bounds it: a 200-item list contributes the
    # same 10 rows a 3-item list does.
    assert len(long_._prefetched_cover_posters) == list_covers.COVER_POSTER_LIMIT


def test_no_list_endpoint_is_n_plus_one_on_covers(db, app, client):
    """Every endpoint that serialises a PAGE of lists must batch the covers.

    `UserList.to_dict()` falls back to a single-list query when no prefetch ran,
    so a comprehension over `pagination.items` without one is an N+1 that looks
    like an innocent list comprehension. This guards the three page endpoints
    at once; a fourth added later without a prefetch fails here.
    """
    owner = _user(db, name='PageOwner')
    public_lists = []
    for i in range(8):
        ul = _list(db, owner, title='Page%d' % i, is_public=True)
        _fill(db, ul, 4)
        public_lists.append(ul)

    from models import ListCategory
    from models.lists import UserListCategory
    cat = ListCategory(name='f48-cat-%s' % uuid.uuid4().hex[:6], usage_count=0)
    db.session.add(cat)
    db.session.commit()
    for ul in public_lists:
        db.session.add(UserListCategory(list_id=ul.id, category_id=cat.id))
    db.session.commit()

    endpoints = [
        '/api/lists?per_page=8',
        '/api/lists/discover?per_page=8',
        '/api/lists/discover/category/%d?per_page=8' % cat.id,
    ]

    for url in endpoints:
        # Detach everything first. Without this the three requests share one
        # session, the identity map hands back the SAME UserList instances, and
        # the prefetch from endpoint #1 is still attached to them when #3 runs.
        # The missing-prefetch regression would then look like "no queries at
        # all" instead of the N+1 it actually is.
        db.session.expunge_all()
        with app.test_request_context():
            with count_statements(app) as stmts:
                response = client.get(url)
        assert response.status_code == 200, (url, response.status_code)
        payload = response.get_json()
        lists = payload.get('lists') or []
        assert len(lists) == 8, (url, len(lists))

        # Exactly ONE windowed cover statement, however many lists were
        # serialized. More than one is the N+1 this guard exists to prevent.
        poster_sql = [s for s in stmts if 'ROW_NUMBER()' in s.upper()]
        assert len(poster_sql) == 1, (
            '%s issued %d cover queries for 8 lists' % (url, len(poster_sql)))

        # And every list actually got its posters from that one query.
        assert all(len(entry['cover_posters']) == 4 for entry in lists), url


def test_generated_tmdb_ids_fit_the_column():
    """The minted ids must fit `MediaItem.tmdb_id` on PostgreSQL.

    `db.Integer` is `integer` / `numeric_precision = 32` on PostgreSQL — signed
    32-bit — while SQLite's `INTEGER` is 64-bit. So the offline suite cannot
    detect an overflow here at all; it surfaces only against PostgreSQL, as
    `ERROR: integer out of range`.

    An earlier version of this helper used
    `1_500_000_000 + uuid % 900_000_000`, whose maximum is 2,399,999,999 —
    252,516,352 past the limit. Every offline test passed. This asserts the
    bound explicitly so the next change to the range fails here instead of in
    someone's PostgreSQL run.
    """
    # 10,000 draws is far more than the module ever needs; the maximum is what
    # matters, not the distribution.
    highest = max(_TMDB_MIN + (uuid.uuid4().int % _TMDB_SPAN)
                  for _ in range(10000))
    # The counter never exceeds a few hundred (the largest single test creates
    # 200 media rows); 10x leaves ample room.
    worst_case = highest + 2000

    assert _TMDB_MIN > 999_999_999, (
        'the base must clear every tmdb_id literal in the suite; the largest '
        'is 999999999')
    assert worst_case <= INT32_MAX, (
        'minted tmdb_id %d exceeds INT32_MAX %d; PostgreSQL will reject it'
        % (worst_case, INT32_MAX))


def test_generated_ids_do_not_collide_with_the_migration_fixtures(db):
    """The specific regression: 900001-900003 belong to another suite.

    `tests/test_migrate_remove_wishlist.py` hard-codes those three ids. When
    this module minted `900000 + counter`, its first three rows collided and
    that suite errored in the full run while passing alone.
    """
    ul = _list(db, title='Range')
    made = _fill(db, ul, 3)

    used = [m.tmdb_id for m in made]
    # Only the rows this test created: the session database is shared, so
    # querying every MediaItem would count other suites' leftovers.
    assert len(set(used)) == 3, 'ids are not distinct within one list'
    for reserved in (900001, 900002, 900003):
        assert reserved not in used


def test_prefetch_is_a_noop_for_empty_input(db):
    assert list_covers.prefetch_list_covers([]) is None
    assert list_covers.prefetch_list_covers(None) is None


def test_prefetch_tolerates_none_entries(db):
    ul = _list(db)
    _fill(db, ul, 2)
    list_covers.prefetch_list_covers([None, ul])
    assert ul._prefetched_cover_posters == ['/p1.jpg', '/p2.jpg']


# ── 5. the renderer ───────────────────────────────────────────────────────
#
# The collage as a browser actually lays it out — gap-free geometry, tile
# order, lazy loading, hostile poster values, responsive widths — is asserted
# in `tests/test_list_cover_browser_f48.py`, which drives Chromium with the
# real CSS. That is a stronger claim than executing the module under node
# with a hand-written DOM shim, which would only prove the shim agrees with
# itself. The invariant a browser cannot observe is asserted here:

def test_renderer_never_uses_innerhtml():
    """The dom-safety contract: build elements, never concatenate strings."""
    source = LIST_COVER_JS.read_text(encoding='utf-8')
    for marker in ('innerHTML', 'outerHTML', 'insertAdjacentHTML',
                   'document.write'):
        assert marker not in source, \
            'list-cover.js must not use %s' % marker


# ── 6. the old data-URI upload flow is gone ────────────────────────────────

def test_list_enhancements_has_no_upload_flow():
    """No data-URI writer remains in the manager.

    Asserted on source because there is no runtime path left to invoke: these
    were private methods reached only from a button that no longer exists.
    """
    # Comments are stripped first: the replacement block is a comment that
    # NAMES `readAsDataURL` in order to explain what was removed and why, so a
    # naive text search would fail on the very documentation of the fix.
    code = _strip_js_comments(
        LIST_ENHANCEMENTS_JS.read_text(encoding='utf-8'))
    for marker in ('readAsDataURL', 'FileReader', 'setupCoverImageUpload',
                   'handleCoverImageUpload', 'updateCoverImage',
                   'upload-cover-btn', 'cover-image-input'):
        assert marker not in code, \
            'list-enhancements.js still contains %s' % marker


def test_list_detail_template_has_no_upload_control():
    """No file input, so no path can produce a data: URL at all."""
    template = ROOT / 'templates' / 'list_detail.html'
    source = template.read_text(encoding='utf-8')
    assert 'upload-cover-btn' not in source
    assert 'cover-image-input' not in source
    assert 'type="file"' not in source


def test_no_template_uses_a_file_input_for_a_cover():
    """Guards against the control being reintroduced on another list page."""
    for template in sorted((ROOT / 'templates').glob('*.html')):
        if template.name == 'list_detail.html':
            continue
        source = template.read_text(encoding='utf-8', errors='replace')
        assert 'upload-cover-btn' not in source, template.name
        assert 'cover-image-input' not in source, template.name


def test_collage_is_never_persisted_as_a_data_uri(db):
    """Covers are derived at read time; nothing is written.

    Guards the constraint that made this cheap: no base64 blob ever reaches a
    column, so no schema change and no migration were needed.
    """
    ul = _list(db)
    _fill(db, ul, 5)

    payload = ul.to_dict()

    assert ul.cover_image is None
    serialised = json.dumps(payload)
    assert 'data:' not in serialised
    assert 'base64' not in serialised
    assert payload['cover_posters'] == ['/p%d.jpg' % n for n in range(1, 6)]


# ── 7. safety invariants that must survive ─────────────────────────────────

def test_this_suite_never_points_at_a_real_database():
    """The offline suite runs on SQLite; a DSN check guards the F46 lesson."""
    assert classify(os.environ.get('DATABASE_URL') or '').is_local or \
        not os.environ.get('DATABASE_URL')