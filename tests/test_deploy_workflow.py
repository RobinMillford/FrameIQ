"""Production CI/CD workflow structural validation (Feature 10E).

The repository has no GitHub Actions runner to exercise, so the practical
test surface for deployment correctness is structural: PyYAML (already a
project dependency) parses both workflow files, every embedded SSH script
parses under ``bash -n``, and the guards below pin the exact deployment
safety contract — success-only gating, exact-SHA deployment, dirty-tree
protection, migration-before-web ordering, dependency health checks,
health/smoke/log verification, and the absence of the destructive
patterns that must never appear (down -v, blind git pull deploys,
blanket schema-guard disabling, plaintext secrets).

Static by design: no network, no docker, no production side effects.
"""
import re
import subprocess
from pathlib import Path

import pytest
import yaml

_CI = Path(".github/workflows/ci-cd.yml")
_DEPLOY = Path(".github/workflows/deploy.yml")

_MIGRATION = "migrates/migrate_movie_release_dates.py"


def _load(path):
    parsed = yaml.safe_load(path.read_text())
    # YAML 1.1: a bare `on:` key parses as boolean True. Normalize.
    if "on" not in parsed and True in parsed:
        parsed["on"] = parsed.pop(True)
    return parsed


def _scripts(wf):
    """Every SSH script in the workflow, keyed by step name."""
    out = {}
    for job in wf["jobs"].values():
        for step in job.get("steps", []):
            script = (step.get("with") or {}).get("script")
            if script is not None:
                out[step.get("name", "<unnamed>")] = script
    return out


@pytest.fixture(scope="module")
def ci_raw():
    return _CI.read_text()


@pytest.fixture(scope="module")
def deploy_raw():
    return _DEPLOY.read_text()


@pytest.fixture(scope="module")
def ci(ci_raw):
    return _load(_CI)


@pytest.fixture(scope="module")
def deploy(deploy_raw):
    return _load(_DEPLOY)


@pytest.fixture(scope="module")
def deploy_scripts(deploy):
    return _scripts(deploy)


def _main_script(scripts):
    """The main deploy step's script (the one with the SHA verification)."""
    return scripts["Deploy and verify (exact SHA)"]


def _code(raw):
    """Workflow/script text without comment lines (comments may
    legitimately mention banned tokens such as 'down -v')."""
    return "\n".join(ln for ln in raw.splitlines()
                     if not ln.lstrip().startswith("#"))


def _code_of_script(script):
    return _code(script)


# ════════════════════════════════════════════════════════════════════════════
# Both workflows: parseability and shell validity
# ════════════════════════════════════════════════════════════════════════════

def test_both_workflow_files_exist():
    assert _CI.is_file()
    assert _DEPLOY.is_file()


def test_ci_yaml_parses(ci):
    assert "jobs" in ci


def test_deploy_yaml_parses(deploy):
    assert "jobs" in deploy


@pytest.mark.parametrize("path", [_CI, _DEPLOY])
def test_every_embedded_script_parses_under_bash_n(path):
    """Section M: the deploy incident was a remote bash syntax failure —
    every SSH script must parse locally before it can ever ship."""
    wf = _load(path)
    for name, script in _scripts(wf).items():
        # GitHub expressions are interpolated by the runner; substitute
        # inert placeholders so bash -n sees valid shell.
        neutral = re.sub(r"\$\{\{[^}]*\}\}", "TEMPLATE", script)
        result = subprocess.run(["bash", "-n"], input=neutral, text=True,
                                capture_output=True)
        assert result.returncode == 0, f"{path.name}: {name}: {result.stderr}"


# ════════════════════════════════════════════════════════════════════════════
# CI workflow contract (Features 0 + 10E sections A/B)
# ════════════════════════════════════════════════════════════════════════════

def test_ci_runs_full_test_suite_not_a_subset(ci):
    step = next(s for s in ci["jobs"]["test"]["steps"]
                if s.get("name") == "Full test suite")
    run = step["run"]
    assert "pytest tests/" in run
    # The whole suite, not a -k subset.
    assert "-k" not in run


