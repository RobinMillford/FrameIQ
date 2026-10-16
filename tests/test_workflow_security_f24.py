"""GitHub Actions supply-chain hardening (Feature 24).

Two classes of defect are pinned here.

**Mutable action references.** GitHub resolves ``uses: owner/action@v4`` at
run time, so whoever controls that tag controls code that executes with this
repository's secrets and a push-capable ``GITHUB_TOKEN``. Every external
reference is now pinned to a 40-character commit SHA with the human-readable
release recorded in a trailing comment. The assertions below are structure
aware -- they read the SHA out of parsed YAML rather than searching for text --
so a comment, a reordered key, or a re-indented step cannot satisfy them, and
they discover workflows by glob so a workflow added later is covered
automatically instead of inheriting an exemption.

**Browser suites that skip in CI.** Seven suites begin with a Playwright
driver import that skips when the package is absent. That is right for a
developer and wrong for CI, where "skipped" and "passed" must not collapse into
the same green build: it would mean stored-XSS rendering, the CSRF header the
chat widget actually sends, keyboard-focus retention and the director detail
page were never exercised. ``tests/browser_requirement.py`` now raises instead
of skipping when ``FRAMEIQ_REQUIRE_BROWSER=1``, which ``ci-cd.yml`` sets.

No runner is available, so the practical surface is configuration correctness
plus a direct functional test of the guard itself.
"""
import re
import sys
from pathlib import Path

import pytest
import yaml

WORKFLOW_DIR = Path(".github/workflows")
CI = WORKFLOW_DIR / "ci-cd.yml"
DEPLOY = WORKFLOW_DIR / "deploy.yml"

# ── Verified pins ────────────────────────────────────────────────────────────
#
# Each SHA was resolved from the action's canonical repository through the
# unauthenticated GitHub REST API and confirmed two independent ways:
#   1. /repos/{repo}/commits/{sha} round-trips and echoes the same SHA
#   2. /repos/{repo}/git/ref/tags/{version} dereferences to that same SHA
#      (following one extra hop for annotated tags)
#
# The version comment is the release the floating tag resolved to at pin time.
# Pinning preserves the version that was already in use; it is not an upgrade.
VERIFIED_PINS = {
    "appleboy/ssh-action": ("029f5b4aeeeb58fdfe1410a5d17f967dacf36262", "v1.0.3"),
    "actions/github-script": ("f28e40c7f34bde8b3046d885e986cb6290c5673b", "v7.1.0"),
    "actions/checkout": ("11d5960a326750d5838078e36cf38b85af677262", "v4.4.0"),
    "actions/setup-python": ("a26af69be951a213d495a4c3e4e4022e16d87065", "v5.6.0"),
}

#: Total external ``uses:`` references across every workflow. Guards against a
#: pin being "fixed" by deleting the step that needed it.
EXPECTED_TOTAL_USES = 15

SHA_RE = re.compile(r"^[0-9a-f]{40}$")

#: Local refs (``./...``) are repo-owned code under this repository's control.
EXTERNAL_REF_RE = re.compile(r"^(?P<action>[\w.-]+/[\w.-]+)@(?P<ref>.+)$")

# Placeholders that must never survive a real commit.
PLACEHOLDER_SHAS = (
    "0000000000000000000000000000000000000000",
    "1111111111111111111111111111111111111111",
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "ffffffffffffffffffffffffffffffffffffffff",
    "deadbeef",
)


def _workflow_paths():
    paths = sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml"))
    assert paths, f"no workflows found under {WORKFLOW_DIR}"
    return paths


def _load(path):
    """Parse a workflow, normalising the YAML 1.1 ``on`` -> True quirk."""
    parsed = yaml.safe_load(path.read_text())
    if isinstance(parsed, dict) and True in parsed:
        parsed["on"] = parsed.pop(True)
    return parsed


@pytest.fixture(scope="module")
def workflows():
    return {p.name: _load(p) for p in _workflow_paths()}


@pytest.fixture(scope="module")
def workflows_raw():
    return {p.name: p.read_text() for p in _workflow_paths()}


@pytest.fixture(scope="module")
def ci():
    return _load(CI)


@pytest.fixture(scope="module")
def ci_raw():
    return CI.read_text()


@pytest.fixture(scope="module")
def deploy():
    return _load(DEPLOY)


@pytest.fixture(scope="module")
def deploy_raw():
    return DEPLOY.read_text()


