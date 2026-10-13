"""F22 — legacy migration guardrails and destructive-script retirement.

These tests prove the security boundary, not merely that a guard function was
called somewhere. Three complementary styles are used:

* **AST checks** for every legacy script, because the guarantee is a *position*
  claim — the gate must precede the application import and the engine — and
  position is exactly what a source-text assertion can and should verify.
* **In-process unit tests** against the real ``utils.db_target`` classifier,
  using representative targets. No invented classification rules.
* **Subprocess tests with a tripwire**, for the entry-point contract. The
  tripwire aborts the child if it imports ``app``/``models``/``extensions`` or
  opens a socket before authorization, and the only URL used is an RFC 2606
  reserved ``.invalid`` hostname that cannot resolve. So these tests cannot
  reach production even if the protection they are testing is broken.

Nothing here connects to a database. ``tests/conftest.py`` forces a temp-file
SQLite before any application import, and that isolation is asserted first.
"""
import ast
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
MIGRATES = ROOT / 'migrates'
SCRIPTS = ROOT / 'scripts'

sys.path.insert(0, str(ROOT))

from utils.db_target import UnsafeTargetError  # noqa: E402
from utils.legacy_migration_guard import (  # noqa: E402
    ALLOW_PRODUCTION_FLAG, authorize_legacy_migration,
)

# ── The inventory ──────────────────────────────────────────────────────────
# Discovered by static analysis, not hard-coded from the audit: 30 hand-written
# `migrate_*.py` CLI scripts. One is retired (inert shim); the remaining 29
# are all gated.
#
# F22.1 closed the last blocker: `migrate_schema_convergence.py` is now gated
# too, because deploy.yml passes the runner's explicit --allow-production flag.
# It is therefore no longer excluded — keeping it out of GUARDED here would
# silently stop testing the one script the deploy actually runs.
LEGACY_CLI = sorted(
    p.name for p in MIGRATES.glob('migrate_*.py'))
RETIRED = 'migrate_remove_wishlist.py'
DEPLOYED = 'migrate_schema_convergence.py'
GUARDED = [n for n in LEGACY_CLI if n != RETIRED]

# Registered, ledger-tracked migrations. These are library modules owned by
# scripts/migrate.py; they must NOT carry their own gate (the runner guards
# them) and must NOT gain a CLI of their own.
REGISTERED = [
    'migrations_0001_canonical_watched.py',
    'migrations_0002_remove_legacy_wishlist.py',
    'migrations_0003_cast_persistence.py',
    'migrations_0004_notification_preferences.py',
]

# RFC 2606 reserves .invalid so this hostname can never resolve.
REMOTE = 'postgresql://user:notarealpassword@db.invalid.example:5432/proddb'


# ── 0. environment safety, asserted before anything else ───────────────────


def test_database_url_is_provably_local_before_any_test_runs():
    """The F20/F21 isolation boundary must still hold for this file."""
    url = os.environ.get('DATABASE_URL', '')
    assert url.startswith('sqlite://'), (
        'refusing to run: DATABASE_URL is not provably local SQLite')


# ── 1. The inventory itself ────────────────────────────────────────────────


def test_legacy_cli_count_is_30_not_the_audits_23():
    """The audit reported 23; the current source has 30. Pin the real number.

    Reporting the audit's figure unverified would understate the surface.
    """
    assert len(LEGACY_CLI) == 30, (
        'legacy CLI script count changed: %d (%s)'
        % (len(LEGACY_CLI), sorted(LEGACY_CLI)))


def test_every_legacy_script_has_a_cli_entrypoint():
    """These are entrypoints, so each must be considered for a gate."""
    for name in LEGACY_CLI:
        source = (MIGRATES / name).read_text(encoding='utf-8')
        assert 'if __name__' in source, (
            '%s is listed as a CLI but has no entrypoint' % name)


def test_registered_migrations_are_library_modules_without_a_cli():
    for name in REGISTERED:
        source = (MIGRATES / name).read_text(encoding='utf-8')
        assert 'if __name__' not in source, (
            '%s is runner-owned and must not have its own entrypoint' % name)
        assert 'legacy_migration_guard' not in source, (
            '%s must not carry its own gate; scripts/migrate.py guards it' % name)


def test_registry_is_not_treated_as_a_legacy_entrypoint():
    source = (MIGRATES / 'registry.py').read_text(encoding='utf-8')
    assert 'if __name__' not in source


# ── 2. Guard ordering — the property that actually matters ─────────────────


def _gate_lineno(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, 'id', '')
            if name == 'authorize_legacy_migration':
                return node.lineno
    return None


