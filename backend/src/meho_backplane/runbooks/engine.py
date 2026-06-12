# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pure-function step-execution engine for runbook runs (G12.3-T2, #1301).

The **load-bearing isolation** for Initiative #1198's adherence floor.
Step opacity, verify gating, and the state-machine transitions all live
in this module. Keeping it pure (no DB session, no HTTP, no MCP, no
contextvars, no clocks except where an outcome explicitly carries a
``completed_at`` produced by the caller) lets the property-style
opacity tests be reviewed in isolation without transport noise.

Two functions, both pure:

* :func:`current_step_body` -- look up a step by id in the template and
  return its substituted :class:`StepBody`. This is the **opacity
  function**: by signature it returns *only* the requested step. There
  is no overload that returns multiple steps.
* :func:`advance` -- drive the state machine one step forward, taking
  the run's current position and the operator's verify response, and
  returning an :class:`AdvanceOutcome` that describes whether the run
  progresses, completes, or fails. The service layer (G12.3-T3) reads
  this outcome and writes the corresponding storage-level rows; the
  engine never touches the DB.

The engine never returns information about steps other than the one
the caller asked about (or the immediately-next one, on successful
advance). The two LOAD-BEARING regression tests (
:func:`test_current_step_body_returns_only_one_step`,
:func:`test_advance_outcome_next_step_body_is_only_next_step`) walk the
serialised outcomes and assert no other step ids leak.

Five typed exceptions communicate engine-level errors. T3's service
catches each and translates it into the typed wire error that the REST
routes (T5) and MCP tools (T6) surface as 400 / -32602:

* :class:`RunAlreadyCompletedError` -- ``advance`` called on a run that
  has already reached its terminal step.
* :class:`PreviousStepNotVerifiedError` -- ``advance`` called with the
  current step still ``pending``; only ``in_progress`` steps may
  advance.
* :class:`VerifyResponseRequiredError` -- ``advance`` called on a step
  whose verify gate requires a response without one provided.
* :class:`VerifyResponseMismatchError` -- the response shape does not
  match the step's verify type (a ``confirm`` step received an
  ``operation_call`` response or vice versa).
* :class:`ConfirmVerifyAnswerNotYesError` -- raised only when a caller
  explicitly tightens this invariant; the engine itself models ``no`` /
  ``escalate`` as legitimate ``failed`` transitions, not exceptions.

**Verify match semantics** (`verify.type='operation_call'`): structural
equality + presence. Every key in the substituted ``expect`` must be
present in the response's ``actual`` with structurally equal value.
Extra keys in ``actual`` are ignored. Dicts compared recursively. Lists
compared element-wise (also with recursion). Scalars compared by
``==``. No JSONPath, no comparison operators, no boolean composition --
per Initiative #1198 scope + the determinism postulate (#1177).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from meho_backplane.runbooks.runs_schemas import (
    ConfirmVerifyResponse,
    OperationCallVerifyResponse,
    StepBody,
    StepBodyVerify,
    VerifyResponse,
)
from meho_backplane.runbooks.schemas import (
    ConfirmVerify,
    OperationCallStep,
    OperationCallVerify,
    RunbookTemplateBody,
    Step,
)
from meho_backplane.runbooks.substitution import resolve_substitutions

__all__ = [
    "AdvanceOutcome",
    "ConfirmVerifyAnswerNotYesError",
    "PreviousStepNotVerifiedError",
    "RunAlreadyCompletedError",
    "VerifyResponseMismatchError",
    "VerifyResponseRequiredError",
    "advance",
    "current_step_body",
]


class RunAlreadyCompletedError(ValueError):
    """Raised when :func:`advance` is called past the terminal step."""


class PreviousStepNotVerifiedError(ValueError):
    """Raised when :func:`advance` is called with the current step still ``pending``.

    The state-machine contract: only a step the operator has explicitly
    started (state ``in_progress``) may advance. A ``pending`` step has
    not been picked up; a ``verified`` step has already advanced; a
    ``failed`` step terminates the run path through the engine -- none
    of these are valid inputs to ``advance``.
    """


class VerifyResponseRequiredError(ValueError):
    """Raised when ``advance`` is called without a *verify_response* but one is required."""


class VerifyResponseMismatchError(ValueError):
    """Raised when the *verify_response* shape does not match the step's verify type.

    A ``confirm`` step requires a :class:`ConfirmVerifyResponse`; an
    ``operation_call`` step requires an :class:`OperationCallVerifyResponse`.
    Mixing shapes is a contract violation by the caller (T3's service or
    a direct unit-test caller), not a malformed wire payload (the
    Pydantic discriminated union catches the latter at the boundary).
    """


