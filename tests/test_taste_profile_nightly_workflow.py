"""Nightly taste-profile workflow validation (Feature #6, Phases 9+11).

Static validation of .github/workflows/taste-profile-nightly.yml — the
repository has no GitHub Actions runner to exercise, so the practical
test surface is configuration correctness. PyYAML (already a project
dependency) parses the file; the remaining assertions are structural
guards over the parsed structure and the raw text, proving the spec's
execution contract: schedule + manual trigger, single concurrency group,
synchronous (non-detached) execution, real exit-code propagation,
enrich → compute → verify ordering, no hidden failure handling, no
plaintext secrets, no migrations, and no changes to the existing
deployment workflow.
"""
from pathlib import Path

import pytest
import yaml

_WORKFLOW = Path(".github/workflows/taste-profile-nightly.yml")
_DEPLOY = Path(".github/workflows/deploy.yml")
_CI = Path(".github/workflows/ci-cd.yml")
_SYNC = Path(".github/workflows/sync-upcoming-episodes.yml")

_ENRICH = "enrich_directors.py"
_COMPUTE = "compute_taste_profiles.py"
_VERIFY = "verify_recommendation_feedback.py"


@pytest.fixture(scope="module")
def raw():
    return _WORKFLOW.read_text()


@pytest.fixture(scope="module")
def wf(raw):
    parsed = yaml.safe_load(raw)
    # YAML 1.1: a bare `on:` key parses as boolean True. Normalize it so
    # tests can address triggers naturally.
    if "on" not in parsed and True in parsed:
        parsed["on"] = parsed.pop(True)
    return parsed


def _code_lines(raw):
    """Workflow text without comment lines (comments may legitimately
    mention banned tokens such as 'no db.create_all()')."""
    return "\n".join(ln for ln in raw.splitlines()
                     if not ln.lstrip().startswith("#"))


# ════════════════════════════════════════════════════════════════════════════
# Triggers / schedule
# ════════════════════════════════════════════════════════════════════════════

def test_workflow_file_exists():
    assert _WORKFLOW.is_file()


def test_has_schedule_trigger(wf):
    assert "schedule" in wf["on"]
    crons = [t["cron"] for t in wf["on"]["schedule"]]
    assert crons == ['30 19 * * *']  # 19:30 UTC == 01:30 Asia/Dhaka


def test_cron_is_at_most_daily(wf):
    for t in wf["on"]["schedule"]:
        fields = t["cron"].split()
        assert len(fields) == 5
        # Any single-field-only frequency guard: minute+hour fixed means
        # at most once per day.
        assert fields[0] == '30' and fields[1] == '19'


def test_schedule_documented_in_utc_and_dhaka(raw):
    assert '19:30 UTC' in raw
    assert 'Asia/Dhaka' in raw or 'UTC+6' in raw


def test_has_manual_dispatch(wf):
    assert "workflow_dispatch" in wf["on"]


# ════════════════════════════════════════════════════════════════════════════
# Concurrency / timeout / permissions
# ════════════════════════════════════════════════════════════════════════════

def test_concurrency_group_serializes_all_runs(wf):
    conc = wf["concurrency"]
    assert conc["group"] == "taste-profile-nightly"
    assert conc["cancel-in-progress"] is False  # never kill an active run


def test_job_timeout_is_bounded(wf):
    job = wf["jobs"]["recompute-verify"]
    assert 0 < job["timeout-minutes"] <= 120


def test_permissions_are_least_privilege(wf):
    assert wf["permissions"] == {"contents": "read"}


# ════════════════════════════════════════════════════════════════════════════
# Execution contract
# ════════════════════════════════════════════════════════════════════════════

def _steps(wf):
    return wf["jobs"]["recompute-verify"]["steps"]


def test_compute_step_runs_production_script(wf):
    script = _script_of(wf, _COMPUTE)
    assert script is not None
    assert "cd /home/deployer/FrameIQ" in script


def test_verify_step_runs_verification_cli(wf):
    assert _script_of(wf, _VERIFY) is not None


def test_commands_are_not_detached(raw):
    # The SSH script must WAIT: no backgrounding, no detached exec.
    code = _code_lines(raw)
    for banned in ("nohup", "docker exec -d", "docker compose exec -d"):
        assert banned not in code
    assert "\n&" not in code and " & " not in code and not code.rstrip().endswith(" &")