def _steps(wf):
    """Yield ``(job_name, step)`` for every step in every job."""
    for job_name, job in (wf.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            yield job_name, step


def _external_uses(wf):
    """Yield ``(job_name, action, ref)`` for every non-local ``uses:``."""
    for job_name, step in _steps(wf):
        uses = step.get("uses")
        if not uses:
            continue
        match = EXTERNAL_REF_RE.match(uses)
        if not match:
            continue  # local path, docker ref, or malformed
        yield job_name, match.group("action"), match.group("ref")


# ═══════════════════════════════════════════════════════════════════════════
# 1. Every external action reference is pinned to an immutable commit
# ═══════════════════════════════════════════════════════════════════════════

def test_every_external_action_is_pinned_to_a_full_commit_sha(workflows):
    """No ``uses:`` may name a tag, a branch, or a short SHA."""
    unpinned = []
    for name, wf in workflows.items():
        for job_name, action, ref in _external_uses(wf):
            if SHA_RE.match(ref):
                continue
            unpinned.append(f"{name}:{job_name} {action}@{ref}")
    assert not unpinned, (
        "mutable action references remain; each must be a 40-char commit SHA:\n  "
        + "\n  ".join(unpinned))


def test_no_placeholder_or_truncated_sha_survives(workflows_raw):
    """A fabricated or shortened SHA is worse than an obvious tag."""
    for name, raw in workflows_raw.items():
        for shas in re.findall(r"uses:[^\n]*?([0-9a-fA-F]{7,40})\b", raw):
            lowered = shas.lower()
            assert SHA_RE.match(lowered), (
                f"{name}: action reference has a truncated SHA: {shas}")
            assert lowered not in PLACEHOLDER_SHAS, (
                f"{name}: placeholder SHA present: {shas}")


def test_each_action_pins_the_verified_sha(workflows):
    """The pin must be the commit that was actually verified, not merely hex."""
    for name, wf in workflows.items():
        for job_name, action, ref in _external_uses(wf):
            assert action in VERIFIED_PINS, (
                f"{name}:{job_name} uses unvetted action {action!r}; it must be "
                f"resolved against its repository and added to VERIFIED_PINS")
            expected, _version = VERIFIED_PINS[action]
            assert ref == expected, (
                f"{name}:{job_name} pins {action}@{ref}, expected {expected}")


def test_each_pin_records_its_release_in_a_comment(workflows_raw):
    """YAML drops comments, so provenance is only observable in the source.

    Without it a pin is a bare SHA that a future maintainer cannot map back to a
    release, and every "bump this action" change becomes archaeology.
    """
    for action, (sha, version) in VERIFIED_PINS.items():
        needle = f"uses: {action}@{sha} # {version}"
        present = [name for name, raw in workflows_raw.items() if needle in raw]
        assert present, f"no workflow records {needle!r} with its version comment"


def test_pinning_did_not_change_the_versions_in_use(workflows_raw):
    """F24 pins what was already deployed; it must not smuggle in an upgrade."""
    for action, (sha, version) in VERIFIED_PINS.items():
        # Any reference to this action, wherever it appears, carries the version.
        for name, raw in workflows_raw.items():
            for line in raw.splitlines():
                if f"uses: {action}@" in line:
                    assert line.rstrip().endswith(f"# {version}"), (
                        f"{name}: {line.strip()!r} should end with '# {version}'")


def test_total_external_uses_matches_the_audit(workflows):
    total = sum(1 for wf in workflows.values() for _ in _external_uses(wf))
    assert total == EXPECTED_TOTAL_USES, (
        f"expected {EXPECTED_TOTAL_USES} external action references across the "
        f"workflows, found {total}")


def test_no_reusable_workflow_or_local_composite_was_left_unpinned():
    """A ``workflow_call`` or local action is still an execution surface."""
    for path in _workflow_paths():
        parsed = _load(path)
        triggers = parsed.get("on") or {}
        assert "workflow_call" not in (triggers or {}), (
            f"{path.name} declares workflow_call; F24 must pin that action too")
    assert not Path(".github/actions").exists(), (
        "local composite actions exist; they need the same supply-chain review")


# ═══════════════════════════════════════════════════════════════════════════
# 2. Least-privilege token permissions
# ═══════════════════════════════════════════════════════════════════════════

def test_every_workflow_declares_explicit_permissions(workflows):
    """No workflow may inherit the repository/org default token scope."""
    missing = [name for name, wf in workflows.items() if wf.get("permissions") is None]
    assert not missing, (
        "these workflows declare no permissions and inherit the default "
        f"GITHUB_TOKEN scope: {missing}")


def test_ci_grants_only_read_access_to_contents(ci):
    """ci-cd.yml only checks out code and runs tests; it needs nothing else."""
    assert ci["permissions"] == {"contents": "read"}, (
        "ci-cd.yml must grant exactly 'contents: read' at workflow level; "
        f"found {ci['permissions']}")


def test_ci_jobs_do_not_widen_permissions(ci):
    """A job-level block overrides the workflow default, so it must not exceed it."""
    for job_name, job in (ci.get("jobs") or {}).items():
        job_perms = job.get("permissions")
        if job_perms is None:
            continue
        for scope, level in job_perms.items():
            assert level == "read", (
                f"ci-cd.yml job {job_name!r} widens {scope} to {level!r}; a test "
                "job has no legitimate reason to write")


def test_no_workflow_grants_a_write_scope_without_using_it(workflows):
    """``issues: write`` is fine, but only where issues are actually created."""
    for name, wf in workflows.items():
        perms = wf.get("permissions") or {}
        writes = {s for s, lvl in perms.items() if lvl == "write"}
        for scope in sorted(writes):
            assert scope == "issues", (
                f"{name} grants {scope}: write -- only issues:write is justified "
                "here (the sync workflows open sync-failure issues)")
            assert "issues.create" in _dump(wf), (
                f"{name} grants issues: write but never calls issues.create")


def _dump(wf):
    return yaml.safe_dump(wf)


def test_no_workflow_uses_pull_request_target(workflows):
    """``pull_request_target`` runs with a write token against fork code."""
    for name, wf in workflows.items():
        triggers = wf.get("on") or {}
        for trigger in triggers:
            assert trigger != "pull_request_target", (
                f"{name} triggers on pull_request_target, which exposes a "
                "write-capable token to code from a fork")


def test_secrets_are_never_interpolated_into_shell(workflows):
    """A secret inside ``run:`` is executed as text; keep it an action input."""
    secret_expr = re.compile(r"\$\{\{\s*secrets\.[A-Z_]+\s*\}\}")
    offenders = []
    for name, wf in workflows.items():
        for job_name, step in _steps(wf):
            run = step.get("run")
            if run and secret_expr.search(run):
                offenders.append(f"{name}:{job_name}: {step.get('name')}")
    assert not offenders, (
        "secret interpolated into a shell script (masking does not prevent "
        f"injection): {offenders}")


# ═══════════════════════════════════════════════════════════════════════════
# 3. Required browser suites cannot silently skip
# ═══════════════════════════════════════════════════════════════════════════

REQUIRED_BROWSER_SUITES = [
    "test_director_detail_f19.py",
    "test_edit_profile_picker_browser.py",
    "test_explore_browser.py",
    "test_f23_2_chat_csrf_browser.py",
    "test_f23_csrf_browser.py",
    # F48: the list-cover collage is asserted on rendered geometry, so it runs
    # in a browser and must be enforced rather than skippable like the rest.
    "test_list_cover_browser_f48.py",
    "test_taste_dna_actor_f14_browser.py",
    "test_xss_f21_browser.py",
    "test_xss_f35_browser.py",
]


def _browser_suites_in_ci():
    """The suite list the CI enforcement step actually names."""
    match = re.search(r"REQUIRED_BROWSER_SUITES=\(([^)]*)\)", CI.read_text())
    assert match, "ci-cd.yml no longer declares REQUIRED_BROWSER_SUITES"
    return re.findall(r"tests/(\S+\.py)", match.group(1))


def test_ci_requires_the_browser_dependency_rather_than_skipping_it(ci):
    """The guard must be armed for the whole test job, not one step."""
    env = (ci["jobs"]["test"].get("env") or {})
    assert env.get("FRAMEIQ_REQUIRE_BROWSER") == "1", (
        "the ci-cd.yml test job must set FRAMEIQ_REQUIRE_BROWSER=1, otherwise "
        "the browser suites skip silently and the job still goes green")


def test_no_browser_suite_still_uses_a_bare_importorskip():
    """A suite left on the old pattern would bypass the guard entirely."""
    offenders = []
    for path in sorted(Path("tests").glob("test_*.py")):
        source = path.read_text()
        if re.search(r"importorskip\(\s*['\"]playwright", source):
            offenders.append(path.name)
    assert not offenders, (
        "these suites still skip on a bare pytest.importorskip and can go dark "
        f"in CI: {offenders}")


def test_every_required_browser_suite_uses_the_guard():
    for name in REQUIRED_BROWSER_SUITES:
        source = (Path("tests") / name).read_text()
        assert "require_playwright(" in source, (
            f"{name} does not call require_playwright(); it must opt into the "
            "CI failure contract explicitly")
        assert "sync_playwright = require_playwright" in source, (
            f"{name} must bind sync_playwright from the guard so the module "
            "imports before any test runs")


def test_enforcement_list_covers_every_suite_that_uses_the_guard():
    """Closes the door on a new browser suite that nobody made required."""
    using_guard = {
        p.name for p in sorted(Path("tests").glob("test_*.py"))
        if "sync_playwright = require_playwright(" in p.read_text()
        # This module names the binding in order to assert on it; it is the
        # audit, not an audited suite.
        and p.name != Path(__file__).name
    }
    enforced = set(_browser_suites_in_ci())
    assert enforced == set(REQUIRED_BROWSER_SUITES), (
        "ci-cd.yml REQUIRED_BROWSER_SUITES drifted from the enforced set: "
        f"{sorted(enforced)}")
    assert using_guard == set(REQUIRED_BROWSER_SUITES), (
        "suites calling require_playwright() but not enforced by CI: "
        f"{sorted(using_guard - set(REQUIRED_BROWSER_SUITES))}; "
        f"enforced but no longer using the guard: "
        f"{sorted(set(REQUIRED_BROWSER_SUITES) - using_guard)}")


def test_enforcement_step_fails_on_zero_passing_tests(ci_raw):
    """The CI check must assert coverage ran, not merely that files exist."""
    assert "contributed 0 passing tests" in ci_raw, (
        "ci-cd.yml lost the zero-passing-tests failure for browser suites")
    assert "REQUIRED_BROWSER_SUITES" in ci_raw
    for suite in REQUIRED_BROWSER_SUITES:
        assert suite in ci_raw, f"{suite} is not named by the enforcement step"


def test_unrelated_importorskip_use_is_untouched():
    """``test_shared_limiter`` needs lupa, not a browser, and stays skippable."""
    source = Path("tests/test_shared_limiter.py").read_text()
    assert "importorskip" in source
    assert "require_playwright" not in source


# ── Functional test of the guard itself ─────────────────────────────────────

@pytest.fixture
def playwright_absent(monkeypatch):
    """Make ``from playwright.sync_api import ...`` fail for this test only.

    ``None`` in ``sys.modules`` makes the import raise, and monkeypatch restores
    the real entry afterwards, so no other test in the session is affected.
    """
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    monkeypatch.delitem(sys.modules, "playwright", raising=False)
    from tests.browser_requirement import require_playwright  # noqa: PLC0415
    return require_playwright


def test_guard_skips_locally_when_playwright_is_missing(
        playwright_absent, monkeypatch):
    """A developer without browser extras keeps working -- unchanged behaviour."""
    monkeypatch.delenv("FRAMEIQ_REQUIRE_BROWSER", raising=False)
    with pytest.raises(pytest.skip.Exception) as excinfo:
        playwright_absent("example_suite.py")
    message = str(excinfo.value)
    assert "example_suite.py" in message, "the skip must name the suite"
    assert "install" in message.lower(), (
        "the skip must tell the developer how to fix it")


def test_guard_raises_in_ci_instead_of_skipping(
        playwright_absent, monkeypatch):
    """CI must never be able to go green with the browser coverage absent."""
    monkeypatch.setenv("FRAMEIQ_REQUIRE_BROWSER", "1")
    with pytest.raises(RuntimeError) as excinfo:
        playwright_absent("example_suite.py")
    message = str(excinfo.value)
    assert "REQUIRED" in message
    assert "example_suite.py" in message, "the failure must name the suite"
    assert "playwright install" in message, (
        "the failure must state the remedy, not just the symptom")


def test_guard_is_a_no_op_when_playwright_is_present(monkeypatch):
    from tests.browser_requirement import browser_required, require_playwright
    monkeypatch.setenv("FRAMEIQ_REQUIRE_BROWSER", "1")
    assert browser_required() is True
    assert require_playwright("example_suite.py") is not None


def test_guard_only_honours_the_exact_ci_value(monkeypatch):
    """'0', 'true' and 'yes' must not arm the guard and break local runs."""
    from tests.browser_requirement import browser_required
    for value in ("0", "", "true", "yes", "on", " 0 "):
        monkeypatch.setenv("FRAMEIQ_REQUIRE_BROWSER", value)
        assert browser_required() is False, f"{value!r} must not arm the guard"
    monkeypatch.setenv("FRAMEIQ_REQUIRE_BROWSER", " 1 ")
    assert browser_required() is True


def test_guard_does_not_convert_per_test_skips(monkeypatch):
    """Only the driver import is gated; a legitimate pytest.skip stays a skip."""
    source = Path("tests/browser_requirement.py").read_text()
    assert "allow_module_level=True" in source
    assert "FRAMEIQ_REQUIRE_BROWSER" in source
    for name in REQUIRED_BROWSER_SUITES:
        suite = (Path("tests") / name).read_text()
        assert "pytest.skip(" not in suite, (
            f"{name} has a per-test skip; the dependency guard must not be "
            "widened to cover it without a deliberate review")


# ═══════════════════════════════════════════════════════════════════════════
# 4. Deployment semantics F24 must not have disturbed
# ═══════════════════════════════════════════════════════════════════════════

def test_deploy_environment_and_reviewer_gate_are_intact(deploy):
    """The destructive path's human gate must survive F24 untouched.

    The regular ``deploy`` job deliberately has no ``environment:`` -- routine
    deploys are authorised by the green-CI workflow_run gate and serialised by
    the ``production-deploy`` concurrency group. Human approval belongs only to
    ``production-destructive``, whose protection rule is additionally verified
    at run time by a step that fails closed when no reviewers are configured.
    Asserting the gate exists matters more here than asserting it is uniform:
    widening approval to routine deploys, or narrowing it away from the
    destructive path, are both silent safety regressions.
    """
    assert "environment" not in deploy["jobs"]["deploy"], (
        "the routine deploy job must not have gained an environment gate")

    destructive = deploy["jobs"]["destructive"]
    assert destructive["environment"]["name"] == "production-destructive"

    # Concurrency group serialising deploys: workflow-level, plus repeated on
    # the destructive job so it queues behind an in-flight deploy too.
    assert deploy["concurrency"]["group"] == "production-deploy"
    assert deploy["concurrency"]["cancel-in-progress"] is False
    assert destructive["concurrency"]["group"] == "production-deploy"


def test_destructive_environment_reviewer_check_is_still_present(deploy_raw):
    """Declaring the environment is necessary but not sufficient."""
    assert "required_reviewers" in deploy_raw, (
        "F24 removed the run-time check that the destructive environment "
        "actually has required reviewers configured")
    assert "no required reviewers" in deploy_raw
    assert "production-destructive" in deploy_raw


def test_deploy_still_verifies_the_exact_sha(ci_raw):
    assert "checkout" in ci_raw
    assert "fetch-depth: 0" in ci_raw, (
        "the whitespace gate compares against the parent commit, which a "
        "depth-1 checkout cannot resolve")


def test_deploy_migration_contract_is_unchanged(deploy_raw):
    for needle in ("--allow-production", "destructive_migration", "backup_ref",
                   "backup_verified_by"):
        assert needle in deploy_raw, f"F24 removed the {needle!r} migration gate"


def test_deploy_still_runs_under_workflow_run_from_ci(deploy):
    """deploy.yml is coupled to CI's success; that coupling is the whole gate."""
    triggers = deploy.get("on") or {}
    assert "workflow_run" in triggers
    assert triggers["workflow_run"]["workflows"] == ["CI"]


def test_ci_still_gates_deploy_by_requiring_success(deploy_raw):
    assert "conclusion" in deploy_raw
    assert re.search(r"success", deploy_raw)


# ═══════════════════════════════════════════════════════════════════════════
# 5. The workflows themselves remain valid
# ═══════════════════════════════════════════════════════════════════════════

def test_every_workflow_parses(workflows):
    assert len(workflows) == len(_workflow_paths())
    for name, wf in workflows.items():
        assert isinstance(wf, dict), f"{name} did not parse to a mapping"
        assert wf.get("jobs"), f"{name} declares no jobs"


def test_every_job_has_steps_and_a_runner(workflows):
    for name, wf in workflows.items():
        for job_name, job in (wf.get("jobs") or {}).items():
            assert job.get("runs-on"), f"{name}:{job_name} has no runs-on"
            assert job.get("steps"), f"{name}:{job_name} has no steps"