class ConfirmVerifyAnswerNotYesError(ValueError):
    """Reserved for callers that explicitly want a ``no``/``escalate`` to raise.

    The engine itself does **not** raise this -- ``no`` and ``escalate``
    are first-class ``failed`` transitions per Initiative #1198, not
    error states. The class is exported so future surfaces that want to
    treat a ``no``/``escalate`` as a hard error (e.g. a smoke test in
    CI) can raise it without inventing their own exception type.
    """


@dataclass(frozen=True, slots=True)
class AdvanceOutcome:
    """The result of one :func:`advance` call.

    Carries exactly one of three transitions:

    * ``kind="next_step"`` -- the current step's verify passed; the next
      step's body is in :attr:`next_step_body`. T3 writes the previous
      step's state to ``verified`` and inserts the next step's row with
      state ``pending``.
    * ``kind="completed"`` -- the current step's verify passed and the
      step was the last one; :attr:`completed_at` carries the engine's
      transition timestamp (caller-supplied, so the engine stays
      clock-free). T3 writes the run's state to ``completed`` with
      ``completed_at`` populated.
    * ``kind="failed"`` -- the verify failed (operator answered
      ``no``/``escalate``, or the ``operation_call`` verify's ``actual``
      did not match ``expect``). T3 writes the current step's state to
      ``failed``; the run as a whole remains ``in_progress`` and the
      operator either retries (by re-issuing ``runbook_next`` with a
      corrected response) or aborts.

    :attr:`verify_response_persisted` is the canonical response shape T3
    should write to ``runbook_run_step_states.verify_response``. For
    ``confirm`` steps this echoes the caller's response verbatim. For
    ``operation_call`` steps the engine constructs a fresh
    :class:`OperationCallVerifyResponse` with :attr:`matched` set to the
    engine's verdict so the persisted row carries the substrate's
    decision, not the caller's claim.
    """

    kind: Literal["next_step", "completed", "failed"]
    next_step_body: StepBody | None = None
    completed_at: datetime | None = None
    verify_response_persisted: VerifyResponse | None = None


def current_step_body(
    template_body: RunbookTemplateBody,
    current_step_id: str,
    *,
    target: str,
    params: dict[str, object],
) -> StepBody:
    """Build the opaque :class:`StepBody` for the operator's current position.

    Looks up the step by id in :attr:`RunbookTemplateBody.steps`, applies
    :func:`resolve_substitutions` to every substitution-bearing field
    (``body``; ``op_id``/``params`` for operation-call steps;
    ``prompt`` / ``params`` / ``expect`` for the verify gate), and
    returns a :class:`StepBody` reflecting only that step.

    **This is the opacity function.** By signature it returns exactly
    one step body; there is no overload that returns multiple steps,
    the surrounding step list, or any structural hint about adjacent
    positions. The return value is what flows out through T3's service
    into the ``CurrentStepResponse`` the operator sees.

    Raises :class:`KeyError` if *current_step_id* is not a step in
    *template_body*. T3 translates that into the typed wire error.
    """
    step = _find_step(template_body, current_step_id)
    return _build_step_body(step, target=target, params=params)


