"""Regression tests for administrator-bypass enforcement (Feature 27).

WHY THESE EXIST
---------------
F25 required reviewers. F26 required ``prevent_self_review``. Neither noticed
that GitHub lets administrators override an environment's protection rules
entirely. With bypass enabled, a repo admin can force a deployment past the
approval screen, so the required-reviewers rule becomes advisory rather than
binding -- the gate would pass while the environment still permitted exactly the
self-authorising path F25 and F26 were written to close.

THE FIELD'S CONTRACT
--------------------
``can_admins_bypass`` is not declared in GitHub's published ``environment``
OpenAPI schema (verified against the ``environment`` component in
github/rest-api-description). It was implemented anyway because the contract is
well established from other sources:

* Observed as a JSON **boolean** in 31 of 31 live environment responses across
  16 repositories. Never a string, never an enum, never absent.
* Both values occur in the wild: ``true`` (29) and ``false`` (2). The two
  ``false`` cases both carry a required-reviewers rule.
* GitHub's reference documentation is explicit about the direction: "By
  default, administrators can bypass the protection rules and force deployments
  to specific environments", and the hardened state is configured by
  de-selecting "Allow administrators to bypass configured protection rules".

So ``true`` is the unsafe default and ``false`` is what this gate demands.

The residual risk is stated plainly: because the REST schema omits the field,
if GitHub ever withdrew it every environment would report absent, the gate would
refuse, and the destructive path would block until this workflow was updated.
That is the safe direction to fail, and it is asserted deliberately rather than
worked around.

HARNESS
-------
Shared with the F25 module. The gate's ``run:`` block is extracted from the
parsed workflow and executed under ``bash`` with a stub ``gh`` earlier on
``PATH``, so the code under test is the literal script CI runs. No network, no
credentials, no environment mutation.
"""
import re

import pytest

from tests.test_f25_destructive_reviewer_gate import (
    ENV_NAME,
    MALFORMED_JSON,
    RESPONSE_EMPTY_REVIEWERS,
    RESPONSE_LEGACY_SHAPE,
    RESPONSE_MISSING_KEY,
    RESPONSE_MISSING_REVIEWERS,
    RESPONSE_NOT_AN_OBJECT,
    RESPONSE_OTHER_RULE_TYPES,
    accepted,
    find_gate_step,
    gate_run_script,
    read_deploy,
    refusal_reasons,
    run_gate,
)

# The workflow readers are plain functions, not fixtures: registering the same
# fixture name in two modules shadows the first and makes every test parameter
# that reuses the name look like a redefinition.


@pytest.fixture(scope="module")
def deploy():
    return read_deploy()


@pytest.fixture(scope="module")
def destructive(deploy):
    return deploy["jobs"]["destructive"]


@pytest.fixture(scope="module")
def gate_script():
    return gate_run_script()


# ── A fully protected environment, varying only the bypass field ────────────


class _Absent:
    """Sentinel meaning "the field was not present at all"."""

    def __repr__(self):
        return "<absent>"


_ABSENT = _Absent()


def _environment(bypass, **rule_overrides):
    """The complete response for a correctly configured environment.

    ``bypass`` is passed as ``_ABSENT`` to model a field GitHub did not send,
    which is why it is not defaulted.
    """
    rule = {
        "type": "required_reviewers",
        "prevent_self_review": True,
        "reviewers": [
            {"type": "User", "reviewer": {"login": "alice", "id": 1}},
        ],
    }
    rule.update(rule_overrides)
    environment = {
        "name": ENV_NAME,
        "can_admins_bypass": bypass,
        "protection_rules": [rule],
    }
    if bypass is _ABSENT:
        del environment["can_admins_bypass"]
    return environment


#: The verified disabled value, with every other condition satisfied.
RESPONSE_BYPASS_DISABLED = _environment(False)

#: Bypass left on. GitHub's default.
RESPONSE_BYPASS_ENABLED = _environment(True)

#: Field missing entirely.
RESPONSE_BYPASS_ABSENT = _environment(_ABSENT)

#: Wrong types. `0 == False` and `bool("false")` is True in Python, so an
#: equality or truthiness test would let both through.
RESPONSE_BYPASS_STRING_FALSE = _environment("false")
RESPONSE_BYPASS_STRING_TRUE = _environment("true")
RESPONSE_BYPASS_INT_ZERO = _environment(0)
RESPONSE_BYPASS_INT_ONE = _environment(1)
RESPONSE_BYPASS_NULL = _environment(None)


# ═══════════════════════════════════════════════════════════════════════════
# 1. Bypass explicitly disabled, everything else correct -> accepted
# ═══════════════════════════════════════════════════════════════════════════

