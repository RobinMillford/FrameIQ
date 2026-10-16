"""Feature F20 — offline / local-diagnostic safety boundary.

Feature F20 exists because of a real accident: during the F19 review a
scratch probe did ``from app import create_app``, and because ``app.py`` runs
``load_dotenv()`` and ``app = create_app()`` at MODULE SCOPE, that single
import connected to the production database configured in ``.env`` and ran
the read-only schema guard against it.

These tests pin the boundary that now prevents that, and — just as important —
pin that the PRODUCTION schema guard has not been weakened. Nothing here opens
a real network connection: the classification is pure, the subprocess probe
boots the app against a throwaway SQLite file, and every remote URL used is a
fictional string that is only ever classified, never dialled.
"""
import contextlib
import os
import socket
import subprocess
import sys
import textwrap

import pytest

from utils import offline_diagnostics as od


# ── classification ──────────────────────────────────────────────────────────

@pytest.mark.parametrize('url', [
    'sqlite:////tmp/frameiq_offline_x.db',
    'sqlite://',
    'sqlite3:///tmp/a.db',
    'memory://',
    'postgresql://u:p@localhost:5432/db',
    'postgresql://u:p@127.0.0.1:5432/db',
    'postgresql://u:p@[::1]:5432/db',
])
def test_local_urls_are_recognised(url):
    assert od.is_local_database_url(url) is True


@pytest.mark.parametrize('url', [
    'postgresql://u:p@db.example.com/db',
    'postgresql://u:p@10.0.0.5:5432/db',
    'postgresql://u:p@192.168.1.10/db',
    'mysql://u:p@example.com/db',
    '',
    '   ',
    'not-a-url',
    '://broken',
    None,
])
def test_remote_or_unparseable_urls_fail_closed(url):
    """Anything not provably local is treated as REMOTE."""
    assert od.is_local_database_url(url) is False


def test_wildcard_bind_address_is_not_treated_as_local():
    """0.0.0.0 is INADDR_ANY (a wildcard BIND), not a loopback destination.

    Accepting it would weaken the guard for no benefit, so it must be remote.
    """
    assert od.is_local_database_url(
        'postgresql://u:p@0.0.0.0:5432/db') is False
    assert '0.0.0.0' not in od._LOOPBACK_HOSTS


def test_ipv6_loopback_is_accepted_in_its_canonical_form():
    """urlparse strips IPv6 brackets, so the set must hold bare '::1'."""
    assert od.is_local_database_url(
        'postgresql://u:p@[::1]:5432/db') is True
    assert '::1' in od._LOOPBACK_HOSTS


def test_an_arbitrary_hostname_is_not_mistaken_for_local():
    """The guard must not rely on a name that merely looks local."""
    for name in ('localhost.evil.com', '127.0.0.1.evil.com',
                 'mylocalhost', 'localhost-db'):
        url = 'postgresql://u:p@%s:5432/db' % name
        assert od.is_local_database_url(url) is False, name


@contextlib.contextmanager
def _env_restored(*names):
    """Restore these environment variables EXACTLY as they were.

    conftest sets SKIP_SCHEMA_GUARD via setdefault, so popping it afterwards
    would silently remove a value the rest of the suite depends on. Anything
    touched here must be put back, present-or-absent.
    """
    saved = {name: os.environ.get(name) for name in
             ('DATABASE_URL', 'SKIP_SCHEMA_GUARD') + tuple(names)}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


# ── the enforceable refusal ─────────────────────────────────────────────────

def test_assert_local_database_refuses_remote():
    with pytest.raises(od.UnsafeDatabaseTargetError):
        od.assert_local_database('postgresql://u:p@db.example.com/db')


def test_assert_local_database_accepts_local():
    assert od.assert_local_database('sqlite:///tmp/x.db') is None


def test_refusal_never_leaks_the_url_or_credentials():
    """A traceback must not become a credential leak."""
    secret = 'postgresql://admin:hunter2@db.internal.example.com/prod'
    with pytest.raises(od.UnsafeDatabaseTargetError) as exc:
        od.assert_local_database(secret, context='probe')
    message = str(exc.value)
    assert 'hunter2' not in message
    assert 'admin' not in message
    assert 'db.internal.example.com' not in message
    assert 'probe' in message


def test_assert_offline_diagnostic_takes_no_relaxation_knob():
    """No parameter can talk this guard out of its verdict."""
    import inspect
    assert list(inspect.signature(od.assert_offline_diagnostic).parameters) == []
    with pytest.raises(TypeError):
        od.assert_offline_diagnostic(allow_dotenv_remote=True)  # noqa