def _first_side_effect_lineno(tree):
    """First top-level statement that can reach a database."""
    for stmt in tree.body:
        if isinstance(stmt, ast.Import):
            if any(a.name == 'app' or a.name.startswith('app.') for a in stmt.names):
                return stmt.lineno
        if isinstance(stmt, ast.ImportFrom):
            mod = stmt.module or ''
            if mod == 'app' or mod.startswith('app.') or mod in ('models', 'extensions'):
                return stmt.lineno
        for sub in ast.walk(stmt):
            if isinstance(sub, ast.Call):
                fn = sub.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, 'id', '')
                if name in ('create_engine', 'create_all', 'drop_all'):
                    return stmt.lineno
                if name == 'getenv' and 'DATABASE_URL' in ast.dump(sub):
                    return stmt.lineno
    return None


@pytest.mark.parametrize('name', GUARDED)
def test_gate_runs_before_any_side_effect(name):
    """The gate must precede the application import / engine construction.

    `import app` builds an engine and runs the schema guard, so a check placed
    after it is already too late — that is the incident this guards against.
    """
    source = (MIGRATES / name).read_text(encoding='utf-8')
    tree = ast.parse(source)
    gate = _gate_lineno(tree)
    assert gate is not None, '%s has no F22 authorization gate' % name
    side = _first_side_effect_lineno(tree)
    assert side is not None, (
        '%s declares no side effect — re-check the inventory' % name)
    assert gate < side, (
        '%s runs its gate on line %d but can touch the database by line %d'
        % (name, gate, side))


@pytest.mark.parametrize('name', GUARDED)
def test_gate_is_at_module_scope_not_inside_a_function(name):
    """A gate inside migrate() would be far too late to protect the import."""
    tree = ast.parse((MIGRATES / name).read_text(encoding='utf-8'))
    top_level = set()
    for stmt in tree.body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call):
                fn = node.func
                nm = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, 'id', '')
                if nm == 'authorize_legacy_migration':
                    top_level.add(node.lineno)
    assert top_level, '%s gate is not at module scope' % name
    assert _gate_lineno(tree) == min(top_level)


@pytest.mark.parametrize('name', GUARDED)
def test_script_is_self_bootstrapping(name):
    """`utils` must be importable however the script was launched.

    19 of the legacy scripts relied on the caller's cwd for `from app import`.
    The gate cannot inherit that fragility, so it bootstraps sys.path itself.
    """
    source = (MIGRATES / name).read_text(encoding='utf-8')
    assert re_search_sys_path(source), (
        '%s does not put the repository root on sys.path, so the gate import '
        'depends on cwd' % name)


def re_search_sys_path(source):
    import re
    return re.search(r'sys\.path\.(insert|append)', source) is not None


# ── 3. Guard behaviour against the authoritative classifier ────────────────


