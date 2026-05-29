# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.runbooks.engine` (G12.3-T2, #1301).

Coverage matrix follows the Initiative #1198 acceptance bar:

* **Opacity** -- :func:`current_step_body` and
  :attr:`AdvanceOutcome.next_step_body` return exactly the requested /
  immediately-next step; the LOAD-BEARING regression tests
  (:func:`test_current_step_body_returns_only_one_step`,
  :func:`test_advance_outcome_next_step_body_is_only_next_step`) walk
  the serialised result and assert no other step ids leak.
* **Verify by type** -- confirm yes/no/escalate paths, operation_call
  match / mismatch / list match / recursive match.
* **State-machine guards** -- ``pending`` previous-step raises;
  unknown step id raises; verify_response missing on a gated step
  raises; verify_response shape mismatch raises.
* **Substitutions** -- the step body's ``${run.target}`` and
  ``${run.params.X}`` are resolved in the returned StepBody.
* **Purity** -- ``advance(X) == advance(X)`` across invocations.
* **Terminal step** -- verify pass on the last step → ``completed``
  outcome with caller-supplied ``completed_at``.

The fixture-style helpers (``_make_template``, ``_confirm_response``,
``_operation_call_response``) keep each test focused on the one
behaviour it locks down without three lines of setup noise per case.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from meho_backplane.runbooks.engine import (
    AdvanceOutcome,
    PreviousStepNotVerifiedError,
    VerifyResponseMismatchError,
    VerifyResponseRequiredError,
    advance,
    current_step_body,
)
from meho_backplane.runbooks.runs_schemas import (
    ConfirmVerifyResponse,
    OperationCallVerifyResponse,
    StepBody,
)
from meho_backplane.runbooks.schemas import (
    ConfirmVerify,
    ManualStep,
    OperationCallStep,
    OperationCallVerify,
    RunbookTemplateBody,
)

# ---------------------------------------------------------------------------
# Fixture helpers -- keep tests focused.
# ---------------------------------------------------------------------------


def _confirm_step(step_id: str, *, prompt: str = "Did it work?") -> ManualStep:
    """Build a ``manual`` step gated by a ``confirm`` verify."""
    return ManualStep(
        id=step_id,
        title=f"Step {step_id}",
        body=f"Body for {step_id}",
        type="manual",
        verify=ConfirmVerify(type="confirm", prompt=prompt),
    )


def _operation_call_step(step_id: str, *, expect: dict[str, object]) -> OperationCallStep:
    """Build an ``operation_call`` step gated by an ``operation_call`` verify."""
    return OperationCallStep(
        id=step_id,
        title=f"Step {step_id}",
        body=f"Body for {step_id}",
        type="operation_call",
        op_id="kb.create",
        params={"slug": step_id},
        verify=OperationCallVerify(
            type="operation_call",
            op_id="kb.show",
            params={"slug": step_id},
            expect=expect,
        ),
    )


def _five_step_template() -> RunbookTemplateBody:
    """A 5-step template used by the opacity regression tests."""
    return RunbookTemplateBody(
        title="Five-step procedure",
        description="for opacity tests",
        steps=[_confirm_step(f"step-{i}") for i in range(1, 6)],
    )


def _confirm_response(answer: str) -> ConfirmVerifyResponse:
    return ConfirmVerifyResponse(type="confirm", answer=answer)  # type: ignore[arg-type]


def _operation_call_response(actual: dict[str, object]) -> OperationCallVerifyResponse:
    return OperationCallVerifyResponse(type="operation_call", matched=False, actual=actual)


# ---------------------------------------------------------------------------
# Confirm verify -- the three answer transitions.
# ---------------------------------------------------------------------------


def test_advance_confirm_yes_returns_next_step() -> None:
    template = _five_step_template()
    outcome = advance(
        template,
        current_step_id="step-1",
        previous_step_state="in_progress",
        target="vc-01",
        params={},
        verify_response=_confirm_response("yes"),
    )
    assert outcome.kind == "next_step"
    assert outcome.next_step_body is not None
    assert outcome.next_step_body.id == "step-2"
    # Persisted response echoes the caller's confirm payload verbatim.
    assert isinstance(outcome.verify_response_persisted, ConfirmVerifyResponse)
    assert outcome.verify_response_persisted.answer == "yes"


