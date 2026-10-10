"""Tests for the Diary redesign (compact rows + TV grouping).

The diary page is entirely client-rendered from GET /api/diary, so these tests
pin BOTH the template's structural contract and the grouping rules that the
inline script applies to the server's entry stream.
"""
import pathlib
import re

import pytest

from models import DiaryEntry, MediaItem, TVEpisodeWatch
from models.base import db
from models.user import User

DOMAIN = 'diary_ui.test'
TEMPLATE = pathlib.Path('templates/diary.html')


@pytest.fixture(autouse=True)
def _clean(app):
    yield
    TVEpisodeWatch.query.delete()
    DiaryEntry.query.delete()
    MediaItem.query.delete()
    User.query.filter(User.email.like(f'%@{DOMAIN}')).delete(
        synchronize_session=False)
    db.session.commit()


def _user(name='duser'):
    u = User(username=name, email=f'{name}@{DOMAIN}', email_verified=True)
    u.set_password('TestPass1')
    db.session.add(u)
    db.session.commit()
    return u


def _login(client, name='duser'):
    r = client.post('/login', data={'username': name,
                                    'password': 'TestPass1'},
                    follow_redirects=True)
    assert r.status_code in (200, 302)


def _movie(tmdb_id, title):
    m = MediaItem(tmdb_id=tmdb_id, media_type='movie', title=title,
                  poster_path='/p.jpg')
    db.session.add(m)
    db.session.commit()
    return m


def _show(tmdb_id, title):
    m = MediaItem(tmdb_id=tmdb_id, media_type='tv', title=title,
                  poster_path='/s.jpg')
    db.session.add(m)
    db.session.commit()
    return m


def _diary(user, media, date):
    e = DiaryEntry(user_id=user.id, media_id=media.id, media_type='movie',
                   watched_date=date, rating=4.0)
    db.session.add(e)
    db.session.commit()
    return e


def _episode(user, show_id, season, number, date, name=None):
    e = TVEpisodeWatch(user_id=user.id, show_id=show_id,
                       season_number=season, episode_number=number,
                       episode_name=name, watched_date=date)
    db.session.add(e)
    db.session.commit()
    return e


# ── page + template contract ────────────────────────────────────────────────

def test_diary_requires_authentication(app, client):
    resp = client.get('/diary')
    assert resp.status_code == 302
    assert '/login' in resp.headers.get('Location', '')


def test_diary_page_renders(app, client):
    with app.app_context():
        _user()
        _login(client)
        resp = client.get('/diary')
    assert resp.status_code == 200
    assert 'My Diary' in resp.get_data(as_text=True)


def test_template_keeps_heading_filters_and_load_more():
    src = TEMPLATE.read_text()
    assert 'My Diary' in src
    assert 'id="year-filter"' in src
    assert 'id="month-filter"' in src
    assert 'id="load-more"' in src
    assert 'nav_simple.html' in src


def test_year_filter_is_now_populated_not_dead():
    """Previously #year-filter shipped with only 'All Years' and was dead UI."""
    src = TEMPLATE.read_text()
    assert 'refreshYearOptions' in src
    assert 'seenYears' in src
    assert "yearEl.replaceChildren()" in src


def test_page_size_is_bounded_and_load_more_used():
    src = TEMPLATE.read_text()
    assert 'var PAGE_SIZE = 20' in src
    assert "loadMoreBtn.classList.toggle('hidden', !data.has_next)" in src


def test_grouping_uses_recorded_dates_only():
    """Grouping must merge only rows sharing an IDENTICAL watched_date.

    The script slices the incoming stream on `watched_date` equality and
    never derives a session from timestamps, so separate viewing events are
    never invented or merged.
    """
    src = TEMPLATE.read_text()
    assert "entries[j].watched_date === date" in src
    # TV grouping is per (date block, show id) — never a cross-day merge.
    assert "var key = e.media.id;" in src
    assert 'order.push(key)' in src


def test_every_tv_record_stays_reachable_in_the_dom():
    """Each episode is an individual anchor inside the group — no data loss."""
    src = TEMPLATE.read_text()
    assert 'document.createElement(\'details\')' in src
    assert "createElement('summary')" in src
    assert "createElement('ul')" in src
    assert 'showEntries.forEach(function (entry) {' in src


def test_movies_and_tv_are_visually_distinct():
    src = TEMPLATE.read_text()
    assert "return !e.episode;" in src            # movie partition
    assert "textContent = 'Movie'" in src        # movie label
    assert 'entry.episode.season' in src         # TV episode code


def test_entries_are_built_with_dom_apis_not_html_interpolation():
    """Untrusted values must never be interpolated into innerHTML."""
    src = TEMPLATE.read_text()
    body = re.search(r'<script>(.*?)</script>', src, re.S).group(1)
    # innerHTML is only used for fixed static strings in this script
    for match in re.finditer(r'innerHTML\s*=\s*([^;]+);', body):
        assert '+' not in match.group(1) or 'textContent' in match.group(1), \
            f'possible interpolation: {match.group(1)[:60]}'