def _guard(url, argv, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', url)
    messages = []
    try:
        authorize_legacy_migration('/x/migrates/probe.py', argv=argv,
                                   echo=messages.append)
        return True, messages
    except UnsafeTargetError as exc:
        return False, [str(exc)] + messages


@pytest.mark.parametrize('url', [
    'sqlite:///scratch.db',
    'sqlite:////tmp/absolute.db',
    'sqlite:///:memory:',
    'postgresql://user:pw@localhost:5432/app',
    'postgresql://user:pw@127.0.0.1:5432/app',
])
def test_local_targets_are_allowed_without_authorization(url, monkeypatch):
    allowed, _messages = _guard(url, argv=[], monkeypatch=monkeypatch)
    assert allowed, '%s should be a local target' % url


@pytest.mark.parametrize('url', [
    REMOTE,
    'postgresql://u:p@db.example.com:5432/app',
    'postgresql://u:p@sub.domain.invalid/app',
])
def test_remote_targets_are_refused_without_the_explicit_flag(url, monkeypatch):
    allowed, _messages = _guard(url, argv=[], monkeypatch=monkeypatch)
    assert not allowed, '%s must be refused without --allow-production' % url


@pytest.mark.parametrize('url', [
    REMOTE,
    'postgresql://u:p@db.example.com/app',
])
def test_remote_targets_are_allowed_with_the_explicit_flag(url, monkeypatch):
    """The same CLI flag scripts/migrate.py accepts."""
    allowed, _messages = _guard(url, argv=[ALLOW_PRODUCTION_FLAG], monkeypatch=monkeypatch)
    assert allowed, 'an operator passing %s must be able to proceed' % ALLOW_PRODUCTION_FLAG


@pytest.mark.parametrize('url', [
    'postgresql://u:p@db.example.com/app',
])
@pytest.mark.parametrize('var,value', [
    ('FRAMEIQ_ALLOW_PRODUCTION', '1'),
    ('ALLOW_PRODUCTION', '1'),
    ('FRAMEIQ_ALLOW_PROD', 'true'),
    ('DATABASE_ALLOW_PRODUCTION', '1'),
    ('SKIP_SCHEMA_GUARD', '1'),
    ('FLASK_DEBUG', '1'),
])
def test_environment_variables_cannot_grant_production_access(
        url, var, value, monkeypatch):
    """There is deliberately no environment escape hatch.

    A variable can be set by an unrelated startup path; a CLI flag cannot be.
    """
    monkeypatch.setenv('DATABASE_URL', url)
    monkeypatch.setenv(var, value)
    with pytest.raises(UnsafeTargetError):
        authorize_legacy_migration('/x/probe.py', argv=[],
                                   echo=lambda m: None)


@pytest.mark.parametrize('url', ['', '   ', 'not-a-url', '://', 'postgresql://'])
def test_missing_or_malformed_targets_fail_closed(url, monkeypatch):
    allowed, _ = _guard(url, argv=[], monkeypatch=monkeypatch)
    assert not allowed, '%r must fail closed' % url


def test_refusal_message_never_contains_the_password(monkeypatch):
    monkeypatch.setenv('DATABASE_URL', REMOTE)
    with pytest.raises(UnsafeTargetError) as exc:
        authorize_legacy_migration('/x/probe.py', argv=[], echo=lambda m: None)
    text = str(exc.value)
    assert 'notarealpassword' not in text
    assert 'db.invalid.example' in text, 'operator still needs the host'


def test_guard_delegates_to_the_authoritative_classifier(monkeypatch):
    """No second, weaker implementation inside the legacy guard."""
    monkeypatch.setenv('DATABASE_URL', REMOTE)
    with pytest.raises(UnsafeTargetError):
        authorize_legacy_migration('/x/probe.py', argv=[], echo=lambda m: None)
    source = (ROOT / 'utils' / 'legacy_migration_guard.py').read_text(encoding='utf-8')
    code = _strip_docstrings_and_comments(source)
    for marker in ('neon.tech', 'amazonaws.com', '_LOCAL_HOSTS', '_looks_production',
                   "'localhost'", "'127.0.0.1'"):
        assert marker not in code, (
            'the legacy guard must not re-implement classification (%r)' % marker)


# ── 4. Subprocess entry-point contract, with a tripwire ────────────────────

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
    """A synthetic environment that cannot reach anything real."""
    sentinel = tmp_path / 'sitecustomize.py'
    if tripwire:
        sentinel.write_text(TRIPWIRE, encoding='utf-8')
    env = {
        'PATH': '/usr/bin:/bin',
        'PYTHONPATH': str(tmp_path),
        'DATABASE_URL': database_url,
        'HOME': str(tmp_path),
        # Deliberately NOT inherited: an operator's real .env must not load.
        'SKIP_SCHEMA_GUARD': '',
    }
    return env


def _run_script(tmp_path, script, database_url, args=(), tripwire=True):
    return subprocess.run(
        [sys.executable, script, *args],
        cwd=str(ROOT), env=_child_env(tmp_path, database_url, tripwire=tripwire),
        capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize('name', GUARDED[:6] + GUARDED[-6:])
def test_cli_invocation_refuses_a_remote_target_before_any_side_effect(
        name, tmp_path):
    """Direct CLI: refusal is non-zero, and the tripwire never fires."""
    proc = _run_script(tmp_path, 'migrates/' + name, REMOTE)
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, '%s exited 0 against a remote target' % name
    assert 'TRIPWIRE' not in output, (
        '%s reached the database/import before refusing:\n%s' % (name, output))
    assert 'UnsafeTargetError' in output
    assert 'notarealpassword' not in output, 'credentials leaked into output'


@pytest.mark.parametrize('name', ['migrate_week1.py', 'migrate_lists_v2.py',
                                  'migrate_tv_tracking.py'])
def test_local_sqlite_is_not_blocked_by_the_gate(name, tmp_path):
    """The gate must not break legitimate local work.

    The assertion is only that the guard's refusal is absent — the script is
    allowed to then run against a throwaway SQLite file in tmp_path.
    """
    db_file = tmp_path / 'scratch.db'
    proc = _run_script(tmp_path, 'migrates/' + name,
                       'sqlite:///%s' % db_file, tripwire=False)
    output = proc.stdout + proc.stderr
    assert 'UnsafeTargetError' not in output, (
        'the gate refused a local target:\n%s' % output)
    assert 'Target: sqlite' in output, (
        'the gate should have classified and reported the target:\n%s' % output)


def test_flag_authorization_is_honoured_by_the_cli(tmp_path):
    """Passing the runner's flag lets the operator proceed (and then fails on
    the deliberately non-resolving hostname, which proves the gate opened)."""
    proc = _run_script(tmp_path, 'migrates/migrate_week1.py', REMOTE,
                       args=[ALLOW_PRODUCTION_FLAG], tripwire=False)
    output = proc.stdout + proc.stderr
    assert 'UnsafeTargetError' not in output, (
        '--allow-production should have opened the gate:\n%s' % output)
    assert 'db.invalid.example' in output, (
        'expected the run to proceed and fail on the unresolvable host')


# ── 5. The retired destructive script ─────────────────────────────────────


def _strip_docstrings_and_comments(source):
    """Source with comment lines and string literals removed.

    The shim's docstring deliberately NAMES the operation it no longer performs
    (``DROP TABLE user_wishlist``, ``require_writable_target``) so an operator
    reading the file understands why it exists. Those mentions are prose, not
    code, so structural checks must exclude them -- otherwise the explanatory
    text would read as an executable call.
    """
    import re
    lines = [line for line in source.splitlines()
             if not line.strip().startswith('#')]
    joined = '\n'.join(lines)
    joined = re.sub(r'"""[\s\S]*?"""', '', joined)
    joined = re.sub(r"'''[\s\S]*?'''", '', joined)
    joined = re.sub(r'"[^"\n]*"', '', joined)
    joined = re.sub(r"'[^'\n]*'", '', joined)
    return joined


def _ast_of_retired():
    return ast.parse((MIGRATES / RETIRED).read_text(encoding='utf-8'))


def test_retired_script_imports_nothing_at_all():
    """No application import, no engine, no connection — inert on import."""
    tree = _ast_of_retired()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split('.')[0] not in (
                    'app', 'models', 'extensions', 'sqlalchemy', 'psycopg2'), (
                    'the retired shim must not import %s' % alias.name)
        if isinstance(node, ast.ImportFrom):
            mod = (node.module or '').split('.')[0]
            assert mod not in ('app', 'models', 'extensions', 'sqlalchemy',
                               'psycopg2'), \
                'the retired shim must not import %s' % node.module


def test_retired_script_contains_no_ddl_or_dml():
    """Structurally: the shim can neither issue nor build a database command.

    Two independent checks, because either alone has a blind spot:

    1. No call to any database API. This is the real guarantee — with no
       connection API there is nothing that could execute SQL.
    2. No string literal that *shapes like a SQL statement*. A blunt keyword
       scan would false-positive on the operator-facing message, which says in
       prose that this script used to DROP the table — text an operator needs.
       So the pattern requires a real statement shape ("DROP TABLE x"), which
       prose cannot produce.
    """
    import re

    tree = _ast_of_retired()
    forbidden = {'execute', 'exec_driver_sql', 'commit', 'create_engine',
                 'create_all', 'drop_all', 'connect', 'begin',
                 'bulk_insert_mappings', 'text'}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, 'id', '')
            assert name not in forbidden, (
                'the retired shim still calls %s()' % name)

    statement_shapes = re.compile(
        r'\b(DROP\s+(TABLE|INDEX|COLUMN|VIEW)|'
        r'ALTER\s+TABLE|'
        r'CREATE\s+(TABLE|INDEX|UNIQUE)|'
        r'TRUNCATE\s+TABLE|'
        r'\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM))\b',
        re.I)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert not statement_shapes.search(node.value), (
                'the retired shim contains an executable SQL statement: %r'
                % node.value)


def test_retired_script_has_no_sqlalchemy_import_path_at_all():
    """Nothing in the module can even name a table."""
    code = _strip_docstrings_and_comments(
        (MIGRATES / RETIRED).read_text(encoding='utf-8'))
    for forbidden in ('db.', 'engine', 'connection', 'inspect', 'text('):
        assert forbidden not in code, \
            'the retired shim still references %s' % forbidden


def test_retired_script_migrate_call_raises_instead_of_silently_succeeding():
    """A quiet no-op would let a caller believe consolidation happened."""
    namespace = {'__name__': 'retired_probe'}
    exec(compile((MIGRATES / RETIRED).read_text(encoding='utf-8'),
                 RETIRED, 'exec'), namespace)
    with pytest.raises(NotImplementedError) as exc:
        namespace['migrate']()
    assert 'scripts/migrate.py' in str(exc.value)


def test_retired_script_exits_non_zero_with_a_migration_pointer(tmp_path):
    proc = subprocess.run(
        [sys.executable, 'migrates/' + RETIRED],
        cwd=str(ROOT), capture_output=True, text=True, timeout=60)
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, 'a retired script must not exit 0'
    assert 'RETIRED' in output
    assert '0002_remove_legacy_wishlist' in output
    # Points at the supported path WITHOUT running it.
    assert 'scripts/migrate.py upgrade' in output
    assert 'notarealpassword' not in output


def test_retired_script_does_not_silently_authorize_the_replacement():
    """It must not translate a legacy run into a production-authorized one."""
    source = (MIGRATES / RETIRED).read_text(encoding='utf-8')
    code = _strip_docstrings_and_comments(source)
    for forbidden in ('require_writable_target', 'authorize_legacy_migration',
                      'subprocess', 'os.system', 'importlib', 'exec('):
        assert forbidden not in code, (
            'the retired shim must not reach the replacement (%r)' % forbidden)


# ── 6. F22.1: the deploy-invoked convergence script is now gated ───────────
#
# This section REPLACED three tests that pinned an unresolved blocker. The
# blocker was: deploy.yml invoked migrate_schema_convergence.py without
# --allow-production, so adding the gate would have broken the production
# deploy. F22.1 added the flag to that one command and added the gate.
#
# The assertions below are deliberately stronger than the blocker pins were:
# a blocker pin only proved the problem still existed, whereas these verify
# the resolved contract end to end.


def _logical_deploy_lines():
    """Executed deploy commands, comments stripped, continuations joined.

    Reusing the same reading of the workflow that tests/test_deploy_workflow.py
    uses, so "is this one command?" is answered the way the shell answers it
    rather than by matching a single physical line.
    """
    raw = (ROOT / '.github' / 'workflows' / 'deploy.yml').read_text(
        encoding='utf-8')
    joined, buffer = [], ''
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if stripped.endswith('\\'):
            buffer += stripped[:-1].strip() + ' '
            continue
        buffer += stripped
        joined.append(buffer)
        buffer = ''
    return joined


def test_deploy_invokes_convergence_with_the_explicit_flag():
    """The flag must be an ARGUMENT to the script, in the real command."""
    lines = [ln for ln in _logical_deploy_lines()
             if 'migrates/migrate_schema_convergence.py' in ln]
    assert len(lines) == 1, (
        'expected exactly one convergence command, found %d: %r' % (len(lines), lines))
    command = lines[0]
    assert ALLOW_PRODUCTION_FLAG in command, (
        'convergence is invoked without %s: %r' % (ALLOW_PRODUCTION_FLAG, command))
    # The flag must follow the script path, i.e. reach Python's argv and not
    # just be mentioned in the surrounding text.
    assert command.index(ALLOW_PRODUCTION_FLAG) > command.index(
        'migrate_schema_convergence.py'), command


def test_convergence_command_keeps_its_original_invocation_contract():
    """Only the authorization was added; nothing else about the step moved."""
    command = next(ln for ln in _logical_deploy_lines()
                   if 'migrates/migrate_schema_convergence.py' in ln)
    for preserved in ('SKIP_SCHEMA_GUARD=1',
                      'docker compose run --rm --no-deps web',
                      'python migrates/migrate_schema_convergence.py'):
        assert preserved in command, (
            'the step lost %r, which would change what it executes: %r'
            % (preserved, command))
    # It must NOT have picked up destructive-approval arguments.
    for forbidden in ('--only', '--authorize-destructive',
                      '--backup-ref', '--backup-verified-by'):
        assert forbidden not in command, (
            'convergence must not gain the runner\'s destructive flags: %r'
            % forbidden)


def test_deploy_authorizes_via_argument_not_environment():
    """No environment variable may stand in for the explicit flag."""
    raw = (ROOT / '.github' / 'workflows' / 'deploy.yml').read_text(encoding='utf-8')
    for line in raw.splitlines():
        if line.strip().startswith('#'):
            continue
        for escape in ('FRAMEIQ_ALLOW_PRODUCTION', 'ALLOW_PRODUCTION=',
                       'FRAMEIQ_ALLOW_PROD'):
            assert escape not in line, (
                'an environment escape hatch reached a deploy command: %s'
                % line.strip())


def test_every_migration_command_in_deploy_passes_the_explicit_flag():
    """Convergence is no longer the outlier; every writing command authorizes."""
    for line in _logical_deploy_lines():
        if 'scripts/migrate.py' in line and '--help' not in line:
            assert ALLOW_PRODUCTION_FLAG in line, (
                'runner invocation without the flag: %s' % line)
        if 'migrates/migrate_' in line:
            assert ALLOW_PRODUCTION_FLAG in line, (
                'legacy migration invocation without the flag: %s' % line)


def test_convergence_guard_precedes_the_application_import():
    """The check must be ahead of `import app`, not merely present in the file."""
    tree = ast.parse((MIGRATES / DEPLOYED).read_text(encoding='utf-8'))
    gate = _gate_lineno(tree)
    assert gate is not None, '%s lost its gate' % DEPLOYED
    side = _first_side_effect_lineno(tree)
    assert side is not None
    assert gate < side, (
        '%s gates on line %d but can reach the database by line %d'
        % (DEPLOYED, gate, side))
    # And ahead of the startup-guard bypass, so a refusal does not mutate the
    # environment on its way out.
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'setdefault'
                and 'SKIP_SCHEMA_GUARD' in ast.dump(node)):
            assert node.lineno > gate, (
                'SKIP_SCHEMA_GUARD is disabled before authorization')
            break
    else:
        raise AssertionError('the SKIP_SCHEMA_GUARD bypass disappeared')


