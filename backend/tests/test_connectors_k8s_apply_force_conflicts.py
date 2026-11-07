# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``k8s.apply`` and ``force_conflicts``: change an object another tool applied.

The fake API server below keeps one object whose fields are owned by
``kubectl-client-side-apply``. Server-side apply under the owner ``meho``
answers 409 unless the request carries ``force_conflicts``. These tests pin:

* without force, the conflict is an error that names the owner and the
  fields and says that ``force_conflicts: true`` takes them over;
* with force, the apply goes through and sends the force switch;
* the dry-run of a forced apply lists the fields it takes over, for the
  approval request;
* a dry-run without force shows the conflict, and a non-conflict failure
  with force is still an error;
* the op schema offers ``force_conflicts`` as a boolean, default false.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from meho_backplane.connectors.kubernetes.ops_write_dangerous import (
    ApplyRejectedError,
    _apply_one,
)
from meho_backplane.connectors.kubernetes.ops_write_meta import WRITE_DANGEROUS_OPS

from .test_connectors_k8s_write import (
    _SINGLE_MANIFEST,
    _TARGET,
    _applied_obj,
    _awaitable,
    _dyn_patch,
    _FakeApplyResponse,
    _make_connector,
    _make_operator,
    _patch_kubeconfig,
    _status_failure,
)

_OWNER = "kubectl-client-side-apply"
_FIELDS = (".spec.replicas", ".spec.template")


def _conflict_response() -> _FakeApplyResponse:
    causes = [
        {
            "reason": "FieldManagerConflict",
            "message": f'conflict with "{_OWNER}" using apps/v1: {field}',
            "field": field,
        }
        for field in _FIELDS
    ]
    body = {
        "kind": "Status",
        "status": "Failure",
        "reason": "Conflict",
        "code": 409,
        "message": f"Apply failed with {len(causes)} conflicts",
        "details": {"name": "web", "kind": "deployments", "causes": causes},
    }
    return _FakeApplyResponse(409, json.dumps(body), reason="Conflict")


def _owned_by_other_tool() -> Any:
    """A ``server_side_apply`` stub: 409 unless the request forces."""

    async def _ssa(_resource: Any, **kwargs: Any) -> _FakeApplyResponse:
        if kwargs.get("force_conflicts"):
            return _applied_obj()
        return _conflict_response()

    return AsyncMock(side_effect=_ssa)


async def _run_apply(ssa: AsyncMock, **params: Any) -> dict[str, Any]:
    conn = _make_connector()
    dyn = MagicMock()
    dyn.resources.get = AsyncMock(return_value=MagicMock())
    dyn.server_side_apply = ssa
    with _patch_kubeconfig(), _dyn_patch() as dyn_cls:
        dyn_cls.return_value = _awaitable(dyn)
        return await conn.k8s_apply(
            operator=_make_operator(),
            target=_TARGET,
            params={"manifest": _SINGLE_MANIFEST, **params},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", ["none", "server"])
async def test_conflict_without_force_names_owner_fields_and_the_fix(dry_run: str) -> None:
    ssa = _owned_by_other_tool()
    with pytest.raises(ApplyRejectedError) as excinfo:
        await _run_apply(ssa, dry_run=dry_run)
    exc = excinfo.value
    text = str(exc)
    assert exc.http_status == 409
    assert _OWNER in text
    assert all(field in text for field in _FIELDS)
    assert "force_conflicts: true" in text
    assert exc.upstream_message is not None
    assert "force_conflicts: true" in exc.upstream_message
    assert "force_conflicts" not in ssa.call_args.kwargs


@pytest.mark.asyncio
async def test_force_takes_over_and_sends_the_force_switch() -> None:
    ssa = _owned_by_other_tool()
    result = await _run_apply(ssa, force_conflicts=True)
    assert result["force_conflicts"] is True
    assert result["dry_run"] is False
    assert result["applied"][0]["resource_version"] == "123"
    assert ssa.call_count == 1
    assert ssa.call_args.kwargs["force_conflicts"] is True
    assert "takes_over" not in result["applied"][0]


@pytest.mark.asyncio
async def test_force_false_is_the_default_and_sends_no_force() -> None:
    ssa = AsyncMock(return_value=_applied_obj())
    result = await _run_apply(ssa, force_conflicts=False)
    assert result["force_conflicts"] is False
    assert "force_conflicts" not in ssa.call_args.kwargs


@pytest.mark.asyncio
async def test_forced_dry_run_lists_the_fields_it_takes_over() -> None:
    ssa = _owned_by_other_tool()
    result = await _run_apply(ssa, force_conflicts=True, dry_run="server")
    assert result["applied"][0]["takes_over"] == [
        {"owner": _OWNER, "field": field} for field in _FIELDS
    ]
    # First call checks for conflicts without force; second is the forced dry-run.
    assert [c.kwargs.get("force_conflicts") for c in ssa.call_args_list] == [None, True]
    assert all(c.kwargs["dry_run"] == "All" for c in ssa.call_args_list)


@pytest.mark.asyncio
async def test_forced_dry_run_with_no_conflict_lists_nothing() -> None:
    ssa = AsyncMock(return_value=_applied_obj())
    result = await _run_apply(ssa, force_conflicts=True, dry_run="server")
    assert result["applied"][0]["takes_over"] == []


@pytest.mark.asyncio
async def test_forced_dry_run_still_fails_on_another_rejection() -> None:
    ssa = AsyncMock(return_value=_status_failure(403, "Forbidden", "denied by a webhook"))
    with pytest.raises(ApplyRejectedError) as excinfo:
        await _run_apply(ssa, force_conflicts=True, dry_run="server")
    assert excinfo.value.http_status == 403
    assert "denied by a webhook" in str(excinfo.value)


@pytest.mark.asyncio
async def test_forced_real_apply_that_still_conflicts_does_not_hint_force() -> None:
    ssa = AsyncMock(return_value=_conflict_response())
    with pytest.raises(ApplyRejectedError) as excinfo:
        await _run_apply(ssa, force_conflicts=True)
    assert _OWNER in str(excinfo.value)
    assert "Set force_conflicts" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_many_conflicts_are_counted_not_all_named() -> None:
    causes = [
        {
            "reason": "FieldManagerConflict",
            "message": f'conflict with "{_OWNER}" using v1: .spec.f{i}',
            "field": f".spec.f{i}",
        }
        for i in range(30)
    ]
    body = json.dumps({"kind": "Status", "code": 409, "details": {"causes": causes}})
    dyn = MagicMock()
    dyn.resources.get = AsyncMock(return_value=MagicMock())
    dyn.server_side_apply = AsyncMock(return_value=_FakeApplyResponse(409, body, reason="Conflict"))
    doc = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "c"}}
    with pytest.raises(ApplyRejectedError) as excinfo:
        await _apply_one(dyn, doc, dry_run=False)
    assert ".spec.f19" in str(excinfo.value)
    assert ".spec.f20" not in str(excinfo.value)
    assert "10 more" in str(excinfo.value)


def test_schema_offers_force_conflicts_as_a_boolean_default_false() -> None:
    apply_op = next(op for op in WRITE_DANGEROUS_OPS if op.op_id == "k8s.apply")
    prop = apply_op.parameter_schema["properties"]["force_conflicts"]
    assert prop["type"] == "boolean"
    assert prop["default"] is False
    assert "force_conflicts" in apply_op.llm_instructions["parameter_hints"]
