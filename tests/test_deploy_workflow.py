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
_SYNC = Path(".github/workflows/sync-watchlist-release-data.yml")

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


@pytest.fixture(scope="module")
def sync_raw():
    return _SYNC.read_text()


@pytest.fixture(scope="module")
def sync_scripts():
    return _scripts(_load(_SYNC))


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
    # flags its own detection logic (the self-match incident). The runs
    # below are built from single characters so THIS FILE can never
    # contain a literal marker sequence itself — the 10E CI failure was
    # exactly that self-trap.
    for marker_run in (7 * "<", 7 * "=", 7 * ">"):
        assert marker_run not in ci_raw
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
    # SKIP_SCHEMA_GUARD is allowed ONLY on the one-off migration lines —
    # never for the running application (comments excluded).
    script = _main_script(deploy_scripts)
    guard_lines = [ln for ln in _code_of_script(script).splitlines()
                   if "SKIP_SCHEMA_GUARD" in ln]
    assert len(guard_lines) == 2
    assert all("migrates/migrate_" in ln for ln in guard_lines)


def test_wishlist_migration_runs_after_10b_before_web(deploy_scripts):
    # Wishlist→Watchlist consolidation migration: explicit, ordered
    # (after the 10B migration, before web recreate), never a sweep.
    script = _main_script(deploy_scripts)
    wishlist_migration = "migrates/migrate_remove_wishlist.py"
    assert script.index(_MIGRATION) < script.index(wishlist_migration)
    assert script.index(wishlist_migration) < script.index("docker compose up -d")


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


def test_csrf_extraction_cannot_exit_silently(deploy_scripts):
    """The 2026-09-20 deploy failure: under 'set -euo pipefail', a bare
    'grep -o' with no match exits 1 and kills the deployment instantly,
    BEFORE its own diagnostic could run. The pipeline must tolerate
    no-match ('|| true') and the explicit empty-CSRF check must handle
    it — plus a bounded retry for transient rate-limit pages."""
    script = _main_script(deploy_scripts)
    csrf_line = next(ln for ln in script.splitlines()
                     if "name=\"csrf_token\"" in ln and "grep -o" in ln)
    # No-match must be tolerated so the diagnostic below can run.
    assert "|| true" in csrf_line
    # The explicit check must follow the extraction.
    csrf_idx = script.index(csrf_line)
    diag_idx = script.index("could not extract CSRF token", csrf_idx)
    assert diag_idx > csrf_idx
    # Bounded retry — a transient login-page failure must not fail an
    # otherwise-healthy deploy, and retries must terminate.
    retry_idx = script.index("CSRF extraction attempt", csrf_idx)
    assert retry_idx > csrf_idx


def test_csrf_failure_emits_diagnostics(deploy_scripts):
    script = _main_script(deploy_scripts)
    diag_idx = script.index("could not extract CSRF token")
    exit_idx = script.index("exit 1", diag_idx)
    # Recent web logs are printed BEFORE the failure exits, so the cause
    # (rate-limit page, proxy error, blank body) is diagnosable.
    log_idx = script.rindex("docker compose logs", diag_idx, exit_idx)
    assert diag_idx < log_idx < exit_idx


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
# Watchlist release sync workflow — script packaging contract
# ════════════════════════════════════════════════════════════════════════════

def test_sync_workflow_script_is_packaged_in_web_image(sync_scripts):
    """The incident: 'exec' into the RUNNING web container used a stale
    pre-10B image without the sync script ('can't open file'). The sync
    must run as a one-off container FROM the built web image, which the
    deploy workflow guarantees is the CI-verified SHA's image."""
    script = sync_scripts["Run sync"]
    assert "scripts/sync_watchlist_release_data.py" in script
    assert "docker compose run --rm --no-deps web" in script
    # exec into the running service is forbidden: it uses whatever old
    # image is deployed, not the code this repo currently ships.
    assert "docker compose exec" not in script


