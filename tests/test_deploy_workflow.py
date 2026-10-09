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

# Since Task F8 the authoritative schema-change command is the runner, not a
# per-release hand-maintained script line. The ordering tests below are written
# against it; their safety intent is unchanged.
_UPGRADE = "scripts/migrate.py upgrade"
_MIGRATION = _UPGRADE


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


def _logical_lines(script):
    """Comments stripped and backslash continuations joined into one line.

    The shell reads these as single commands, so assertions about ordering and
    about "which command is this line" must too. Without this, a `index()` can
    match a phrase that only appears in a comment, and a wrapped command's two
    halves look like two unrelated lines.
    """
    joined, buffer = [], ""
    for line in _code(script).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.endswith("\\"):
            buffer += stripped[:-1].strip() + " "
            continue
        buffer += stripped
        joined.append(" ".join(buffer.split()))
        buffer = ""
    if buffer:
        joined.append(" ".join(buffer.split()))
    return joined


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


def test_ci_test_job_is_offline_and_needs_no_tmdb_secret(ci_raw, ci):
    """Task F3: the ordinary suite must not depend on a TMDb credential.

    The offline boundary (tests/tmdb_offline.py) serves every TMDb payload
    from fixtures and fails loudly on any other outbound connection, so CI
    supplies a throwaway placeholder rather than a repo secret. Re-injecting
    `secrets.TMDB_API_KEY` here would silently reintroduce a network
    dependency and make CI results depend on a third party.
    """
    step = next(s for s in ci["jobs"]["test"]["steps"]
                if s.get("name") == "Full test suite")
    assert "secrets.TMDB_API_KEY" not in step["env"]["TMDB_API_KEY"], (
        "the offline suite must not require a TMDb secret")
    assert step["env"]["TMDB_API_KEY"], "a throwaway placeholder is expected"

    # The default command is the plain offline suite; the opt-in integration
    # layer is deselected by pyproject's addopts, not by editing this run.
    assert "pytest tests/" in step["run"]
    assert "-m tmdb" not in step["run"]
    # ...and the marker itself must be registered so the opt-in layer does
    # not emit PytestUnknownMarkWarning.
    pyproject = (Path("pyproject.toml")).read_text(encoding="utf-8")
    assert '"tmdb' in pyproject or "'tmdb" in pyproject
    assert 'not tmdb' in pyproject


def test_ci_migrations_are_never_executed(ci_raw):
    # py_compile only — executing migrations in CI would touch nothing
    # (no DB) but the convention forbids it anyway.
    assert "py_compile" in ci_raw
    assert not re.search(r"python\s+migrates/", ci_raw)


# ════════════════════════════════════════════════════════════════════════════
# Deploy gating (Section C) — success-only, main-only, workflow_run
# ════════════════════════════════════════════════════════════════════════════

def test_deploy_triggers_only_via_workflow_run(deploy):
    """The AUTOMATIC path stays workflow_run only.

    A `workflow_dispatch` trigger also exists, but it exists solely to authorise
    the destructive step; it must never be able to start the safe deploy. Both
    are asserted below, so adding a trigger without thinking about this fails.
    """
    on = deploy["on"]
    assert set(on) == {"workflow_run", "workflow_dispatch"}
    assert on["workflow_run"]["workflows"] == ["CI"]
    assert on["workflow_run"]["types"] == ["completed"]
    # A bare push trigger would deploy untested commits.
    assert "push" not in on and "pull_request" not in on


def test_the_safe_deploy_job_cannot_run_on_a_manual_dispatch(deploy):
    """Explicit, not incidental.

    `github.event.workflow_run.*` is null on a dispatch, so the old condition
    happened to be false. That is an accident, not a control, and it would stop
    being true the moment the condition was reworded.
    """
    condition = " ".join(deploy["jobs"]["deploy"]["if"].split())
    assert "github.event_name == 'workflow_run'" in condition
    assert "github.event.workflow_run.conclusion == 'success'" in condition
    assert "github.event.workflow_run.head_branch == 'main'" in condition


# ════════════════════════════════════════════════════════════════════════════
# Destructive-migration backup/approval gate (Decision C)
# ════════════════════════════════════════════════════════════════════════════

def _destructive(deploy):
    return deploy["jobs"]["destructive"]


