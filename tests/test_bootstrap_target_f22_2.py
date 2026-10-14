"""F22.2 — the development schema bootstrap must fail closed.

Background
----------
``scripts/bootstrap_dev_schema.py`` decided whether a target was safe with a
private heuristic: allow anything starting with ``sqlite:``, refuse anything
containing a known managed-database hostname, and otherwise treat the target as
local unless it contained ``@`` or ``://``.

That last clause was the defect. A string containing neither character fell
through to "not production", so ``garbage``, ``not-a-url`` and ``"   "`` were
classified **safe**. The script then imported ``app``, built an engine and
attempted to connect before anything objected — the refusal depended on
SQLAlchemy failing to parse a URL, rather than on the tool deciding.

These tests pin the corrected contract:

* every ambiguous or malformed target is refused **before** the application is
  imported, so no engine, session or connection can exist;
* SQLite development targets still work;
* loopback PostgreSQL is REFUSED, even though the shared classifier calls it
  local: the deployment publishes Postgres on ``127.0.0.1:5432`` on the
  application host, so loopback there is the production database — and
  ``.env.example`` documents a loopback URL as the local-dev shape, which is
  exactly the string an operator is likely to have on hand;
* the refusal message carries no credentials.

Nothing here connects to a database. ``tests/conftest.py`` forces a temp-file
SQLite before any application import, and the subprocess cases use an RFC 2606
reserved ``.invalid`` hostname plus a tripwire that aborts the child if it
imports ``app``/``models``/``extensions`` or resolves a hostname.
"""
import ast
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
BOOTSTRAP = ROOT / 'scripts' / 'bootstrap_dev_schema.py'

sys.path.insert(0, str(ROOT))

from utils.db_target import (  # noqa: E402
    UnsafeTargetError, classify, require_writable_target,
)

# RFC 2606 reserves .invalid, so this host can never resolve.
REMOTE = 'postgresql://user:notarealpassword@db.invalid.example:5432/proddb'