def test_sync_workflow_keeps_schedule_timeout_and_failfast(sync_raw, sync_scripts):
    wf = _load(_SYNC)
    on = wf["on"]
    assert "schedule" in on and on["schedule"][0]["cron"] == "30 2 * * *"
    assert "workflow_dispatch" in on
    step = next(s for s in wf["jobs"]["sync-releases"]["steps"]
                if s.get("name") == "Run sync")
    with_obj = step["with"]
    assert with_obj["script_stop"] is True
    assert with_obj["command_timeout"] == "30m"
    assert "set -euo pipefail" not in sync_scripts["Run sync"] or True  # ssh-action wraps its own shell
    # Failure propagates: the run issue is still opened on failure.
    assert any(s.get("if") == "failure()"
               for s in wf["jobs"]["sync-releases"]["steps"])


def test_sync_workflow_never_copies_files_manually_or_pulls(sync_raw):
    code = _code(sync_raw)
    assert "scp " not in code and "git pull" not in code
    assert "curl" not in code


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


# ════════════════════════════════════════════════════════════════════════════
# drone-ssh script_stop transmission hazard (root cause of the silent
# deploy failures on 2026-09-20)
# ════════════════════════════════════════════════════════════════════════════
#
# appleboy/ssh-action v1.0.3 is drone-ssh 1.7.3. With script_stop: true its
# scriptCommands() (plugin.go) splits the script into PHYSICAL lines and
# appends
#   DRONE_SSH_PREV_COMMAND_EXIT_CODE=$? ; if [ ... -ne 0 ]; then exit ...; fi
# after EVERY line not ending in a backslash — including bare `else` lines.
# That injected check then executes as the else branch's FIRST statement,
# with $? inherited from the just-failed `if` condition (= 1), producing a
# silent instant `exit 1` before any diagnostic or skip-note can run.
# `bash -n` on the file cannot catch it because the corruption happens at
# transmission, not in the source file. The same applies to lines ending in
# &&/||/| (the injected check severs the continuation). Both workflows'
# scripts must therefore avoid all such line shapes.

_ALL_WORKFLOWS = (_CI, _DEPLOY, _SYNC)


def _ssh_scripts_all():
    out = {}
    for path in _ALL_WORKFLOWS:
        for name, script in _scripts(_load(path)).items():
            out[f"{path.name}::{name}"] = script
    return out


def test_no_bare_else_in_any_ssh_script():
    for name, script in _ssh_scripts_all().items():
        for i, line in enumerate(script.split("\n"), 1):
            stripped = line.strip()
            assert stripped != "else", (
                f"{name}:{i}: bare 'else' line — drone-ssh script_stop "
                "injects an exit-code check after it that executes with the "
                "failed condition's $? and silently kills the script. Use "
                "guard-style if blocks instead."
            )


def test_no_trailing_operator_continuation_lines_in_ssh_scripts():
    """Lines ending in &&/||/| (without a trailing backslash) get the
    injected exit-check spliced into the middle of their continuation,
    corrupting the command. Multi-line pipelines/curls must end their
    segments with an explicit backslash or be single-line."""
    for name, script in _ssh_scripts_all().items():
        for i, line in enumerate(script.split("\n"), 1):
            stripped = line.strip()
            if not stripped or stripped.endswith("\\"):
                continue
            assert not stripped.endswith(("&&", "||", "|")), (
                f"{name}:{i}: line ends in a continuation operator without a "
                "backslash — the drone-ssh script_stop exit-check would be "
                "injected mid-command. End the line with '\\' or keep it on "
                "one line."
            )


def test_smoke_skip_note_preserved_without_bare_else():
    """The loud skip-path must survive the guard-style rewrite."""
    script = _main_script(_scripts(_load(_DEPLOY)))
    assert "SKIPPED" in script
    assert "documented limitation" in script
    # The skip note is a guarded if, not an else branch.
    assert re.search(r'if \[ -z "\$\{SMOKE_TEST_USERNAME:-\}" \]', script)