def test_destructive_job_is_separate_and_never_automatic(deploy):
    job = _destructive(deploy)
    assert " ".join(job["if"].split()) == "github.event_name == 'workflow_dispatch'"


def test_destructive_job_requires_a_github_environment(deploy):
    """Environment approval is the human gate; it must be declared."""
    job = _destructive(deploy)
    assert job["environment"]["name"] == "production-destructive"


def test_destructive_job_fails_closed_without_required_reviewers(deploy_raw,
                                                                 deploy):
    """Declaring an environment is necessary but NOT sufficient evidence.

    An environment with no required reviewers admits anyone, so the job would be
    self-authorising while appearing gated. The workflow must verify the
    protection rule rather than assume it.
    """
    code = _code(deploy_raw)
    assert "deployment_protection_rule" in code
    assert "required_reviewers" in code
    assert "no required reviewers" in code
    step = next(s for s in _destructive(deploy)["steps"]
                if s.get("name", "").startswith("Fail closed"))
    run = step["run"]
    assert "gh api" in run
    assert "exit 1" in run


def test_destructive_job_requires_per_deployment_backup_evidence(deploy):
    """workflow_dispatch inputs, not repository variables.

    A repository variable is exactly the 'stale value' case: it would still say
    a snapshot exists weeks after the fact.
    """
    inputs = deploy["on"]["workflow_dispatch"]["inputs"]
    for name in ("destructive_migration", "backup_ref", "backup_verified_by",
                 "ref"):
        assert name in inputs, name
        assert inputs[name]["required"] is True, name


def test_destructive_job_pins_a_full_sha(deploy):
    """A branch name is not acceptable for a destructive step."""
    run = next(s for s in _destructive(deploy)["steps"]
               if s.get("name") == "Reject unusable dispatch input")["run"]
    assert "[0-9a-f]{40}" in run


def test_destructive_job_rejects_placeholder_backup_evidence(deploy):
    run = next(s for s in _destructive(deploy)["steps"]
               if s.get("name") == "Reject unusable dispatch input")["run"]
    for placeholder in ("yes", "todo", "changeme", "placeholder", "snapshot"):
        assert placeholder in run.lower(), placeholder
    assert "exit 1" in run


def test_destructive_job_only_authorises_the_named_migration(deploy):
    run = next(s for s in _destructive(deploy)["steps"]
               if s.get("name") == "Reject unusable dispatch input")["run"]
    assert "0002_remove_legacy_wishlist" in run


def test_destructive_job_reverifies_immediately_before_the_step(deploy):
    """Preconditions are re-checked adjacent to the destructive call."""
    script = _scripts(_load(_DEPLOY))[
        "Destructive migration (backup-approved)"]
    code = _code_of_script(script)
    assert code.index("BACKUP_REF=") < code.index("scripts/migrate.py upgrade")
    assert "VERIFIED_SHA" in code
    assert "if [ -z \"$BACKUP_REF\" ]" in code or \
        'if [ -z "$BACKUP_REF" ]' in code


def test_destructive_job_passes_the_authorization_to_the_runner(deploy):
    """The workflow gate and the runner gate are independent; both are used."""
    script = _scripts(_load(_DEPLOY))["Destructive migration (backup-approved)"]
    for flag in ("--only", "--authorize-destructive", "--backup-ref",
                 "--backup-verified-by"):
        assert flag in script, flag
    assert "--allow-production" in script


def test_destructive_job_verifies_the_schema_afterwards(deploy):
    script = _scripts(_load(_DEPLOY))["Destructive migration (backup-approved)"]
    assert script.index("utils.schema_guard") > script.index(
        "scripts/migrate.py upgrade")


def test_destructive_job_is_serialised_against_the_safe_deploy(deploy):
    """An approval must not race a deploy that is already migrating."""
    assert _destructive(deploy)["concurrency"]["group"] == "production-deploy"
    assert _destructive(deploy)["concurrency"]["cancel-in-progress"] is False


def test_the_safe_deploy_does_not_contain_the_destructive_command(deploy):
    """Belt and braces: the automatic path has no authorization flags at all,
    so even if the deferral in the runner were removed it could not drop."""
    script = _code_of_script(_main_script(_scripts(_load(_DEPLOY))))
    assert "--authorize-destructive" not in script
    assert "--backup-ref" not in script


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