def test_call_order_is_documented_on_the_guard_itself():
    """The error must tell the caller what to do, and when."""
    previous = os.environ.get('DATABASE_URL')
    os.environ['DATABASE_URL'] = 'postgresql://u:p@db.example.com/db'
    try:
        with pytest.raises(od.UnsafeDatabaseTargetError) as exc:
            od.assert_offline_diagnostic()
    finally:
        if previous is None:
            os.environ.pop('DATABASE_URL', None)
        else:
            os.environ['DATABASE_URL'] = previous
    message = str(exc.value)
    assert 'use_offline_database()' in message
    assert 'BEFORE' in message


# ── establishing the boundary ───────────────────────────────────────────────

def test_env_helper_restores_a_variable_that_was_present():
    """conftest sets SKIP_SCHEMA_GUARD via setdefault; removing it silently
    breaks every later test that relies on the guard being skippable."""
    assert 'SKIP_SCHEMA_GUARD' in os.environ, 'precondition: conftest set it'
    before = os.environ['SKIP_SCHEMA_GUARD']
    with _env_restored():
        os.environ['SKIP_SCHEMA_GUARD'] = 'tampered'
        od.use_offline_database()
    assert os.environ['SKIP_SCHEMA_GUARD'] == before


def test_env_helper_restores_a_variable_that_was_absent():
    os.environ.pop('FRAMEIQ_PROBE', None)
    with _env_restored('FRAMEIQ_PROBE'):
        os.environ['FRAMEIQ_PROBE'] = 'x'
    assert 'FRAMEIQ_PROBE' not in os.environ


def test_env_helper_restores_database_url_even_after_a_failure():
    before = os.environ.get('DATABASE_URL')
    with pytest.raises(RuntimeError):
        with _env_restored():
            od.use_offline_database()
            raise RuntimeError('boom')
    assert os.environ.get('DATABASE_URL') == before


def test_use_offline_database_configures_a_local_target():
    with _env_restored():
        url = od.use_offline_database()
        assert od.is_local_database_url(url)
        assert os.environ['DATABASE_URL'] == url
        assert os.environ['SKIP_SCHEMA_GUARD'] == '1'


def test_use_offline_database_refuses_a_remote_override():
    with pytest.raises(od.UnsafeDatabaseTargetError):
        od.use_offline_database('postgresql://u:p@db.example.com/db')


def test_use_offline_database_accepts_an_explicit_local_override():
    with _env_restored():
        url = od.use_offline_database('sqlite:///:memory:')
        assert os.environ['DATABASE_URL'] == 'sqlite:///:memory:'
        assert od.is_local_database_url(url)


def test_assert_offline_diagnostic_refuses_an_inherited_remote_target():
    """The 'I forgot to call use_offline_database()' case."""
    with _env_restored():
        os.environ['DATABASE_URL'] = 'postgresql://u:p@db.example.com/db'
        with pytest.raises(od.UnsafeDatabaseTargetError):
            od.assert_offline_diagnostic()


def test_assert_offline_diagnostic_passes_for_a_local_target():
    with _env_restored():
        os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
        assert od.assert_offline_diagnostic() is None


# ── ambient reporting (never exposes credentials) ───────────────────────────

def test_describe_ambient_target_exposes_no_secrets():
    report = od.describe_ambient_target()
    assert set(report) == {
        'env_set', 'env_is_local', 'env_host',
        'dotenv_set', 'dotenv_is_local', 'dotenv_host'}
    blob = repr(report).lower()
    for secret in ('password', 'postgresql://', 'mysql://', '@', 'user:'):
        assert secret not in blob, secret


def test_describe_ambient_target_reports_dotenv_without_reading_it_publicly():
    """It may notice .env points at production, but must not surface secrets."""
    report = od.describe_ambient_target()
    assert isinstance(report['dotenv_set'], bool)
    assert isinstance(report['dotenv_is_local'], bool)
    # Whatever it found, the returned dict carries a bare host at most.
    assert '://' not in str(report.get('dotenv_host', ''))


# ── the actual failure path from F19, end to end ────────────────────────────

PROBE = textwrap.dedent('''
    import os, json
    from utils.offline_diagnostics import use_offline_database
    local = use_offline_database()
    from dotenv import load_dotenv
    load_dotenv()                       # exactly what app.py:79 does
    survived = os.environ.get("DATABASE_URL")
    from app import create_app          # the import that caused the accident
    app = create_app()
    print("RESULT" + json.dumps({
        "configured_local": local.startswith("sqlite:///"),
        "survived_dotenv": survived == local,
        "app_db_local": app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite:///"),
        "app_db_is_production": "neon" in
            app.config["SQLALCHEMY_DATABASE_URI"].lower(),
    }))
''')


