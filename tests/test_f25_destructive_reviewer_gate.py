"""Regression tests for the production-destructive reviewer gate (Feature 25).

WHY THESE EXIST
---------------
``deploy.yml`` gated its destructive migration behind a check that the
``production-destructive`` environment really carries required reviewers. The
check parsed the API response with::

    .deployment_protection_rule.required_reviewers // [] | length

GitHub's documented environment response does not contain that property. It
exposes protection rules as an array::

    "protection_rules": [ {"type": "required_reviewers",
                           "prevent_self_review": true,
                           "reviewers": [ {...}, {...} ]}, ... ]

so the ``// []`` fallback silently produced zero on *every* run and the gate
could never pass -- including against a correctly configured environment. A
control that can never succeed is not a control, even though it fails in the
safe direction.

HOW THESE TESTS WORK
--------------------
These do not assert that certain words appear in the YAML. They extract the
gate's ``run:`` block from the parsed workflow, execute it under ``bash``, and
drive it against recorded and constructed API responses by putting a stub
``gh`` earlier on ``PATH``. The code under test is therefore the literal script
that runs in CI, including ``set -euo pipefail``, the exit status, and the
operator-facing message.

No network, no credentials, no production contact: the stub ``gh`` shadows any
real binary and only ever reads a local fixture file.
"""
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / ".github" / "workflows" / "deploy.yml"

ENV_NAME = "production-destructive"
GATE_STEP_PREFIX = "Fail closed"

# ── Recorded / constructed API responses ─────────────────────────────────────

#: Shapes exactly as GitHub documents and as observed live against a
#: public environment that genuinely has required reviewers.
RESPONSE_ONE_REVIEWER = {
    "id": 187000001,
    "node_id": "EN_kwDOAAAAAA",
    "name": ENV_NAME,
    "url": f"https://api.github.com/repos/owner/repo/environments/{ENV_NAME}",
    "html_url": f"https://github.com/owner/repo/settings/environments/{ENV_NAME}",
    "created_at": "2026-01-04T09:12:33Z",
    "updated_at": "2026-02-19T14:02:11Z",
    "can_admins_bypass": False,
    "deployment_branch_policy": None,
    "protection_rules": [
        {
            "id": 7609712,
            "node_id": "GA_kwDODKw3uc4AdB1w",
            "type": "required_reviewers",
            "prevent_self_review": True,
            "reviewers": [
                {"type": "User",
                 "reviewer": {"login": "alice", "id": 1, "type": "User"}},
            ],
        }
    ],
}

RESPONSE_TWO_REVIEWERS = {
    "name": ENV_NAME,
    "can_admins_bypass": False,
    "deployment_branch_policy": None,
    # A wait timer is present too, and must NOT be counted as a human gate.
    "protection_rules": [
        {"type": "wait_timer", "wait_timer": 30},
        {"type": "required_reviewers", "prevent_self_review": True,
         "reviewers": [
             {"type": "User", "reviewer": {"login": "alice", "id": 1}},
             {"type": "Team", "reviewer": {"slug": "ops", "id": 2}},
         ]},
    ],
}

#: Only non-reviewer rule types. Sufficient to protect nothing.
RESPONSE_OTHER_RULE_TYPES = {
    "name": ENV_NAME,
    "protection_rules": [
        {"type": "wait_timer", "wait_timer": 30},
        {"type": "custom_branch_policies"},
    ],
}

#: A non-reviewer rule type that nonetheless carries a populated ``reviewers``
#: array. GitHub does not emit this today -- it is constructed deliberately, to
#: pin the invariant that the DISCRIMINATOR IS THE ``type`` FIELD and not the
#: presence of a reviewers array. Without this fixture, deleting the type check
#: would leave every other case passing, because real wait_timer and
#: branch_policy rules carry no reviewers and so contribute nothing either way.
RESPONSE_OTHER_TYPE_WITH_REVIEWERS = {
    "name": ENV_NAME,
    "protection_rules": [
        # Neither of these may count. The first pins that the comparison is an
        # exact match rather than a substring test, since its type does contain
        # "reviewer" while not being "required_reviewers". The second pins that
        # the reviewers array itself confers nothing.
        {"type": "optional_reviewers",
         "reviewers": [{"type": "User", "reviewer": {"login": "mallory", "id": 3}}]},
        {"type": "branch_policy",
         "reviewers": [{"type": "User", "reviewer": {"login": "alice", "id": 1}}]},
    ],
}