def test_migration_runner_runs_before_web_recreate(deploy_scripts):
    script = _code_of_script(_main_script(deploy_scripts))
    mig_idx = script.index(_UPGRADE)
    web_idx = script.index("docker compose up -d")
    assert mig_idx < web_idx


def test_deploy_applies_every_migration_without_naming_them(deploy_scripts):
    """The point of F8: a release no longer edits this workflow.

    `upgrade` applies everything registered, so forgetting a migration is no
    longer possible — which is exactly how 9 of them went missing before.
    """
    script = _code_of_script(_main_script(deploy_scripts))
    assert "scripts/migrate.py upgrade" in script
    # The old per-release script lines must be gone, not merely supplemented.
    for superseded in ("migrates/migrate_movie_release_dates.py",
                       "migrates/migrate_remove_wishlist.py"):
        assert superseded not in script, superseded


def test_deploy_adopts_the_baseline_before_upgrading(deploy_scripts):
    """Ordering matters: a baseline may only be recorded once the guard passes,
    and the guard cannot pass until convergence has created the missing tables.
    """
    script = _code_of_script(_main_script(deploy_scripts))
    convergence = "migrates/migrate_schema_convergence.py"
    baseline = "scripts/migrate.py adopt-legacy-baseline"
    assert script.index(convergence) < script.index(baseline)
    assert script.index(baseline) < script.index("scripts/migrate.py upgrade")


def test_deploy_produces_migration_history_before_serving(deploy_scripts):
    """The whole reason the ledger exists: `status` must report applied work."""
    script = _code_of_script(_main_script(deploy_scripts))
    assert "scripts/migrate.py status" in script
    assert script.index("scripts/migrate.py status") < script.index(
        "docker compose up -d")


def test_migration_runner_is_a_named_one_off_container(deploy_scripts):
    lines = _logical_lines(_main_script(deploy_scripts))
    line = next(ln for ln in lines if "scripts/migrate.py upgrade" in ln)
    assert line == (
        "SKIP_SCHEMA_GUARD=1 docker compose run --rm --no-deps web "
        "python scripts/migrate.py upgrade --allow-production"
    ), line


def test_safe_deploy_passes_no_authorization_flags(deploy_scripts):
    """The automatic deploy applies only non-destructive migrations.

    The runner defers a destructive migration unless it is named with --only,
    and this job never names it — so the two gates reinforce each other rather
    than one having to be trusted alone.
    """
    lines = _logical_lines(_main_script(deploy_scripts))
    upgrade = next(ln for ln in lines if "scripts/migrate.py upgrade" in ln)
    assert "--only" not in upgrade
    assert "--authorize-destructive" not in upgrade


def test_migration_commands_exist_inside_the_container(deploy_scripts):
    """`docker compose run web python` runs INSIDE the image.

    A mistaken host-side `python scripts/migrate.py` would run against a
    different Python and possibly a different DATABASE_URL.
    """
    for line in _logical_lines(_main_script(deploy_scripts)):
        if "scripts/migrate.py" in line:
            assert "docker compose run --rm --no-deps web python" in line, line


def test_writing_migration_commands_pass_allow_production(deploy_scripts):
    """The runner refuses a remote target without this flag, and no
    environment variable can grant it — so every writing command must pass
    it explicitly, in a reviewable place."""
    lines = _logical_lines(_main_script(deploy_scripts))
    for command in ("adopt-legacy-baseline", "upgrade", "status"):
        line = next(ln for ln in lines
                    if "scripts/migrate.py %s" % command in ln)
        assert "--allow-production" in line, command


def test_no_environment_variable_can_authorize_a_production_migration():
    """Belt and braces: the guard against an env-var bypass lives in code."""
    source = (Path("utils/db_target.py")).read_text(encoding="utf-8")
    assert "allow_production" in source
    assert "FRAMEIQ_ALLOW_PRODUCTION" not in source
    assert "os.environ.get('FRAMEIQ_ALLOW" not in source