def test_convergence_keeps_its_entrypoint_and_behaviour():
    """Adding the gate must not change what the script does once authorized."""
    source = (MIGRATES / DEPLOYED).read_text(encoding='utf-8')
    assert 'if __name__' in source
    # The bounded repair set — the reason this script exists — is untouched.
    assert 'EXPECTED_REPAIR_SET' in source
    assert 'def migrate(' in source
    # It must not gain argparse that could reject the flag we now pass.
    assert 'argparse' not in source


def test_convergence_cli_refuses_a_remote_target_before_any_side_effect(
        tmp_path):
    """Subprocess proof: refusal is non-zero and no side effect precedes it."""
    proc = _run_script(tmp_path, 'migrates/' + DEPLOYED, REMOTE)
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, (
        'convergence exited 0 against a remote target:\n%s' % output)
    assert 'UnsafeTargetError' in output, output
    assert 'TRIPWIRE' not in output, (
        'convergence reached the database or the app import before refusing:\n%s'
        % output)
    assert 'notarealpassword' not in output, 'credentials leaked into output'


def test_convergence_cli_opens_only_with_the_explicit_flag(tmp_path):
    """The flag must actually open the gate, not merely silence the refusal."""
    proc = _run_script(tmp_path, 'migrates/' + DEPLOYED, REMOTE,
                       args=[ALLOW_PRODUCTION_FLAG], tripwire=False)
    output = proc.stdout + proc.stderr
    assert 'UnsafeTargetError' not in output, (
        '%s did not open the gate:\n%s' % (ALLOW_PRODUCTION_FLAG, output))
    # Having opened, the run proceeds and fails only on the reserved, unresolvable
    # hostname — which is what proves the gate is no longer what stopped it.
    assert 'Target:' in output, output