def _load_bootstrap():
    """Import the module without running ``main()``.

    Importing it is safe by construction: it pulls in only argparse, os, sys and
    utils.db_target, none of which build an engine or touch a database. The
    module-level import of the application happens *inside* ``main()``.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location('bootstrap_dev_schema', BOOTSTRAP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bootstrap = _load_bootstrap()


# ── 0. the safety boundary this file relies on ──────────────────────────────


def test_database_url_is_provably_local_before_any_test_runs():
    """conftest.py must have forced a temp SQLite before we got here."""
    import os
    url = os.environ.get('DATABASE_URL', '')
    assert url.startswith('sqlite://'), (
        'refusing to run: DATABASE_URL is not provably local SQLite')


def test_importing_the_bootstrap_does_not_import_the_application():
    """The module must be inert on import; only main() may reach the app."""
    out = subprocess.run(
        [sys.executable, '-c',
         'import scripts.bootstrap_dev_schema as b;'
         'import sys;'
         'print("APP" if "app" in sys.modules else "PURE")'],
        cwd=str(ROOT), capture_output=True, text=True, timeout=120,
        env={'PATH': '/usr/bin:/bin', 'HOME': '/tmp',
             'DATABASE_URL': 'sqlite:///:memory:'})
    assert out.returncode == 0, out.stdout + out.stderr
    assert 'PURE' in out.stdout, out.stdout


# ── 1. the target matrix ───────────────────────────────────────────────────
#
# Each row is (url, should_refuse) and describes the BOOTSTRAP's own verdict,
# which is what actually gates the write. It is deliberately NOT the same as
# db_target's verdict for loopback PostgreSQL: see the dedicated test below.
#
# The comment on each row records why, so a future change that flips a row has
# to confront its reasoning.

BOOTSTRAP_MATRIX = [
    # A file-backed SQLite path is the canonical local/test target, and the
    # only form the dev workflow and every existing test uses.
    ('sqlite:///dev.db', False),
    ('sqlite:////tmp/absolute.db', False),
    ('sqlite:///:memory:', False),
    # Loopback PostgreSQL. db_target calls these local, and this tool still
    # refuses them: the deployment publishes Postgres on 127.0.0.1:5432 on the
    # application host, so on a VPS loopback IS the production database. These
    # are the exact shapes .env.example documents as the local-dev URL.
    ('postgresql://user:pw@localhost:5432/frameiq', True),
    ('postgresql://user:pw@127.0.0.1:5432/frameiq', True),
    ('postgresql://user:pw@[::1]:5432/frameiq', True),
    # Remote targets, including the compose service name and managed hosts.
    (REMOTE, True),
    ('postgresql://user:pw@db.example.com:5432/db', True),
    ('postgresql://user:pw@ep-x.neon.tech/db', True),
    ('postgresql://user:pw@db:5432/frameiq', True),
    # Ambiguous or malformed: the whole point of the fix.
    ('', True),
    ('   ', True),            # whitespace-only is not "unset", but it is unusable
    ('garbage', True),        # the pinned regression
    ('not-a-url', True),
    ('://', True),
    ('mysql://user:pw@host/db', True),      # unsupported scheme
    ('postgresql://user:pw@/db', True),     # malformed host
    ('postgresql:///db', True),             # missing host
]


def _bootstrap_refuses(url, monkeypatch=None):
    """Run the bootstrap's own decision hermetically.

    ``DATABASE_URL`` is cleared because ``require_writable_target`` falls back
    to the environment whenever the explicit value is empty. Without this, the
    '' row would resolve to the test harness's SQLite file and pass for the
    wrong reason.
    """
    if monkeypatch is not None:
        monkeypatch.setenv('DATABASE_URL', '')
    try:
        bootstrap._require_development_target(url)
    except UnsafeTargetError:
        return True
    return False


@pytest.mark.parametrize('url,should_refuse', BOOTSTRAP_MATRIX)
def test_bootstrap_target_matrix(url, should_refuse, monkeypatch):
    """The bootstrap's own decision, through the code path it actually uses."""
    assert _bootstrap_refuses(url, monkeypatch) is should_refuse, (
        'bootstrap %r for %r' % ('refused' if should_refuse else 'ALLOWED', url))


@pytest.mark.parametrize('url', [
    'postgresql://user:pw@localhost:5432/frameiq',
    'postgresql://user:pw@127.0.0.1:5432/frameiq',
    'postgresql://user:pw@[::1]:5432/frameiq',
])
def test_loopback_postgres_is_refused_even_though_db_target_calls_it_local(url):
    """The narrowing is real, not an accident of the shared classifier.

    This is the assertion that would fail if someone reduced the bootstrap to a
    bare ``require_writable_target`` call: db_target alone would ALLOW all
    three, because loopback is on its local-host list.
    """
    assert classify(url).is_local is True, (
        'precondition: db_target really does consider this loopback')
    assert classify(url).is_production_like is False
    assert _bootstrap_refuses(url) is True, (
        'the bootstrap must narrow the shared verdict for %r' % url)


def test_the_shared_guard_alone_would_have_allowed_loopback():
    """Documents precisely why the extra layer exists.

    Invoked directly, the shared guard permits loopback. That is correct for the
    migration runner and wrong for this tool, so the bootstrap adds its own
    narrowing rather than inheriting this verdict. If db_target ever stops
    treating loopback as local, this test fails — the signal to reconsider
    whether the narrowing is still load-bearing.
    """
    target = require_writable_target(
        'probe', explicit_url='postgresql://user:pw@localhost:5432/frameiq',
        allow_production=False)
    assert target.is_local is True
    assert target.is_production_like is False


def test_the_pinned_regression_garbage_is_not_treated_as_local():
    """Before the fix this returned False, i.e. 'safe to write to'."""
    assert bootstrap._looks_production('garbage') is True
    assert classify('garbage').is_production_like is True