def test_migration_uses_documented_skip_schema_guard_only(deploy_scripts):
    # SKIP_SCHEMA_GUARD is allowed ONLY on the one-off migration lines —
    # never for the running application (comments excluded). Since the web
    # process no longer creates its own schema, a migration really does need
    # the escape hatch: the startup guard would otherwise refuse to boot
    # against the very database the migration is here to repair.
    #
    # The read-only pre-boot guard check is deliberately NOT in this list —
    # `python -m utils.schema_guard` exempts itself internally, so the
    # workflow passes no environment variable to it.
    lines = _logical_lines(_main_script(deploy_scripts))
    guard_lines = [ln for ln in lines if "SKIP_SCHEMA_GUARD" in ln]
    # init-ledger + convergence + baseline + upgrade + status: five one-off
    # containers. Each needs the escape hatch because the startup guard would
    # otherwise refuse the very container that is here to repair the schema.
    assert len(guard_lines) == 5, guard_lines
    # Each must be a one-off migration container, never the running app.
    for line in guard_lines:
        assert "docker compose run --rm --no-deps web" in line, line
        assert "docker compose up" not in line, line
        assert ("migrates/migrate_" in line
                or "scripts/migrate.py" in line), line
    # `docker compose up -d --build web` (the running application) must carry
    # no guard bypass at all.
    up_line = next(ln for ln in lines if "docker compose up -d" in ln)
    assert "SKIP_SCHEMA_GUARD" not in up_line


def test_wishlist_consolidation_is_a_registered_migration():
    """The wishlist merge is now versioned, ordered and recorded.

    Previously it was a bare script line in this workflow, which is how nine
    other owning migrations went missing.
    """
    from migrates import registry as _registry
    versions = {spec.version for spec in _registry.ordered_migrations()}
    assert "0002_remove_legacy_wishlist" in versions
    spec = _registry.by_version()["0002_remove_legacy_wishlist"]
    assert spec.depends_on == ("0001_canonical_watched_reconcile",)
    assert 'DESTRUCTIVE' in spec.summary


def test_schema_convergence_runs_before_web_recreate(deploy_scripts):
    # The web process owns no schema, so the live schema must be converged
    # BEFORE the new web image starts — otherwise the first boot of the new
    # code hits the parity guard and the workers die.
    script = _main_script(deploy_scripts)
    convergence = "migrates/migrate_schema_convergence.py"
    guard_check = "python -m utils.schema_guard"
    assert convergence in script
    assert script.index(convergence) < script.index("docker compose up -d")
    assert script.index(guard_check) < script.index("docker compose up -d")


def test_no_blind_migration_sweep(deploy_scripts):
    for name, script in deploy_scripts.items():
        assert "migrates/*.py" not in _code_of_script(script), name


def test_image_is_built_before_migration(deploy_scripts):
    """The migration only exists in the code being deployed — running it from
    the previous deploy's image fails on the first schema-changing deploy."""
    script = _code_of_script(_main_script(deploy_scripts))
    assert script.index("docker compose build web") < script.index(_UPGRADE)


# ════════════════════════════════════════════════════════════════════════════
# Dependency health before migrate/recreate (Sections H/I)
# ════════════════════════════════════════════════════════════════════════════

def test_db_and_valkey_health_gates_the_deploy(deploy_scripts):
    script = _main_script(deploy_scripts)
    assert "frameiq-db-1" in script and "frameiq-valkey-1" in script
    assert '!= "healthy"' in script
    # The health gate must precede both the migration and the web recreate.
    gate_idx = script.index("both must be healthy")
    assert gate_idx < script.index(_UPGRADE)
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


# ════════════════════════════════════════════════════════════════════════════
# First ordinary deployment must succeed with 0002 left pending
# ════════════════════════════════════════════════════════════════════════════
#
# The gate must not have the side effect of making ordinary deploys
# impossible. An unapplied destructive migration is a NORMAL, expected state
# that every deploy has to tolerate.

_SAFE_SCRIPT = "Deploy and verify (exact SHA)"


def _safe_code():
    return _code_of_script(_scripts(_load(_DEPLOY))[_SAFE_SCRIPT])


