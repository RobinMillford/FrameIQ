"""Regression tests for the four F28 findings closed in F30.

Each finding was reported against the F24-F29 change set:

1. The destructive gate counted ``len(reviewers)``, so a malformed entry such
   as ``null`` or ``{}`` satisfied "at least one reviewer".
2. The CI browser step extracted its count with ``grep | head | grep``, where a
   SIGPIPE plus the ``|| echo 0`` fallback could append to a partial capture
   and yield a multi-line value. ``[ "$v" -lt 1 ]`` returns 2 on such a value,
   which SKIPS the refusal body and passes the step.
3. ``_ALL_WORKFLOWS`` in ``test_deploy_workflow.py`` was a hard-coded triple,
   silently exempting two workflows that carry six of the twelve remote
   scripts.
4. ``persist-credentials`` was left at its ``true`` default on both ``ci-cd.yml``
   checkouts, neither of which touches the remote afterwards.

The deployment gate and the CI step are exercised as the literal scripts that
run in CI: both are extracted from the parsed workflows and executed under
``bash``, with a stub on ``PATH`` standing in for ``gh`` and ``pytest``. No
network, no credentials, no repository mutation.
"""
import json
import os
import re
import subprocess
import textwrap

import pytest
import yaml

from tests.test_f25_destructive_reviewer_gate import (
    accepted,
    gate_run_script,
    read_deploy,
    refusal_reasons,
    run_gate,
)

# ── Reviewer entry shapes ────────────────────────────────────────────────────
#
# Grounded in two independent sources, not invented:
#
#   * GitHub's OpenAPI description composes a reviewers item as
#     `deployment-reviewer-type` (enum: User | Team) plus
#     `anyOf[simple-user, team]`. `simple-user` carries `login` and `id`;
#     `team` carries `slug` and `id`.
#   * Live responses: `home-assistant/core` environment `stable` returned a
#     User reviewer (login "frenck", id 195327); `saltstack/salt` environment
#     `release-check` and `cli/cli` `production` returned Team reviewers
#     (slug "team-core", id 1734030).

LIVE_USER_REVIEWER = {
    "type": "User",
    "reviewer": {
        "login": "frenck", "id": 195327, "node_id": "MDQ6VXNlcjE5NTMy",
        "avatar_url": "https://avatars.githubusercontent.com/u/195327",
        "html_url": "https://github.com/frenck", "type": "User",
        "site_admin": False,
        "url": "https://api.github.com/users/frenck",
    },
}

LIVE_TEAM_REVIEWER = {
    "type": "Team",
    "reviewer": {
        "id": 1734030, "node_id": "MDQ6VGVhbTE3MzQw", "name": "Core",
        "slug": "team-core", "description": "these people review code",
        "privacy": "closed", "permission": "push",
        "notification_setting": "notifications_enabled",
        "html_url": "https://github.com/saltstack/teams/core",
        "type": "organization",
    },
}


def _env(reviewers):
    """A fully protected environment whose reviewer list is the variable."""
    return {
        "name": "production-destructive",
        "can_admins_bypass": False,
        "protection_rules": [{
            "id": 7609712, "node_id": "GA_kwDODKw3uc4AdB1w",
            "type": "required_reviewers", "prevent_self_review": True,
            "reviewers": reviewers,
        }],
    }


@pytest.fixture(scope="module")
def deploy():
    return read_deploy()


@pytest.fixture(scope="module")
def gate_script():
    return gate_run_script()


# ═══════════════════════════════════════════════════════════════════════════
# Finding 1 — every reviewer entry is validated, not just counted
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("reviewers,label", [
    ([LIVE_USER_REVIEWER], "user"),
    ([LIVE_TEAM_REVIEWER], "team"),
    ([LIVE_USER_REVIEWER, LIVE_TEAM_REVIEWER], "user+team"),
], ids=lambda v: v if isinstance(v, str) else None)
def test_accepts_valid_user_and_team_reviewers(tmp_path, gate_script,
                                               reviewers, label):
    result = run_gate(tmp_path, gate_script, _env(reviewers))
    assert accepted(result), (
        f"[{label}] a documented, well-formed reviewer must be accepted; "
        f"refused with: {refusal_reasons(result)}")
    assert f"Configured required reviewers: {len(reviewers)}" in result.stderr