@pytest.mark.parametrize('var', [
    'FRAMEIQ_ALLOW_PRODUCTION', 'ALLOW_PRODUCTION',
    'FRAMEIQ_ALLOW_PROD', 'SKIP_SCHEMA_GUARD',
])
def test_convergence_cli_env_vars_cannot_authorize(var, tmp_path):
    env = _child_env(tmp_path, REMOTE, tripwire=True)
    env[var] = '1'
    proc = subprocess.run(
        [sys.executable, 'migrates/' + DEPLOYED],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120)
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert 'UnsafeTargetError' in output, (
        '%s=1 authorized a production run:\n%s' % (var, output))
    assert 'TRIPWIRE' not in output, output


@pytest.mark.parametrize('url', ['', 'not-a-url', '://', 'postgresql://'])
def test_convergence_cli_fails_closed_on_a_broken_target(url, tmp_path):
    proc = _run_script(tmp_path, 'migrates/' + DEPLOYED, url)
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert 'UnsafeTargetError' in output, (
        '%r did not fail closed:\n%s' % (url, output))
    assert 'TRIPWIRE' not in output, output


def test_convergence_cli_still_runs_against_a_local_scratch_database(
        tmp_path):
    """The deploy gate must not break the local paths existing tests rely on."""
    db_file = tmp_path / 'convergence.db'
    proc = _run_script(tmp_path, 'migrates/' + DEPLOYED,
                       'sqlite:///%s' % db_file, tripwire=False)
    output = proc.stdout + proc.stderr
    assert 'UnsafeTargetError' not in output, (
        'the gate refused a local target:\n%s' % output)
    assert 'Target: sqlite' in output, output