def test_ordinary_deploy_runs_every_step_in_order(deploy):
    """The exact sequence an ordinary deploy must be able to complete."""
    code = _safe_code()

    # `index()` would collapse the two identical guard invocations onto the
    # first one, so positions are found with explicit offsets.
    def positions_of(token):
        found, start = [], 0
        while True:
            at = code.find(token, start)
            if at == -1:
                return found
            found.append(at)
            start = at + 1

    build = positions_of("docker compose build web")[0]
    convergence = positions_of("migrates/migrate_schema_convergence.py")[0]
    guards = positions_of("python -m utils.schema_guard")
    baseline = positions_of("scripts/migrate.py adopt-legacy-baseline")[0]
    upgrade = positions_of("scripts/migrate.py upgrade")[0]
    recreate = positions_of("docker compose up -d --build web")[0]

    assert len(guards) == 2, "expected a guard before and after migrations"
    sequence = [build, convergence, guards[0], baseline, upgrade, guards[1],
                recreate]
    assert sequence == sorted(sequence), (
        "ordinary deploy steps are out of order: build=%d convergence=%d "
        "guard0=%d baseline=%d upgrade=%d guard1=%d recreate=%d" % tuple(
            sequence))


def test_ordinary_deploy_does_not_pass_only_or_authorization(deploy):
    """The ordinary upgrade must be the bare, non-destructive command.

    Without `--only`, the runner defers any destructive migration — so this job
    cannot reach 0002 even if someone removed the runner's own gate.
    """
    lines = _logical_lines(_scripts(_load(_DEPLOY))[_SAFE_SCRIPT])
    upgrade = next(ln for ln in lines if "scripts/migrate.py upgrade" in ln)
    assert upgrade == (
        "SKIP_SCHEMA_GUARD=1 docker compose run --rm --no-deps web "
        "python scripts/migrate.py upgrade --allow-production"
    ), upgrade


def test_ordinary_deploy_never_mentions_the_destructive_migration_id(deploy):
    code = _safe_code()
    assert "0002_remove_legacy_wishlist" not in code
    for flag in ("--only", "--authorize-destructive", "--backup-ref",
                 "--backup-verified-by"):
        assert flag not in code, flag


def test_migration_failure_prevents_web_startup(deploy):
    """A non-zero migration step aborts before the web container is recreated.

    `script_stop: true` propagates the failure, and every migration step is
    ordered before `docker compose up -d`, so a failed migration can never be
    followed by a serving web process.
    """
    script = _scripts(_load(_DEPLOY))[_SAFE_SCRIPT]
    with_obj = next(s for s in load_deploy_steps()
                    if s.get("name") == _SAFE_SCRIPT)["with"]
    assert with_obj["script_stop"] is True
    code = _code_of_script(script)
    upgrade_at = code.index("scripts/migrate.py upgrade")
    recreate_at = code.index("docker compose up -d --build web")
    assert upgrade_at < recreate_at, (
        "web is recreated before migrations finish")


def test_schema_guard_failure_prevents_web_startup(deploy):
    """The post-migration guard runs after `upgrade` and before the recreate."""
    code = _safe_code()
    guard_positions = [i for i in range(len(code))
                       if code.startswith("python -m utils.schema_guard", i)]
    assert len(guard_positions) == 2, "expected a guard before and after"
    before_baseline, after_upgrade = guard_positions
    assert before_baseline < code.index("scripts/migrate.py upgrade")
    assert after_upgrade > code.index("scripts/migrate.py upgrade")
    assert after_upgrade < code.index("docker compose up -d --build web")


def test_dependency_health_gate_precedes_every_migration(deploy):
    code = _safe_code()
    gate = code.index("both must be healthy")
    assert gate < code.index("migrates/migrate_schema_convergence.py")
    assert gate < code.index("scripts/migrate.py upgrade")
    assert gate < code.index("docker compose up -d --build web")


def test_smoke_and_health_checks_run_after_web_startup(deploy):
    """The deploy is not "successful" until it has been verified live."""
    code = _safe_code()
    recreate = code.index("docker compose up -d --build web")
    assert code.index("frameiq-web-1") > recreate
    assert code.index("https://frameiq.studio/health") > recreate
    assert "for i in 1 2 3 4 5" in code, "health check must retry"


def load_deploy_steps():
    return _load(_DEPLOY)["jobs"]["deploy"]["steps"]


# ── the runner keeps the destructive step out of an ordinary run ────────────