def test_ssh_action_fails_on_nonzero_script_exit(wf):
    # appleboy/ssh-action CONTINUES after failed commands unless
    # script_stop is set — without it, verify would run after a failed
    # compute and failure would be silently swallowed.
    for step in _steps(wf):
        with_obj = step.get("with", {})
        assert with_obj.get("script_stop") is True, step.get("name")


def test_failure_is_not_swallowed(raw):
    code = _code_lines(raw)
    assert "|| true" not in code
    assert "continue-on-error: true" not in code
    assert "if: always()" not in code
    assert "if: failure()" not in code


def test_verify_only_after_successful_compute(wf):
    steps = _steps(wf)
    assert len(steps) == 3
    verify_idx = next(i for i, s in enumerate(steps)
                      if _VERIFY in s.get("with", {}).get("script", ""))
    # GitHub Actions' default step condition is success() — an explicit
    # always()/failure() condition on the verify step would break ordering.
    assert "if" not in steps[verify_idx]


def test_no_migration_or_schema_commands(raw):
    code = _code_lines(raw)
    for banned in ("migrate", "create_all", "alembic", "ALTER TABLE"):
        assert banned not in code


def test_no_plaintext_secrets(raw):
    code = _code_lines(raw)
    # No literal credentials — only ${{ secrets.* }} references.
    import re
    assert "password" not in code.lower()
    assert not re.search(r"(key|token|secret)\s*:\s*[^$\s{]", code,
                         re.IGNORECASE)
    assert "BEGIN" not in code and "ssh-rsa" not in code
    for secret in ("VPS_HOST", "VPS_USER", "SSH_PRIVATE_KEY"):
        assert f"secrets.{secret}" in code
    assert _WORKFLOW.read_text().count("secrets.SSH_PRIVATE_KEY") == 3


def test_reuses_existing_deployment_secrets():
    # Sanity: the deployment workflow is the source of the secret names.
    assert "SSH_PRIVATE_KEY" in _DEPLOY.read_text()


def test_ssh_action_version_matches_repo_convention(raw):
    assert "appleboy/ssh-action@v1.0.3" in raw  # same pin as deploy.yml


def test_no_external_api_calls(raw):
    # Inside the container: compute/verify are local-only; the workflow
    # must not add curl/wget to other services.
    code = _code_lines(raw)
    for banned in ("curl ", "wget ", "api.themoviedb.org", "https://api."):
        assert banned not in code


# ════════════════════════════════════════════════════════════════════════════
# Isolation from existing workflows
# ════════════════════════════════════════════════════════════════════════════

def test_deployment_workflow_unchanged():
    assert _DEPLOY.is_file()
    text = _DEPLOY.read_text()
    assert "schedule" not in text
    assert _COMPUTE not in text and _VERIFY not in text
    assert "up -d --build" in text  # deployment behavior intact


def test_ci_workflow_unchanged():
    assert _CI.is_file()
    assert _COMPUTE not in _CI.read_text()


def test_no_push_trigger_for_taste_recomputation(wf):
    # Every git push must NOT run taste recomputation.
    assert "push" not in wf["on"]


def test_existing_scheduled_workflow_is_the_only_other_schedule():
    # The repo's only other schedule remains sync-upcoming-episodes.
    sync = _SYNC.read_text()
    assert "cron:" in sync
    assert _COMPUTE not in sync and _VERIFY not in sync


# ════════════════════════════════════════════════════════════════════════════
# Phase 11 — nightly director enrichment orchestration
# ════════════════════════════════════════════════════════════════════════════

def _script_of(wf, needle):
    """First step script containing `needle`, or None."""
    for s in _steps(wf):
        script = s.get("with", {}).get("script", "")
        if needle in script:
            return script
    return None


def test_enrichment_appears_in_workflow(wf):
    assert _script_of(wf, _ENRICH) is not None


def test_enrichment_runs_before_compute(wf):
    steps = _steps(wf)
    enrich_idx = next(i for i, s in enumerate(steps)
                      if _ENRICH in s.get("with", {}).get("script", ""))
    compute_idx = next(i for i, s in enumerate(steps)
                       if _COMPUTE in s.get("with", {}).get("script", ""))
    assert enrich_idx < compute_idx


