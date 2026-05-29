# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``runbook_*_template`` MCP tools (G12.2-T4, #1298).

Covers the task's acceptance criteria for the six template-lifecycle
tools that wrap :class:`~meho_backplane.runbooks.service.RunbookTemplateService`
on the MCP transport:

* All six tools register against the G0.5 registry with strict 2020-12
  ``inputSchema`` and the MEHO-internal RBAC fields stripped from the
  wire shape.
* RBAC: five tools are ``TENANT_ADMIN``-only; ``runbook_list_templates``
  is ``OPERATOR``-readable.
* The edit tool's draft-in-place vs fork-from-published paths round-trip
  through the dispatcher (``forked_from`` present iff a published version
  exists).
* Typed-exception → ``-32602`` mapping for the operator-actionable
  service errors.
* Tenant isolation on the list surface.
* The load-bearing description prose (the multi-session drafting pattern,
  the opacity-floor rationale) is present — a regression guard so a
  refactor that drops it surfaces here rather than as silently degraded
  agent UX.

The service uses the SQLite-backed default test DB (the
``runbook_templates`` table is materialised by the autouse
``_default_database_url`` fixture's ``alembic upgrade head``); no
embedding / external service is involved, so every assertion runs
in-sandbox.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp.schemas import INVALID_PARAMS
from meho_backplane.runbooks.schemas import DraftTemplateRequest, PublishTemplateRequest
from meho_backplane.runbooks.service import RunbookTemplateService
from tests.mcp_test_fixtures import (
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_ADMIN_TOOLS = {
    "runbook_draft_template",
    "runbook_edit_template",
    "runbook_publish_template",
    "runbook_deprecate_template",
    "runbook_show_template",
}
_OPERATOR_TOOLS = {"runbook_list_templates"}


def _body(title: str = "Rotate cert", *, step_body: str = "Rotate the cert.") -> dict[str, Any]:
    """Build a minimal valid template-body wire dict with one manual step."""
    return {
        "title": title,
        "description": "Procedure for rotating a certificate.",
        "target_kind": "host",
        "steps": [
            {
                "id": "rotate",
                "title": "Rotate the certificate",
                "body": step_body,
                "type": "manual",
                "verify": {"type": "confirm", "prompt": "Cert rotated?"},
            }
        ],
    }


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
def test_all_six_tools_registered_with_strict_schema(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: all six tools surface for a TENANT_ADMIN with strict input schemas."""
    client, _op = client_with_operator
    response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools_by_name = {t["name"]: t for t in response.json()["result"]["tools"]}

    for name in _ADMIN_TOOLS | _OPERATOR_TOOLS:
        assert name in tools_by_name, name
        schema = tools_by_name[name]["inputSchema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        # MEHO-internal RBAC fields never reach the wire.
        assert "required_role" not in tools_by_name[name]
        assert "op_class" not in tools_by_name[name]


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_admin_tools_hidden_from_operator_list_is_visible(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: an OPERATOR sees only ``runbook_list_templates``; the five admin tools are hidden."""
    client, _op = client_with_operator
    response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tool_names = {t["name"] for t in response.json()["result"]["tools"]}

    assert tool_names >= _OPERATOR_TOOLS
    assert not (_ADMIN_TOOLS & tool_names)


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_tool_descriptions_include_load_bearing_text(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: the edit + show descriptions carry their load-bearing prose.

    Regression guard — these strings are agent-UX-load-bearing; a refactor
    that drops them silently regresses authoring quality.
    """
    client, _op = client_with_operator
    response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools_by_name = {t["name"]: t for t in response.json()["result"]["tools"]}

    edit_desc = tools_by_name["runbook_edit_template"]["description"]
    assert "MULTI-SESSION DRAFTING PATTERN" in edit_desc
    assert "forked_from" in edit_desc
    # Cross-link to the authoring doc T5 (#1299) ships.
    assert "docs/runbooks/authoring.md" in edit_desc

    show_desc = tools_by_name["runbook_show_template"]["description"]
    assert "TENANT_ADMIN" in show_desc
    assert "POST-COMPLETION EXCEPTION" in show_desc
    # The opacity-floor-lives-on-the-run-surface rationale.
    assert "runbook_next" in show_desc
    assert "docs/runbooks/authoring.md" in show_desc


# ---------------------------------------------------------------------------
# runbook_draft_template
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
@pytest.mark.asyncio
async def test_draft_tool_invocation_success(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: admin call creates a draft scoped to the operator's tenant."""
    client, op = client_with_operator
    payload = _result_payload(
        _call(client, "runbook_draft_template", {"slug": "cert-rotate", "body": _body()})
    )
    assert payload == {"slug": "cert-rotate", "version": 1, "status": "draft"}

    # The row landed under the operator's JWT-bound tenant, never an input one.
    shown = await RunbookTemplateService().show_template(tenant_id=op.tenant_id, slug="cert-rotate")
    assert shown.version == 1
    assert shown.created_by == op.sub


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_draft_duplicate_error_mapping(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: ``DuplicateDraftError`` surfaces as ``-32602``."""
    client, _op = client_with_operator
    first = _call(client, "runbook_draft_template", {"slug": "cert-rotate", "body": _body()})
    assert first["result"]["isError"] is False

    dupe = _call(client, "runbook_draft_template", {"slug": "cert-rotate", "body": _body()})
    assert dupe["error"]["code"] == INVALID_PARAMS


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_draft_invalid_slug_error_mapping(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A bad slug fails the request-model validation → ``-32602``."""
    client, _op = client_with_operator
    body = _call(client, "runbook_draft_template", {"slug": "Bad_Slug", "body": _body()})
    assert body["error"]["code"] == INVALID_PARAMS


# ---------------------------------------------------------------------------
# runbook_edit_template — in-place vs fork
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_edit_in_place_path(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: editing an existing draft mutates in place — ``forked_from is None``."""
    client, _op = client_with_operator
    _call(client, "runbook_draft_template", {"slug": "cert-rotate", "body": _body()})

    payload = _result_payload(
        _call(
            client,
            "runbook_edit_template",
            {"slug": "cert-rotate", "body": _body(step_body="Rotate the cert, carefully.")},
        )
    )
    assert payload["version"] == 1
    assert payload["status"] == "draft"
    assert payload["forked_from"] is None


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
@pytest.mark.asyncio
async def test_edit_fork_path(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: editing a slug whose only version is published forks a new draft.

    Seed a published v1 via the service, then edit through the dispatcher
    and assert the response forks to v2 carrying ``forked_from``.
    """
    client, op = client_with_operator
    service = RunbookTemplateService()
    await service.create_draft(
        tenant_id=op.tenant_id,
        operator_sub=op.sub,
        request=DraftTemplateRequest.model_validate({"slug": "cert-rotate", "body": _body()}),
    )
    await service.publish(
        tenant_id=op.tenant_id,
        request=PublishTemplateRequest(slug="cert-rotate", version=1),
    )

    payload = _result_payload(
        _call(client, "runbook_edit_template", {"slug": "cert-rotate", "body": _body("v2")})
    )
    assert payload["version"] == 2
    assert payload["status"] == "draft"
    assert payload["forked_from"] is not None
    assert payload["forked_from"]["version"] == 1
    assert payload["forked_from"]["in_flight_run_count"] == 0


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_edit_missing_slug_error_mapping(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Editing a slug with no rows → ``TemplateNotFoundError`` → ``-32602``."""
    client, _op = client_with_operator
    body = _call(client, "runbook_edit_template", {"slug": "ghost", "body": _body()})
    assert body["error"]["code"] == INVALID_PARAMS


# ---------------------------------------------------------------------------
# runbook_publish_template / runbook_deprecate_template
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_publish_tool_invocation(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: happy-path publish, plus ``-32602`` for a missing version."""
    client, _op = client_with_operator
    _call(client, "runbook_draft_template", {"slug": "cert-rotate", "body": _body()})

    ok = _result_payload(
        _call(client, "runbook_publish_template", {"slug": "cert-rotate", "version": 1})
    )
    assert ok == {"slug": "cert-rotate", "version": 1, "status": "published"}

    missing = _call(client, "runbook_publish_template", {"slug": "cert-rotate", "version": 99})
    assert missing["error"]["code"] == INVALID_PARAMS


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_deprecate_tool_invocation(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: deprecate a published version; deprecating a draft → ``-32602``."""
    client, _op = client_with_operator
    _call(client, "runbook_draft_template", {"slug": "cert-rotate", "body": _body()})

    # Deprecating a draft (not published) → TemplateNotPublishedError → -32602.
    not_published = _call(
        client, "runbook_deprecate_template", {"slug": "cert-rotate", "version": 1}
    )
    assert not_published["error"]["code"] == INVALID_PARAMS

    _call(client, "runbook_publish_template", {"slug": "cert-rotate", "version": 1})
    ok = _result_payload(
        _call(client, "runbook_deprecate_template", {"slug": "cert-rotate", "version": 1})
    )
    assert ok == {"slug": "cert-rotate", "version": 1, "status": "deprecated"}


# ---------------------------------------------------------------------------
# runbook_list_templates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.asyncio
async def test_list_templates_operator_role_ok(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: an OPERATOR can list; the response is summaries with no step bodies."""
    client, op = client_with_operator
    await RunbookTemplateService().create_draft(
        tenant_id=op.tenant_id,
        operator_sub=op.sub,
        request=DraftTemplateRequest.model_validate({"slug": "cert-rotate", "body": _body()}),
    )

    payload = _result_payload(_call(client, "runbook_list_templates", {}))
    assert isinstance(payload["templates"], list)
    assert len(payload["templates"]) == 1
    summary = payload["templates"][0]
    assert summary["slug"] == "cert-rotate"
    assert summary["status"] == "draft"
    # Summary projection — no full step bodies.
    assert "steps" not in summary


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.asyncio
async def test_list_templates_tenant_isolation(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: a list under one tenant never returns another tenant's templates."""
    client, op = client_with_operator
    service = RunbookTemplateService()
    # Operator's own tenant.
    await service.create_draft(
        tenant_id=op.tenant_id,
        operator_sub=op.sub,
        request=DraftTemplateRequest.model_validate({"slug": "mine", "body": _body("Mine")}),
    )
    # A different tenant — must not leak.
    other_tenant = uuid.uuid4()
    await service.create_draft(
        tenant_id=other_tenant,
        operator_sub="other-admin",
        request=DraftTemplateRequest.model_validate({"slug": "theirs", "body": _body("Theirs")}),
    )

    payload = _result_payload(_call(client, "runbook_list_templates", {}))
    slugs = {t["slug"] for t in payload["templates"]}
    assert slugs == {"mine"}


# ---------------------------------------------------------------------------
# runbook_show_template
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_show_template_admin_ok(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: an admin gets the full body including step contents."""
    client, _op = client_with_operator
    _call(client, "runbook_draft_template", {"slug": "cert-rotate", "body": _body()})

    payload = _result_payload(_call(client, "runbook_show_template", {"slug": "cert-rotate"}))
    assert payload["slug"] == "cert-rotate"
    assert payload["version"] == 1
    assert len(payload["steps"]) == 1
    assert payload["steps"][0]["id"] == "rotate"


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_show_template_operator_denied(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: an OPERATOR calling show is refused by the dispatcher role gate → ``-32602``.

    The opacity floor lives on the run surface (G12.3); the template-side
    read is TENANT_ADMIN-only. The dispatcher refuses the call before the
    handler runs.
    """
    client, _op = client_with_operator
    body = _call(client, "runbook_show_template", {"slug": "cert-rotate"})
    assert body["error"]["code"] == INVALID_PARAMS
    assert "forbidden" in body["error"]["message"].lower()


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_show_template_missing_error(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: ``TemplateNotFoundError`` surfaces as ``-32602``."""
    client, _op = client_with_operator
    body = _call(client, "runbook_show_template", {"slug": "ghost"})
    assert body["error"]["code"] == INVALID_PARAMS