def test_advance_confirm_no_returns_failed() -> None:
    template = _five_step_template()
    outcome = advance(
        template,
        current_step_id="step-1",
        previous_step_state="in_progress",
        target="vc-01",
        params={},
        verify_response=_confirm_response("no"),
    )
    assert outcome.kind == "failed"
    assert outcome.next_step_body is None
    assert isinstance(outcome.verify_response_persisted, ConfirmVerifyResponse)
    assert outcome.verify_response_persisted.answer == "no"


def test_advance_confirm_escalate_returns_failed() -> None:
    template = _five_step_template()
    outcome = advance(
        template,
        current_step_id="step-1",
        previous_step_state="in_progress",
        target="vc-01",
        params={},
        verify_response=_confirm_response("escalate"),
    )
    assert outcome.kind == "failed"
    assert isinstance(outcome.verify_response_persisted, ConfirmVerifyResponse)
    assert outcome.verify_response_persisted.answer == "escalate"


def test_advance_confirm_missing_response_raises() -> None:
    template = _five_step_template()
    with pytest.raises(VerifyResponseRequiredError):
        advance(
            template,
            current_step_id="step-1",
            previous_step_state="in_progress",
            target="vc-01",
            params={},
            verify_response=None,
        )


# ---------------------------------------------------------------------------
# Operation_call verify -- structural match semantics.
# ---------------------------------------------------------------------------


def test_advance_operation_call_match_returns_next_step() -> None:
    template = RunbookTemplateBody(
        title="op-call match",
        description="...",
        steps=[
            _operation_call_step("s1", expect={"foo": 1}),
            _confirm_step("s2"),
        ],
    )
    outcome = advance(
        template,
        current_step_id="s1",
        previous_step_state="in_progress",
        target="vc-01",
        params={},
        verify_response=OperationCallVerifyResponse(
            type="operation_call",
            matched=False,  # engine will overwrite -- the caller's claim is ignored.
            actual={"foo": 1, "bar": 2},
        ),
    )
    assert outcome.kind == "next_step"
    assert outcome.next_step_body is not None
    assert outcome.next_step_body.id == "s2"
    # Engine sets `matched` to its own verdict, not the caller's claim.
    assert isinstance(outcome.verify_response_persisted, OperationCallVerifyResponse)
    assert outcome.verify_response_persisted.matched is True
    # Actual is retained verbatim for audit.
    assert outcome.verify_response_persisted.actual == {"foo": 1, "bar": 2}


def test_advance_operation_call_mismatch_returns_failed() -> None:
    template = RunbookTemplateBody(
        title="op-call mismatch",
        description="...",
        steps=[
            _operation_call_step("s1", expect={"foo": 1}),
            _confirm_step("s2"),
        ],
    )
    outcome = advance(
        template,
        current_step_id="s1",
        previous_step_state="in_progress",
        target="vc-01",
        params={},
        verify_response=OperationCallVerifyResponse(
            type="operation_call",
            matched=True,  # engine ignores the caller's claim.
            actual={"foo": 2},
        ),
    )
    assert outcome.kind == "failed"
    assert isinstance(outcome.verify_response_persisted, OperationCallVerifyResponse)
    assert outcome.verify_response_persisted.matched is False
    assert outcome.verify_response_persisted.actual == {"foo": 2}


def test_advance_operation_call_recursive_match() -> None:
    # Nested dict: expect is satisfied by a deeper structure with extra keys.
    template = RunbookTemplateBody(
        title="nested",
        description="...",
        steps=[
            _operation_call_step(
                "s1",
                expect={"outer": {"inner": "value"}},
            ),
            _confirm_step("s2"),
        ],
    )
    outcome = advance(
        template,
        current_step_id="s1",
        previous_step_state="in_progress",
        target="vc-01",
        params={},
        verify_response=OperationCallVerifyResponse(
            type="operation_call",
            matched=False,
            actual={
                "outer": {"inner": "value", "extra": 1},
                "another_key": "ignored",
            },
        ),
    )
    assert outcome.kind == "next_step"