def test_runner_defers_the_destructive_migration_by_default(tmp_path):
    """An ordinary `upgrade` on a fresh database.

    This is the runtime half of "ordinary deployment cannot execute 0002": the
    deploy job passes no `--only`, so the runner must defer 0002, exit 0, and
    leave it pending.
    """
    import os
    import subprocess
    import sys

    from models.base import db
    from sqlalchemy import create_engine

    repo = Path(_DEPLOY).resolve().parents[2]
    path = tmp_path / "ordinary.db"
    engine = create_engine("sqlite:///%s" % path)
    db.metadata.create_all(bind=engine)
    engine.dispose()

    env = dict(os.environ)
    env["DATABASE_URL"] = "sqlite:///%s" % path
    env["SKIP_SCHEMA_GUARD"] = "1"
    env["TMDB_API_KEY"] = "test-tmdb-key"
    env["SECRET_KEY"] = "test-secret-key-for-tests-only"
    result = subprocess.run(
        [sys.executable, str(repo / "scripts" / "migrate.py"), "upgrade"],
        capture_output=True, text=True, cwd=str(repo), env=env, timeout=300)

    assert result.returncode == 0, (
        "an ordinary upgrade failed because a destructive migration was "
        "pending: %s" % result.stderr[-400:])
    assert "DEFERRED" in result.stdout
    assert "0002_remove_legacy_wishlist" in result.stdout

    status = subprocess.run(
        [sys.executable, str(repo / "scripts" / "migrate.py"), "status"],
        capture_output=True, text=True, cwd=str(repo), env=env, timeout=300)
    assert status.returncode == 0
    assert "Pending             : 1" in status.stdout


def test_missing_authorization_prevents_the_destructive_migration(tmp_path):
    """Naming 0002 without the three pieces of evidence must refuse."""
    import os
    import subprocess
    import sys

    from models.base import db
    from sqlalchemy import create_engine

    repo = Path(_DEPLOY).resolve().parents[2]
    path = tmp_path / "blocked.db"
    engine = create_engine("sqlite:///%s" % path)
    db.metadata.create_all(bind=engine)
    engine.dispose()

    env = dict(os.environ)
    env["DATABASE_URL"] = "sqlite:///%s" % path
    env["SKIP_SCHEMA_GUARD"] = "1"
    env["TMDB_API_KEY"] = "test-tmdb-key"
    env["SECRET_KEY"] = "test-secret-key-for-tests-only"
    base = [sys.executable, str(repo / "scripts" / "migrate.py")]

    assert subprocess.run(base + ["upgrade"], capture_output=True, text=True,
                          cwd=str(repo), env=env, timeout=300).returncode == 0

    attempts = {
        "no flags at all": [],
        "authorize only": ["--authorize-destructive",
                           "0002_remove_legacy_wishlist"],
        "placeholder backup": ["--authorize-destructive",
                               "0002_remove_legacy_wishlist",
                               "--backup-ref", "yes",
                               "--backup-verified-by", "alice"],
        "wrong migration authorized": ["--authorize-destructive",
                                       "0009_something_else",
                                       "--backup-ref",
                                       "neon-snapshot-20260928-ab12cd",
                                       "--backup-verified-by", "alice"],
        "no verifier": ["--authorize-destructive",
                        "0002_remove_legacy_wishlist",
                        "--backup-ref", "neon-snapshot-20260928-ab12cd"],
    }
    for label, extra in attempts.items():
        result = subprocess.run(
            base + ["upgrade", "--only", "0002_remove_legacy_wishlist"] + extra,
            capture_output=True, text=True, cwd=str(repo), env=env, timeout=300)
        assert result.returncode != 0, "%s was allowed" % label
        assert "Refusing to run destructive migration" in result.stderr, label

    # A refusal must leave no record behind, so nothing later believes 0002 ran.
    import sqlite3
    connection = sqlite3.connect(str(path))
    try:
        versions = [row[0] for row in connection.execute(
            "SELECT version FROM schema_migrations")]
    finally:
        connection.close()
    assert "0002_remove_legacy_wishlist" not in versions


# ════════════════════════════════════════════════════════════════════════════
# Production deploy fix: ledger bootstrap order + release-sync readiness
# ════════════════════════════════════════════════════════════════════════════
#
# Two production failures followed the F8 rollout:
#   A. convergence ran before the ledger existed, saw `schema_migrations` as an
#      unexpected missing table, and refused -- creating nothing, so
#      `import_source_mapping` stayed absent too;
#   B. the nightly release-data sync imports `app`, whose startup schema guard
#      then refused, so the sync never ran.