def test_accepts_fully_protected_environment(tmp_path, gate_script):
    result = run_gate(tmp_path, gate_script, RESPONSE_BYPASS_DISABLED)
    assert accepted(result), (
        f"reviewers + prevent_self_review + administrator bypass disabled is a "
        f"real gate; refused with: {refusal_reasons(result)}")
    assert "Configured required reviewers: 1" in result.stderr
    assert "administrator bypass: DISABLED" in result.stderr
    assert "administrators cannot bypass the protection rules" in result.stdout


def test_f25_and_f26_accepted_fixtures_still_pass(tmp_path, gate_script):
    """The recorded live-shaped responses remain accepted after F27."""
    from tests.test_f25_destructive_reviewer_gate import (
        RESPONSE_ONE_REVIEWER,
        RESPONSE_TWO_REVIEWERS,
    )
    for name, fixture in (("one-reviewer", RESPONSE_ONE_REVIEWER),
                          ("two-reviewers", RESPONSE_TWO_REVIEWERS)):
        result = run_gate(tmp_path, gate_script, fixture)
        assert accepted(result), f"[{name}] must still be accepted: {refusal_reasons(result)}"


# ═══════════════════════════════════════════════════════════════════════════
# 2-4. Bypass enabled, absent, malformed, or wrong type -> rejected
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("response,label", [
    (RESPONSE_BYPASS_ENABLED, "true"),
    (RESPONSE_BYPASS_ABSENT, "absent"),
    (RESPONSE_BYPASS_NULL, "null"),
    (RESPONSE_BYPASS_STRING_FALSE, "string-false"),
    (RESPONSE_BYPASS_STRING_TRUE, "string-true"),
    (RESPONSE_BYPASS_INT_ZERO, "int-0"),
    (RESPONSE_BYPASS_INT_ONE, "int-1"),
], ids=lambda v: v if isinstance(v, str) else None)
def test_rejects_anything_but_explicitly_disabled_bypass(
        tmp_path, gate_script, response, label):
    """Only the JSON boolean false opens the gate."""
    result = run_gate(tmp_path, gate_script, response)
    assert not accepted(result), (
        f"can_admins_bypass={label} must be rejected: with bypass permitted a "
        f"repository admin can force a deployment past the approval gate")
    reasons = refusal_reasons(result)
    assert "does NOT forbid administrator bypass" in reasons
    assert "De-select 'Allow administrators to bypass configured protection rules'" \
        in reasons


def test_rejection_reports_the_observed_value(tmp_path, gate_script):
    """Each misconfiguration must be distinguishable in the CI annotation."""
    expected = [
        (RESPONSE_BYPASS_ENABLED, "True"),
        (RESPONSE_BYPASS_ABSENT, "<absent>"),
        (RESPONSE_BYPASS_NULL, "None"),
        (RESPONSE_BYPASS_STRING_FALSE, "'false'"),
        (RESPONSE_BYPASS_INT_ZERO, "0"),
        (RESPONSE_BYPASS_INT_ONE, "1"),
    ]
    for response, shown in expected:
        result = run_gate(tmp_path, gate_script, response)
        assert f"reports can_admins_bypass={shown}." in refusal_reasons(result), (
            f"expected the annotation to report {shown!r} so the operator can "
            f"tell this misconfiguration apart from the others")


# ═══════════════════════════════════════════════════════════════════════════
# 5. The earlier gates still fire first, and still fire at all
# ═══════════════════════════════════════════════════════════════════════════

def test_zero_reviewers_still_refuses_with_the_reviewer_message(
        tmp_path, gate_script):
    """Bypass disabled but nobody to review: the reviewer gate must fire."""
    response = _environment(False, reviewers=[])
    result = run_gate(tmp_path, gate_script, response)
    assert not accepted(result)
    reasons = refusal_reasons(result)
    assert "no required reviewers configured" in reasons
    assert "does NOT forbid administrator bypass" not in reasons


def test_missing_self_review_still_fires_before_the_bypass_check(
        tmp_path, gate_script):
    response = _environment(False, prevent_self_review=False)
    result = run_gate(tmp_path, gate_script, response)
    assert not accepted(result)
    reasons = refusal_reasons(result)
    assert "does NOT enforce independent approval" in reasons
    assert "does NOT forbid administrator bypass" not in reasons, (
        "the checks are ordered, so a self-review failure must not be reported "
        "as a bypass failure and send the operator to the wrong setting")


def test_non_reviewer_rules_cannot_satisfy_the_full_gate(tmp_path, gate_script):
    """A wait timer plus bypass disabled is still not a human gate."""
    response = {
        "name": ENV_NAME,
        "can_admins_bypass": False,
        "protection_rules": [{"type": "wait_timer", "wait_timer": 30}],
    }
    result = run_gate(tmp_path, gate_script, response)
    assert not accepted(result)
    assert "no required reviewers configured" in refusal_reasons(result)