def test_advance_operation_call_list_match() -> None:
    # Lists compared element-wise; length must match.
    template = RunbookTemplateBody(
        title="lists",
        description="...",
        steps=[
            _operation_call_step(
                "s1",
                expect={"items": [1, 2, 3]},
            ),
            _confirm_step("s2"),
        ],
    )
    outcome_match = advance(
        template,
        current_step_id="s1",
        previous_step_state="in_progress",
        target="vc-01",
        params={},
        verify_response=OperationCallVerifyResponse(
            type="operation_call",
            matched=False,
            actual={"items": [1, 2, 3]},
        ),
    )
    assert outcome_match.kind == "next_step"

    # Different length → mismatch.
    outcome_mismatch = advance(
        template,
        current_step_id="s1",
        previous_step_state="in_progress",
        target="vc-01",
        params={},
        verify_response=OperationCallVerifyResponse(
            type="operation_call",
            matched=False,
            actual={"items": [1, 2]},
        ),
    )
    assert outcome_mismatch.kind == "failed"


# ---------------------------------------------------------------------------
# Terminal-step transition.
# ---------------------------------------------------------------------------


def test_advance_at_last_step_returns_completed() -> None:
    template = RunbookTemplateBody(
        title="two",
        description="...",
        steps=[_confirm_step("s1"), _confirm_step("s2")],
    )
    completed_at = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
    outcome = advance(
        template,
        current_step_id="s2",
        previous_step_state="in_progress",
        target="vc-01",
        params={},
        verify_response=_confirm_response("yes"),
        completed_at=completed_at,
    )
    assert outcome.kind == "completed"
    assert outcome.completed_at == completed_at
    assert outcome.next_step_body is None
    assert isinstance(outcome.verify_response_persisted, ConfirmVerifyResponse)


# ---------------------------------------------------------------------------
# State-machine guards.
# ---------------------------------------------------------------------------


def test_advance_previous_not_in_progress_raises() -> None:
    template = _five_step_template()
    with pytest.raises(PreviousStepNotVerifiedError):
        advance(
            template,
            current_step_id="step-1",
            previous_step_state="pending",
            target="vc-01",
            params={},
            verify_response=_confirm_response("yes"),
        )


def test_advance_unknown_step_id_raises_keyerror() -> None:
    template = _five_step_template()
    with pytest.raises(KeyError):
        advance(
            template,
            current_step_id="not-in-template",
            previous_step_state="in_progress",
            target="vc-01",
            params={},
            verify_response=_confirm_response("yes"),
        )


def test_advance_confirm_step_with_operation_call_response_raises() -> None:
    template = _five_step_template()
    with pytest.raises(VerifyResponseMismatchError):
        advance(
            template,
            current_step_id="step-1",
            previous_step_state="in_progress",
            target="vc-01",
            params={},
            verify_response=OperationCallVerifyResponse(
                type="operation_call",
                matched=True,
                actual={},
            ),
        )


def test_advance_operation_call_step_with_confirm_response_raises() -> None:
    template = RunbookTemplateBody(
        title="op",
        description="...",
        steps=[_operation_call_step("s1", expect={"foo": 1}), _confirm_step("s2")],
    )
    with pytest.raises(VerifyResponseMismatchError):
        advance(
            template,
            current_step_id="s1",
            previous_step_state="in_progress",
            target="vc-01",
            params={},
            verify_response=_confirm_response("yes"),
        )


# ---------------------------------------------------------------------------
# Opacity -- the load-bearing regression tests.
# ---------------------------------------------------------------------------


