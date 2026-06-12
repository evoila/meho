# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.runbooks.schemas` (G12.2-T1, #1295).

Coverage matrix:

* Step discriminated union -- both step types parse; an unknown ``type``
  tag, a missing ``id``, and an id violating :data:`STEP_ID_PATTERN` all
  reject.
* Verify discriminated union -- both verify types parse; an unknown
  ``type`` tag rejects.
* :func:`validate_substitutions` -- ``${run.target}`` and
  ``${run.params.X}`` are accepted everywhere a template carries a
  string (body, op params, verify params, verify expect); every other
  pattern (``${run.bad}``, nested ``${run.params.X.Y}``, capitalised
  ``${run.params.WithCaps}``, arbitrary ``${anything_else}``) is rejected
  with the documented message.
* :meth:`RunbookTemplateBody._validate_step_ids_unique_and_substitutions_allowlisted`
  -- duplicate step ids reject.
* Tool request/response shapes -- the fork-info path on
  :class:`EditTemplateResponse`, optional filter fields, and a template
  body round-trip through ``model_dump`` / re-parse with no information
  loss.
* The :func:`validate_substitutions` helper is importable for G12.3 reuse.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from meho_backplane.runbooks.schemas import (
    ConfirmVerify,
    EditTemplateResponse,
    ForkInfo,
    ListTemplatesFilter,
    ManualStep,
    OperationCallStep,
    OperationCallVerify,
    RunbookTemplateBody,
    Step,
    Verify,
    validate_substitutions,
)

# A reusable TypeAdapter so the discriminated-union aliases can be
# exercised standalone (they are type aliases, not BaseModel subclasses).
_STEP_ADAPTER = TypeAdapter(Step)
_VERIFY_ADAPTER = TypeAdapter(Verify)

_CONFIRM_VERIFY: dict[str, object] = {"type": "confirm", "prompt": "Did it work?"}


# ---------------------------------------------------------------------------
# Step shape
# ---------------------------------------------------------------------------


def test_operation_call_step_valid() -> None:
    step = _STEP_ADAPTER.validate_python(
        {
            "id": "create-vm",
            "title": "Create the VM",
            "body": "Provision the guest.",
            "type": "operation_call",
            "op_id": "vmware.composite.vm.create",
            "params": {"name": "web-01"},
            "verify": _CONFIRM_VERIFY,
        }
    )
    assert isinstance(step, OperationCallStep)
    assert step.op_id == "vmware.composite.vm.create"
    assert isinstance(step.verify, ConfirmVerify)


def test_manual_step_valid() -> None:
    step = _STEP_ADAPTER.validate_python(
        {
            "id": "drain-node",
            "title": "Drain the node",
            "body": "SSH in and cordon the node.",
            "type": "manual",
            "verify": _CONFIRM_VERIFY,
        }
    )
    assert isinstance(step, ManualStep)
    assert step.id == "drain-node"


def test_step_unknown_type_rejected() -> None:
    with pytest.raises(ValidationError):
        _STEP_ADAPTER.validate_python(
            {
                "id": "x",
                "title": "t",
                "body": "b",
                "type": "magic",
                "verify": _CONFIRM_VERIFY,
            }
        )


def test_step_missing_id_rejected() -> None:
    with pytest.raises(ValidationError):
        _STEP_ADAPTER.validate_python(
            {
                "title": "t",
                "body": "b",
                "type": "manual",
                "verify": _CONFIRM_VERIFY,
            }
        )


def test_step_id_pattern_rejected() -> None:
    with pytest.raises(ValidationError):
        _STEP_ADAPTER.validate_python(
            {
                "id": "Has-Caps",
                "title": "t",
                "body": "b",
                "type": "manual",
                "verify": _CONFIRM_VERIFY,
            }
        )


# ---------------------------------------------------------------------------
# Verify shape
# ---------------------------------------------------------------------------


def test_confirm_verify_valid() -> None:
    verify = _VERIFY_ADAPTER.validate_python(_CONFIRM_VERIFY)
    assert isinstance(verify, ConfirmVerify)
    assert verify.prompt == "Did it work?"


def test_operation_call_verify_valid() -> None:
    verify = _VERIFY_ADAPTER.validate_python(
        {
            "type": "operation_call",
            "op_id": "vmware.vm.power_state",
            "params": {"vm": "web-01"},
            "expect": {"power_state": "poweredOn"},
        }
    )
    assert isinstance(verify, OperationCallVerify)
    assert verify.expect == {"power_state": "poweredOn"}


def test_verify_unknown_type_rejected() -> None:
    with pytest.raises(ValidationError):
        _VERIFY_ADAPTER.validate_python({"type": "magic", "prompt": "?"})


# ---------------------------------------------------------------------------
# Substitution allowlist
# ---------------------------------------------------------------------------