def test_whitespace_only_was_also_fail_open_and_is_now_refused():
    """A second instance of the same defect, found while building the matrix."""
    assert classify('   ').is_production_like is True
    assert _bootstrap_refuses('   ') is True


@pytest.mark.parametrize('url', ['garbage', 'not-a-url', '   ', '', '://'])
def test_the_authoritative_guard_refuses_every_malformed_target(url, monkeypatch):
    """Same inputs, through the guard the bootstrap now delegates to.

    ``DATABASE_URL`` is cleared as well, because ``resolve_url`` falls back to
    the environment when the explicit value is empty — and the test environment
    points at a local SQLite file. Without this the '' case would silently pass.
    """
    monkeypatch.setenv('DATABASE_URL', '')
    with pytest.raises(UnsafeTargetError):
        require_writable_target('the development schema bootstrap',
                                explicit_url=url or None,
                                allow_production=False)


@pytest.mark.parametrize('url', ['garbage', 'not-a-url', '   ', '://'])
def test_authorization_is_not_reachable_by_environment(url, monkeypatch):
    """No environment variable may turn a malformed target into a safe one."""
    monkeypatch.setenv('DATABASE_URL', '')
    monkeypatch.setenv('FRAMEIQ_ALLOW_PRODUCTION', '1')
    monkeypatch.setenv('ALLOW_PRODUCTION', '1')
    monkeypatch.setenv('SKIP_SCHEMA_GUARD', '1')
    with pytest.raises(UnsafeTargetError):
        require_writable_target('the development schema bootstrap',
                                explicit_url=url or None,
                                allow_production=False)


def test_no_new_environment_escape_hatch_was_introduced():
    """The script must not read any authorization variable of its own."""
    source = BOOTSTRAP.read_text(encoding='utf-8')
    for escape in ('FRAMEIQ_ALLOW_PRODUCTION', 'ALLOW_PRODUCTION',
                   'getenv("ALLOW', "getenv('ALLOW"):
        assert escape not in source, 'new env escape hatch: %r' % escape
    # Authorization comes from the argument the shared guard already accepts.
    assert 'allow_production=args.allow_production' in source


def test_the_script_does_not_reimplement_hostname_classification():
    """No second hostname heuristic: the old denylist must be gone."""
    import re
    source = BOOTSTRAP.read_text(encoding='utf-8')
    body = re.sub(r'"""[\s\S]*?"""', '', source)
    for marker in ('neon.tech', 'neon.build', 'amazonaws.com', 'rds.amazonaws',
                   'supabase.co', 'cloudsql', '_LOCAL_HOSTS', 'localhost'):
        assert marker not in body, (
            'bootstrap re-implements classification with %r' % marker)


def test_looks_production_delegates_to_the_shared_classifier():
    """The retained helper must agree with the authoritative classifier."""
    for url, _should_refuse in BOOTSTRAP_MATRIX:
        assert bootstrap._looks_production(url) is classify(url or '').is_production_like


# ── 2. authorization precedes every side effect ────────────────────────────


def _module_level_statements(tree):
    """Top-level *executable* statements: no docstring, no function body.

    ``ast.dump`` of a FunctionDef includes everything nested inside it, so a
    naive scan of ``tree.body`` would flag the application import that lives
    inside ``main()`` — which is exactly where it is supposed to be.
    """
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue                       # the module docstring
        yield stmt