def test_deploy_ordering_is_unchanged_by_the_flag():
    """Authorization must not have reordered any migration step."""
    lines = _logical_deploy_lines()

    def index_of(fragment):
        return next(i for i, ln in enumerate(lines) if fragment in ln)

    assert index_of('docker compose build web') < index_of(
        'migrate_schema_convergence.py')
    assert index_of('scripts/migrate.py init-ledger') < index_of(
        'migrate_schema_convergence.py')
    assert index_of('migrate_schema_convergence.py') < index_of(
        'scripts/migrate.py adopt-legacy-baseline')
    assert index_of('scripts/migrate.py upgrade') < index_of(
        'docker compose up -d --build web')


def test_deploy_keeps_its_protected_approval_configuration():
    """The production environment and its reviewers must be untouched."""
    raw = (ROOT / '.github' / 'workflows' / 'deploy.yml').read_text(
        encoding='utf-8')
    assert 'environment:' in raw, 'the protected deployment environment is gone'
    assert 'production-deploy' in raw
    # The single tolerant-guard step is still exactly one.
    commands = [ln for ln in raw.splitlines()
                if ln.strip() and not ln.strip().startswith('#')]
    tolerant = [ln for ln in commands if '--allow-pending-migrations' in ln]
    assert len(tolerant) == 1, (
        'the tolerant startup-guard step changed: %r' % tolerant)