def test_ci_has_no_failure_suppression(ci_raw, ci):
    code = _code(ci_raw)
    for banned in ("continue-on-error: true", "--exit-zero"):
        assert banned not in code
    # The only tolerated '|| true' is the conflict gate's grep no-match
    # handling; test and flake8 steps must never suppress failures.
    for step in ci["jobs"]["test"]["steps"]:
        if step.get("name") in ("Full test suite",
                                "Flake8 (errors-only baseline)"):
            assert "|| true" not in step["run"], step.get("name")


def test_ci_full_history_checkout(ci_raw):
    # The diff gate compares against github.event.before, which does not
    # exist in a depth-1 checkout (the shallow-history incident).
    assert "fetch-depth: 0" in ci_raw


def test_ci_conflict_gate_is_self_safe(ci_raw):
    # The scanner must not contain literal 7-char marker sequences, or it
    # flags its own detection logic (the self-match incident).
    assert "<<<<<<<" not in ci_raw
    assert ">>>>>>>" not in ci_raw
    assert "=======" not in ci_raw
    # And it must still detect markers via the 7-char run classes.
    assert "(<{7}|={7}|>{7})" in ci_raw


def test_ci_validation_gates_present_in_order(ci):
    names = [s.get("name") for s in ci["jobs"]["test"]["steps"]]
    order = ["Install dependencies", "Compile Python", "Validate migrations",
             "Validate JavaScript", "Flake8 (errors-only baseline)",
             "Full test suite"]
    idxs = [names.index(n) for n in order]
    assert idxs == sorted(idxs)


def test_ci_migrations_are_never_executed(ci_raw):
    # py_compile only — executing migrations in CI would touch nothing
    # (no DB) but the convention forbids it anyway.
    assert "py_compile" in ci_raw
    assert not re.search(r"python\s+migrates/", ci_raw)


# ════════════════════════════════════════════════════════════════════════════
# Deploy gating (Section C) — success-only, main-only, workflow_run
# ════════════════════════════════════════════════════════════════════════════

def test_deploy_triggers_only_via_workflow_run(deploy):
    on = deploy["on"]
    assert set(on) == {"workflow_run"}
    assert on["workflow_run"]["workflows"] == ["CI"]
    assert on["workflow_run"]["types"] == ["completed"]
    # A bare push trigger would deploy untested commits.
    assert "push" not in on and "pull_request" not in on


def test_deploy_gate_is_success_and_main_only(deploy_raw):
    assert "github.event.workflow_run.conclusion == 'success'" in deploy_raw
    assert "github.event.workflow_run.head_branch == 'main'" in deploy_raw


def test_deploy_never_deploys_feature_branches_or_prs(deploy_raw):
    code = _code(deploy_raw)
    assert "master-feature-roadmap" not in code
    assert "develop" not in code


def test_deploy_concurrency_serializes_production(deploy):
    conc = deploy["concurrency"]
    assert conc["group"] == "production-deploy"
    # Queue, never cancel — a cancelled in-flight deploy leaves
    # production in an unknown state.
    assert conc["cancel-in-progress"] is False


def test_deploy_job_has_timeout(deploy):
    assert 0 < deploy["jobs"]["deploy"]["timeout-minutes"] <= 60


# ════════════════════════════════════════════════════════════════════════════
# Exact-SHA deployment (Sections C/D)
# ════════════════════════════════════════════════════════════════════════════

def test_deploy_uses_ci_verified_sha(deploy_raw):
    assert "github.event.workflow_run.head_sha" in deploy_raw


def test_vps_resets_to_verified_sha_and_reverifies(deploy_scripts):
    script = _main_script(deploy_scripts)
    assert 'git reset --hard "$VERIFIED_SHA"' in script
    assert 'ACTUAL="$(git rev-parse HEAD)"' in script
    assert '[ "$ACTUAL" != "$VERIFIED_SHA" ]' in script
    # Mismatch must abort before any application replacement.
    mismatch_idx = script.index('[ "$ACTUAL" != "$VERIFIED_SHA" ]')
    exit_idx = script.index("exit 1", mismatch_idx)
    web_idx = script.index("docker compose up -d")
    assert exit_idx < web_idx