def test_app_import_after_the_boundary_never_targets_a_remote_database():
    """The regression test for the F19 accident itself.

    Runs in a subprocess with a throwaway SQLite file. If the boundary stopped
    working, ``create_app()`` would inherit .env's production DATABASE_URL —
    this assertion is what would catch it.
    """
    proc = subprocess.run([sys.executable, '-c', PROBE],
                          capture_output=True, text=True, timeout=600,
                          cwd=os.path.dirname(os.path.dirname(
                              os.path.abspath(__file__))))
    line = next((ln for ln in proc.stdout.splitlines()
                 if ln.startswith('RESULT')), None)
    assert line, 'probe produced no result:\n%s' % proc.stderr[-2000:]
    import json
    result = json.loads(line[len('RESULT'):])
    assert result['configured_local'] is True
    # load_dotenv() must NOT clobber the pre-set local URL.
    assert result['survived_dotenv'] is True
    assert result['app_db_local'] is True
    assert result['app_db_is_production'] is False


# ── the production guard must NOT be weakened ───────────────────────────────

def test_app_factory_still_invokes_the_schema_guard():
    """``create_app`` must call the guard at startup, unskipped.

    This replaces a weaker ``git diff --quiet HEAD -- app.py`` proxy, which
    was vacuous in CI: on a clean checkout the working tree always equals HEAD,
    so it could never fail there, and locally it failed for *any* later change
    to app.py rather than for a weakened guard.

    What it was actually reaching for is the property below, which is checked
    directly instead. The guard's own behaviour — raising
    ``SchemaMismatchError`` on drift — is covered behaviourally by
    ``tests/test_schema_guard.py``; this covers the part that lives in
    app.py, namely that startup is guarded at all.
    """
    import ast

    path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'app.py')
    with open(path, encoding='utf-8') as fh:
        tree = ast.parse(fh.read())

    create_app = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == 'create_app'),
        None)
    assert create_app is not None, 'app.py no longer defines create_app'

    calls = [
        node for node in ast.walk(create_app)
        if isinstance(node, ast.Call)
        and ((isinstance(node.func, ast.Name)
              and node.func.id == 'ensure_schema_compatible')
             or (isinstance(node.func, ast.Attribute)
                 and node.func.attr == 'ensure_schema_compatible'))
    ]
    assert len(calls) == 1, (
        'create_app must invoke ensure_schema_compatible exactly once; found '
        '%d call(s)' % len(calls))
    # The single positional argument must be the app being built, and there
    # must be no keyword arguments: `skip=True` or any other opt-out would
    # defeat the guard, and the guard accepts no such parameter, so an extra
    # argument can only be an attempt to bypass it.
    (argument,) = calls[0].args
    assert isinstance(argument, ast.Name) and argument.id == 'app', (
        'the guard must be called as ensure_schema_compatible(app), got %r'
        % (ast.dump(argument),))
    assert not calls[0].keywords, (
        'the startup guard must take no keyword arguments, not %r'
        % ([k.arg for k in calls[0].keywords],))


def test_skip_schema_guard_still_requires_an_explicit_value():
    """The escape hatch is honoured ONLY for the exact explicit value '1'."""
    import ast
    import inspect
    from utils import schema_guard
    src = inspect.getsource(schema_guard.ensure_schema_compatible)
    # The repo ships `os.getenv("SKIP_SCHEMA_GUARD") == "1"` — an EXACT
    # explicit value, not a truthy test.
    assert 'SKIP_SCHEMA_GUARD' in src
    assert '== "1"' in src or "== '1'" in src, \
        'the guard no longer requires an explicit "1"'
    ast.parse(src)


def test_offline_helper_is_the_only_new_writer_of_the_escape_hatch():
    """F20 does not widen who can set SKIP_SCHEMA_GUARD for app startup."""
    import inspect
    src = inspect.getsource(od)
    # Written exactly once, explicitly, with the exact value '1'.
    assert src.count("SKIP_SCHEMA_GUARD'] = '1'") == 1


# ── safe diagnostic scripts stay app-free ───────────────────────────────────

def test_readonly_production_script_still_avoids_importing_the_app():
    path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))),
        'scripts', 'check_production_schema_readonly.py')
    import ast
    with open(path, encoding='utf-8') as fh:
        source = fh.read()
    # Inspect real IMPORT NODES, not the prose: the file's own docstring
    # explains that it never imports `app`, so a substring test would match
    # that explanation rather than an actual import.
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split('.')[0])
    # No real import of the application (and therefore of its module-scope
    # create_app()) anywhere in the safe production diagnostic.
    assert 'app' not in imported, imported


# ── no network ──────────────────────────────────────────────────────────────

def test_classification_opens_no_socket(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError('classification must not touch the network')
    monkeypatch.setattr(socket, 'socket', _boom)
    monkeypatch.setattr(socket, 'create_connection', _boom)
    assert od.is_local_database_url('postgresql://u:p@db.example.com/db') \
        is False
    assert od.is_local_database_url('sqlite:///tmp/a.db') is True
    assert isinstance(od.describe_ambient_target(), dict)