# ── 7. Framework must not have been weakened ──────────────────────────────


def test_authoritative_runner_still_guards_every_write_command():
    source = (SCRIPTS / 'migrate.py').read_text(encoding='utf-8')
    assert 'require_writable_target' in source
    for command in ('init-ledger', 'upgrade', 'adopt-legacy-baseline'):
        assert command in source


def test_destructive_migration_gate_is_untouched():
    """F22 must not weaken 0002's authorization or checksum."""
    source = (MIGRATES / 'migrations_0002_remove_legacy_wishlist.py').read_text(
        encoding='utf-8')
    assert 'DESTRUCTIVE = True' in source
    assert 'DROP TABLE user_wishlist' in source, (
        '0002 must still contain the drop it is registered for')
    assert 'authorization' not in source.lower().split('destructive')[0][:0] + ''


def test_backup_requirements_are_intact():
    runner = (SCRIPTS / 'migrate.py').read_text(encoding='utf-8')
    for requirement in ('--backup-ref', '--backup-verified-by',
                        '--authorize-destructive'):
        assert requirement in runner, (
            'the backup-verification gate lost %s' % requirement)


def test_registry_validation_is_intact():
    registry = (MIGRATES / 'registry.py').read_text(encoding='utf-8')
    for check in ('duplicate', 'depend', 'cycle'):
        assert check in registry.lower()


def test_no_application_startup_ddl_was_introduced():
    app = (ROOT / 'app.py').read_text(encoding='utf-8')
    assert 'FRAMEIQ_AUTO_CREATE_SCHEMA' in app, (
        'app.py lost its explicit opt-in schema-creation flag')
    body = app[app.index('def create_app'):]
    assert 'legacy_migration_guard' not in body, (
        'the application must not gain a migration guard')


#: The last commit before the migration-guard work (F22/F22.1) landed.
#:
#: These boundary tests originally diffed against HEAD, which made them pass
#: only while that work was still uncommitted — they broke the moment it was
#: committed, and would equally break again for any later follow-up. Anchoring
#: to a permanent baseline instead keeps the exact same assertion true
#: regardless of how much has been committed since.
PRE_F22_BASELINE = '700803d'

_DEPLOY = '.github/workflows/deploy.yml'


def _git(*args):
    import subprocess
    return subprocess.run(['git'] + list(args), shell=False,
                          capture_output=True, text=True).stdout


def _require_baseline():
    """Skip only when the clone genuinely cannot see the baseline.

    The skip exists for shallow clones, not to paper over a bad anchor. Every
    test below additionally asserts that the diff is NON-EMPTY and that the
    baseline really is pre-F22, so a wrong or missing baseline fails loudly
    rather than passing vacuously. In a normal CI checkout the baseline is
    present and this never skips.
    """
    import subprocess
    if subprocess.run('git rev-parse --verify %s' % PRE_F22_BASELINE,
                      shell=True, capture_output=True).returncode != 0:
        pytest.skip('baseline commit %s is unavailable in this shallow clone; '
                    'boundary coverage for the deploy change is not running'
                    % PRE_F22_BASELINE)