def test_current_step_body_returns_only_one_step() -> None:
    """LOAD-BEARING: the opacity function returns exactly one step's contents.

    Build a 5-step template, ask for step-3, serialise the returned
    :class:`StepBody`, and assert by **string search** that none of the
    other step ids appear anywhere in the serialised shape. String
    search catches both field-name leaks (a field called ``step_1``
    set to ``None``) and value-level leaks (an embedded body
    referencing another step).
    """
    template = _five_step_template()
    body = current_step_body(
        template,
        "step-3",
        target="vc-01",
        params={},
    )
    assert isinstance(body, StepBody)
    assert body.id == "step-3"

    serialised = json.dumps(body.model_dump(mode="json"))
    for forbidden_id in ("step-1", "step-2", "step-4", "step-5"):
        assert forbidden_id not in serialised, (
            f"opacity leak: {forbidden_id!r} appears in current_step_body output: {serialised!r}"
        )


def test_advance_outcome_next_step_body_is_only_next_step() -> None:
    """LOAD-BEARING: the post-advance step body is exactly the next step.

    Same 5-step template; advance with a verify-pass on step-2. Walk
    the serialised next_step_body and assert no other step id appears.
    """
    template = _five_step_template()
    outcome = advance(
        template,
        current_step_id="step-2",
        previous_step_state="in_progress",
        target="vc-01",
        params={},
        verify_response=_confirm_response("yes"),
    )
    assert outcome.kind == "next_step"
    assert outcome.next_step_body is not None
    assert outcome.next_step_body.id == "step-3"

    serialised = json.dumps(outcome.next_step_body.model_dump(mode="json"))
    for forbidden_id in ("step-1", "step-2", "step-4", "step-5"):
        assert forbidden_id not in serialised, (
            f"opacity leak: {forbidden_id!r} appears in advance outcome: {serialised!r}"
        )


# ---------------------------------------------------------------------------
# Substitution resolution at the engine boundary.
# ---------------------------------------------------------------------------


def test_current_step_body_substitutes_target_and_params() -> None:
    """Substitutions in body, op-call params, and verify params resolve."""
    template = RunbookTemplateBody(
        title="sub",
        description="...",
        steps=[
            OperationCallStep(
                id="provision",
                title="Provision VM",
                body="connect to ${run.target}",
                type="operation_call",
                op_id="vm.create",
                params={"size": "${run.params.size}", "host": "${run.target}"},
                verify=OperationCallVerify(
                    type="operation_call",
                    op_id="vm.show",
                    params={"host": "${run.target}"},
                    expect={"size": "${run.params.size}"},
                ),
            ),
        ],
    )
    body = current_step_body(
        template,
        "provision",
        target="vc-01",
        params={"size": "large"},
    )
    assert body.body == "connect to vc-01"
    assert body.op_id == "vm.create"
    assert body.params == {"size": "large", "host": "vc-01"}
    assert body.verify.type == "operation_call"
    assert body.verify.params == {"host": "vc-01"}
    assert body.verify.expect == {"size": "large"}


def test_advance_pure_function() -> None:
    """Same inputs → same outputs across multiple invocations.

    Property-style purity check: the engine has no hidden state, so
    two invocations with identical args produce equal outcomes. The
    dataclass is frozen + slots, so equality is structural.
    """
    template = _five_step_template()
    args = {
        "current_step_id": "step-2",
        "previous_step_state": "in_progress",
        "target": "vc-01",
        "params": {},
        "verify_response": _confirm_response("yes"),
    }
    outcome_a = advance(template, **args)  # type: ignore[arg-type]
    outcome_b = advance(template, **args)  # type: ignore[arg-type]
    assert outcome_a == outcome_b
    assert isinstance(outcome_a, AdvanceOutcome)


def test_advance_failed_outcome_carries_no_next_step() -> None:
    """A ``failed`` outcome on any step (terminal or not) carries no next_step_body.

    Locks the contract: T3's writer must not stumble into "the verify
    failed but here's the next step anyway" -- the failed outcome
    terminates this advance call's transition.
    """
    template = _five_step_template()
    outcome = advance(
        template,
        current_step_id="step-2",
        previous_step_state="in_progress",
        target="vc-01",
        params={},
        verify_response=_confirm_response("no"),
    )
    assert outcome.kind == "failed"
    assert outcome.next_step_body is None
    assert outcome.completed_at is None