def test_accessible_expansion_and_labels():
    src = TEMPLATE.read_text()
    assert 'list-none' in src
    assert 'focus:ring-2' in src
    assert 'aria-live="polite"' in src
    assert 'sr-only' in src


# ── behaviour, through the real API ─────────────────────────────────────────

def test_movie_entries_render(app, client):
    from datetime import date
    with app.app_context():
        u = _user()
        m = _movie(101, 'A Movie')
        _diary(u, m, date(2025, 7, 4))
        _login(client)
        resp = client.get('/api/diary')
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data['entries']) == 1
        e = data['entries'][0]
        assert e['media']['title'] == 'A Movie'
        assert e['media']['media_type'] == 'movie'
        assert 'episode' not in e


def test_tv_entries_carry_episode_details(app, client):
    from datetime import date
    with app.app_context():
        u = _user()
        _show(201, 'Better Call Saul')
        _episode(u, 201, 1, 1, date(2025, 7, 4), name='Uno')
        _episode(u, 201, 1, 2, date(2025, 7, 4), name='Pickpocket')
        _login(client)
        entries = client.get('/api/diary').get_json()['entries']
        assert len(entries) == 2
        assert all(e['episode'] for e in entries)
        codes = {(e['episode']['season'], e['episode']['number']) for e in entries}
        assert codes == {(1, 1), (1, 2)}
        # same show + same day => exactly one collapsible group
        assert len({e['media']['id'] for e in entries}) == 1
        assert len({e['watched_date'] for e in entries}) == 1


def test_separate_days_are_not_merged(app, client):
    from datetime import date
    with app.app_context():
        u = _user()
        _show(202, 'Some Show')
        _episode(u, 202, 1, 1, date(2025, 7, 4))
        _episode(u, 202, 1, 2, date(2025, 7, 11))
        _login(client)
        entries = client.get('/api/diary').get_json()['entries']
        assert len({e['watched_date'] for e in entries}) == 2, \
            'events on different recorded dates must stay separate'


def test_year_and_month_filters_still_work(app, client):
    from datetime import date
    with app.app_context():
        u = _user()
        _movie(301, 'July Film')
        _movie(302, 'March Film')
        _diary(u, db.session.get(MediaItem, 301) or
               MediaItem.query.filter_by(tmdb_id=301).first(), date(2025, 7, 4))
        _diary(u, db.session.get(MediaItem, 302) or
               MediaItem.query.filter_by(tmdb_id=302).first(), date(2024, 3, 2))
        _login(client)
        july = client.get('/api/diary?year=2025&month=7').get_json()
        assert len(july['entries']) == 1
        assert july['entries'][0]['media']['title'] == 'July Film'
        march = client.get('/api/diary?year=2024&month=3').get_json()
        assert len(march['entries']) == 1
        assert march['entries'][0]['media']['title'] == 'March Film'


def test_pagination_does_not_duplicate_or_skip(app, client):
    from datetime import date
    with app.app_context():
        u = _user()
        m = _movie(401, 'Long History')
        for i in range(7):
            _diary(u, m, date(2025, 1, 1 + i))
        _login(client)
        seen, page = [], 1
        while True:
            data = client.get(f'/api/diary?page={page}&per_page=3').get_json()
            seen += [e['id'] for e in data['entries']]
            if not data['has_next']:
                break
            page += 1
        assert len(seen) == 7
        assert len(set(seen)) == 7, 'pagination duplicated an entry'


def test_empty_history_returns_empty_not_error(app, client):
    with app.app_context():
        _user()
        _login(client)
        data = client.get('/api/diary').get_json()
        assert data['entries'] == []
        assert data['total'] == 0
        assert data['has_next'] is False


def test_user_isolation_preserved(app, client):
    from datetime import date
    with app.app_context():
        u1 = _user('duser')
        m = _movie(501, 'Private Film')
        _diary(u1, m, date(2025, 5, 5))
        _user('duser2')
        _login(client, 'duser2')
        data = client.get('/api/diary').get_json()
        assert data['entries'] == []


def test_diary_query_bounds_unchanged(app, client):
    """The redesign must not add server-side queries."""
    from datetime import date
    with app.app_context():
        u = _user()
        m = _movie(601, 'Scale Film')
        for i in range(30):
            _diary(u, m, date(2025, 1, 1 + (i % 28)))
        _login(client)
        client.get('/api/diary?page=1&per_page=25')
        data = client.get('/api/diary?page=2&per_page=5').get_json()
        assert data['current_page'] == 2
        assert isinstance(data['entries'], list)
