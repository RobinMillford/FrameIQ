"""Regression tests for independent approval on the destructive gate (Feature 26).

WHY THESE EXIST
---------------
F25 established that the ``production-destructive`` environment carries a
required-reviewers rule naming at least one reviewer. That alone is not a
human gate: the person who dispatched the workflow can sit on the reviewer list
and approve their own destructive migration. GitHub models the missing half as
``prevent_self_review`` on the rule, and the gate ignored it entirely.

So the gate was refusing an environment with NO reviewers, while happily
accepting one whose sole reviewer was the dispatcher. That is the worst shape
for a safety control -- it looked configured.

THE RULE UNDER TEST
-------------------
The gate must refuse unless the response is valid, ``protection_rules`` is an
array, a ``required_reviewers`` rule is present with a non-empty ``reviewers``
list, and that rule's ``prevent_self_review`` is explicitly the JSON boolean
``true``. Missing, ``false``, ``null``, or a wrong type all fail closed.

``is True`` in the implementation is deliberate identity against the ``True``
singleton, not equality and not truthiness. In Python ``True == 1`` and
``False == 0``, so ``== True`` would accept the integer 1, and ``if value:``
would additionally accept ``null``, ``0`` and the string ``"true"``. None of
those mean "enabled" in this API, and the tests below pin that distinction.

HARNESS
-------
These extract the gate's ``run:`` block from the parsed workflow and execute it
under ``bash`` with a stub ``gh`` earlier on ``PATH``, so the code under test is
the literal script CI runs. The harness is shared with the F25 module rather
than duplicated. No network, no credentials, no production contact.
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
    RESPONSE_TWO_REVIEWERS,
    accepted,
    gate_run_script,
    read_deploy,
    refusal_reasons,
    run_gate,
)


# The workflow readers are imported as plain functions, not as fixtures.
# Registering the same fixture name in two modules shadows the first, and makes
# every test parameter that reuses the name look like a redefinition. So the
# fixtures below are thin wrappers over the shared functions.
@pytest.fixture(scope="module")
def deploy():
    return read_deploy()


@pytest.fixture(scope="module")
def destructive(deploy):
    return deploy["jobs"]["destructive"]


@pytest.fixture(scope="module")
def gate_script():
    return gate_run_script()


# ── Responses that differ from the F25 fixtures only in prevent_self_review ──

def _rule(**overrides):
    rule = {
        "type": "required_reviewers",
        "prevent_self_review": True,
        "reviewers": [
            {"type": "User", "reviewer": {"login": "alice", "id": 1}},
        ],
    }
    rule.update(overrides)
    # F27 added administrator bypass to what "fully configured" means, so an
    # accept-path fixture that omits it is no longer a complete example. The
    # bypass itself is what F27's own fixtures vary.
    return {"name": ENV_NAME, "can_admins_bypass": False,
            "protection_rules": [rule]}


#: Reviewers configured, independence explicitly enabled. The only shape that
#: may pass.
RESPONSE_INDEPENDENT_OK = _rule()

#: Reviewers configured but self-approval permitted.
RESPONSE_SELF_REVIEW_FALSE = _rule(prevent_self_review=False)

#: Key absent entirely -- the pre-F26 shape GitHub may still return for an
#: environment created before the setting was touched.
RESPONSE_SELF_REVIEW_ABSENT = {
    "name": ENV_NAME,
    "protection_rules": [{
        "type": "required_reviewers",
        "reviewers": [{"type": "User", "reviewer": {"login": "alice", "id": 1}}],
    }],
}

RESPONSE_SELF_REVIEW_NULL = _rule(prevent_self_review=None)

#: Wrong types. `True == 1` in Python and bool("true") is True, so an equality
#: or truthiness check would let both of these through.
RESPONSE_SELF_REVIEW_STRING = _rule(prevent_self_review="true")
RESPONSE_SELF_REVIEW_INT = _rule(prevent_self_review=1)
RESPONSE_SELF_REVIEW_ZERO = _rule(prevent_self_review=0)

#: Independent approval enabled, but nobody to approve. Independence alone is
#: not a gate either.
RESPONSE_NO_REVIEWERS_BUT_SELF_REVIEW = _rule(reviewers=[])

#: Non-reviewer rule types, each carrying BOTH a populated reviewers array and
#: prevent_self_review true. Constructed to prove the discriminator is the exact
#: rule type: real wait_timer/branch_policy rules carry neither field, so
#: without this fixture a dropped type check would leave every other case in
#: this file passing.
RESPONSE_OTHER_TYPES_FULLY_ARMED = {
    "name": ENV_NAME,
    "protection_rules": [
        {"type": "optional_reviewers", "prevent_self_review": True,
         "reviewers": [{"type": "User", "reviewer": {"login": "mallory", "id": 3}}]},
        {"type": "branch_policy", "prevent_self_review": True,
         "reviewers": [{"type": "User", "reviewer": {"login": "alice", "id": 1}}]},
    ],
}


# ═══════════════════════════════════════════════════════════════════════════
# 1. Acceptance requires BOTH reviewers and independent approval
# ═══════════════════════════════════════════════════════════════════════════

def test_accepts_reviewers_with_self_review_prevention(tmp_path, gate_script):
    result = run_gate(tmp_path, gate_script, RESPONSE_INDEPENDENT_OK)
    assert accepted(result), (
        f"reviewers plus prevent_self_review=true is a real human gate and must "
        f"be accepted; refused with: {refusal_reasons(result)}")
    assert "Required reviewers are configured (1), self-review is prevented," \
        in result.stdout
    assert "independent approval: yes" in result.stderr


def test_still_accepts_the_f25_multi_reviewer_response(tmp_path, gate_script):
    """F25's accepted fixture must stay accepted; its rule already sets the flag."""
    result = run_gate(tmp_path, gate_script, RESPONSE_TWO_REVIEWERS)
    assert accepted(result), refusal_reasons(result)
    assert "Required reviewers are configured (2), self-review is prevented," \
        in result.stdout