def test_substitution_allowed() -> None:
    # ``${run.target}`` and ``${run.params.X}`` surface in every place a
    # template carries a string: step body, op-call params, verify params,
    # and verify expect. The model validator must accept all of them.
    body = RunbookTemplateBody(
        title="Rotate creds on ${run.target}",
        description="d",
        steps=[
            OperationCallStep(
                id="rotate",
                title="Rotate",
                body="Rotate on ${run.target} for ${run.params.account}",
                type="operation_call",
                op_id="vault.kv.rotate",
                params={"path": "${run.params.secret_path}", "host": "${run.target}"},
                verify=OperationCallVerify(
                    type="operation_call",
                    op_id="vault.kv.read",
                    params={"path": "${run.params.secret_path}"},
                    expect={"target": "${run.target}"},
                ),
            ),
        ],
    )
    assert body.steps[0].id == "rotate"


@pytest.mark.parametrize(
    "bad",
    [
        "${anything_else}",
        "${run.bad}",
        "${run.params.X.Y}",  # nested path -- only one flat level allowed
        "${run.params.WithCaps}",  # capital letters rejected
    ],
)
def test_substitution_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="disallowed substitution pattern"):
        validate_substitutions(bad)

    # And the same pattern is rejected when reached through a template
    # body (the publish-time defense-in-depth path).
    with pytest.raises(ValidationError, match="disallowed substitution pattern"):
        RunbookTemplateBody(
            title="t",
            description="d",
            steps=[
                ManualStep(
                    id="s1",
                    title="t",
                    body=f"do {bad}",
                    type="manual",
                    verify=ConfirmVerify(type="confirm", prompt="ok?"),
                ),
            ],
        )


# ---------------------------------------------------------------------------
# Template-body invariants
# ---------------------------------------------------------------------------


def test_step_ids_must_be_unique() -> None:
    with pytest.raises(ValidationError, match="duplicate step id"):
        RunbookTemplateBody(
            title="t",
            description="d",
            steps=[
                ManualStep(
                    id="dup",
                    title="a",
                    body="b",
                    type="manual",
                    verify=ConfirmVerify(type="confirm", prompt="?"),
                ),
                ManualStep(
                    id="dup",
                    title="c",
                    body="d",
                    type="manual",
                    verify=ConfirmVerify(type="confirm", prompt="?"),
                ),
            ],
        )


# ---------------------------------------------------------------------------
# Tool request / response shapes
# ---------------------------------------------------------------------------


def test_fork_info_response_shape() -> None:
    forked = EditTemplateResponse(
        slug="drain-node",
        version=2,
        status="draft",
        forked_from=ForkInfo(slug="drain-node", version=1, in_flight_run_count=3),
    )
    assert forked.forked_from is not None
    assert forked.forked_from.in_flight_run_count == 3

    draft_edit = EditTemplateResponse(slug="drain-node", version=1, status="draft")
    assert draft_edit.forked_from is None


def test_list_templates_filter_optional_fields() -> None:
    assert ListTemplatesFilter().status is None
    assert ListTemplatesFilter().target_kind is None

    filtered = ListTemplatesFilter(status="published", target_kind="k8s")
    assert filtered.status == "published"
    assert filtered.target_kind == "k8s"


def test_template_body_roundtrip() -> None:
    body = RunbookTemplateBody(
        title="Drain a node",
        description="Cordon, drain, verify empty.",
        target_kind="k8s",
        steps=[
            ManualStep(
                id="cordon",
                title="Cordon",
                body="kubectl cordon ${run.target}",
                type="manual",
                verify=ConfirmVerify(type="confirm", prompt="Cordoned?"),
            ),
            OperationCallStep(
                id="verify-empty",
                title="Verify empty",
                body="Check no pods remain.",
                type="operation_call",
                op_id="k8s.node.pod_count",
                params={"node": "${run.target}"},
                verify=OperationCallVerify(
                    type="operation_call",
                    op_id="k8s.node.pod_count",
                    params={"node": "${run.target}"},
                    expect={"count": 0},
                ),
            ),
        ],
    )

    dumped = body.model_dump()
    reparsed = RunbookTemplateBody.model_validate(dumped)
    assert reparsed == body


def test_validate_substitutions_helper_exported() -> None:
    # G12.3 reuses this helper at advance time -- the import must work and
    # the function must accept the allowlisted forms without raising.
    from meho_backplane.runbooks.schemas import validate_substitutions as imported

    imported("${run.target}")
    imported({"k": ["${run.params.foo}", 1, None]})


def test_datetime_carrying_response_parses() -> None:
    # Sanity: the datetime-bearing summary/show shapes parse a tz-aware
    # value (the column type the G12.2 service lifts from the model).
    from meho_backplane.runbooks.schemas import TemplateSummary

    summary = TemplateSummary(
        slug="drain-node",
        version=1,
        title="Drain a node",
        status="published",
        target_kind=None,
        edited_at=datetime.now(UTC),
    )
    assert summary.target_kind is None