def advance(
    template_body: RunbookTemplateBody,
    current_step_id: str,
    previous_step_state: str,
    *,
    target: str,
    params: dict[str, object],
    verify_response: VerifyResponse | None = None,
    completed_at: datetime | None = None,
) -> AdvanceOutcome:
    """Drive the state machine one step forward.

    Pre-conditions:

    * *current_step_id* names a step present in *template_body* (raises
      :class:`KeyError` otherwise).
    * *previous_step_state* describes the state of the **current step**
      -- the engine's parameter name reflects that the caller has
      already locked the "previous" step (the one before this advance
      call) as ``verified``; what's pending is the verify of the step
      we're advancing past. Must be ``in_progress`` -- a ``pending``
      step has not been picked up by the operator, so there is nothing
      to verify (raises :class:`PreviousStepNotVerifiedError`).

    Behavior by the current step's :attr:`verify.type`:

    * ``confirm``: the *verify_response* must be a
      :class:`ConfirmVerifyResponse`. ``answer="yes"`` produces a
      ``next_step`` outcome (or ``completed`` if the current step is
      the last). ``"no"`` and ``"escalate"`` produce a ``failed``
      outcome with the response echoed verbatim into
      :attr:`AdvanceOutcome.verify_response_persisted`.
    * ``operation_call``: the *verify_response* must be an
      :class:`OperationCallVerifyResponse` whose :attr:`actual` carries
      the dispatched call's return value (T3 populates this from
      ``call_operation()`` before invoking the engine; the engine
      itself does not dispatch). The engine compares ``actual`` to the
      substituted ``expect`` by structural equality + presence and
      surfaces the verdict as :attr:`OperationCallVerifyResponse.matched`
      on a freshly constructed response (so the persisted row carries
      the substrate's decision, not a caller-supplied claim).

    Missing *verify_response* on a step whose verify gate requires one
    raises :class:`VerifyResponseRequiredError`; a response of the
    wrong shape (``confirm`` step + ``operation_call`` response or vice
    versa) raises :class:`VerifyResponseMismatchError`.

    *completed_at* is supplied by the caller when the engine determines
    the step's verify passed *and* the step is the last one; the engine
    is otherwise clock-free. T3 supplies ``datetime.now(UTC)`` from the
    service layer so test code can pin the timestamp deterministically.
    A ``completed`` outcome with no *completed_at* falls back to the
    parsed ``StartRunRequest``-side default of ``None``; T3's writer
    treats that as "use the row's CURRENT_TIMESTAMP default".
    """
    if previous_step_state != "in_progress":
        raise PreviousStepNotVerifiedError(
            f"current step {current_step_id!r} is in state {previous_step_state!r}; "
            f"only 'in_progress' may advance",
        )

    step = _find_step(template_body, current_step_id)
    verify = step.verify

    verdict: tuple[bool, VerifyResponse]
    if isinstance(verify, ConfirmVerify):
        verdict = _evaluate_confirm(verify_response)
    elif isinstance(verify, OperationCallVerify):
        verdict = _evaluate_operation_call(verify, verify_response, target=target, params=params)
    else:
        # Defensive: the Verify discriminated union is closed over
        # ConfirmVerify | OperationCallVerify, so this branch is
        # unreachable from a Pydantic-validated template. Surface as a
        # hard error so a future expansion that forgets to extend the
        # engine fails closed rather than silently.
        raise VerifyResponseMismatchError(
            f"unknown verify type on step {current_step_id!r}: {type(verify).__name__}",
        )

    verify_passed, response_to_persist = verdict

    if not verify_passed:
        return AdvanceOutcome(
            kind="failed",
            verify_response_persisted=response_to_persist,
        )

    next_step = _next_step(template_body, current_step_id)
    if next_step is None:
        return AdvanceOutcome(
            kind="completed",
            completed_at=completed_at,
            verify_response_persisted=response_to_persist,
        )

    return AdvanceOutcome(
        kind="next_step",
        next_step_body=_build_step_body(next_step, target=target, params=params),
        verify_response_persisted=response_to_persist,
    )


# ---------------------------------------------------------------------------
# Internal helpers -- not exported.
# ---------------------------------------------------------------------------


def _find_step(template_body: RunbookTemplateBody, step_id: str) -> Step:
    """Return the step with *step_id* or raise :class:`KeyError`."""
    for step in template_body.steps:
        if step.id == step_id:
            return step
    raise KeyError(step_id)


def _next_step(template_body: RunbookTemplateBody, current_step_id: str) -> Step | None:
    """Return the step that follows *current_step_id* or ``None`` if it is the last."""
    steps = template_body.steps
    for index, step in enumerate(steps):
        if step.id == current_step_id:
            if index + 1 < len(steps):
                return steps[index + 1]
            return None
    raise KeyError(current_step_id)


def _build_step_body(step: Step, *, target: str, params: dict[str, object]) -> StepBody:
    """Construct a :class:`StepBody` for *step* with substitutions resolved.

    Branches on the step type to keep the populated fields consistent
    with the wire shape from T1 (#1300): ``op_id`` / ``params`` carried
    only for ``operation_call`` steps; ``prompt`` / ``op_id`` /
    ``params`` / ``expect`` on the embedded :class:`StepBodyVerify`
    populated only for the corresponding verify type.
    """
    body = _resolve_str(step.body, target=target, params=params)
    verify_body = _build_step_body_verify(step.verify, target=target, params=params)
    if isinstance(step, OperationCallStep):
        return StepBody(
            id=step.id,
            title=step.title,
            body=body,
            type="operation_call",
            op_id=step.op_id,
            params=_resolve_dict(step.params, target=target, params=params),
            verify=verify_body,
        )
    # ManualStep -- closed over Step union; the only other variant.
    return StepBody(
        id=step.id,
        title=step.title,
        body=body,
        type="manual",
        verify=verify_body,
    )