# ═══════════════════════════════════════════════════════════════════════════
# 2-4. Every way self-review protection can be absent must fail closed
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("response,label", [
    (RESPONSE_SELF_REVIEW_FALSE, "false"),
    (RESPONSE_SELF_REVIEW_ABSENT, "absent"),
    (RESPONSE_SELF_REVIEW_NULL, "null"),
    (RESPONSE_SELF_REVIEW_STRING, "string-true"),
    (RESPONSE_SELF_REVIEW_INT, "int-1"),
    (RESPONSE_SELF_REVIEW_ZERO, "int-0"),
], ids=lambda v: v if isinstance(v, str) else None)
def test_rejects_reviewers_without_independent_approval(
        tmp_path, gate_script, response, label):
    """A non-empty reviewer list is never a substitute for independence."""
    result = run_gate(tmp_path, gate_script, response)
    assert not accepted(result), (
        f"prevent_self_review={label} must be rejected: a reviewer list alone "
        f"lets the dispatcher approve their own destructive migration")
    reasons = refusal_reasons(result)
    assert "does NOT enforce independent approval" in reasons
    assert "Enable 'Prevent self-review'" in reasons


def test_rejection_reports_the_observed_value_not_just_false(
        tmp_path, gate_script):
    """Absent, null, and the number 1 must be told apart in the CI annotation.

    An operator who is told "false" when the field is actually missing will go
    looking in the wrong place, so the annotation carries the real value.
    """
    expected = [
        (RESPONSE_SELF_REVIEW_FALSE, "False"),
        (RESPONSE_SELF_REVIEW_ABSENT, "<absent>"),
        (RESPONSE_SELF_REVIEW_NULL, "None"),
        (RESPONSE_SELF_REVIEW_STRING, "'true'"),
        (RESPONSE_SELF_REVIEW_INT, "1"),
    ]
    for response, shown in expected:
        result = run_gate(tmp_path, gate_script, response)
        assert f"reports prevent_self_review={shown}." in refusal_reasons(result), (
            f"expected the annotation to report {shown!r} so the operator can "
            f"distinguish this misconfiguration from the others")


# ═══════════════════════════════════════════════════════════════════════════
# 5-6. Reviewers still have to exist
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("response,label", [
    (RESPONSE_EMPTY_REVIEWERS, "empty-reviewers"),
    (RESPONSE_MISSING_REVIEWERS, "reviewers-key-absent"),
    (RESPONSE_NO_REVIEWERS_BUT_SELF_REVIEW, "no-reviewers-self-review-on"),
], ids=lambda v: v if isinstance(v, str) else None)
def test_independent_approval_alone_is_not_a_gate(
        tmp_path, gate_script, response, label):
    result = run_gate(tmp_path, gate_script, response)
    assert not accepted(result), (
        f"[{label}] prevent_self_review=true with nobody to approve must not "
        f"open the gate")
    assert "no required reviewers configured" in refusal_reasons(result)


def test_missing_required_reviewers_rule_is_rejected(tmp_path, gate_script):
    result = run_gate(tmp_path, gate_script, RESPONSE_OTHER_RULE_TYPES)
    assert not accepted(result)
    assert "no required reviewers configured" in refusal_reasons(result)


# ═══════════════════════════════════════════════════════════════════════════
# 7. F25's structural rejections are unchanged
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("response,raw,gh_exit,label", [
    (None, MALFORMED_JSON, None, "malformed-json"),
    (RESPONSE_NOT_AN_OBJECT, None, None, "not-an-object"),
    (RESPONSE_MISSING_KEY, None, None, "missing-protection-rules"),
    (RESPONSE_LEGACY_SHAPE, None, None, "legacy-obsolete-shape"),
    (None, None, 1, "api-failure"),
], ids=lambda v: v if isinstance(v, str) else None)
def test_f25_structural_rejections_still_hold(
        tmp_path, gate_script, response, raw, gh_exit, label):
    result = run_gate(tmp_path, gate_script, response=response, raw=raw,
                      gh_exit=gh_exit)
    assert not accepted(result), f"[{label}] must still fail closed"