@pytest.mark.parametrize("reviewers,label", [
    ([None], "entry-null"),
    ([[]], "entry-list"),
    (["x"], "entry-string"),
    ([{}], "entry-empty-object"),
    ([{"type": "User"}], "reviewer-key-absent"),
    ([{"reviewer": {"id": 1, "login": "a"}}], "type-key-absent"),
    ([{"type": "User", "reviewer": None}], "reviewer-null"),
    ([{"type": "User", "reviewer": []}], "reviewer-is-list"),
    ([{"type": "User", "reviewer": {}}], "reviewer-empty-object"),
    ([{"type": "Group", "reviewer": {"id": 1, "slug": "s"}}], "unknown-type"),
    ([{"type": "user", "reviewer": {"id": 1, "login": "a"}}], "type-wrong-case"),
    ([{"type": "Team", "reviewer": {"slug": "s"}}], "team-id-missing"),
    ([{"type": "Team", "reviewer": {"id": "1734030", "slug": "s"}}], "team-id-string"),
    ([{"type": "Team", "reviewer": {"id": True, "slug": "s"}}], "team-id-bool"),
    ([{"type": "Team", "reviewer": {"id": 1}}], "team-slug-missing"),
    ([{"type": "Team", "reviewer": {"id": 1, "slug": ""}}], "team-slug-empty"),
    ([{"type": "Team", "reviewer": {"id": 1, "slug": 123}}], "team-slug-wrong-type"),
    ([{"type": "User", "reviewer": {"id": 1}}], "user-login-missing"),
    ([{"type": "User", "reviewer": {"id": 1, "login": ""}}], "user-login-empty"),
    ([{"type": "User", "reviewer": {"id": 1.5, "login": "a"}}], "user-id-float"),
], ids=lambda v: v if isinstance(v, str) else None)
def test_rejects_every_malformed_reviewer_entry(tmp_path, gate_script,
                                                reviewers, label):
    result = run_gate(tmp_path, gate_script, _env(reviewers))
    assert not accepted(result), (
        f"[{label}] a malformed reviewer entry must not satisfy the gate")


def test_whole_list_refused_rather_than_counting_the_survivors(
        tmp_path, gate_script):
    """The mission's explicit preference: refuse the list, do not salvage it.

    Counting only the well-formed entries would let [valid, null, valid]
    report "2 reviewers" while the response is internally inconsistent — the
    same shape of silent optimism F25 was created to remove.
    """
    reviewers = [LIVE_USER_REVIEWER, None, LIVE_TEAM_REVIEWER]
    result = run_gate(tmp_path, gate_script, _env(reviewers))
    assert not accepted(result), (
        "a list mixing valid and malformed entries must be refused outright")
    assert "reviewer entry 1" in refusal_reasons(result)


def test_reviewer_error_names_the_offending_position(tmp_path, gate_script):
    reviewers = [LIVE_USER_REVIEWER, {"type": "Team", "reviewer": {"id": 1}}]
    result = run_gate(tmp_path, gate_script, _env(reviewers))
    reasons = refusal_reasons(result)
    assert "reviewer entry 1" in reasons
    assert "'slug'" in reasons, "the message must name the missing field"


def test_reviewer_validation_lives_in_the_gate(gate_script):
    """Guard the guard: the checks must be present and not merely plausible."""
    assert 'entry_type not in ("User", "Team")' in gate_script
    assert 'identity_field = "login" if entry_type == "User" else "slug"' \
        in gate_script
    # bool must be excluded explicitly: it is a subclass of int in Python.
    assert "isinstance(identity_id, bool)" in gate_script
    # And the count may only be incremented after the loop validates everything.
    assert gate_script.index("reviewers_found += len(reviewers)") > \
        gate_script.index('entry_type not in ("User", "Team")')