#: No protection_rules property at all.
RESPONSE_MISSING_KEY = {
    "name": ENV_NAME,
    "deployment_branch_policy": None,
    "can_admins_bypass": False,
}

RESPONSE_EMPTY_RULES = {"name": ENV_NAME, "protection_rules": []}

#: Rule declared, but nobody on it.
RESPONSE_EMPTY_REVIEWERS = {
    "name": ENV_NAME,
    "protection_rules": [
        {"type": "required_reviewers", "prevent_self_review": True,
         "reviewers": []},
    ],
}

#: Rule declared with reviewers omitted entirely.
RESPONSE_MISSING_REVIEWERS = {
    "name": ENV_NAME,
    "protection_rules": [
        {"type": "required_reviewers", "prevent_self_review": True},
    ],
}

#: The obsolete shape. It names reviewers, but the response no longer uses it,
#: so treating it as configured would be trusting a field GitHub does not send.
RESPONSE_LEGACY_SHAPE = {
    "name": ENV_NAME,
    "deployment_protection_rule": {
        "wait_timer": 0,
        "prevent_self_review": False,
        "required_reviewers": [
            {"type": "User", "reviewer": {"login": "alice", "id": 1}},
        ],
    },
}

MALFORMED_JSON = '{"name": "production-destructive", "protection_rules": ['

RESPONSE_NOT_AN_OBJECT = [{"type": "required_reviewers", "reviewers": [{}]}]


# ── Harness ──────────────────────────────────────────────────────────────────

# ── Workflow readers ─────────────────────────────────────────────────────────
#
# Plain functions rather than fixtures, so the F26 module can share one
# definition instead of re-declaring them. Registering the same fixture name in
# two modules shadows it and makes every test parameter look like a
# redefinition.

def read_deploy():
    """deploy.yml parsed, with the YAML 1.1 ``on`` -> True quirk normalised."""
    parsed = yaml.safe_load(DEPLOY.read_text())
    if isinstance(parsed, dict) and True in parsed:
        parsed["on"] = parsed.pop(True)
    return parsed


def find_gate_step(deploy):
    steps = [s for s in deploy["jobs"]["destructive"]["steps"]
             if (s.get("name") or "").startswith(GATE_STEP_PREFIX)]
    assert len(steps) == 1, (
        f"expected exactly one '{GATE_STEP_PREFIX}' step, found {len(steps)}")
    return steps[0]


def gate_run_script():
    """The reviewer gate's ``run:`` block exactly as GitHub will execute it."""
    script = find_gate_step(read_deploy())["run"]
    assert script, "the reviewer gate has an empty run: block"
    # The heredoc terminator must sit at column 0 after the YAML block scalar
    # dedents, otherwise Python sees indented top-level statements.
    assert "\nPYTHON\n" in script, (
        "the embedded Python heredoc terminator must be at column 0; "
        "re-indent the run block as a unit, never the heredoc alone")
    return script


@pytest.fixture(scope="module")
def deploy():
    return read_deploy()


@pytest.fixture(scope="module")
def destructive(deploy):
    return deploy["jobs"]["destructive"]


@pytest.fixture(scope="module")
def gate_script():
    return gate_run_script()


def _stub_gh(bindir, body_path=None, exit_code=None):
    """A `gh` that replays a fixture, or fails like a real API error would."""
    bindir.mkdir(parents=True, exist_ok=True)
    gh = bindir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'if [ -n "${STUB_GH_EXIT:-}" ]; then\n'
        '  echo "gh: HTTP 404 (Not Found)" >&2\n'
        '  exit "${STUB_GH_EXIT}"\n'
        "fi\n"
        'cat "${STUB_GH_BODY}"\n'
    )
    gh.chmod(0o755)