# ═══════════════════════════════════════════════════════════════════════════
# 8. Other rule types cannot satisfy the gate even fully armed
# ═══════════════════════════════════════════════════════════════════════════

def test_other_rule_types_cannot_satisfy_the_gate_even_when_armed(
        tmp_path, gate_script):
    result = run_gate(tmp_path, gate_script, RESPONSE_OTHER_TYPES_FULLY_ARMED)
    assert not accepted(result), (
        "reviewers AND prevent_self_review on a rule that is not "
        "required_reviewers must never count as a human gate")
    assert "Configured required reviewers: 0" in result.stderr


# ═══════════════════════════════════════════════════════════════════════════
# 9. The gate stays mandatory and stays ahead of every destructive action
# ═══════════════════════════════════════════════════════════════════════════

def test_self_review_check_runs_inside_the_mandatory_gate(gate_script):
    """The independence check must live in the gate, not in a later step."""
    assert "prevent_self_review" in gate_script
    assert "SELF_REVIEW_VALUE" in gate_script
    # An identity comparison, not equality and not truthiness. Matched with
    # word boundaries so the legitimate `"<absent>" if self_review_raw is
    # _MISSING` display expression is not mistaken for a truthiness test.
    assert re.search(r"self_review_raw\s+is\s+True", gate_script), (
        "the independence decision must be an identity test against the True "
        "singleton")
    for unsafe in (r"self_review_raw\s*==\s*True",
                   r"if\s+self_review_raw\s*:",
                   r"not\s+self_review_raw",
                   r"bool\(\s*self_review_raw"):
        assert not re.search(unsafe, gate_script), (
            f"{unsafe!r} would accept null, 0, 1 or the string 'true', none of "
            f"which mean 'enabled' in this API")


def test_gate_still_precedes_every_privileged_ssh_step(destructive):
    steps = destructive["steps"]
    gate_index = next(i for i, s in enumerate(steps)
                      if (s.get("name") or "").startswith("Fail closed"))
    ssh_indexes = [i for i, s in enumerate(steps)
                   if (s.get("uses") or "").startswith("appleboy/ssh-action")]
    assert ssh_indexes
    assert gate_index < min(ssh_indexes), (
        "independent approval must be verified before any production side "
        "effect, not after")
    assert steps[gate_index].get("continue-on-error") is None
    assert steps[gate_index].get("if") in (None, "success()")


def test_f26_added_no_permissions_and_no_mutating_calls(deploy, gate_script):
    assert deploy["permissions"] == {"contents": "read"}
    for mutating in ("-X POST", "-X PUT", "-X PATCH", "-X DELETE"):
        assert mutating not in gate_script


# ═══════════════════════════════════════════════════════════════════════════
# 10. The operator-facing guidance names the missing setting
# ═══════════════════════════════════════════════════════════════════════════

def test_guidance_names_the_prevent_self_review_setting(tmp_path, gate_script):
    result = run_gate(tmp_path, gate_script, RESPONSE_SELF_REVIEW_FALSE)
    reasons = refusal_reasons(result)
    assert "STOPPED SAFELY" in reasons
    assert "no production change was made" in reasons
    assert "'Prevent self-review' ENABLED" in reasons, (
        "the operator must be told which setting is missing")
    assert "dispatched this run cannot be the one who approves it" in reasons
    assert "Settings > Environments" in reasons


def test_guidance_is_present_on_the_reviewers_refusal_too(tmp_path, gate_script):
    """The shared guidance must not depend on which check fired first."""
    result = run_gate(tmp_path, gate_script, RESPONSE_EMPTY_REVIEWERS)
    reasons = refusal_reasons(result)
    assert "'Prevent self-review' ENABLED" in reasons
    assert "Creating the environment alone is NOT sufficient" in reasons


def test_acceptance_message_states_approval_must_come_from_someone_else(
        tmp_path, gate_script):
    result = run_gate(tmp_path, gate_script, RESPONSE_INDEPENDENT_OK)
    assert "Approval must come from a reviewer other than the person who " \
           "dispatched this run." in result.stdout


def test_numeric_guard_closes_the_non_numeric_comparison_hole(gate_script):
    """`[ x -lt 1 ]` returns 2 on a non-numeric operand and SKIPS the body.

    Inside an `if` condition that is a silent pass, not a refusal, so a
    malformed count would walk straight through the gate.
    """
    assert "*[!0-9]*" in gate_script, (
        "the reviewer count must be validated as digits before it is compared")
    assert 'refuse "internal error: reviewer count' in gate_script