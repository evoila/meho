# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``runbook_start`` / ``runbook_next`` / ``runbook_abort`` /
``runbook_reassign`` / ``runbook_list_runs`` MCP tools (G12.3-T6, #1313).

Covers the task's acceptance criteria for the five run-lifecycle tools
that wrap :class:`~meho_backplane.runbooks.run_service.RunbookRunService`
on the MCP transport:

* Registration: all five tools register against the G0.5 registry with
  strict 2020-12 ``inputSchema`` shapes; the MEHO-internal RBAC fields
  never reach the wire.
* RBAC: four tools (``runbook_start`` / ``runbook_next`` /
  ``runbook_abort`` / ``runbook_list_runs``) are ``OPERATOR``-callable;
  ``runbook_reassign`` is ``TENANT_ADMIN``-only.
* Typed-exception -> ``-32602`` mapping for the ten operator-actionable
  service + engine errors.
* The load-bearing ``runbook_next`` description carries the verbatim
  load-bearing strings (``OPACITY CONTRACT``, ``WHEN A STEP FAILS``,
  ``SINGLE-ASSIGNEE``, ``no skip, no force_advance``) -- a regression
  guard so a refactor that drops them surfaces here rather than as
  silently degraded agent UX.
* Structural opacity: ``runbook_next`` response carries exactly one
  step body (no future-step leakage); ``runbook_list_runs`` summaries
  carry no step contents.
* Single-assignee discipline at the tool boundary -- operators *and*
  admins are refused if they are not the run's assignee.
* Tenant isolation on the list surface.

The tests use the SQLite-backed default test DB (the ``runbook_runs``
and ``runbook_run_step_states`` tables are materialised by the autouse
``_default_database_url`` fixture's ``alembic upgrade head``); no
embedding / external service is involved, so every assertion runs
in-sandbox.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.schemas import (
    FingerprintResult,
    OperationResult,
    ProbeResult,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, Target
from meho_backplane.mcp.schemas import INVALID_PARAMS
from meho_backplane.operations import (
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.retrieval.embedding import EMBEDDING_DIMENSION
from meho_backplane.runbooks.run_service import RunbookRunService
from meho_backplane.runbooks.runs_schemas import StartRunRequest
from meho_backplane.runbooks.schemas import DraftTemplateRequest, PublishTemplateRequest
from meho_backplane.runbooks.service import RunbookTemplateService
from tests.mcp_test_fixtures import (
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_OPERATOR_TOOLS = {
    "runbook_start",
    "runbook_next",
    "runbook_abort",
    "runbook_list_runs",
}
_ADMIN_TOOLS = {"runbook_reassign"}
_ALL_TOOLS = _OPERATOR_TOOLS | _ADMIN_TOOLS


def _template_body(
    *,
    title: str = "Two-step procedure",
    steps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a minimal valid template-body wire dict with two manual steps.

    Two steps is the smallest shape that lets ``runbook_next`` advance
    *between* steps (rather than completing the run immediately).
    """
    if steps is None:
        steps = [
            {
                "id": "step-1",
                "title": "Step 1",
                "body": "Do the first thing.",
                "type": "manual",
                "verify": {"type": "confirm", "prompt": "Done?"},
            },
            {
                "id": "step-2",
                "title": "Step 2",
                "body": "Do the second thing.",
                "type": "manual",
                "verify": {"type": "confirm", "prompt": "Done?"},
            },
        ]
    return {
        "title": title,
        "description": "Procedure for tests.",
        "target_kind": "host",
        "steps": steps,
    }


async def _seed_published_template(
    *,
    tenant_id: uuid.UUID,
    operator_sub: str,
    slug: str,
    body: dict[str, Any] | None = None,
) -> None:
    """Helper: create + publish v1 of *slug* in *tenant_id*."""
    service = RunbookTemplateService()
    await service.create_draft(
        tenant_id=tenant_id,
        operator_sub=operator_sub,
        request=DraftTemplateRequest.model_validate(
            {"slug": slug, "body": body if body is not None else _template_body()}
        ),
    )
    await service.publish(
        tenant_id=tenant_id,
        request=PublishTemplateRequest(slug=slug, version=1),
    )


def _call(client: TestClient, name: str, arguments: dict[str, Any]) -> Any:
    """Issue a ``tools/call`` against *name* and return the parsed JSON-RPC body."""
    return post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    ).json()


def _result_payload(body: Any) -> Any:
    """Extract the parsed tool result from a successful ``tools/call`` body."""
    assert body["result"]["isError"] is False
    return json.loads(body["result"]["content"][0]["text"])


# ---------------------------------------------------------------------------
# Registration + tools/list shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_all_five_tools_registered_with_strict_schema(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: all five tools surface for a TENANT_ADMIN with strict input schemas."""
    client, _op = client_with_operator
    response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools_by_name = {t["name"]: t for t in response.json()["result"]["tools"]}

    for name in _ALL_TOOLS:
        assert name in tools_by_name, name
        schema = tools_by_name[name]["inputSchema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        # MEHO-internal RBAC fields never reach the wire.
        assert "required_role" not in tools_by_name[name]
        assert "op_class" not in tools_by_name[name]


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_admin_tool_hidden_from_operator_list(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: an OPERATOR sees the four operator tools; ``runbook_reassign`` is hidden."""
    client, _op = client_with_operator
    response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tool_names = {t["name"] for t in response.json()["result"]["tools"]}

    assert tool_names >= _OPERATOR_TOOLS
    assert not (_ADMIN_TOOLS & tool_names)


# ---------------------------------------------------------------------------
# runbook_start
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.asyncio
async def test_start_tool_invocation_success(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: operator can start a run; response carries step-1 body."""
    client, op = client_with_operator
    await _seed_published_template(tenant_id=op.tenant_id, operator_sub=op.sub, slug="r1")

    payload = _result_payload(
        _call(client, "runbook_start", {"template_slug": "r1", "target": "host-1"})
    )
    assert payload["kind"] == "current_step"
    assert payload["template_slug"] == "r1"
    assert payload["template_version"] == 1
    assert payload["current_step"]["id"] == "step-1"
    assert payload["position"]["n"] == 1
    assert payload["position"]["total"] == 2


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
@pytest.mark.asyncio
async def test_start_admin_can_call(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: TENANT_ADMIN can also start a run (admin implies operator).

    The role gate uses ``role_at_least`` -- TENANT_ADMIN ranks above
    OPERATOR so the admin sees and can invoke the OPERATOR-floored
    tools.
    """
    client, op = client_with_operator
    await _seed_published_template(tenant_id=op.tenant_id, operator_sub=op.sub, slug="r1")

    payload = _result_payload(
        _call(client, "runbook_start", {"template_slug": "r1", "target": "host-1"})
    )
    assert payload["kind"] == "current_step"
    assert payload["template_slug"] == "r1"


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.asyncio
async def test_start_deprecated_template_error_maps_to_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: ``DeprecatedTemplateError`` surfaces as ``-32602``."""
    client, op = client_with_operator
    service = RunbookTemplateService()
    await service.create_draft(
        tenant_id=op.tenant_id,
        operator_sub=op.sub,
        request=DraftTemplateRequest.model_validate({"slug": "old", "body": _template_body()}),
    )
    await service.publish(
        tenant_id=op.tenant_id, request=PublishTemplateRequest(slug="old", version=1)
    )
    await service.deprecate(
        tenant_id=op.tenant_id, request=PublishTemplateRequest(slug="old", version=1)
    )

    body = _call(client, "runbook_start", {"template_slug": "old", "target": "h"})
    assert body["error"]["code"] == INVALID_PARAMS


# ---------------------------------------------------------------------------
# runbook_next — opacity-load-bearing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.asyncio
async def test_next_confirm_yes_advances(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: confirm verify with ``answer='yes'`` advances to step 2."""
    client, op = client_with_operator
    await _seed_published_template(tenant_id=op.tenant_id, operator_sub=op.sub, slug="r1")
    started = _result_payload(
        _call(client, "runbook_start", {"template_slug": "r1", "target": "host-1"})
    )

    payload = _result_payload(
        _call(
            client,
            "runbook_next",
            {
                "run_id": started["run_id"],
                "last_verified": True,
                "verify_response": {"type": "confirm", "answer": "yes"},
            },
        )
    )
    assert payload["kind"] == "current_step"
    assert payload["current_step"]["id"] == "step-2"
    assert payload["position"]["n"] == 2


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.asyncio
async def test_next_confirm_no_transitions_to_failed(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: ``answer='no'`` on confirm -> ``PreviousStepFailedError`` -> ``-32602``.

    Mirrors the run-service contract: the engine flips the step to
    ``failed`` and the service surfaces :class:`PreviousStepFailedError`
    so the caller's next move is :func:`runbook_abort` rather than a
    spurious retry on a state the substrate no longer accepts.
    """
    client, op = client_with_operator
    await _seed_published_template(tenant_id=op.tenant_id, operator_sub=op.sub, slug="r1")
    started = _result_payload(
        _call(client, "runbook_start", {"template_slug": "r1", "target": "host-1"})
    )

    body = _call(
        client,
        "runbook_next",
        {
            "run_id": started["run_id"],
            "last_verified": False,
            "verify_response": {"type": "confirm", "answer": "no"},
        },
    )
    assert body["error"]["code"] == INVALID_PARAMS


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.asyncio
async def test_next_completes_at_last_step(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: verify on the last step -> ``RunCompletedResponse`` shape."""
    client, op = client_with_operator
    await _seed_published_template(
        tenant_id=op.tenant_id,
        operator_sub=op.sub,
        slug="r1",
        body=_template_body(
            steps=[
                {
                    "id": "only",
                    "title": "Only step",
                    "body": "Just one.",
                    "type": "manual",
                    "verify": {"type": "confirm", "prompt": "Done?"},
                }
            ]
        ),
    )
    started = _result_payload(
        _call(client, "runbook_start", {"template_slug": "r1", "target": "host-1"})
    )
    payload = _result_payload(
        _call(
            client,
            "runbook_next",
            {
                "run_id": started["run_id"],
                "last_verified": True,
                "verify_response": {"type": "confirm", "answer": "yes"},
            },
        )
    )
    assert payload["kind"] == "completed"
    assert payload["state"] == "completed"
    assert "current_step" not in payload


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.asyncio
async def test_next_response_opacity_property(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """LOAD-BEARING: ``runbook_next`` response carries exactly one step body.

    Serialise the response, assert exactly one step id appears (the
    current one), and that no other step ids leak into the JSON. Opacity
    is what makes Initiative #1198's adherence floor real -- the
    structural test guards refactors that might widen the response shape.
    """
    client, op = client_with_operator
    five_steps = [
        {
            "id": f"step-{i}",
            "title": f"Step {i}",
            "body": f"Body {i}",
            "type": "manual",
            "verify": {"type": "confirm", "prompt": "Done?"},
        }
        for i in range(1, 6)
    ]
    await _seed_published_template(
        tenant_id=op.tenant_id,
        operator_sub=op.sub,
        slug="r1",
        body=_template_body(steps=five_steps),
    )
    started = _result_payload(
        _call(client, "runbook_start", {"template_slug": "r1", "target": "host-1"})
    )
    payload = _result_payload(
        _call(
            client,
            "runbook_next",
            {
                "run_id": started["run_id"],
                "last_verified": True,
                "verify_response": {"type": "confirm", "answer": "yes"},
            },
        )
    )
    serialised = json.dumps(payload)
    # The response is on step-2 now -- step-1 (just verified) and
    # step-3 / step-4 / step-5 (future) must not appear in the wire shape.
    assert '"id": "step-2"' in serialised or '"id":"step-2"' in serialised
    for leak in ("step-1", "step-3", "step-4", "step-5"):
        assert leak not in serialised, f"step id leaked into response: {leak}"


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.asyncio
async def test_next_not_assignee_denied(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: operator who isn't the assignee -> ``NotRunAssigneeError`` -> ``-32602``."""
    client, op = client_with_operator
    await _seed_published_template(tenant_id=op.tenant_id, operator_sub=op.sub, slug="r1")
    # Start as a different operator via the service so the fixture
    # operator isn't the assignee.
    service = RunbookRunService()
    other_sub = "operator-other"
    started = await service.start_run(
        tenant_id=op.tenant_id,
        operator_sub=other_sub,
        request=StartRunRequest(template_slug="r1", target="host-1", params={}),
    )

    body = _call(
        client,
        "runbook_next",
        {
            "run_id": str(started.run_id),
            "last_verified": True,
            "verify_response": {"type": "confirm", "answer": "yes"},
        },
    )
    assert body["error"]["code"] == INVALID_PARAMS


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
@pytest.mark.asyncio
async def test_next_admin_non_assignee_still_denied(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: TENANT_ADMIN who isn't the assignee -> still ``-32602``.

    Single-assignee discipline is unconditional. The right way for a
    senior to take over is :func:`runbook_reassign`, not a role-based
    bypass on :func:`runbook_next`.
    """
    client, op = client_with_operator
    await _seed_published_template(tenant_id=op.tenant_id, operator_sub=op.sub, slug="r1")
    service = RunbookRunService()
    started = await service.start_run(
        tenant_id=op.tenant_id,
        operator_sub="operator-other",
        request=StartRunRequest(template_slug="r1", target="host-1", params={}),
    )

    body = _call(
        client,
        "runbook_next",
        {
            "run_id": str(started.run_id),
            "last_verified": True,
            "verify_response": {"type": "confirm", "answer": "yes"},
        },
    )
    assert body["error"]["code"] == INVALID_PARAMS


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_next_description_includes_load_bearing_text(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """LOAD-BEARING regression: the verbatim agent-UX strings live in the description.

    The :data:`~meho_backplane.mcp.tools.runbook_runs._NEXT_DESCRIPTION`
    text teaches the agent the opacity contract, the no-skip discipline,
    and the single-assignee enforcement. Each load-bearing string is
    asserted verbatim so a refactor that paraphrases or drops them
    surfaces here rather than as silently degraded agent UX.
    """
    client, _op = client_with_operator
    response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools_by_name = {t["name"]: t for t in response.json()["result"]["tools"]}

    next_desc = tools_by_name["runbook_next"]["description"]
    # The four load-bearing strings -- per the issue body's acceptance
    # criteria. Each one is a specific contract the agent needs to learn.
    assert "OPACITY CONTRACT" in next_desc
    assert "WHEN A STEP FAILS" in next_desc
    assert "SINGLE-ASSIGNEE" in next_desc
    assert "no skip, no force_advance" in next_desc


# ---------------------------------------------------------------------------
# runbook_abort
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.asyncio
async def test_abort_by_assignee_succeeds(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: assignee aborts -> state=abandoned."""
    client, op = client_with_operator
    await _seed_published_template(tenant_id=op.tenant_id, operator_sub=op.sub, slug="r1")
    started = _result_payload(
        _call(client, "runbook_start", {"template_slug": "r1", "target": "host-1"})
    )
    payload = _result_payload(
        _call(
            client,
            "runbook_abort",
            {"run_id": started["run_id"], "reason": "human cancelled"},
        )
    )
    assert payload["state"] == "abandoned"
    assert payload["run_id"] == started["run_id"]


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
@pytest.mark.asyncio
async def test_abort_by_admin_succeeds(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: TENANT_ADMIN can abort someone else's run.

    The service-level ``caller_is_admin`` allowance is wired through
    from the handler's role check on the JWT-bound operator. A senior
    taking over a stuck junior's run is the canonical use case.
    """
    client, op = client_with_operator
    await _seed_published_template(tenant_id=op.tenant_id, operator_sub=op.sub, slug="r1")
    service = RunbookRunService()
    started = await service.start_run(
        tenant_id=op.tenant_id,
        operator_sub="operator-junior",
        request=StartRunRequest(template_slug="r1", target="host-1", params={}),
    )

    payload = _result_payload(
        _call(
            client,
            "runbook_abort",
            {"run_id": str(started.run_id), "reason": "senior taking over"},
        )
    )
    assert payload["state"] == "abandoned"


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.asyncio
async def test_abort_by_non_assignee_non_admin_denied(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: non-assignee non-admin operator -> ``NotRunAssigneeError`` -> ``-32602``."""
    client, op = client_with_operator
    await _seed_published_template(tenant_id=op.tenant_id, operator_sub=op.sub, slug="r1")
    service = RunbookRunService()
    started = await service.start_run(
        tenant_id=op.tenant_id,
        operator_sub="operator-other",
        request=StartRunRequest(template_slug="r1", target="host-1", params={}),
    )

    body = _call(
        client,
        "runbook_abort",
        {"run_id": str(started.run_id), "reason": "nope"},
    )
    assert body["error"]["code"] == INVALID_PARAMS


# ---------------------------------------------------------------------------
# runbook_reassign
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
@pytest.mark.asyncio
async def test_reassign_by_admin_succeeds(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: TENANT_ADMIN reassigns -> response carries new ``assigned_to``."""
    client, op = client_with_operator
    await _seed_published_template(tenant_id=op.tenant_id, operator_sub=op.sub, slug="r1")
    service = RunbookRunService()
    started = await service.start_run(
        tenant_id=op.tenant_id,
        operator_sub="operator-junior",
        request=StartRunRequest(template_slug="r1", target="host-1", params={}),
    )

    payload = _result_payload(
        _call(
            client,
            "runbook_reassign",
            {"run_id": str(started.run_id), "new_assignee": "operator-senior"},
        )
    )
    assert payload["assigned_to"] == "operator-senior"
    assert payload["run_id"] == str(started.run_id)


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.asyncio
async def test_reassign_by_operator_denied(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: OPERATOR cannot reassign -- the dispatcher gate refuses with ``-32602``.

    The :class:`ToolDefinition` role gate is enforced by the dispatcher
    *before* the handler runs. The operator never reaches the service
    layer; the refusal is the tool-boundary gate.
    """
    client, op = client_with_operator
    await _seed_published_template(tenant_id=op.tenant_id, operator_sub=op.sub, slug="r1")
    service = RunbookRunService()
    started = await service.start_run(
        tenant_id=op.tenant_id,
        operator_sub=op.sub,
        request=StartRunRequest(template_slug="r1", target="host-1", params={}),
    )

    body = _call(
        client,
        "runbook_reassign",
        {"run_id": str(started.run_id), "new_assignee": "operator-other"},
    )
    assert "error" in body
    assert body["error"]["code"] == INVALID_PARAMS


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_reassign_missing_run_error(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: ``RunNotFoundError`` -> ``-32602``.

    Probing a random UUID surfaces the typed error from the service
    layer; the handler maps it to the spec-correct INVALID_PARAMS.
    """
    client, _op = client_with_operator
    body = _call(
        client,
        "runbook_reassign",
        {"run_id": str(uuid.uuid4()), "new_assignee": "operator-other"},
    )
    assert body["error"]["code"] == INVALID_PARAMS


# ---------------------------------------------------------------------------
# runbook_list_runs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.asyncio
async def test_list_runs_operator_sees_only_own(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: ``caller_is_admin=False`` forces ``assignee=operator.sub`` regardless of filter."""
    client, op = client_with_operator
    await _seed_published_template(tenant_id=op.tenant_id, operator_sub=op.sub, slug="r1")
    service = RunbookRunService()
    started_self = await service.start_run(
        tenant_id=op.tenant_id,
        operator_sub=op.sub,
        request=StartRunRequest(template_slug="r1", target="host-1", params={}),
    )
    # A second run by a different operator -- must not be visible.
    await service.start_run(
        tenant_id=op.tenant_id,
        operator_sub="operator-other",
        request=StartRunRequest(template_slug="r1", target="host-2", params={}),
    )

    # Operator tries to filter to ``operator-other`` explicitly; the
    # service overrides to the caller's own sub.
    payload = _result_payload(_call(client, "runbook_list_runs", {"assignee": "operator-other"}))
    runs = payload["runs"]
    assert len(runs) == 1
    assert runs[0]["run_id"] == str(started_self.run_id)
    assert runs[0]["assigned_to"] == op.sub


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
@pytest.mark.asyncio
async def test_list_runs_admin_sees_all_tenant(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: TENANT_ADMIN sees runs for every assignee in the tenant."""
    client, op = client_with_operator
    await _seed_published_template(tenant_id=op.tenant_id, operator_sub=op.sub, slug="r1")
    service = RunbookRunService()
    await service.start_run(
        tenant_id=op.tenant_id,
        operator_sub="operator-alpha",
        request=StartRunRequest(template_slug="r1", target="host-1", params={}),
    )
    await service.start_run(
        tenant_id=op.tenant_id,
        operator_sub="operator-beta",
        request=StartRunRequest(template_slug="r1", target="host-2", params={}),
    )

    payload = _result_payload(_call(client, "runbook_list_runs", {}))
    assigned_subs = {r["assigned_to"] for r in payload["runs"]}
    assert assigned_subs == {"operator-alpha", "operator-beta"}


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
@pytest.mark.asyncio
async def test_list_runs_tenant_isolation(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: a list under one tenant never returns another tenant's runs."""
    client, op = client_with_operator
    await _seed_published_template(tenant_id=op.tenant_id, operator_sub=op.sub, slug="r1")

    # Foreign tenant's seed.
    foreign_tenant = uuid.uuid4()
    await _seed_published_template(
        tenant_id=foreign_tenant, operator_sub="foreign-admin", slug="r1"
    )
    service = RunbookRunService()
    await service.start_run(
        tenant_id=op.tenant_id,
        operator_sub="operator-mine",
        request=StartRunRequest(template_slug="r1", target="host-mine", params={}),
    )
    await service.start_run(
        tenant_id=foreign_tenant,
        operator_sub="operator-foreign",
        request=StartRunRequest(template_slug="r1", target="host-theirs", params={}),
    )

    payload = _result_payload(_call(client, "runbook_list_runs", {}))
    assigned_subs = {r["assigned_to"] for r in payload["runs"]}
    assert assigned_subs == {"operator-mine"}


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.asyncio
async def test_list_runs_omits_step_contents(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """LOAD-BEARING property: ``RunSummary`` rows never carry step bodies.

    Step-by-step content is opaque-by-construction (only
    :func:`runbook_next` ever returns a step body, and only one step at
    a time). The list surface only exposes ``current_step_id`` -- the
    *id* of the step the run is on, not its body. This test asserts the
    summary shape never widens to leak the step list.
    """
    client, op = client_with_operator
    await _seed_published_template(tenant_id=op.tenant_id, operator_sub=op.sub, slug="r1")
    _call(client, "runbook_start", {"template_slug": "r1", "target": "host-1"})

    payload = _result_payload(_call(client, "runbook_list_runs", {}))
    runs = payload["runs"]
    assert len(runs) == 1
    summary = runs[0]
    # Run-level columns are present.
    assert summary["assigned_to"] == op.sub
    assert summary["template_slug"] == "r1"
    assert summary["state"] == "in_progress"
    # Position + current step id give "where" without giving "what".
    assert summary.get("current_step_id") == "step-1"
    # No leakage of step bodies / lists.
    for forbidden in ("steps", "current_step", "body", "title", "verify"):
        assert forbidden not in summary, f"summary leaked {forbidden!r}"


# ---------------------------------------------------------------------------
# runbook_next — operation_call verify dispatch (audit correlation)
# ---------------------------------------------------------------------------


class _StubConnector(Connector):
    """Minimal connector so the dispatcher resolver finds a class for the triple.

    The runbook engine resolves an ``op_id`` to a
    :class:`~meho_backplane.db.models.EndpointDescriptor` row and
    reconstructs a ``connector_id``; the dispatcher then looks up the
    connector class by ``(product, version, impl_id)``. Without a
    registered class the call to :func:`call_operation` errors before
    reaching the typed handler.
    """

    product = "stub"
    version = "1.x"
    impl_id = "stub"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self,
        target: Any,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        raise NotImplementedError


async def _ok_handler(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Typed handler that returns the shape ``expect`` looks for."""
    return {"ok": True}


@pytest.fixture
def _stub_embedding_service() -> AsyncMock:
    """Embedding stub for the typed-op descriptor's embedding column."""
    service = AsyncMock()
    service.encode_one.return_value = [0.0] * EMBEDDING_DIMENSION
    service.encode.return_value = [[0.0] * EMBEDDING_DIMENSION]
    service.dimension = EMBEDDING_DIMENSION
    return service


@pytest.fixture
def _scoped_stub_registration() -> Iterator[None]:
    """Reset dispatcher caches around the operation_call verify test.

    Not autouse: blanket-clearing the connector registry mid-session
    would break the FastAPI lifespan startup, which validates the
    connector-spec catalog against every registered connector class.
    The single test that needs a stub connector requests this fixture
    explicitly. The dispatcher-cache reset on both ends keeps the
    stub op's descriptor cache from leaking across tests.

    Why the connector registry itself stays untouched: the stub
    connector is registered via :func:`register_connector_v2` at the
    process-wide registry. Clearing it would unregister production
    connectors needed by every other test's FastAPI lifespan startup.
    Using a stable ``"stub"`` product / impl_id means the registration
    is idempotent at the registry level for the second test-run in the
    same process (duplicate-registration raises -- we catch and
    swallow on re-entry). The :class:`Target` row + the typed-op
    registration go to the per-test SQLite schema which is discarded
    by the autouse ``_default_database_url`` fixture in conftest.
    """
    from meho_backplane.connectors.registry import _REGISTRY_V2

    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()
    # The next test invocation may re-register the same stub; drop the
    # entry here so the re-registration is a fresh insert rather than a
    # duplicate-raise.
    _REGISTRY_V2.pop(("stub", "", ""), None)


async def _seed_stub_op(stub_embedding_service: AsyncMock, op_id: str = "stub.op_call") -> None:
    """Register the stub connector + typed op the dispatcher can resolve."""
    register_connector_v2(product="stub", version="", impl_id="", cls=_StubConnector)
    await register_typed_operation(
        product="stub",
        version="1.x",
        impl_id="stub",
        op_id=op_id,
        handler=_ok_handler,
        summary="Stub op for runbook MCP tool tests.",
        description="Echo OK; used for operation_call verify dispatch.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )


async def _seed_target(tenant_id: uuid.UUID, name: str) -> None:
    """Seed a tenant-scoped :class:`Target` row the dispatcher can resolve."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            Target(
                tenant_id=tenant_id,
                name=name,
                product="stub",
                host="stub-host.example",
                auth_model="shared_service_account",
            )
        )
        await session.commit()


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.asyncio
async def test_next_operation_call_match_advances(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    _stub_embedding_service: AsyncMock,
    _scoped_stub_registration: None,
) -> None:
    """AC: operation_call verify dispatches the call, matches expect, advances.

    Crucially: the dispatched call's audit row carries ``run_id`` and
    ``step_id`` populated -- the run/step correlation contract G12.1-T2
    (#1294) wired and this Initiative depends on. The contextvars are
    bound by :meth:`RunbookRunService.next_step`; this test verifies
    the binding survives the MCP tool boundary (the boundary doesn't
    swallow or rebind the contextvars).
    """
    client, op = client_with_operator
    await _seed_stub_op(_stub_embedding_service)
    await _seed_target(op.tenant_id, "host-1")
    await _seed_published_template(
        tenant_id=op.tenant_id,
        operator_sub=op.sub,
        slug="r1",
        body=_template_body(
            steps=[
                {
                    "id": "call-it",
                    "title": "Call the op",
                    "body": "stub call",
                    "type": "operation_call",
                    "op_id": "stub.op_call",
                    "params": {},
                    "verify": {
                        "type": "operation_call",
                        "op_id": "stub.op_call",
                        "params": {},
                        "expect": {"ok": True},
                    },
                }
            ]
        ),
    )
    started = _result_payload(
        _call(client, "runbook_start", {"template_slug": "r1", "target": "host-1"})
    )

    payload = _result_payload(
        _call(
            client,
            "runbook_next",
            {
                "run_id": started["run_id"],
                "last_verified": True,
                "verify_response": None,
            },
        )
    )
    # One-step template -- the operation_call verify advances past the
    # last step and the run completes.
    assert payload["kind"] == "completed"
    assert payload["state"] == "completed"

    # The verify dispatch produced an audit row tagged with the verify
    # op_id; the run/step correlation columns (or the payload mirror on
    # schemas predating migration 0034) must be populated.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (
            await session.scalars(
                select(AuditLog)
                .where(AuditLog.method == "DISPATCH")
                .where(AuditLog.path == "stub.op_call")
            )
        ).all()
    assert len(rows) == 1
    row = rows[0]
    column_run_id = getattr(row, "run_id", None)
    column_step_id = getattr(row, "step_id", None)
    payload_row = row.payload
    assert isinstance(payload_row, dict)
    assert (column_run_id == uuid.UUID(started["run_id"])) or (
        payload_row.get("run_id") == started["run_id"]
    )
    assert (column_step_id == "call-it") or (payload_row.get("step_id") == "call-it")