# ═══════════════════════════════════════════════════════════════════════════
# Finding 2 — the browser count check fails closed
# ═══════════════════════════════════════════════════════════════════════════

BROWSER_STEP = "Browser suites must not silently skip"

#: pytest's own trailing-summary separator. Composed rather than written inline
#: because a source line beginning with a long run of `=` is indistinguishable
#: from a merge-conflict marker to conflict-marker scanners, and this fixture
#: legitimately contains that exact byte sequence. The composed value is
#: byte-identical to the literal it replaces.
PYTEST_SUMMARY_BAR = "=" * 11

#: pytest output shapes. `real` is the verbatim trailing summary of an actual
#: run in this repository.
PYTEST_OUTPUTS = {
    "valid": ("collected 3935 items\n\ntests/a.py ....  [100%]\n\n"
              + PYTEST_SUMMARY_BAR
              + " 3935 passed, 3 skipped, 32 deselected in 876.81s "
              + PYTEST_SUMMARY_BAR + "\n"),
    "valid-single-passed": "13 passed, 174 warnings in 43.07s\n",
    "zero": "collected 13 items\n13 skipped in 12.00s\n",
    "empty": "",
    "non-numeric": "collected 13 items\nbanana passed in 1s\n",
    "multiline": "1 passed\n0\n",
    "buried": "13 passed, 1 warning in 1.00s\nsome trailing noise line\n",
    "no-tests": "collected 0 items\nno tests ran in 0.01s\n",
}

FAILING_OUTPUT = "ERROR: collection failed\nno tests ran\n"


@pytest.fixture(scope="module")
def browser_step_script():
    """The CI step's ``run:`` block, verbatim."""
    ci = yaml.safe_load(open(".github/workflows/ci-cd.yml"))
    step = next(s for s in ci["jobs"]["test"]["steps"]
                if s.get("name") == BROWSER_STEP)
    return step["run"]


def required_suites_from(script):
    """The suites the step requires, read out of the step itself.

    This list used to be a hard-coded tuple mirroring ``ci-cd.yml``. That made
    one fact live in three places -- the workflow, ``REQUIRED_BROWSER_SUITES``
    in ``test_workflow_security_f24.py``, and here -- and only two of them were
    cross-checked. Adding a suite and forgetting this copy made the step
    refuse a perfectly valid run with "Required browser suite ... no longer
    exists", which reads like a real CI break and is not one.

    Parsing the script the fixture is already executing removes the third
    mirror instead of extending it.
    """
    match = re.search(r"REQUIRED_BROWSER_SUITES=\(([^)]*)\)", script)
    assert match, "the step no longer declares REQUIRED_BROWSER_SUITES"
    names = re.findall(r"tests/(\S+\.py)", match.group(1))
    assert names, "no suites parsed out of REQUIRED_BROWSER_SUITES"
    return names


def run_browser_step(script, tmp_path, output=None, pytest_exit=0):
    """Execute the real step with a stub pytest and every suite file present."""
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    pytest_stub = bindir / "pytest"
    body = textwrap.dedent(f"""\
        #!/bin/sh
        # %b, not %s: the fixtures below contain real newlines, and json.dumps
        # has already turned them into \\n escapes for the shell to re-expand.
        printf '%b' {json.dumps(output if output is not None else "")}
        exit {int(pytest_exit)}
    """)
    pytest_stub.write_text(body)
    pytest_stub.chmod(0o755)

    for name in required_suites_from(script):
        # The step resolves them as tests/<name>.py relative to the working
        # directory, so the scratch tree must mirror that layout.
        (tmp_path / "tests").mkdir(exist_ok=True)
        (tmp_path / "tests" / name).write_text("")

    step = tmp_path / "step.sh"
    step.write_text(script)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    return subprocess.run(["bash", str(step)], capture_output=True, text=True,
                          env=env, cwd=tmp_path, timeout=60)