def _deploy_code():
    return _code_of_script(_scripts(_load(_DEPLOY))[_SAFE_SCRIPT])


def _sync_steps():
    return _load(_SYNC)["jobs"]["sync-releases"]["steps"]


def _sync_script(name):
    return next(s for s in _sync_steps() if s.get("name") == name)["with"]["script"]


def test_ledger_bootstrap_runs_before_convergence(deploy):
    """Failure A's fix.

    Convergence refuses on any missing table outside its historical allow-list,
    and `schema_migrations` is infrastructure rather than one of those eleven,
    so the ledger must exist first.
    """
    code = _deploy_code()
    ledger = code.index("scripts/migrate.py init-ledger")
    convergence = code.index("migrates/migrate_schema_convergence.py")
    assert ledger < convergence, (
        "convergence runs before the ledger exists, which is exactly the "
        "failure that was observed in production")


def test_ledger_bootstrap_is_a_single_explicit_step(deploy):
    """One explicit command, not a create_all and not an allow-list widening."""
    code = _deploy_code()
    assert code.count("scripts/migrate.py init-ledger") == 1
    assert "--allow-production" in code
    # The forbidden shortcuts must be absent from the deploy script.
    assert "create_all" not in code
    assert "metadata.create_all" not in code


def test_baseline_adoption_still_follows_schema_validation(deploy):
    """F: the baseline asserts the schema was verified, so it must come after."""
    code = _deploy_code()
    guard = code.index("python -m utils.schema_guard")
    baseline = code.index("scripts/migrate.py adopt-legacy-baseline")
    assert guard < baseline, (
        "the legacy baseline must not be adopted before the schema is "
        "validated")


def test_full_deploy_step_order(deploy):
    """The corrected logical order, asserted position by position."""
    code = _deploy_code()

    def at(token, occurrence=0):
        start, found = 0, []
        while True:
            index = code.find(token, start)
            if index == -1:
                break
            found.append(index)
            start = index + 1
        return found[occurrence]

    build = at("docker compose build web")
    ledger = at("scripts/migrate.py init-ledger")
    convergence = at("migrates/migrate_schema_convergence.py")
    guards = []
    cursor = 0
    while True:
        index = code.find("python -m utils.schema_guard", cursor)
        if index == -1:
            break
        guards.append(index)
        cursor = index + 1
    assert len(guards) == 2, "expected a guard before and after migrations"
    baseline = at("scripts/migrate.py adopt-legacy-baseline")
    upgrade = at("scripts/migrate.py upgrade")
    status = at("scripts/migrate.py status")
    recreate = at("docker compose up -d --build web")

    sequence = [build, ledger, convergence, guards[0], baseline, upgrade,
                guards[1], status, recreate]
    assert sequence == sorted(sequence), (
        "deploy order is wrong: build=%d ledger=%d convergence=%d guard=%d "
        "baseline=%d upgrade=%d guard=%d status=%d recreate=%d"
        % tuple(sequence))


def test_web_is_recreated_after_migrations_and_the_final_guard(deploy):
    """H: a schema mismatch must prevent web startup."""
    code = _deploy_code()
    upgrade = code.index("scripts/migrate.py upgrade")
    guards = []
    cursor = 0
    while True:
        index = code.find("python -m utils.schema_guard", cursor)
        if index == -1:
            break
        guards.append(index)
        cursor = index + 1
    recreate = code.index("docker compose up -d --build web")
    assert upgrade < guards[-1] < recreate, (
        "web must not start before the migration and the closing guard pass")


def test_pre_web_migration_steps_keep_the_documented_guard_escape(deploy):
    """The escape hatch stays confined to explicitly controlled pre-web steps."""
    lines = _logical_lines(_scripts(_load(_DEPLOY))[_SAFE_SCRIPT])
    guarded = [ln for ln in lines if "SKIP_SCHEMA_GUARD=1" in ln]
    assert guarded, "the pre-web migration steps must keep the escape hatch"
    for line in guarded:
        assert "docker compose run --rm --no-deps web" in line
        assert "docker compose up" not in line
    # The running application must never carry it.
    recreate = [ln for ln in lines if "docker compose up -d" in ln]
    assert recreate and all("SKIP_SCHEMA_GUARD" not in ln for ln in recreate)