def _build_step_body_verify(
    verify: ConfirmVerify | OperationCallVerify,
    *,
    target: str,
    params: dict[str, object],
) -> StepBodyVerify:
    """Construct the substituted :class:`StepBodyVerify`."""
    if isinstance(verify, ConfirmVerify):
        return StepBodyVerify(
            type="confirm",
            prompt=_resolve_str(verify.prompt, target=target, params=params),
        )
    # OperationCallVerify -- closed over Verify union; the only other variant.
    return StepBodyVerify(
        type="operation_call",
        op_id=verify.op_id,
        params=_resolve_dict(verify.params, target=target, params=params),
        expect=_resolve_dict(verify.expect, target=target, params=params),
    )


def _evaluate_confirm(
    verify_response: VerifyResponse | None,
) -> tuple[bool, ConfirmVerifyResponse]:
    """Return ``(passed, persisted_response)`` for a ``confirm`` verify step.

    Raises :class:`VerifyResponseRequiredError` if *verify_response* is
    ``None`` and :class:`VerifyResponseMismatchError` if the response
    is not a :class:`ConfirmVerifyResponse`.
    """
    if verify_response is None:
        raise VerifyResponseRequiredError("confirm step requires a verify_response")
    if not isinstance(verify_response, ConfirmVerifyResponse):
        raise VerifyResponseMismatchError(
            f"confirm step received a non-confirm verify_response: "
            f"{type(verify_response).__name__}",
        )
    return verify_response.answer == "yes", verify_response


def _evaluate_operation_call(
    verify: OperationCallVerify,
    verify_response: VerifyResponse | None,
    *,
    target: str,
    params: dict[str, object],
) -> tuple[bool, OperationCallVerifyResponse]:
    """Return ``(passed, persisted_response)`` for an ``operation_call`` verify step.

    Substitutes ``${...}`` in *verify*'s ``expect`` before comparing,
    runs :func:`_matches` for structural-equality + presence, and
    constructs a fresh :class:`OperationCallVerifyResponse` whose
    :attr:`matched` reflects the engine's verdict (not the caller's
    claim) so the persisted audit row carries the substrate's decision.
    """
    if verify_response is None:
        raise VerifyResponseRequiredError("operation_call step requires a verify_response")
    if not isinstance(verify_response, OperationCallVerifyResponse):
        raise VerifyResponseMismatchError(
            f"operation_call step received a non-operation_call verify_response: "
            f"{type(verify_response).__name__}",
        )
    resolved_expect = _resolve_dict(verify.expect, target=target, params=params)
    matched = _matches(verify_response.actual, resolved_expect)
    return matched, OperationCallVerifyResponse(
        type="operation_call",
        matched=matched,
        actual=verify_response.actual,
    )


def _matches(actual: object, expect: object) -> bool:
    """Structural-equality + presence match -- the verify comparison contract.

    Every key in *expect* must be present in *actual* with structurally
    equal value. Extra keys in *actual* are ignored (the "presence"
    half). Dicts compared recursively. Lists compared element-wise
    (also with recursion). Scalars compared by ``==``.

    Deliberately minimal: no JSONPath, no operators, no boolean
    composition (per #1177's determinism postulate + Initiative
    #1198's verify-surface scope). A future authoring need for richer
    matching is a separate Initiative, not a quiet feature add here.
    """
    if isinstance(expect, dict):
        if not isinstance(actual, dict):
            return False
        for key, expected_value in expect.items():
            if key not in actual:
                return False
            if not _matches(actual[key], expected_value):
                return False
        return True
    if isinstance(expect, list):
        if not isinstance(actual, list) or len(actual) != len(expect):
            return False
        return all(_matches(a, e) for a, e in zip(actual, expect, strict=True))
    return actual == expect


def _resolve_str(value: str, *, target: str, params: dict[str, object]) -> str:
    """Type-narrow wrapper around :func:`resolve_substitutions` for a string."""
    resolved = resolve_substitutions(value, target=target, params=params)
    # resolve_substitutions returns str for str input -- the cast is
    # for the type checker, not for runtime.
    assert isinstance(resolved, str)
    return resolved


def _resolve_dict(
    value: dict[str, object],
    *,
    target: str,
    params: dict[str, object],
) -> dict[str, object]:
    """Type-narrow wrapper around :func:`resolve_substitutions` for a dict."""
    resolved = resolve_substitutions(value, target=target, params=params)
    assert isinstance(resolved, dict)
    return resolved