@pytest.mark.parametrize("label", ["valid", "valid-single-passed"])
def test_browser_step_accepts_a_well_formed_count(
        tmp_path, browser_step_script, label):
    result = run_browser_step(browser_step_script, tmp_path,
                              output=PYTEST_OUTPUTS[label])
    assert result.returncode == 0, (
        f"[{label}] a real pytest summary must pass the step; got "
        f"{result.returncode}\n{result.stdout}\n{result.stderr}")


@pytest.mark.parametrize("label", ["zero", "empty", "non-numeric", "multiline",
                                   "buried", "no-tests"])
def test_browser_step_refuses_unusable_output(tmp_path, browser_step_script,
                                              label):
    result = run_browser_step(browser_step_script, tmp_path,
                              output=PYTEST_OUTPUTS[label])
    assert result.returncode != 0, (
        f"[{label}] must refuse; it reported success, so a malformed count "
        f"could pass the step silently")


def test_browser_step_refuses_a_failing_pytest(tmp_path, browser_step_script):
    result = run_browser_step(browser_step_script, tmp_path,
                              output=FAILING_OUTPUT, pytest_exit=1)
    assert result.returncode != 0
    assert "FAILED" in (result.stdout + result.stderr)


def test_browser_step_cannot_produce_a_multiline_count(tmp_path,
                                                       browser_step_script):
    """The defect was a PARTIAL capture plus an appended fallback.

    Removing the `grep | head | grep` pipeline is what makes the count
    structurally unable to become multi-line: parameter expansion yields one
    line and `[0-9]+` cannot span a newline. If anyone reintroduces a pipeline
    with an `|| echo` fallback, this fails.
    """
    assert "grep -oE" not in browser_step_script, (
        "the fragile count-extraction pipeline has returned")
    assert "${out##*$'\\n'}" in browser_step_script or \
        'summary="${out##*$' in browser_step_script
    assert "head -1" not in browser_step_script


def test_browser_step_guards_the_numeric_comparison(browser_step_script):
    """The `[ "$v" -lt 1 ]` fail-open needs the same guard the gate has."""
    assert "*[!0-9]*" in browser_step_script
    assert 'case "${passed}" in' in browser_step_script
    # And the comparison may only appear after that guard.
    assert browser_step_script.index('case "${passed}" in') < \
        browser_step_script.index('if [ "${passed}" -lt 1 ]')


@pytest.mark.parametrize("passed,should_refuse", [
    ("13", False),          # the one shape that may proceed
    ("0", True),            # no coverage ran
    ("", True),             # extraction produced nothing
    ("abc", True),          # non-numeric
    ("1\n2", True),         # multi-line: the F28 defect's exact shape
    ("1 2", True),          # whitespace-separated
    ("-1", True),           # negative: not a non-negative integer
    ("1.0", True),          # float-looking
    (" 13", True),          # leading space
])
def test_numeric_guard_refuses_malformed_counts(browser_step_script, tmp_path,
                                                passed, should_refuse):
    """Execute the literal guard with an injected count.

    The extraction cannot currently yield anything but digits, so this is
    defence in depth — but defence that has never been executed is not a
    defence. Running the real `case` + comparison from the workflow with a
    hostile `passed` proves the guard refuses rather than merely existing.
    Without the guard, `-lt` returns 2 on these values and the refusal body is
    skipped, which is the fail-open this finding described.
    """
    start = browser_step_script.index('case "${passed}" in')
    end = browser_step_script.index('echo "${suite}: ${passed} passed"')
    guard = browser_step_script[start:end]

    harness = tmp_path / "guard.sh"
    harness.write_text(
        'set -euo pipefail\n'
        f'suite="tests/example.py"\n'
        f'passed={json.dumps(passed)}\n'
        + guard
        + '\necho REACHED_END\n'
    )
    result = subprocess.run(["bash", str(harness)], capture_output=True,
                            text=True, timeout=60)
    refused = "REACHED_END" not in result.stdout
    assert refused == should_refuse, (
        f"injected passed={passed!r}: expected "
        f"{'refusal' if should_refuse else 'proceed'}, got "
        f"rc={result.returncode} out={result.stdout!r} err={result.stderr!r}")


