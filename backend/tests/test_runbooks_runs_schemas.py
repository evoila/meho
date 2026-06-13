# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.runbooks.runs_schemas` (G12.3-T1, #1300).

Coverage matrix:

* :class:`StartRunRequest` -- minimal valid shape parses; missing
  :attr:`target` rejects with a clean ``ValidationError``.
* :class:`StepBody` -- round-trips through ``model_dump`` /
  re-parse with no information loss; the load-bearing opacity
  property is locked down by
  :func:`test_step_body_omits_future_step_fields` (no field name
  in the serialised dict suggests adjacent or future steps).
* :data:`VerifyResponse` discriminated union -- ``confirm`` answer
  vocabulary is closed to ``yes`` / ``no`` / ``escalate``;
  ``operation_call`` carries ``matched`` / ``actual``; an unknown
  ``type`` tag surfaces as a validation error.
* :data:`NextStepResponse` discriminated union -- ``kind="current_step"``
  routes to :class:`CurrentStepResponse`; ``kind="completed"`` routes
  to :class:`RunCompletedResponse`; unknown ``kind`` rejects.
* :class:`RunSummary` -- terminal-state runs carry ``position=None`` /
  ``current_step_id=None``.
* :class:`AbortRunRequest` -- empty :attr:`reason` rejects (audit
  trail must not be vacuous).
* :class:`ListRunsFilter` -- a bare filter (no fields) is valid.
* :class:`StepPosition` -- ``n >= 1``, ``n <= total`` enforced.
* :class:`ReassignRunRequest` -- empty :attr:`new_assignee` rejects
  (the ownership predicate must not be vacuous).

Shared idiom: use ``TypeAdapter`` to exercise discriminated-union
aliases (``VerifyResponse``, ``NextStepResponse``) standalone -- they
are type aliases, not :class:`BaseModel` subclasses. Mirrors the
predecessor :mod:`backend.tests.test_runbooks_schemas`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from meho_backplane.runbooks.runs_schemas import (
    AbortRunRequest,
    ConfirmVerifyResponse,
    CurrentStepResponse,
    ListRunsFilter,
    NextStepResponse,
    OperationCallVerifyResponse,
    ReassignRunRequest,
    RunCompletedResponse,
    RunSummary,
    StartRunRequest,
    StepBody,
    StepBodyVerify,
    StepPosition,
    VerifyResponse,
)

# Discriminated unions are type aliases; ``TypeAdapter`` is the
# canonical way to drive them in tests.
_VERIFY_RESPONSE_ADAPTER = TypeAdapter(VerifyResponse)
_NEXT_STEP_RESPONSE_ADAPTER = TypeAdapter(NextStepResponse)

# A reusable verify shape for embedding inside :class:`StepBody`.
_CONFIRM_STEP_VERIFY = StepBodyVerify(type="confirm", prompt="Did it work?")


def _step_body(step_id: str = "drain-node") -> StepBody:
    """Build a minimal ``manual`` :class:`StepBody` for shape-level tests."""
    return StepBody(
        id=step_id,
        title="Drain the node",
        body="SSH in and cordon ${run.target}.",  # post-substitution at run time
        type="manual",
        verify=_CONFIRM_STEP_VERIFY,
    )


# ---------------------------------------------------------------------------
# StartRunRequest
# ---------------------------------------------------------------------------


def test_start_run_request_valid() -> None:
    req = StartRunRequest(
        template_slug="rotate-creds",
        target="vault-prod-01",
        params={"account": "service-a"},
    )
    assert req.template_slug == "rotate-creds"
    assert req.target == "vault-prod-01"
    assert req.params == {"account": "service-a"}


def test_start_run_request_target_required() -> None:
    with pytest.raises(ValidationError):
        StartRunRequest.model_validate({"template_slug": "rotate-creds", "params": {}})


# ---------------------------------------------------------------------------
# StepBody -- round-trip and the load-bearing opacity property
# ---------------------------------------------------------------------------


def test_step_body_round_trip() -> None:
    original = _step_body()
    dumped = original.model_dump()
    restored = StepBody.model_validate(dumped)
    assert restored == original


def test_step_body_omits_future_step_fields() -> None:
    """Locks the Initiative #1198 opacity floor at the schema layer.

    ``CurrentStepResponse`` is the wire surface that ``meho.runbook.next``
    returns on the non-completion path. The acceptance bar of the
    parent Initiative is that an agent / operator who parses this
    response cannot deduce the contents of any step other than the
    current one. Field names are themselves a signal -- a field called
    ``next_step`` would hint at the next step's existence even if the
    value were ``None`` -- so this regression checks the **field-name
    surface** of the response, not just its values.
    """
    response = CurrentStepResponse(
        run_id=uuid.uuid4(),
        template_slug="rotate-creds",
        template_version=3,
        position=StepPosition(n=2, total=7),
        current_step=_step_body(),
    )
    dumped = response.model_dump()

    # Walk every dict key in the serialised shape (the wire surface).
    def _all_keys(value: object) -> list[str]:
        if isinstance(value, dict):
            collected: list[str] = []
            for key, nested in value.items():
                if isinstance(key, str):
                    collected.append(key)
                collected.extend(_all_keys(nested))
            return collected
        if isinstance(value, list):
            return [k for nested in value for k in _all_keys(nested)]
        return []

    keys_lower = {k.lower() for k in _all_keys(dumped)}
    # Forbidden field-name fragments -- any of these would leak
    # adjacent or future step content (or the full template).
    forbidden = {
        "next_step",
        "next_steps",
        "following_step",
        "following_steps",
        "template_body",
        "all_steps",
        "previous_step",
        "previous_steps",
        "remaining_steps",
        "future_steps",
    }
    for fragment in forbidden:
        for key in keys_lower:
            assert fragment not in key, f"leaked future/adjacent-step hint in field name: {key!r}"

    # And the bare ``steps`` field name (singular ``step`` is allowed
    # because ``current_step`` is the legitimate field). Check exactly,
    # not as a substring, so ``current_step`` does not match.
    assert "steps" not in keys_lower