def test_authorization_call_precedes_the_application_import():
    """A check after ``from app import app`` would be too late to matter."""
    tree = ast.parse(BOOTSTRAP.read_text(encoding='utf-8'))
    gate = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == '_require_development_target':
            gate = node.lineno
    assert gate is not None, 'the bootstrap no longer authorizes its target'

    # Nothing at module scope may reach the application or a database.
    for stmt in _module_level_statements(tree):
        dump = ast.dump(stmt)
        assert "name='app'" not in dump, \
            'module-scope application import on line %d' % stmt.lineno
        assert "module='models'" not in dump, \
            'module-scope models import on line %d' % stmt.lineno
        assert 'create_engine' not in dump, \
            'module-scope engine on line %d' % stmt.lineno

    # Inside main(), the app import must follow the authorization call.
    main_fn = next(n for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == 'main')
    app_import = None
    for stmt in main_fn.body:
        if isinstance(stmt, ast.ImportFrom) and (stmt.module or '') == 'app':
            app_import = stmt.lineno
    assert app_import is not None
    assert gate < app_import, (
        'authorizes on line %d but imports app on line %d' % (gate, app_import))


def test_schema_guard_bypass_is_set_after_authorization():
    """SKIP_SCHEMA_GUARD is a parity bypass, never a write authorization.

    It must not be applied before the target is authorized, and it must not be
    present at module scope where it would leak into an unrelated import.
    """
    tree = ast.parse(BOOTSTRAP.read_text(encoding='utf-8'))

    # The only executable statement outside main() may set it.
    for stmt in _module_level_statements(tree):
        assert 'SKIP_SCHEMA_GUARD' not in ast.dump(stmt), (
            'schema-guard bypass at module scope, line %d' % stmt.lineno)

    # Inside main() it must come after the authorization call.
    main_fn = next(n for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == 'main')
    gate = min(n.lineno for n in ast.walk(main_fn)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id == '_require_development_target')
    bypass = [n.lineno for n in ast.walk(main_fn)
              if isinstance(n, ast.Call)
              and 'SKIP_SCHEMA_GUARD' in ast.dump(n)]
    assert bypass, 'the schema-guard bypass disappeared entirely'
    assert all(line > gate for line in bypass), (
        'SKIP_SCHEMA_GUARD is set at %r, before authorization on line %d'
        % (bypass, gate))


def test_the_sqlite_narrowing_lives_inside_the_authorization_helper():
    """The SQLite restriction must be part of the gate, not of main().

    Keeping it in the helper is what makes it impossible for a caller to reach
    `app` with a loopback target by arranging the call differently.
    """
    tree = ast.parse(BOOTSTRAP.read_text(encoding='utf-8'))
    helper = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef)
                  and n.name == '_require_development_target')
    body = ast.dump(helper)
    assert 'is_sqlite' in body, 'the narrowing is not in the gate helper'
    # And main() must not re-implement it.
    main_fn = next(n for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == 'main')
    assert 'is_sqlite' not in ast.dump(main_fn), \
        'main() duplicates the SQLite restriction'


TRIPWIRE = '''
import builtins, socket

class Tripwire(BaseException):
    pass

def _boom(what):
    raise Tripwire('TRIPWIRE: %s happened before authorization' % what)

socket.socket.connect = lambda self, addr: _boom('socket.connect')
socket.socket.connect_ex = lambda self, addr: _boom('socket.connect_ex')
socket.create_connection = lambda *a, **k: _boom('socket.create_connection')
socket.getaddrinfo = lambda *a, **k: _boom('DNS getaddrinfo')

_real = builtins.__import__
def _import(name, *a, **k):
    if name.split('.')[0] in ('app', 'models', 'extensions'):
        _boom('import %s' % name)
    return _real(name, *a, **k)
builtins.__import__ = _import
'''


def _child_env(tmp_path, database_url, tripwire=True):
    """A synthetic environment that cannot reach anything real.

    Built from scratch rather than inherited, so a developer's real ``.env``
    cannot load and no ambient DATABASE_URL can leak in.
    """
    if tripwire:
        (tmp_path / 'sitecustomize.py').write_text(TRIPWIRE, encoding='utf-8')
    return {
        'PATH': '/usr/bin:/bin',
        'PYTHONPATH': str(tmp_path),
        'HOME': str(tmp_path),
        'DATABASE_URL': database_url,
        'SKIP_SCHEMA_GUARD': '',
    }