def run_gate(tmp_path, gate_script, response=None, raw=None, gh_exit=None):
    """Execute the real gate script against a stubbed environments API."""
    bindir = tmp_path / "bin"
    _stub_gh(bindir)

    script_path = tmp_path / "gate.sh"
    script_path.write_text(gate_script)

    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["GITHUB_REPOSITORY"] = "owner/repo"
    # Make the stub win over any real configuration, and prove no token is used.
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)

    if gh_exit is not None:
        env["STUB_GH_EXIT"] = str(gh_exit)
    else:
        body = tmp_path / "response.json"
        body.write_text(raw if raw is not None else json.dumps(response))
        env["STUB_GH_BODY"] = str(body)

    return subprocess.run(
        ["bash", str(script_path)], capture_output=True, text=True, env=env,
        timeout=60,
    )


def accepted(result):
    return result.returncode == 0


def refusal_reasons(result):
    """Every ::error:: line the operator would see, joined for matching."""
    lines = [ln[len("::error::"):].strip()
             for ln in (result.stdout + result.stderr).splitlines()
             if ln.startswith("::error::")]
    return " | ".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# 1-2. Documented response with one and with several reviewers
# ═══════════════════════════════════════════════════════════════════════════

def test_accepts_documented_response_with_one_reviewer(tmp_path, gate_script):
    result = run_gate(tmp_path, gate_script, RESPONSE_ONE_REVIEWER)
    assert accepted(result), (
        f"a correctly configured environment must be accepted; "
        f"gate refused with: {refusal_reasons(result)}\n{result.stdout}{result.stderr}")
    assert "Required reviewers are configured (1)" in result.stdout


def test_accepts_documented_response_with_several_reviewers(tmp_path, gate_script):
    result = run_gate(tmp_path, gate_script, RESPONSE_TWO_REVIEWERS)
    assert accepted(result), (
        f"two configured reviewers must be accepted; "
        f"gate refused with: {refusal_reasons(result)}")
    assert "Required reviewers are configured (2)" in result.stdout
    # The co-present wait timer must not inflate or replace the count.
    assert "Protection rule types present: ['wait_timer', 'required_reviewers']" \
        in result.stderr


# ═══════════════════════════════════════════════════════════════════════════
# 3-6. Everything that is not "a required-reviewers rule with a reviewer"
# ═══════════════════════════════════════════════════════════════════════════

def test_rejects_other_protection_rule_types(tmp_path, gate_script):
    """A wait timer or a branch policy is not a human gate."""
    result = run_gate(tmp_path, gate_script, RESPONSE_OTHER_RULE_TYPES)
    assert not accepted(result), (
        "wait_timer / custom_branch_policies must not be mistaken for a "
        "reviewer gate")
    assert "no required reviewers configured" in refusal_reasons(result)


def test_type_field_is_the_discriminator_not_the_presence_of_reviewers(
        tmp_path, gate_script):
    """A reviewers array on a NON-reviewer rule must not be counted.

    This is the assertion that makes the ``type == "required_reviewers"`` check
    load-bearing. Deleting that check still passes every other case in this file,
    because real wait_timer and branch_policy rules carry no reviewers array and
    contribute zero either way. Here the array is populated on a rule whose type
    is not ``required_reviewers``, so only the type check rejects it.
    """
    result = run_gate(tmp_path, gate_script, RESPONSE_OTHER_TYPE_WITH_REVIEWERS)
    assert not accepted(result), (
        "reviewers on a rule that is not required_reviewers must never count "
        "as a human gate")
    assert "Configured required reviewers: 0" in result.stderr


def test_rejects_missing_protection_rules(tmp_path, gate_script):
    result = run_gate(tmp_path, gate_script, RESPONSE_MISSING_KEY)
    assert not accepted(result)
    assert "no 'protection_rules' property" in refusal_reasons(result)


def test_rejects_empty_protection_rules(tmp_path, gate_script):
    """This is the shape GitHub auto-creates for a brand-new environment."""
    result = run_gate(tmp_path, gate_script, RESPONSE_EMPTY_RULES)
    assert not accepted(result), (
        "an environment with no protection rules applies no protection")
    assert "no required reviewers configured" in refusal_reasons(result)