def test_browser_step_keeps_the_primary_guard(browser_step_script):
    """Log parsing is only the second line of defence; it must not replace it."""
    assert "FRAMEIQ_REQUIRE_BROWSER" not in browser_step_script, (
        "the env var is a job-level setting, not part of this step")
    ci = yaml.safe_load(open(".github/workflows/ci-cd.yml"))
    assert ci["jobs"]["test"]["env"]["FRAMEIQ_REQUIRE_BROWSER"] == "1"
    assert "Browser tests (Edit Profile picker)" in [
        s.get("name") for s in ci["jobs"]["test"]["steps"]]


# ═══════════════════════════════════════════════════════════════════════════
# Finding 3 — SSH script coverage reaches every workflow
# ═══════════════════════════════════════════════════════════════════════════

def _write_workflow(directory, name, body):
    path = directory / name
    path.write_text(textwrap.dedent(body))
    return path


@pytest.fixture
def temp_workflow_dir(tmp_path, monkeypatch):
    """Point the guard's discovery at a scratch directory."""
    from tests import test_deploy_workflow as guard
    monkeypatch.setattr(guard, "_WORKFLOW_DIR", tmp_path)
    return tmp_path


UNSAFE_SCRIPT_WORKFLOW = """
    name: {name}
    on:
      workflow_dispatch:
    permissions:
      contents: read
    jobs:
      j:
        runs-on: ubuntu-latest
        steps:
          - uses: appleboy/ssh-action@029f5b4aeeeb58fdfe1410a5d17f967dacf36262 # v1.0.3
            with:
              script_stop: true
              script: |
                set -euo pipefail
                if [ -z "$X" ]
                else
                  echo unreachable
                fi
"""


def test_new_workflow_with_unsafe_script_is_caught(temp_workflow_dir):
    """A workflow added after the fact must be covered without an edit here."""
    from tests import test_deploy_workflow as guard
    _write_workflow(temp_workflow_dir, "brand-new.yml",
                    UNSAFE_SCRIPT_WORKFLOW.format(name="brand new"))
    scripts = guard._ssh_scripts_all()
    assert "brand-new.yml::" in " ".join(scripts), (
        "a newly added workflow was not discovered by the SSH guard")
    with pytest.raises(AssertionError, match="bare 'else' line"):
        guard.test_no_bare_else_in_any_ssh_script()


def test_yaml_extension_workflow_is_discovered(temp_workflow_dir):
    from tests import test_deploy_workflow as guard
    _write_workflow(temp_workflow_dir, "dots.yaml",
                    UNSAFE_SCRIPT_WORKFLOW.format(name="dots"))
    assert "dots.yaml" in {k.split("::")[0] for k in guard._ssh_scripts_all()}
    with pytest.raises(AssertionError):
        guard.test_no_bare_else_in_any_ssh_script()


@pytest.mark.parametrize("omitted", [
    "sync-upcoming-episodes.yml",
    "taste-profile-nightly.yml",
])
def test_previously_omitted_workflows_are_now_covered(omitted):
    """The regression this finding was raised for, pinned by name."""
    from tests import test_deploy_workflow as guard
    covered = {key.split("::", 1)[0] for key in guard._ssh_scripts_all()}
    assert omitted in covered, (
        f"{omitted} carries remote scripts but the guard never inspects it")
    # And it actually contributes scripts, so coverage is not nominal.
    scripts = [k for k in guard._ssh_scripts_all() if k.startswith(omitted)]
    assert scripts, f"{omitted} contributed no scripts to the guard"