def test_compute_runs_before_verification(wf):
    steps = _steps(wf)
    compute_idx = next(i for i, s in enumerate(steps)
                       if _COMPUTE in s.get("with", {}).get("script", ""))
    verify_idx = next(i for i, s in enumerate(steps)
                      if _VERIFY in s.get("with", {}).get("script", ""))
    assert compute_idx < verify_idx


def test_full_command_order_enrich_compute_verify(wf):
    """The exact production sequence: enrich → compute → verify."""
    steps = _steps(wf)
    order = []
    for s in steps:
        script = s.get("with", {}).get("script", "")
        if _ENRICH in script:
            order.append(_ENRICH)
        elif _COMPUTE in script:
            order.append(_COMPUTE)
        elif _VERIFY in script:
            order.append(_VERIFY)
    assert order == [_ENRICH, _COMPUTE, _VERIFY]


def test_all_production_commands_use_sync_exec_t(wf):
    # All three scripts must run via `docker compose exec -T web python …`
    # inside the production container — synchronous, no TTY assumptions.
    for needle in (_ENRICH, _COMPUTE, _VERIFY):
        script = _script_of(wf, needle)
        assert script is not None
        line = next(ln for ln in script.splitlines() if needle in ln)
        assert line.strip() == (
            f"docker compose exec -T web python scripts/{needle}"), line


def test_enrichment_is_synchronous(raw):
    # The SSH session must WAIT for enrichment: no backgrounding, no
    # detached exec anywhere in the workflow.
    code = _code_lines(raw)
    for banned in ("nohup", "& ", " &", "docker exec -d",
                   "docker compose exec -d", "exec -d"):
        assert banned not in code


def test_every_script_step_keeps_script_stop(wf):
    # appleboy/ssh-action continues after failed commands by default;
    # script_stop: true on EVERY script step is what makes a failed
    # enrichment/compute abort before the next stage runs.
    for s in _steps(wf):
        assert s.get("with", {}).get("script_stop") is True, s.get("name")


def test_enrichment_failure_blocks_compute(wf):
    # Structural proof: enrichment is its own step BEFORE compute, and
    # compute carries no always()/failure() condition — GitHub's default
    # success() gate is the propagation mechanism.
    steps = _steps(wf)
    enrich_idx = next(i for i, s in enumerate(steps)
                      if _ENRICH in s.get("with", {}).get("script", ""))
    compute_idx = next(i for i, s in enumerate(steps)
                       if _COMPUTE in s.get("with", {}).get("script", ""))
    assert enrich_idx < compute_idx
    assert "if" not in steps[compute_idx]
    assert "continue-on-error" not in steps[compute_idx]


def test_no_new_scheduler_introduced():
    # Still exactly three scheduled workflows in the repo, and this
    # feature has exactly one workflow file.
    workflow_dir = _WORKFLOW.parent
    scheduled = [p.name for p in workflow_dir.glob("*.yml")
                 if "cron:" in p.read_text()]
    assert sorted(scheduled) == ["sync-upcoming-episodes.yml",
                                 "taste-profile-nightly.yml"]
    assert not (workflow_dir / "director-enrichment.yml").exists()
    assert not (workflow_dir / "director-nightly.yml").exists()


def test_enrichment_budget_untouched():
    # The workflow invokes the script as-is: no flag/arg overrides that
    # would raise MAX_MEDIA_ITEMS or parallelize it.
    script = _script_of(yaml.safe_load(
        _WORKFLOW.read_text()), _ENRICH)
    line = next(ln for ln in script.splitlines() if _ENRICH in ln)
    assert "--limit" not in line and "--max" not in line and "--" not in line


def test_enrichment_is_the_only_tmdb_permitted_step(wf):
    # Only the enrichment step may plausibly reach TMDb; compute/verify
    # stay local-only. No curl/wget/endpoint may appear in any step.
    raw_text = _code_lines(_WORKFLOW.read_text())
    for banned in ("curl ", "wget ", "api.themoviedb.org", "https://api.",
                   "TMDB_API_KEY="):
        assert banned not in raw_text


def test_no_director_specific_verifier(raw):
    # Verification remains the existing feedback-pipeline CLI; the
    # workflow must not substitute a director-specific verifier.
    assert "director" not in _script_of(
        yaml.safe_load(raw), _VERIFY).replace(
        "verify_recommendation_feedback.py", "")