@pytest.mark.parametrize("response", [
    RESPONSE_EMPTY_REVIEWERS,
    RESPONSE_MISSING_REVIEWERS,
], ids=["empty-reviewers-array", "reviewers-key-absent"])
def test_rejects_required_reviewers_rule_with_nobody_on_it(
        tmp_path, gate_script, response):
    result = run_gate(tmp_path, gate_script, response)
    assert not accepted(result), (
        "a required_reviewers rule naming zero reviewers must not pass")
    assert "no required reviewers configured" in refusal_reasons(result)


# ═══════════════════════════════════════════════════════════════════════════
# 7. Malformed JSON, a failed API request, and unexpected shapes
# ═══════════════════════════════════════════════════════════════════════════

def test_rejects_malformed_json(tmp_path, gate_script):
    result = run_gate(tmp_path, gate_script, raw=MALFORMED_JSON)
    assert not accepted(result), "unparseable JSON must never be treated as a pass"
    assert "not readable JSON" in refusal_reasons(result)


def test_rejects_failed_api_request(tmp_path, gate_script):
    """A 404/403 from the environments API must refuse, not pass."""
    result = run_gate(tmp_path, gate_script, gh_exit=1)
    assert not accepted(result), "an API failure must never be treated as a pass"
    reasons = refusal_reasons(result)
    assert "environments API request failed" in reasons
    assert "does not exist" in reasons


def test_rejects_response_that_is_not_an_object(tmp_path, gate_script):
    result = run_gate(tmp_path, gate_script, RESPONSE_NOT_AN_OBJECT)
    assert not accepted(result)
    assert "not a JSON object" in refusal_reasons(result)


def test_rejects_empty_body(tmp_path, gate_script):
    """Zero bytes is not evidence of anything."""
    result = run_gate(tmp_path, gate_script, raw="")
    assert not accepted(result)


# ═══════════════════════════════════════════════════════════════════════════
# 8. The obsolete shape is refused unless back-compat is deliberate
# ═══════════════════════════════════════════════════════════════════════════

def test_rejects_legacy_single_object_shape(tmp_path, gate_script):
    """The F25 defect itself, pinned as a regression.

    This body names a reviewer under the old ``deployment_protection_rule``
    path. Accepting it would mean trusting a field GitHub no longer returns --
    the exact bug that made the gate unusable. Back-compatibility is therefore
    deliberately NOT implemented, and this test records that decision.
    """
    result = run_gate(tmp_path, gate_script, RESPONSE_LEGACY_SHAPE)
    assert not accepted(result), (
        "the legacy deployment_protection_rule shape must be refused; "
        "GitHub does not return it, so accepting it would mask a broken gate")
    reasons = refusal_reasons(result)
    assert "no 'protection_rules' property" in reasons
    assert "deliberately NOT treated as configured" in reasons


# ═══════════════════════════════════════════════════════════════════════════
# 9. The gate is mandatory and runs before any destructive work
# ═══════════════════════════════════════════════════════════════════════════

def test_gate_precedes_every_privileged_ssh_step(destructive):
    steps = destructive["steps"]
    gate_index = next(i for i, s in enumerate(steps)
                      if (s.get("name") or "").startswith(GATE_STEP_PREFIX))
    ssh_indexes = [i for i, s in enumerate(steps)
                   if (s.get("uses") or "").startswith("appleboy/ssh-action")]
    assert ssh_indexes, "the destructive job no longer has any ssh step?"
    assert gate_index < min(ssh_indexes), (
        f"the reviewer gate is step {gate_index} but the first privileged ssh "
        f"step is {min(ssh_indexes)}; approval must be verified before any "
        f"production side effect")
    assert steps[gate_index].get("continue-on-error") is None, (
        "the reviewer gate must never be allowed to fail softly")
    assert steps[gate_index].get("if") in (None, "success()"), (
        "the reviewer gate must not be skipped by a condition")