def test_deploy_does_not_use_blind_git_pull(deploy_scripts):
    for name, script in deploy_scripts.items():
        assert not re.search(r"^\s*git pull\b", script, re.MULTILINE), name


# ════════════════════════════════════════════════════════════════════════════
# Dirty worktree protection (Section E)
# ════════════════════════════════════════════════════════════════════════════

def test_preflight_fails_on_dirty_vps_tree(deploy_scripts):
    pre = deploy_scripts["Pre-flight check (clean VPS working tree)"]
    assert "git status --porcelain" in pre
    assert "::error::VPS working tree is dirty" in pre


# ════════════════════════════════════════════════════════════════════════════
# Migration ordering (Sections F/G)
# ════════════════════════════════════════════════════════════════════════════

def test_named_migration_runs_before_web_recreate(deploy_scripts):
    script = _main_script(deploy_scripts)
    mig_idx = script.index(_MIGRATION)
    web_idx = script.index("docker compose up -d")
    assert mig_idx < web_idx


def test_migration_is_a_named_one_off_container(deploy_scripts):
    script = _main_script(deploy_scripts)
    line = next(ln for ln in script.splitlines() if _MIGRATION in ln)
    assert line.strip() == (
        f"SKIP_SCHEMA_GUARD=1 docker compose run --rm --no-deps web "
        f"python {_MIGRATION}"), line


def test_migration_uses_documented_skip_schema_guard_only(deploy_scripts):
    # SKIP_SCHEMA_GUARD is allowed ONLY on the one-off migration line —
    # never for the running application (comments excluded).
    script = _main_script(deploy_scripts)
    guard_lines = [ln for ln in _code_of_script(script).splitlines()
                   if "SKIP_SCHEMA_GUARD" in ln]
    assert len(guard_lines) == 1
    assert _MIGRATION in guard_lines[0]


def test_no_blind_migration_sweep(deploy_scripts):
    for name, script in deploy_scripts.items():
        assert "migrates/*.py" not in _code_of_script(script), name


def test_image_is_built_before_migration(deploy_scripts):
    """The migration script only exists in the code being deployed —
    running it from the previous deploy's image fails on the first
    schema-changing deploy."""
    script = _main_script(deploy_scripts)
    assert script.index("docker compose build web") < script.index(_MIGRATION)


# ════════════════════════════════════════════════════════════════════════════
# Dependency health before migrate/recreate (Sections H/I)
# ════════════════════════════════════════════════════════════════════════════

def test_db_and_valkey_health_gates_the_deploy(deploy_scripts):
    script = _main_script(deploy_scripts)
    assert "frameiq-db-1" in script and "frameiq-valkey-1" in script
    assert '!= "healthy"' in script
    # The health gate must precede both the migration and the web recreate.
    gate_idx = script.index("both must be healthy")
    assert gate_idx < script.index(_MIGRATION)
    assert gate_idx < script.index("docker compose up -d")


def test_web_container_running_is_verified(deploy_scripts):
    script = _main_script(deploy_scripts)
    assert 'frameiq-web-1' in script
    assert '!= "running"' in script


def test_no_volume_destruction_anywhere(deploy_raw):
    # `down -v` must not appear outside comments (the header documents
    # the prohibition).
    assert "down -v" not in _code(deploy_raw)


# ════════════════════════════════════════════════════════════════════════════
# Health / smoke / log verification (Sections I/J/K/L)
# ════════════════════════════════════════════════════════════════════════════

def test_health_check_requires_exact_200_with_retries(deploy_scripts):
    script = _main_script(deploy_scripts)
    assert "https://frameiq.studio/health" in script
    assert '"$CODE" = "200"' in script
    # Bounded retries, not infinite.
    assert "for i in 1 2 3 4 5" in script


def test_public_smokes_cover_calendar_and_tv_upcoming(deploy_scripts):
    script = _main_script(deploy_scripts)
    assert "/calendar" in script
    assert "/tv/upcoming" in script
    # Non-5xx semantics: 2xx/3xx accepted (unauthenticated /calendar 302s).
    assert '[[ "$CODE" =~ ^2[0-9][0-9]$ || "$CODE" =~ ^3[0-9][0-9]$ ]]' in script