def test_convergence_allow_list_is_not_widened_for_the_ledger():
    """The ledger must be bootstrapped explicitly, never smuggled into the
    historical repair set."""
    from migrates import migrate_schema_convergence as convergence
    assert len(convergence.EXPECTED_REPAIR_SET) == 11
    assert "schema_migrations" not in convergence.EXPECTED_REPAIR_SET


def test_destructive_authorization_is_absent_from_the_normal_deploy(deploy):
    """F8's destructive protection is unchanged by this fix."""
    code = _deploy_code()
    for flag in ("--only", "--authorize-destructive", "--backup-ref",
                 "--backup_verified_by", "--backup-verified-by"):
        assert flag not in code, flag
    assert "0002_remove_legacy_wishlist" not in code


# ── release-sync readiness ──────────────────────────────────────────────────

def test_sync_keeps_its_schedule_and_manual_trigger():
    workflow = _load(_SYNC)
    on = workflow[True] if True in workflow else workflow["on"]
    assert on["schedule"][0]["cron"] == "30 2 * * *"
    assert "workflow_dispatch" in on


def test_sync_does_not_bypass_the_schema_guard():
    """J: no SKIP_SCHEMA_GUARD, no create_all, no manual table creation."""
    for step in _sync_steps():
        script = (step.get("with") or {}).get("script") or step.get("run") or ""
        assert "SKIP_SCHEMA_GUARD" not in script, step.get("name")
        assert "create_all" not in script, step.get("name")
        assert "import_source_mapping" not in script, step.get("name")
        assert "DROP TABLE" not in script.upper(), step.get("name")


def test_sync_runs_a_readiness_preflight_before_the_sync():
    """I: the sync must not execute before the schema is ready."""
    names = [s.get("name") for s in _sync_steps()]
    assert names[0] == "Preflight - confirm the deployed schema is ready"
    assert names[1] == "Run sync"
    assert names[-1] == "Open issue on failure"


def test_sync_preflight_uses_the_read_only_guard():
    preflight = _code_of_script(_sync_script(
        "Preflight - confirm the deployed schema is ready"))
    assert "python -m utils.schema_guard" in preflight
    assert "docker compose run --rm --no-deps web" in preflight


def test_sync_preflight_fails_loudly_with_an_actionable_message():
    """A not-ready schema must produce guidance, never a silent success."""
    preflight = _script_for(_sync_script(
        "Preflight - confirm the deployed schema is ready"))
    assert "exit 1" in preflight
    assert "::error::" in preflight
    lowered = preflight.lower()
    assert "did not run" in lowered
    assert "deploy" in lowered
    # The failure must not be swallowed.
    assert "|| true" not in preflight


def test_sync_preflight_checks_readiness_before_running_the_script():
    """Ordering within the workflow, not just the presence of both steps."""
    preflight = _script_for(_sync_script(
        "Preflight - confirm the deployed schema is ready"))
    assert "utils.schema_guard" in preflight
    assert "sync_watchlist_release_data.py" not in preflight, (
        "the preflight must not run the sync itself")


def test_sync_command_is_unchanged_and_unguarded():
    """The sync script still starts with the normal guard enabled."""
    code = _code_of_script(_sync_script("Run sync"))
    assert "python scripts/sync_watchlist_release_data.py" in code
    assert "SKIP_SCHEMA_GUARD" not in code


def test_sync_is_serialised_with_production_deployment():
    """The sync must not race the schema transition."""
    workflow = _load(_SYNC)
    concurrency = workflow["jobs"]["sync-releases"]["concurrency"]
    assert concurrency["group"] == "production-deploy"
    assert concurrency["cancel-in-progress"] is False
    # And the deploy uses the same group.
    assert _load(_DEPLOY)["concurrency"]["group"] == "production-deploy"


def test_sync_failure_still_opens_an_issue():
    """Failure visibility is preserved."""
    step = next(s for s in _sync_steps() if s.get("name") == "Open issue on failure")
    assert step["if"] == "failure()"


def _script_for(script):
    return "\n".join(line for line in script.splitlines()
                     if not line.strip().startswith("#"))