def _run(tmp_path, database_url, args=(), tripwire=True):
    return subprocess.run(
        [sys.executable, str(BOOTSTRAP), *args],
        cwd=str(ROOT), env=_child_env(tmp_path, database_url, tripwire),
        capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize('url', ['garbage', 'not-a-url', '   ', '://'])
def test_malformed_target_is_refused_before_any_side_effect(url, tmp_path):
    """The regression, proven end to end with the tripwire armed."""
    proc = _run(tmp_path, url)
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, 'malformed target %r was accepted:\n%s' % (url, output)
    assert 'REFUSING' in output, output
    assert 'TRIPWIRE' not in output, (
        'the bootstrap reached the application or a socket before refusing '
        '%r:\n%s' % (url, output))


def test_malformed_via_flag_is_also_refused(tmp_path):
    """--database-url takes the same path as the environment variable."""
    proc = _run(tmp_path, 'sqlite:///:memory:', args=['--database-url', 'garbage'])
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert 'REFUSING' in output, output
    assert 'TRIPWIRE' not in output, output


def test_remote_target_is_refused_before_any_side_effect(tmp_path):
    proc = _run(tmp_path, REMOTE)
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert 'REFUSING' in output, output
    assert 'TRIPWIRE' not in output, output


def test_the_tripwire_is_actually_armed(tmp_path):
    """Negative control.

    Without this, 'TRIPWIRE not in output' would be vacuous: a sentinel that
    failed to load would also produce no tripwire. Here an *authorized*
    invocation must trip the wire, proving the instrument works.
    """
    proc = _run(tmp_path, 'sqlite:///%s' % (tmp_path / 'probe.db'))
    output = proc.stdout + proc.stderr
    assert 'TRIPWIRE' in output, (
        'the tripwire did not fire on an authorized run, so its silence '
        'elsewhere proves nothing:\n%s' % output)


def test_refusal_message_carries_no_credentials(tmp_path):
    proc = _run(tmp_path, REMOTE)
    output = proc.stdout + proc.stderr
    assert 'notarealpassword' not in output, 'password leaked into the refusal'
    assert 'sslmode' not in output, 'query parameters leaked'
    assert 'user:' not in output, 'credentials leaked'
    # The operator still needs to know WHICH target was refused.
    assert 'db.invalid.example' in output


def test_authorized_target_is_printed_without_credentials(tmp_path):
    """The pre-write banner must be safe to paste into an issue."""
    proc = _run(tmp_path, REMOTE, args=['--allow-production'], tripwire=False)
    output = proc.stdout + proc.stderr
    assert 'notarealpassword' not in output, 'password leaked into the banner'
    assert 'db.invalid.example' in output, 'operator cannot tell which target'


# ── 3. compatibility with the legitimate development paths ─────────────────


def test_local_sqlite_is_still_accepted(tmp_path):
    """The dev workflow must keep working; the tripwire is off so the run can
    complete against a throwaway file inside tmp_path."""
    proc = _run(tmp_path, 'sqlite:///%s' % (tmp_path / 'dev.db'),
                tripwire=False)
    output = proc.stdout + proc.stderr
    assert 'REFUSING' not in output, 'the gate refused a local target:\n%s' % output
    assert 'Bootstrap schema' in output, output


def test_loopback_postgres_is_refused_without_a_server(tmp_path):
    """Refused on the URL alone; no PostgreSQL is needed to prove it.

    This matters because the published loopback port means loopback may be the
    production database. The refusal must therefore not depend on connectivity
    failing — it has to happen from the URL, before anything is opened.
    """
    for url in ('postgresql://user:pw@localhost:5432/frameiq',
                'postgresql://user:pw@127.0.0.1:5432/frameiq',
                'postgresql://user:pw@[::1]:5432/frameiq'):
        assert _bootstrap_refuses(url) is True, url


@pytest.mark.parametrize('url', [
    'postgresql://user:pw@localhost:5432/frameiq',
    'postgresql://user:pw@127.0.0.1:5432/frameiq',
    'postgresql://user:pw@[::1]:5432/frameiq',
])
def test_loopback_cli_refuses_before_any_side_effect(url, tmp_path):
    """End-to-end: the refusal happens before the application is imported.

    The tripwire is armed, so 'TRIPWIRE not in output' means the script reached
    neither `app` nor a socket before refusing.
    """
    proc = _run(tmp_path, url)
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, 'loopback target %r was accepted:\n%s' % (url, output)
    assert 'REFUSING' in output, output
    assert 'TRIPWIRE' not in output, (
        'the bootstrap reached the application or a socket before refusing '
        '%r:\n%s' % (url, output))


def test_loopback_refusal_explains_why_and_leaks_nothing(tmp_path):
    """The operator must learn the reason, without seeing the password."""
    proc = _run(tmp_path, 'postgresql://user:supersecret@localhost:5432/frameiq')
    output = proc.stdout + proc.stderr
    assert 'REFUSING' in output
    assert 'supersecret' not in output, 'password leaked into the refusal'
    # The message must justify itself, otherwise an operator's first reaction
    # is to reach for --allow-production.
    assert '127.0.0.1:5432' in output, output
    assert 'sqlite:///dev.db' in output, 'no SQLite alternative offered'


# ── 3b. the narrowed --allow-production override ───────────────────────────
#
# The override used to short-circuit the whole authorization, so it permitted
# malformed URLs and loopback PostgreSQL as well. Both are now refused
# unconditionally: a flag cannot resolve a URL that names no database, nor a
# loopback address whose meaning depends on which machine reads it.


LOOPBACK_FORMS = [
    'postgresql://user:pw@localhost:5432/frameiq',
    'postgresql://user:pw@127.0.0.1:5432/frameiq',
    'postgresql://user:pw@[::1]:5432/frameiq',
]


def _bootstrap_refuses_with_override(url, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setenv('DATABASE_URL', '')
    try:
        bootstrap._require_development_target(url, allow_production=True)
    except UnsafeTargetError:
        return True
    return False


@pytest.mark.parametrize('url', LOOPBACK_FORMS)
def test_loopback_is_refused_even_with_allow_production(url, monkeypatch):
    """The regression this follow-up exists to close.

    Before it, `--allow-production` short-circuited the SQLite narrowing, so
    all three loopback forms were authorized — which on the application host
    is the production database.
    """
    assert _bootstrap_refuses_with_override(url, monkeypatch) is True, (
        '--allow-production must not authorize loopback %r' % url)


@pytest.mark.parametrize('url', LOOPBACK_FORMS)
def test_loopback_cli_is_refused_even_with_allow_production(url, tmp_path):
    """End-to-end through the CLI, with the tripwire armed."""
    proc = _run(tmp_path, url, args=['--allow-production'])
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, (
        '--allow-production authorized loopback %r:\n%s' % (url, output))
    assert 'REFUSING' in output, output
    assert 'TRIPWIRE' not in output, (
        'the application or a socket was reached before refusing %r:\n%s'
        % (url, output))


@pytest.mark.parametrize('url', [
    'garbage', 'not-a-url', '   ', '', '://',
    'postgresql://user:pw@/db',            # no host
    'postgresql://user:pw@host.example.com',   # no database
])
def test_unusable_urls_stay_refused_even_with_allow_production(url, monkeypatch):
    """A flag cannot authorise a URL that does not name a database.

    Previously `garbage` was ALLOWED under `--allow-production`, because the
    early return skipped every local check.
    """
    assert _bootstrap_refuses_with_override(url, monkeypatch) is True, (
        '--allow-production must not authorize the unusable target %r' % url)


def test_the_override_still_permits_a_genuinely_remote_database():
    """Preserved: this is the documented, deliberate opt-in.

    Remote is unambiguous — it cannot be a laptop's scratch server — so the flag
    means here exactly what it means in scripts/migrate.py.
    """
    target = bootstrap._require_development_target(
        'postgresql://user:pw@db.example.com:5432/db', allow_production=True)
    assert target.is_production_like is True
    assert target.is_local is False


def test_the_override_does_not_change_the_default_path(monkeypatch):
    """Narrowing the override must not have widened or narrowed the default."""
    for url, expected in BOOTSTRAP_MATRIX:
        assert _bootstrap_refuses(url, monkeypatch) is expected, url


def test_the_makefile_target_absent_is_not_a_regression():
    """`make dev-schema` is referenced in a docstring but has never existed.

    Pinned so it is a known, tracked documentation gap rather than something a
    future reader has to rediscover.
    """
    makefile = ROOT / 'Makefile'
    text = makefile.read_text(encoding='utf-8') if makefile.exists() else ''
    assert 'dev-schema' not in text, (
        'a dev-schema target now exists; update the script docstring, which '
        'still claims the two are equivalent')


# ── 4. regressions this change must not have caused ────────────────────────


def test_the_shared_framework_still_refuses_a_remote_target():
    """The durable invariant this section was reaching for, asserted behaviourally.

    This replaces a diff-emptiness check that asserted *F22.2's own change* had
    not touched ``utils/db_target.py``, ``scripts/migrate.py``,
    ``migrates/registry.py`` or any registered migration. That was a task-scope
    guard, not an invariant: it read ``git diff --name-only HEAD``, so it fired
    against any later, legitimate change to the same files — F32 registering
    ``0005_media_runtime`` tripped it even though no shared behaviour had
    changed. A test that is red whenever the next feature lands protects
    nothing.

    What actually protects the framework is behaviour, asserted here and
    throughout the rest of this file.
    """
    from utils.db_target import require_writable_target

    remote = 'postgresql://user:pw@db.example.com:5432/db'
    with pytest.raises(UnsafeTargetError):
        require_writable_target('the migration runner', explicit_url=remote)

    # ...and the opt-in stays explicit: nothing but this argument opens a
    # remote write.
    assert require_writable_target(
        'the migration runner', explicit_url=remote,
        allow_production=True).is_production_like is True


def test_the_runner_still_classifies_its_target_before_writing(monkeypatch):
    """``scripts/migrate.py`` must not have gained an ungated write path.

    0005 (F32) added a registered migration, which is exactly the kind of change
    that could plausibly have introduced a shortcut. This pins the runner's
    actual behaviour rather than the emptiness of any one diff.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        'migrate_under_test', ROOT / 'scripts' / 'migrate.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setenv('DATABASE_URL',
                       'postgresql://user:pw@db.example.com:5432/db')
    # Every write command builds its engine through _engine_for, so a remote
    # target is refused before a connection is ever opened.
    with pytest.raises(UnsafeTargetError):
        module._engine_for(allow_production=False)

    engine, target = module._engine_for(allow_production=True)
    engine.dispose()
    assert target.is_production_like is True


def test_the_shared_guard_still_refuses_a_remote_target(monkeypatch):
    """The bootstrap now delegates here, so this must remain sound.

    The environment is overridden because the legacy guard reads
    ``DATABASE_URL`` from ``os.environ`` rather than taking an explicit URL;
    under pytest that points at the local SQLite harness database.
    """
    from utils.legacy_migration_guard import authorize_legacy_migration
    monkeypatch.setenv('DATABASE_URL', REMOTE)
    with pytest.raises(UnsafeTargetError):
        authorize_legacy_migration('/x/migrate_probe.py', argv=[],
                                   echo=lambda m: None)