def test_authenticated_calendar_api_smoke_exists(deploy_scripts):
    script = _main_script(deploy_scripts)
    assert "/api/calendar" in script
    assert 'SMOKE_TEST_USERNAME' in script and 'SMOKE_TEST_PASSWORD' in script


def test_api_smoke_validates_the_full_envelope(deploy_scripts):
    script = _main_script(deploy_scripts)
    # events list + meta.today + all three counts — the 10A envelope.
    for token in ("'events'", "'today'", "'tv'", "'movie'", "'total'"):
        assert token in script


def test_api_smoke_skips_loudly_without_credentials(deploy_scripts):
    script = _main_script(deploy_scripts)
    assert "SKIPPED" in script
    # Skipping must be loud and documented, never silent.
    assert "documented limitation" in script


def test_api_smoke_never_logs_credentials_or_event_data(deploy_scripts):
    script = _main_script(deploy_scripts)
    # No echo of the credential variables, and the envelope check prints
    # only aggregate counts.
    assert "echo \"$SMOKE_TEST" not in script
    assert "calendar API envelope OK" in script


def test_log_scan_targets_fatal_patterns_only(deploy_scripts):
    script = _main_script(deploy_scripts)
    for pattern in ("SchemaMismatchError", "Worker failed to boot",
                    "AssertionError", "CRITICAL",
                    "Exception in worker process"):
        assert pattern in script
    # The documented 429 policy must remain stated.
    assert "429" in script


def test_recent_logs_are_printed_for_diagnosis(deploy_scripts):
    script = _main_script(deploy_scripts)
    assert "docker compose logs" in script
    assert "--since=3m" in script


# ════════════════════════════════════════════════════════════════════════════
# Shell robustness / secrets (Sections M/N/P)
# ════════════════════════════════════════════════════════════════════════════

def test_all_ssh_steps_use_script_stop_and_timeouts(deploy):
    for step in deploy["jobs"]["deploy"]["steps"]:
        with_obj = step.get("with", {})
        if "script" in with_obj:
            assert with_obj.get("script_stop") is True, step.get("name")
            assert with_obj.get("command_timeout"), step.get("name")


def test_remote_scripts_set_strict_mode(deploy_scripts):
    for name, script in deploy_scripts.items():
        assert "set -euo pipefail" in script, name


def test_ssh_action_pin_matches_repo_convention(deploy_raw):
    assert "appleboy/ssh-action@v1.0.3" in deploy_raw


def test_only_existing_secrets_are_used(deploy_raw):
    used = set(re.findall(r"secrets\.([A-Z_]+)", deploy_raw))
    assert used <= {"VPS_HOST", "VPS_USER", "SSH_PRIVATE_KEY",
                    "SMOKE_TEST_USERNAME", "SMOKE_TEST_PASSWORD"}


def test_no_plaintext_secrets(deploy_raw):
    code = _code(deploy_raw)
    assert "BEGIN" not in code and "ssh-rsa" not in code
    assert not re.search(r"(key|token|secret|password)\s*[:=]\s*['\"]?\w",
                         code, re.IGNORECASE)


def test_no_secret_echoed_in_remote_scripts(deploy_scripts):
    for name, script in deploy_scripts.items():
        # The envs pass-through is fine; printing the values is not.
        assert not re.search(r"echo\s+.*\$\{?(SMOKE_TEST_|SSH_PRIVATE)",
                             script), name


# ════════════════════════════════════════════════════════════════════════════
# Regression protection (Section R) — calendar behavior untouched
# ════════════════════════════════════════════════════════════════════════════

def test_deploy_workflow_touches_no_product_code():
    # The deploy workflow may only orchestrate: no application source,
    # no schema edits beyond the named migration invocation.
    script = _main_script(_scripts(_load(_DEPLOY)))
    for banned in ("models.py", "api/calendar.py", "routes/calendar.py",
                   "static/js", "ALTER TABLE", "create_all"):
        assert banned not in script


def test_ci_does_not_deploy(ci_raw):
    assert "docker compose" not in ci_raw
    assert "ssh-action" not in ci_raw
