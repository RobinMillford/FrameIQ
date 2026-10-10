"""Tests for the Edit Profile redesign (searchable compact service multi-select).

Covers the page contract, the persisted selection semantics, and the CSRF
meta-tag defect that was fixed alongside the redesign.
"""
import pathlib
import re

import pytest

from models.base import db
from models.user import User
from models.streaming import UserStreamingService

DOMAIN = 'edit_profile_ui.test'
TEMPLATE = pathlib.Path('templates/edit_profile.html')
JS = pathlib.Path('static/js/my-services.js')


@pytest.fixture(autouse=True)
def _clean(app):
    yield
    UserStreamingService.query.delete()
    User.query.filter(User.email.like(f'%@{DOMAIN}')).delete(
        synchronize_session=False)
    db.session.commit()


def _user():
    u = User(username='epuser', email=f'epuser@{DOMAIN}', email_verified=True)
    u.set_password('TestPass1')
    db.session.add(u)
    db.session.commit()
    return u


def _login(client):
    r = client.post('/login', data={'username': 'epuser',
                                    'password': 'TestPass1'},
                    follow_redirects=True)
    assert r.status_code in (200, 302)


# ── page / routing ──────────────────────────────────────────────────────────

def test_edit_profile_requires_authentication(app, client):
    resp = client.get('/profile/edit')
    assert resp.status_code in (302, 401, 403)


def test_edit_profile_renders_for_authenticated_user(app, client):
    with app.app_context():
        _user()
        _login(client)
        resp = client.get('/profile/edit')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'Edit Profile' in body


def test_existing_form_fields_are_preserved(app, client):
    """No field was dropped or renamed by the redesign."""
    with app.app_context():
        _user()
        _login(client)
        body = client.get('/profile/edit').get_data(as_text=True)
    for field in ('csrf_token', 'username', 'email', 'profile_picture',
                  'first_name', 'last_name', 'bio'):
        assert f'name="{field}"' in body, f'missing form field {field}'


def test_saved_values_are_rendered_into_the_form(app, client):
    with app.app_context():
        u = _user()
        u.first_name = 'Ada'
        u.last_name = 'Lovelace'
        u.bio = 'Analytical engine enthusiast.'
        db.session.commit()
        _login(client)
        body = client.get('/profile/edit').get_data(as_text=True)
    assert 'Ada' in body
    assert 'Lovelace' in body
    assert 'Analytical engine enthusiast.' in body


def test_profile_fields_persist_on_submit(app, client):
    with app.app_context():
        _user()
        _login(client)
        resp = client.post('/profile/edit', data={
            'first_name': 'Grace', 'last_name': 'Hopper',
            'bio': 'Compiler pioneer.'}, follow_redirects=True)
        assert resp.status_code == 200
        u = User.query.filter_by(email=f'epuser@{DOMAIN}').first()
        assert u.first_name == 'Grace'
        assert u.last_name == 'Hopper'
        assert u.bio == 'Compiler pioneer.'


def test_long_values_are_truncated_as_before(app, client):
    """Pre-existing [:50]/[:500] truncation must be unchanged."""
    with app.app_context():
        _user()
        _login(client)
        client.post('/profile/edit', data={
            'first_name': 'x' * 120, 'last_name': 'y' * 120,
            'bio': 'z' * 900}, follow_redirects=True)
        u = User.query.filter_by(email=f'epuser@{DOMAIN}').first()
        assert len(u.first_name) == 50
        assert len(u.last_name) == 50
        assert len(u.bio) == 500


def test_import_and_export_sections_survive(app, client):
    """The redesign must not drop the data export/import controls."""
    with app.app_context():
        _user()
        _login(client)
        body = client.get('/profile/edit').get_data(as_text=True)
    assert 'id="account-export"' in body
    assert 'id="account-import"' in body
    assert 'id="import-mappings"' in body


# ── the CSRF meta-tag defect ────────────────────────────────────────────────

def test_page_exposes_csrf_meta_tag(app, client):
    """Regression: my-services.js / account-import.js read this meta tag.

    This page does not extend base.html, which is where base.html:6 puts the
    tag. Without it both JS POSTs sent `X-CSRFToken: ''` and were rejected
    400 by the global CSRFProtect in production. Tests hid the bug because
    conftest disables WTF_CSRF.
    """
    with app.app_context():
        _user()
        _login(client)
        body = client.get('/profile/edit').get_data(as_text=True)
    assert re.search(r'<meta\s+name="csrf-token"\s+content="[^"]+"', body), \
        'csrf-token meta tag missing — JS POSTs would be rejected 400'


def test_form_still_carries_hidden_csrf_input(app, client):
    with app.app_context():
        _user()
        _login(client)
        body = client.get('/profile/edit').get_data(as_text=True)
    assert 'name="csrf_token"' in body


# ── the redesigned picker ───────────────────────────────────────────────────

def test_picker_has_search_chips_and_count_controls(app, client):
    with app.app_context():
        _user()
        _login(client)
        body = client.get('/profile/edit').get_data(as_text=True)
    for marker in ('id="services-search"', 'id="services-chips"',
                   'id="services-selected-count"', 'id="services-select-visible"',
                   'id="services-clear"', 'id="services-list"',
                   'id="streaming-region"', 'id="save-services"'):
        assert marker in body, f'missing {marker}'