@pytest.mark.parametrize("response,raw,gh_exit,label", [
    (None, MALFORMED_JSON, None, "malformed-json"),
    (RESPONSE_NOT_AN_OBJECT, None, None, "not-an-object"),
    (RESPONSE_MISSING_KEY, None, None, "missing-protection-rules"),
    (RESPONSE_LEGACY_SHAPE, None, None, "legacy-obsolete-shape"),
    (RESPONSE_EMPTY_REVIEWERS, None, None, "empty-reviewers"),
    (RESPONSE_MISSING_REVIEWERS, None, None, "reviewers-key-absent"),
    (RESPONSE_OTHER_RULE_TYPES, None, None, "other-rule-types"),
    (None, None, 1, "api-failure"),
], ids=lambda v: v if isinstance(v, str) else None)
def test_f25_f26_structural_rejections_still_hold(
        tmp_path, gate_script, response, raw, gh_exit, label):
    result = run_gate(tmp_path, gate_script, response=response, raw=raw,
                      gh_exit=gh_exit)
    assert not accepted(result), f"[{label}] must still fail closed"


# ═══════════════════════════════════════════════════════════════════════════
# 6. The gate is still mandatory, read-only, and ahead of production effects
# ═══════════════════════════════════════════════════════════════════════════

def test_gate_still_precedes_every_privileged_ssh_step(destructive):
    steps = destructive["steps"]
    gate_index = next(i for i, s in enumerate(steps)
                      if (s.get("name") or "").startswith("Fail closed"))
    ssh_indexes = [i for i, s in enumerate(steps)
                   if (s.get("uses") or "").startswith("appleboy/ssh-action")]
    assert ssh_indexes
    assert gate_index < min(ssh_indexes)
    assert steps[gate_index].get("continue-on-error") is None
    assert steps[gate_index].get("if") in (None, "success()")


def test_admins_bypass_check_is_a_strict_identity_test(gate_script):
    """`is False`, never equality and never truthiness.

    In Python `0 == False`, so an equality test would accept the number 0, and
    `bool("false")` is True, so a truthiness test would accept the string.
    Neither means "disabled" in this API.
    """
    assert re.search(r"admins_raw\s+is\s+False", gate_script), (
        "the bypass decision must be an identity test against the False "
        "singleton")
    for unsafe in (r"admins_raw\s*==\s*False",
                   r"if\s+admins_raw\s*:",
                   r"not\s+admins_raw",
                   r"bool\(\s*admins_raw"):
        assert not re.search(unsafe, gate_script), (
            f"{unsafe!r} would accept null, 0 or the string 'false', none of "
            f"which is a verified 'bypass disabled'")
    assert 'if [ "${ADMINS_DISABLED}" != "true" ]; then' in gate_script


def test_field_is_read_not_defaulted(gate_script):
    """A field GitHub stopped sending must refuse, not default to disabled."""
    assert 'environment.get("can_admins_bypass", _MISSING)' in gate_script, (
        "can_admins_bypass must be read with a missing sentinel, so an absent "
        "field cannot be silently treated as bypass-disabled")


def test_f27_added_no_permissions_and_no_mutating_calls(deploy, gate_script):
    assert deploy["permissions"] == {"contents": "read"}, (
        "F27 must not widen workflow permissions to work around the refusal")
    for mutating in ("-X POST", "-X PUT", "-X PATCH", "-X DELETE"):
        assert mutating not in gate_script
    # The repository owner must configure the environment; the gate only reads.
    # GH_TOKEN lives in the step's env:, not in the run: block.
    step = find_gate_step(read_deploy())
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "secrets." not in gate_script
    assert "secrets." not in str(step["env"])


# ═══════════════════════════════════════════════════════════════════════════
# 7. The operator-facing guidance names the missing setting
# ═══════════════════════════════════════════════════════════════════════════

def test_guidance_names_the_administrator_bypass_setting(tmp_path, gate_script):
    result = run_gate(tmp_path, gate_script, RESPONSE_BYPASS_ENABLED)
    reasons = refusal_reasons(result)
    assert "STOPPED SAFELY" in reasons
    assert "no production change was made" in reasons
    assert "'Allow administrators to bypass configured protection rules' must be" \
        in reasons
    assert "DESELECTED" in reasons
    assert "Settings > Environments" in reasons


def test_guidance_lists_every_required_protection(tmp_path, gate_script):
    """All three protections must appear on every refusal path."""
    for response in (RESPONSE_BYPASS_ENABLED, RESPONSE_BYPASS_ABSENT,
                     RESPONSE_EMPTY_REVIEWERS):
        reasons = refusal_reasons(run_gate(tmp_path, gate_script, response))
        assert "Required reviewers' protection rule" in reasons
        assert "'Prevent self-review' ENABLED" in reasons
        assert "'Allow administrators to bypass configured protection rules'" in reasons
        assert "Creating the environment alone is NOT sufficient" in reasons