def test_gate_is_the_only_environment_reader_and_never_mutates(deploy):
    """No permission widening and no environment-mutating API."""
    gate = deploy["jobs"]["destructive"]["steps"]
    gate = [s for s in gate if (s.get("name") or "").startswith(GATE_STEP_PREFIX)][0]
    run = gate["run"]
    assert deploy["permissions"] == {"contents": "read"}, (
        "F25 must not widen workflow permissions")
    assert "secrets." not in run, "the gate must read only the workflow token"
    assert "${{ github.token }}" in gate["env"]["GH_TOKEN"]
    for mutating in ("-X POST", "-X PUT", "-X PATCH", "-X DELETE",
                     "--method POST", "method: PUT", "/environments -f"):
        assert mutating not in run, (
            f"the gate must be read-only; found {mutating!r}")


def test_gate_keeps_strict_mode(gate_script):
    """`set -euo pipefail` must survive, or an unset variable could pass."""
    assert "set -euo pipefail" in gate_script, (
        "strict mode is load-bearing: without `set -u` an unset variable would "
        "expand to empty and could be misread as a passing reviewer count")


def test_every_refusal_path_funnels_through_one_function(gate_script):
    """One `refuse` keeps the operator guidance from drifting per branch."""
    assert "refuse()" in gate_script
    call_sites = len(re.findall(r"\brefuse\b", gate_script))
    assert call_sites >= 3, (
        "expected the refusal helper to be defined and called from each "
        f"failure path, found {call_sites} mentions")


# ═══════════════════════════════════════════════════════════════════════════
# 10. The operator-facing guidance
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("response,gh_exit,label", [
    (RESPONSE_EMPTY_RULES, None, "auto-created-environment"),
    (RESPONSE_MISSING_KEY, None, "missing-protection-rules"),
    (None, 1, "api-failure"),
], ids=lambda v: v if isinstance(v, str) else None)
def test_refusal_explains_how_to_configure_the_environment(
        tmp_path, gate_script, response, gh_exit, label):
    result = run_gate(tmp_path, gate_script, response=response, gh_exit=gh_exit)
    assert not accepted(result), f"[{label}] must fail closed"
    reasons = refusal_reasons(result)

    # Stopped safely.
    assert "STOPPED SAFELY" in reasons
    assert "no production change was made" in reasons
    # What to configure, and where.
    assert ENV_NAME in reasons
    assert "Settings > Environments" in reasons
    assert "Required reviewers" in reasons
    assert "at least one reviewer" in reasons
    # Creating the environment is explicitly called out as not enough.
    assert "Creating the environment alone is NOT sufficient" in reasons


def test_gate_script_is_syntactically_valid_bash(gate_script, tmp_path):
    script = tmp_path / "gate_only.sh"
    script.write_text(gate_script)
    result = subprocess.run(["bash", "-n", str(script)],
                            capture_output=True, text=True)
    assert result.returncode == 0, (
        f"the gate's run: block is not valid bash: {result.stderr}")


def test_embedded_python_is_valid(gate_script):
    """The heredoc body must be real Python, not just plausible-looking text."""
    start = gate_script.index("<<'PYTHON'\n") + len("<<'PYTHON'\n")
    end = gate_script.index("\nPYTHON\n", start)
    body = gate_script[start:end]
    compile(body, "<gate-heredoc>", "exec")


def test_no_real_network_or_credentials_are_used(gate_script):
    """The gate reads the token from env; it must not embed or fetch one."""
    assert "gh auth token" not in gate_script
    assert "secrets." not in gate_script
    assert "curl" not in gate_script, (
        "use gh so the GH_TOKEN contract stays in one place")


# ═══════════════════════════════════════════════════════════════════════════
# Cross-checks against what the F24 tests assert about this same step
# ═══════════════════════════════════════════════════════════════════════════

def test_f24_required_reviewer_presence_check_still_holds(gate_script):
    """F24 asserted the gate exists; F25 must not have removed it."""
    assert "required_reviewers" in gate_script
    assert "gh api" in gate_script
    assert "exit 1" in gate_script