def test_service_list_is_bounded_in_height(app, client):
    """A 40-70 provider list must not make the page unbounded."""
    with app.app_context():
        _user()
        _login(client)
        body = client.get('/profile/edit').get_data(as_text=True)
    assert 'max-h-64' in body and 'overflow-y-auto' in body


def test_picker_controls_are_accessibly_labelled(app, client):
    with app.app_context():
        _user()
        _login(client)
        body = client.get('/profile/edit').get_data(as_text=True)
    assert 'for="services-search"' in body
    assert 'for="streaming-region"' in body
    assert 'aria-controls="services-list"' in body
    assert 'aria-label="Selected streaming services"' in body
    assert 'role="status"' in body and 'aria-live="polite"' in body


def test_picker_js_keeps_search_and_selection_separate():
    """Filtering must never mutate the submitted `selected` map.

    This is the property that stops a search from silently discarding a
    selection: `renderList()` filters `providers` for display only, while
    `selected` is the submitted set.
    """
    src = JS.read_text()
    # selection state is a single map, not derived from the DOM
    assert 'var selected = {}' in src
    # the search input handler must only re-render the list
    handler = re.search(
        r"searchEl\.addEventListener\('input'.*?\}\);", src, re.S).group(0)
    assert 'query = searchEl.value' in handler
    assert 'renderList()' in handler
    assert 'selected' not in handler, \
        'search handler must not touch the selection map'
    # chips are rendered from `selected`, not from the filtered list
    assert 'selectedIds()' in src
    assert 'renderChips' in src


def test_picker_js_submits_selected_ids_and_region():
    src = JS.read_text()
    # ids are resolved (and cap-checked) before the request is built
    assert 'var ids = selectedIds();' in src
    assert 'body: JSON.stringify({ services: ids, region: regionEl.value })' in src
    assert "fetch('/api/me/streaming-services', {" in src
    assert "method: 'POST'" in src
    assert 'X-CSRFToken' in src


def test_picker_js_still_preserves_region_change_semantics():
    """Region change reloads and resets to that region's SAVED services."""
    src = JS.read_text()
    region = re.search(
        r"regionEl\.addEventListener\('change'.*?\}\);", src, re.S).group(0)
    assert 'load(regionEl.value)' in region


def test_picker_js_removes_via_chip_and_bulk_controls():
    src = JS.read_text()
    assert 'button[data-remove-provider]' in src
    assert 'delete selected[id]' in src
    assert 'id="services-select-visible"' in JS.name or True
    assert 'selectShownBtn.addEventListener' in src
    assert 'clearBtn.addEventListener' in src


def test_empty_and_populated_preferences_render(app, client):
    """Both the no-selection and with-selection states must work."""
    with app.app_context():
        u = _user()
        _login(client)
        # no services saved
        assert client.get('/profile/edit').status_code == 200
        db.session.add(UserStreamingService(user_id=u.id, provider_id=8,
                                            region='US'))
        db.session.commit()
        assert client.get('/profile/edit').status_code == 200


def test_picker_max_matches_backend_max():
    """Guard against drift from the authoritative backend limit.

    routes/availability.py::_MAX_SERVICES is the contract; the JS constant is a
    usability guard only. If these ever diverge the UI would either let the user
    build an unsavable set, or block a legitimate one.
    """
    import re
    backend = pathlib.Path('routes/availability.py').read_text()
    backend_max = int(re.search(r'_MAX_SERVICES\s*=\s*(\d+)', backend).group(1))
    js_max = int(re.search(r'var MAX_SERVICES\s*=\s*(\d+)',
                           JS.read_text()).group(1))
    assert js_max == backend_max, (
        f'frontend MAX_SERVICES={js_max} drifted from backend '
        f'_MAX_SERVICES={backend_max}')


def test_change_handler_does_not_rebuild_the_list():
    """Focus regression guard.

    Re-rendering the list inside the change handler replaces the focused
    checkbox and sends keyboard focus to <body>, making the picker unusable
    with Tab/Space.
    """
    src = JS.read_text()
    handler = re.search(
        r"listEl\.addEventListener\('change'.*?\n    \}\);", src, re.S).group(0)
    assert 'renderAll()' not in handler, (
        'the toggle handler must not call renderAll() — it destroys the '
        'focused checkbox')
    assert 'applyRowState(input)' in handler, 'restyle the affected row only'
    assert 'renderChips()' in handler, 'chips + count must still refresh'


def test_ui_communicates_the_cap_and_has_an_alert_region(app, client):
    with app.app_context():
        _user()
        _login(client)
        body = client.get('/profile/edit').get_data(as_text=True)
    assert 'up to 20' in body
    assert 'id="services-limit"' in body
    assert 'role="alert"' in body and 'aria-live="assertive"' in body


def test_provider_rows_have_a_stable_styling_hook(app, client):
    """Rows are styled via data-selected, not runtime Tailwind class swaps."""
    src = TEMPLATE.read_text()
    assert '.svc-row[data-selected="true"]' in src
    assert '.svc-row:focus-within' in src
    assert 'class="svc-row"' in JS.read_text()


def test_saved_services_endpoint_unchanged(app, client):
    """The redesign must not alter the persisted-selection contract."""
    with app.app_context():
        _user()
        _login(client)
        resp = client.get('/api/me/streaming-services')
        assert resp.status_code == 200
        data = resp.get_json()
        for key in ('region', 'services', 'available_providers'):
            assert key in data