# ---------------------------------------------------------------------------
# VerifyResponse discriminated union
# ---------------------------------------------------------------------------


def test_confirm_verify_response_only_yes_no_escalate() -> None:
    for answer in ("yes", "no", "escalate"):
        parsed = _VERIFY_RESPONSE_ADAPTER.validate_python({"type": "confirm", "answer": answer})
        assert isinstance(parsed, ConfirmVerifyResponse)
        assert parsed.answer == answer

    with pytest.raises(ValidationError):
        _VERIFY_RESPONSE_ADAPTER.validate_python({"type": "confirm", "answer": "maybe"})


def test_operation_call_verify_response_carries_matched_and_actual() -> None:
    parsed = _VERIFY_RESPONSE_ADAPTER.validate_python(
        {
            "type": "operation_call",
            "matched": True,
            "actual": {"power_state": "poweredOn", "ip": "10.0.0.42"},
        }
    )
    assert isinstance(parsed, OperationCallVerifyResponse)
    assert parsed.matched is True
    assert parsed.actual == {"power_state": "poweredOn", "ip": "10.0.0.42"}


def test_verify_response_discriminator_catches_unknown_type() -> None:
    with pytest.raises(ValidationError):
        _VERIFY_RESPONSE_ADAPTER.validate_python({"type": "magic", "answer": "yes"})


# ---------------------------------------------------------------------------
# NextStepResponse discriminated union
# ---------------------------------------------------------------------------


def test_next_step_response_current_kind() -> None:
    run_id = uuid.uuid4()
    parsed = _NEXT_STEP_RESPONSE_ADAPTER.validate_python(
        {
            "kind": "current_step",
            "run_id": str(run_id),
            "template_slug": "rotate-creds",
            "template_version": 3,
            "position": {"n": 2, "total": 7},
            "current_step": _step_body().model_dump(),
        }
    )
    assert isinstance(parsed, CurrentStepResponse)
    assert parsed.run_id == run_id
    assert parsed.template_version == 3


def test_next_step_response_completed_kind() -> None:
    run_id = uuid.uuid4()
    completed_at = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
    parsed = _NEXT_STEP_RESPONSE_ADAPTER.validate_python(
        {
            "kind": "completed",
            "run_id": str(run_id),
            "state": "completed",
            "completed_at": completed_at.isoformat(),
        }
    )
    assert isinstance(parsed, RunCompletedResponse)
    assert parsed.run_id == run_id
    assert parsed.completed_at == completed_at


def test_next_step_response_unknown_kind_rejected() -> None:
    with pytest.raises(ValidationError):
        _NEXT_STEP_RESPONSE_ADAPTER.validate_python(
            {
                "kind": "magic",
                "run_id": str(uuid.uuid4()),
                "state": "completed",
                "completed_at": "2026-05-28T12:00:00+00:00",
            }
        )


# ---------------------------------------------------------------------------
# RunSummary -- terminal-state shape
# ---------------------------------------------------------------------------


def test_run_summary_optional_position_null_for_completed() -> None:
    summary = RunSummary(
        run_id=uuid.uuid4(),
        template_slug="rotate-creds",
        template_version=3,
        assigned_to="op-alice",
        target="vault-prod-01",
        state="completed",
        started_at=datetime(2026, 5, 28, 11, 0, tzinfo=UTC),
        completed_at=datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
    )
    # Terminal-state runs carry no current step.
    assert summary.position is None
    assert summary.current_step_id is None
    assert summary.abandoned_at is None


# ---------------------------------------------------------------------------
# AbortRunRequest -- non-vacuous reason
# ---------------------------------------------------------------------------


def test_abort_run_request_reason_required() -> None:
    # Non-empty reason parses.
    req = AbortRunRequest(reason="operator cancelled — wrong target")
    assert req.reason == "operator cancelled — wrong target"

    # Empty reason rejects (would defeat the audit trail).
    with pytest.raises(ValidationError):
        AbortRunRequest.model_validate({"reason": ""})


# ---------------------------------------------------------------------------
# ListRunsFilter -- all fields optional
# ---------------------------------------------------------------------------


def test_list_runs_filter_all_optional() -> None:
    bare = ListRunsFilter()
    assert bare.assignee is None
    assert bare.status is None
    assert bare.template_slug is None


# ---------------------------------------------------------------------------
# StepPosition -- bounds
# ---------------------------------------------------------------------------


def test_position_n_at_least_1() -> None:
    # n must be >= 1 (1-indexed by contract).
    with pytest.raises(ValidationError):
        StepPosition(n=0, total=10)


def test_position_n_le_total() -> None:
    # n cannot exceed total.
    with pytest.raises(ValidationError):
        StepPosition(n=11, total=10)


# ---------------------------------------------------------------------------
# ReassignRunRequest -- non-vacuous assignee
# ---------------------------------------------------------------------------


def test_reassign_request_new_assignee_non_empty() -> None:
    # Non-empty assignee parses.
    req = ReassignRunRequest(new_assignee="op-bob")
    assert req.new_assignee == "op-bob"

    # Empty assignee rejects (the ownership predicate cannot be vacuous).
    with pytest.raises(ValidationError):
        ReassignRunRequest.model_validate({"new_assignee": ""})