def test_guard_reads_only_remote_scripts_not_runner_shell(temp_workflow_dir):
    """A `run:` block with a bare `else` must NOT be flagged.

    The drone-ssh hazard is a property of the `script:` input, not of ordinary
    runner shell. Folding `run:` into the scan would produce false positives on
    perfectly safe workflow code.
    """
    from tests import test_deploy_workflow as guard
    _write_workflow(temp_workflow_dir, "runner_only.yml", """
        name: runner only
        on:
          workflow_dispatch:
        permissions:
          contents: read
        jobs:
          j:
            runs-on: ubuntu-latest
            steps:
              - name: local shell
                run: |
                  set -euo pipefail
                  if [ -z "$X" ]
                  else
                    echo hi
                  fi
    """)
    scripts = guard._ssh_scripts_all()
    assert not any(k.startswith("runner_only.yml") for k in scripts), (
        "a `run:` block was mistaken for a remote SSH script")
    guard.test_no_bare_else_in_any_ssh_script()  # must not raise


def test_comment_no_longer_claims_two_workflows():
    source = open("tests/test_deploy_workflow.py").read()
    assert "Both workflows'" not in source, (
        "the stale comment claiming only two workflows are covered is back")
    assert "_all_workflow_paths" in source


# ═══════════════════════════════════════════════════════════════════════════
# Finding 4 — checkout credentials are explicitly not persisted
# ═══════════════════════════════════════════════════════════════════════════

def _checkout_steps():
    for path in sorted(__import__("pathlib").Path(".github/workflows").glob("*.yml")):
        parsed = yaml.safe_load(path.read_text())
        if isinstance(parsed, dict) and True in parsed:
            parsed["on"] = parsed.pop(True)
        for job_name, job in (parsed.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                if (step.get("uses") or "").startswith("actions/checkout"):
                    yield path.name, job_name, step


def test_every_checkout_disables_persisted_credentials():
    steps = list(_checkout_steps())
    assert steps, "no checkout steps found; the inventory is wrong"
    for workflow, job, step in steps:
        with_obj = step.get("with") or {}
        assert with_obj.get("persist-credentials") is False, (
            f"{workflow}:{job} still persists the token in .git/config; "
            f"nothing after checkout needs it")


def test_checkout_inputs_are_otherwise_untouched():
    """fetch-depth, the pinned SHA, and the rest of the inputs must survive."""
    for workflow, job, step in _checkout_steps():
        assert step["uses"] == (
            "actions/checkout@11d5960a326750d5838078e36cf38b85af677262"
        ), f"{workflow}:{job} checkout SHA changed"
        with_obj = step.get("with") or {}
        assert with_obj.get("fetch-depth") == 0, (
            f"{workflow}:{job} lost fetch-depth: 0 — the whitespace gate needs "
            f"the base commit to exist locally")


def test_no_checkout_needs_credentials_afterwards():
    """Re-derive the justification rather than trusting the assertion above."""
    remote = re.compile(
        r"\bgit\s+(push|fetch|pull|clone|commit|tag|remote|submodule|ls-remote)\b"
        r"|\bgh\s+\w")
    for path in sorted(__import__("pathlib").Path(".github/workflows").glob("*.yml")):
        parsed = yaml.safe_load(path.read_text())
        for job_name, job in (parsed.get("jobs") or {}).items():
            steps = job.get("steps") or []
            for index, step in enumerate(steps):
                if not (step.get("uses") or "").startswith("actions/checkout"):
                    continue
                for later in steps[index + 1:]:
                    assert not remote.search(later.get("run") or ""), (
                        f"{path.name}:{job_name}: a step after checkout uses a "
                        f"remote git/gh command, so persist-credentials:false "
                        f"would break it")
                    assert not remote.search(later.get("uses") or ""), (
                        f"{path.name}:{job_name}: a step after checkout needs "
                        f"pushed credentials")