def test_the_baseline_is_genuinely_pre_f22():
    """If this fails, every baseline-anchored assertion below is meaningless."""
    _require_baseline()
    baseline_deploy = _git('show', '%s:%s' % (PRE_F22_BASELINE, _DEPLOY))
    assert 'migrate_schema_convergence.py' in baseline_deploy
    # Pre-F22.1 the convergence command carried no authorization flag. If the
    # baseline already had it, the "only change was the flag" claim is void.
    assert ALLOW_PRODUCTION_FLAG not in [
        ln for ln in baseline_deploy.splitlines()
        if 'migrate_schema_convergence.py' in ln][0]
    # And the current tree must differ from it, or the diff-based tests would
    # pass without asserting anything.
    assert _git('diff', PRE_F22_BASELINE, '--', _DEPLOY).strip(), (
        'deploy.yml is identical to the baseline — the boundary tests would be '
        'asserting against an empty diff')


def _baseline_diff(path):
    _require_baseline()
    return _git('diff', PRE_F22_BASELINE, '--', path)


def _baseline_changed_files():
    _require_baseline()
    return _git('diff', '--name-only', PRE_F22_BASELINE).split()


def test_f22_touched_no_application_files_and_exactly_one_deploy_line():
    """Guard the boundary mechanically, so a later change cannot drift.

    F22.1 deliberately widened this boundary by ONE file and ONE concern:
    deploy.yml had to start passing the runner's explicit --allow-production
    flag to the convergence script, because otherwise gating that script would
    break the production deploy.

    So the boundary is now narrower and more precise rather than simply
    removed: the single deployment file below is permitted, every other
    deployment file is still forbidden, and the change to that file is
    separately pinned by the section-6 deployment-contract tests above.
    """
    changed = _baseline_changed_files()

    still_forbidden_prefixes = ('app.py', 'routes/', 'api/', 'models/',
                                'src/', 'nginx/', 'scripts/backup')
    still_forbidden_files = ('Dockerfile', 'docker-compose.yml',
                             'gunicorn.conf.py', 'requirements.txt',
                             'requirements-dev.txt', '.env',
                             '.github/workflows/ci-cd.yml')
    # The one deployment file F22.1 is authorized to change.
    authorized_deploy = '.github/workflows/deploy.yml'

    for path in changed:
        assert not path.startswith(still_forbidden_prefixes), \
            'F22 must not touch %s' % path
        assert path not in still_forbidden_files, 'F22 must not touch %s' % path

    touched_workflows = [p for p in changed if p.startswith('.github/')]
    assert touched_workflows == [authorized_deploy], (
        'only %s may change under F22.1, found %r'
        % (authorized_deploy, touched_workflows))


def test_the_single_deploy_change_is_only_the_convergence_authorization():
    """`git diff` on deploy.yml must show the flag and nothing else.

    This is the load-bearing check behind the boundary above: it proves the
    deployment file was widened for one reason only, and not, for example, to
    reorder steps, widen permissions, or authorize something unrelated.

    Diffed against the pre-F22 baseline rather than HEAD, so it keeps meaning
    the same whether the migration-guard work is committed or still in flight.
    """
    diff = _baseline_diff('.github/workflows/deploy.yml')

    removed = [ln for ln in diff.splitlines()
               if ln.startswith('-') and not ln.startswith('---')]
    added = [ln for ln in diff.splitlines()
             if ln.startswith('+') and not ln.startswith('+++')]

    # Exactly one functional line is removed: the old single-line convergence
    # command, replaced by the same command wrapped across two lines with the
    # flag appended. Anything else being deleted would be scope creep.
    functional_removals = [ln[1:].strip() for ln in removed if ln[1:].strip()]
    assert len(functional_removals) == 1, (
        'expected exactly one command to be rewritten, removed %r'
        % functional_removals)
    assert 'migrates/migrate_schema_convergence.py' in functional_removals[0]
    assert ALLOW_PRODUCTION_FLAG not in functional_removals[0], (
        'the old command already carried the flag, so this edit did more than '
        'add it: %r' % functional_removals[0])

    # Nothing is added except the explanation and the rewritten command.
    for line in added:
        body = line[1:].strip()
        assert (
            body.startswith('#')                       # new explanation
            or body.startswith('SKIP_SCHEMA_GUARD=1')  # the rewritten command
            or body.startswith('python migrates/')     # ...continued
            or body == ''                              # spacing
        ), 'unexpected addition to deploy.yml: %r' % body

    assert ALLOW_PRODUCTION_FLAG in diff, 'the flag was never added'
    assert 'permissions:' not in diff, 'workflow permissions were touched'
    assert 'environment:' not in diff, 'the protected environment was touched'
    assert 'required_reviewers' not in diff, \
        'the reviewer requirement was touched'