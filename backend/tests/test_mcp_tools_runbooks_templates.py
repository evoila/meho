# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``meho.runbook.*`` template-lifecycle MCP tools (G12.2-T4, #1298).

Covers the task's acceptance criteria for the seven template-lifecycle
tools that wrap :class:`~meho_backplane.runbooks.service.RunbookTemplateService`
on the MCP transport (``discard_template`` added by #135):

* All seven tools register against the G0.5 registry with strict 2020-12
  ``inputSchema`` and the MEHO-internal RBAC fields stripped from the
  wire shape.
* RBAC: five tools are ``TENANT_ADMIN``-only; ``meho.runbook.list_templates``
  is ``OPERATOR``-readable.
* #1612 naming + field canonicalisation, #1625 removal: the dotted names
  are the only registered names; the flat ``runbook_*`` aliases and the
  ``slug`` input alias were removed after their one-release window, so a
  flat-name call falls through to unknown-tool and ``slug`` is rejected.
  ``template_slug`` is the sole input field; responses still mirror
  ``slug`` as ``template_slug``.
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
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import RunbookRun, RunbookTemplate
from meho_backplane.mcp.schemas import INTERNAL_ERROR, INVALID_PARAMS
from meho_backplane.runbooks.hydration_errors import TEMPLATE_BODY_VALIDATION_FAILED
from meho_backplane.runbooks.schemas import DraftTemplateRequest, PublishTemplateRequest
from meho_backplane.runbooks.service import RunbookTemplateService
from tests.mcp_test_fixtures import (
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_ADMIN_TOOLS = {
    "meho.runbook.draft_template",
    "meho.runbook.edit_template",
    "meho.runbook.publish_template",
    "meho.runbook.deprecate_template",
    "meho.runbook.discard_template",
}
# ``meho.runbook.show_template`` is OPERATOR-readable at the dispatcher gate
# (G12.3-T4 / #1309); the run-state-conditional opacity-floor check lives
# inside the handler, not in :func:`required_role`. Operators see the tool
# in ``tools/list`` but a call without a completed/abandoned run against
# the resolved (slug, version) is refused by the handler with
# ``-32602`` and the ``opacity_floor`` reason.
_OPERATOR_TOOLS = {"meho.runbook.list_templates", "meho.runbook.show_template"}

#: Flat template-verb aliases removed by #1625 (kept as deprecated
#: aliases for one release by #1612). Each must now fall through to the
#: dispatcher's unknown-tool error.
_REMOVED_FLAT_ALIASES = (
    "runbook_draft_template",
    "runbook_edit_template",
    "runbook_publish_template",
    "runbook_deprecate_template",
    "runbook_list_templates",
    "runbook_show_template",
)


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
def test_all_seven_tools_registered_with_strict_schema(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: all seven tools surface for a TENANT_ADMIN with strict input schemas."""
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
    """AC: an OPERATOR sees only ``meho.runbook.list_templates``; admin tools hidden."""
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

    edit_desc = tools_by_name["meho.runbook.edit_template"]["description"]
    assert "MULTI-SESSION DRAFTING PATTERN" in edit_desc
    assert "forked_from" in edit_desc
    # Cross-link to the authoring doc T5 (#1299) ships.
    assert "docs/runbooks/authoring.md" in edit_desc

    show_desc = tools_by_name["meho.runbook.show_template"]["description"]
    assert "TENANT_ADMIN" in show_desc
    assert "POST-COMPLETION EXCEPTION" in show_desc
    # The opacity-floor-lives-on-the-run-surface rationale.
    assert "meho.runbook.next" in show_desc
    assert "docs/runbooks/authoring.md" in show_desc


# ---------------------------------------------------------------------------
# meho.runbook.draft_template
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
@pytest.mark.asyncio
async def test_draft_tool_invocation_success(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: admin call creates a draft scoped to the operator's tenant."""
    client, op = client_with_operator
    payload = _result_payload(
        _call(
            client, "meho.runbook.draft_template", {"template_slug": "cert-rotate", "body": _body()}
        )
    )
    assert payload == {
        "slug": "cert-rotate",
        "template_slug": "cert-rotate",
        "version": 1,
        "status": "draft",
    }

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
    first = _call(
        client, "meho.runbook.draft_template", {"template_slug": "cert-rotate", "body": _body()}
    )
    assert first["result"]["isError"] is False

    dupe = _call(
        client, "meho.runbook.draft_template", {"template_slug": "cert-rotate", "body": _body()}
    )
    assert dupe["error"]["code"] == INVALID_PARAMS


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_draft_invalid_slug_error_mapping(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A bad slug fails the request-model validation → ``-32602``."""
    client, _op = client_with_operator
    body = _call(
        client, "meho.runbook.draft_template", {"template_slug": "Bad_Slug", "body": _body()}
    )
    assert body["error"]["code"] == INVALID_PARAMS


# ---------------------------------------------------------------------------
# meho.runbook.edit_template — in-place vs fork
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_edit_in_place_path(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: editing an existing draft mutates in place — ``forked_from is None``."""
    client, _op = client_with_operator
    _call(client, "meho.runbook.draft_template", {"template_slug": "cert-rotate", "body": _body()})

    payload = _result_payload(
        _call(
            client,
            "meho.runbook.edit_template",
            {
                "template_slug": "cert-rotate",
                "body": _body(step_body="Rotate the cert, carefully."),
            },
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
        _call(
            client,
            "meho.runbook.edit_template",
            {"template_slug": "cert-rotate", "body": _body("v2")},
        )
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
    body = _call(client, "meho.runbook.edit_template", {"template_slug": "ghost", "body": _body()})
    assert body["error"]["code"] == INVALID_PARAMS


# ---------------------------------------------------------------------------
# meho.runbook.publish_template / meho.runbook.deprecate_template
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_publish_tool_invocation(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: happy-path publish, plus ``-32602`` for a missing version."""
    client, _op = client_with_operator
    _call(client, "meho.runbook.draft_template", {"template_slug": "cert-rotate", "body": _body()})

    ok = _result_payload(
        _call(
            client, "meho.runbook.publish_template", {"template_slug": "cert-rotate", "version": 1}
        )
    )
    assert ok == {
        "slug": "cert-rotate",
        "template_slug": "cert-rotate",
        "version": 1,
        "status": "published",
    }

    missing = _call(
        client, "meho.runbook.publish_template", {"template_slug": "cert-rotate", "version": 99}
    )
    assert missing["error"]["code"] == INVALID_PARAMS


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_deprecate_tool_invocation(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: deprecate a published version; deprecating a draft → ``-32602``."""
    client, _op = client_with_operator
    _call(client, "meho.runbook.draft_template", {"template_slug": "cert-rotate", "body": _body()})

    # Deprecating a draft (not published) → TemplateNotPublishedError → -32602.
    not_published = _call(
        client, "meho.runbook.deprecate_template", {"template_slug": "cert-rotate", "version": 1}
    )
    assert not_published["error"]["code"] == INVALID_PARAMS

    _call(client, "meho.runbook.publish_template", {"template_slug": "cert-rotate", "version": 1})
    ok = _result_payload(
        _call(
            client,
            "meho.runbook.deprecate_template",
            {"template_slug": "cert-rotate", "version": 1},
        )
    )
    assert ok == {
        "slug": "cert-rotate",
        "template_slug": "cert-rotate",
        "version": 1,
        "status": "deprecated",
    }


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_discard_tool_invocation(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC4: discard a draft; a published version and a re-discard both → ``-32602``."""
    client, _op = client_with_operator
    _call(client, "meho.runbook.draft_template", {"template_slug": "cert-rotate", "body": _body()})

    ok = _result_payload(
        _call(
            client, "meho.runbook.discard_template", {"template_slug": "cert-rotate", "version": 1}
        )
    )
    assert ok == {
        "slug": "cert-rotate",
        "template_slug": "cert-rotate",
        "version": 1,
        "status": "discarded",
    }

    # Re-discarding the now-removed draft → TemplateNotFoundError → -32602.
    gone = _call(
        client, "meho.runbook.discard_template", {"template_slug": "cert-rotate", "version": 1}
    )
    assert gone["error"]["code"] == INVALID_PARAMS

    # Discarding a published version is refused (retired via deprecate, not discarded).
    _call(client, "meho.runbook.draft_template", {"template_slug": "drain-node", "body": _body()})
    _call(client, "meho.runbook.publish_template", {"template_slug": "drain-node", "version": 1})
    published = _call(
        client, "meho.runbook.discard_template", {"template_slug": "drain-node", "version": 1}
    )
    assert published["error"]["code"] == INVALID_PARAMS


# ---------------------------------------------------------------------------
# meho.runbook.list_templates
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

    payload = _result_payload(_call(client, "meho.runbook.list_templates", {}))
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

    payload = _result_payload(_call(client, "meho.runbook.list_templates", {}))
    slugs = {t["slug"] for t in payload["templates"]}
    assert slugs == {"mine"}


# ---------------------------------------------------------------------------
# meho.runbook.show_template
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_show_template_admin_ok(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: an admin gets the full body including step contents."""
    client, _op = client_with_operator
    _call(client, "meho.runbook.draft_template", {"template_slug": "cert-rotate", "body": _body()})

    payload = _result_payload(
        _call(client, "meho.runbook.show_template", {"template_slug": "cert-rotate"})
    )
    assert payload["slug"] == "cert-rotate"
    assert payload["version"] == 1
    assert len(payload["steps"]) == 1
    assert payload["steps"][0]["id"] == "rotate"


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_show_tool_admin_unchanged(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Regression: an admin still reads the full body unconditionally.

    G12.3-T4 changes the operator path; the admin path must remain a
    pass-through to :meth:`RunbookTemplateService.show_template` with no
    run-state predicate consulted.
    """
    client, _op = client_with_operator
    _call(client, "meho.runbook.draft_template", {"template_slug": "cert-rotate", "body": _body()})

    payload = _result_payload(
        _call(client, "meho.runbook.show_template", {"template_slug": "cert-rotate"})
    )
    assert payload["slug"] == "cert-rotate"
    assert payload["version"] == 1
    assert len(payload["steps"]) == 1


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.asyncio
async def test_show_tool_operator_with_completed_run_succeeds(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: operator with a completed run gets the full body (post-mortem read).

    Seed a published v1 directly via the template service, then insert a
    ``completed`` :class:`RunbookRun` for ``op`` against ``(slug, v1)``. The
    handler's predicate call must return ``True`` and the response must be
    the full body (with step contents).
    """
    client, op = client_with_operator
    # Seed a published v1 from a fresh admin-shaped service call.
    other_admin_sub = "seed-admin"
    service = RunbookTemplateService()
    await service.create_draft(
        tenant_id=op.tenant_id,
        operator_sub=other_admin_sub,
        request=DraftTemplateRequest.model_validate({"slug": "cert-rotate", "body": _body()}),
    )
    await service.publish(
        tenant_id=op.tenant_id,
        request=PublishTemplateRequest(slug="cert-rotate", version=1),
    )

    # Seed a completed run for the operator against (cert-rotate, v1).
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            RunbookRun(
                tenant_id=op.tenant_id,
                template_slug="cert-rotate",
                template_version=1,
                assigned_to=op.sub,
                target="host:edge-01",
                params={},
                state="completed",
                started_by=op.sub,
            )
        )
        await session.commit()

    payload = _result_payload(
        _call(client, "meho.runbook.show_template", {"template_slug": "cert-rotate"})
    )
    assert payload["slug"] == "cert-rotate"
    assert payload["version"] == 1
    # Full body including step contents.
    assert len(payload["steps"]) == 1
    assert payload["steps"][0]["id"] == "rotate"


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.asyncio
async def test_show_tool_operator_with_no_run_denied(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: operator with no completed/abandoned run → -32602, ``opacity_floor`` reason.

    The template exists (seeded for the operator's tenant) but no run row
    is inserted; the handler-internal predicate returns ``False`` and the
    refusal carries the ``opacity_floor`` keyword that distinguishes
    "opacity floor held" from "schema invalid".
    """
    client, op = client_with_operator
    # Seed a published v1 so the existence path is OK; the denial is purely
    # about the missing run row.
    service = RunbookTemplateService()
    await service.create_draft(
        tenant_id=op.tenant_id,
        operator_sub="seed-admin",
        request=DraftTemplateRequest.model_validate({"slug": "cert-rotate", "body": _body()}),
    )
    await service.publish(
        tenant_id=op.tenant_id,
        request=PublishTemplateRequest(slug="cert-rotate", version=1),
    )

    body = _call(client, "meho.runbook.show_template", {"template_slug": "cert-rotate"})
    assert body["error"]["code"] == INVALID_PARAMS
    assert "opacity_floor" in body["error"]["message"]


async def _seed_poisoned_template(tenant_id: uuid.UUID) -> None:
    """Insert a published template whose only step has an empty body.

    Goes through the ORM (not the service) so the #2122 ``min_length=1``
    constraint -- enforced on the Pydantic request models, not the DB model
    -- is bypassed, reproducing a legacy pre-v0.20.0 row that reached
    storage before the constraint existed.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            RunbookTemplate(
                tenant_id=tenant_id,
                slug="cert-rotate",
                version=1,
                title="Rotate cert",
                description="Rotate the expiring TLS cert.",
                steps=[
                    {
                        "id": "revoke",
                        "title": "Revoke",
                        "body": "",
                        "type": "manual",
                        "verify": {"type": "confirm", "prompt": "Done?"},
                    }
                ],
                target_kind="host",
                status="published",
                created_by="seed-admin",
                edited_by="seed-admin",
            )
        )
        await session.commit()


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
@pytest.mark.asyncio
async def test_show_tool_admin_hydration_failure_returns_structured_internal_error(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC #3: a poisoned stored body → structured ``-32603`` with ``data`` (admin).

    Not the dispatcher catch-all's opaque ``internal error: ValidationError``:
    the handler classifies the hydration failure into :class:`McpInternalError`
    with the shared envelope on ``error.data``.
    """
    client, op = client_with_operator
    await _seed_poisoned_template(op.tenant_id)

    body = _call(client, "meho.runbook.show_template", {"template_slug": "cert-rotate"})
    assert body["error"]["code"] == INTERNAL_ERROR
    data = body["error"]["data"]
    assert data["error"] == TEMPLATE_BODY_VALIDATION_FAILED
    assert data["slug"] == "cert-rotate"
    assert data["errors"][0]["type"] == "string_too_short"
    # The message is the actionable envelope, not the opaque class name.
    assert "migration 0054" in body["error"]["message"]
    assert body["error"]["message"] != "internal error: ValidationError"


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.asyncio
async def test_show_tool_operator_hydration_failure_returns_structured_internal_error(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC #3: the operator path also surfaces the structured ``-32603`` envelope.

    The operator is authorized (a completed run against the pinned version);
    the corrupt stored body surfaces as the structured internal error while
    hydrating the resolved latest version.
    """
    client, op = client_with_operator
    await _seed_poisoned_template(op.tenant_id)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            RunbookRun(
                tenant_id=op.tenant_id,
                template_slug="cert-rotate",
                template_version=1,
                assigned_to=op.sub,
                target="host:edge-01",
                params={},
                state="completed",
                started_by=op.sub,
            )
        )
        await session.commit()

    body = _call(client, "meho.runbook.show_template", {"template_slug": "cert-rotate"})
    assert body["error"]["code"] == INTERNAL_ERROR
    assert body["error"]["data"]["error"] == TEMPLATE_BODY_VALIDATION_FAILED
    assert body["error"]["data"]["slug"] == "cert-rotate"


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_show_tool_description_includes_post_completion_text(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Regression: the verbatim POST-COMPLETION EXCEPTION text is in the description.

    The description is part of the contract -- agents read it to learn when
    they are allowed to call this tool. A refactor that drops the carve-out
    text would silently regress junior-agent UX.
    """
    client, _op = client_with_operator
    response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools_by_name = {t["name"]: t for t in response.json()["result"]["tools"]}
    show_desc = tools_by_name["meho.runbook.show_template"]["description"]
    assert "POST-COMPLETION EXCEPTION" in show_desc
    # The two halves of the contract: the carve-out and the still-held
    # in_progress denial. Both are load-bearing.
    assert "completed or abandoned" in show_desc
    assert "in_progress" in show_desc


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_show_template_missing_error(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: ``TemplateNotFoundError`` surfaces as ``-32602``."""
    client, _op = client_with_operator
    body = _call(client, "meho.runbook.show_template", {"template_slug": "ghost"})
    assert body["error"]["code"] == INVALID_PARAMS


# ---------------------------------------------------------------------------
# #1625 — removal of the deprecated flat-name aliases + slug input alias
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_removed_flat_template_aliases_return_unknown_tool(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC #1625: the flat ``runbook_*_template`` names no longer resolve.

    #1612 kept the flat names as deprecated aliases for one release;
    #1625 removed them. A consumer that never migrated and calls a flat
    name gets the dispatcher's standard unknown-tool error (``-32602``,
    ``unknown tool: …``) — the same fall-through any unregistered name
    hits — and the flat names are absent from ``tools/list``.
    """
    client, _op = client_with_operator
    listed = {
        t["name"]
        for t in post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()[
            "result"
        ]["tools"]
    }
    assert not [name for name in listed if name.startswith("runbook_")]

    for flat in _REMOVED_FLAT_ALIASES:
        body = _call(client, flat, {"template_slug": "cert-rotate", "body": _body()})
        assert body["error"]["code"] == INVALID_PARAMS, flat
        assert "unknown tool" in body["error"]["message"], flat


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_slug_input_field_rejected_template_slug_required(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC #1625: template verbs accept ``template_slug`` only; ``slug`` is rejected.

    The deprecated ``slug`` input alias and its XOR guard are gone. The
    canonical ``template_slug`` works; supplying the removed ``slug``
    field is an unknown property under the schema's
    ``additionalProperties: false`` gate and surfaces as a clean
    ``-32602`` validation error before the handler runs.
    """
    client, _op = client_with_operator

    ok = _result_payload(
        _call(
            client,
            "meho.runbook.draft_template",
            {"template_slug": "cert-rotate", "body": _body()},
        )
    )
    assert ok["template_slug"] == "cert-rotate"

    rejected = _call(
        client, "meho.runbook.draft_template", {"slug": "cert-rotate", "body": _body()}
    )
    assert rejected["error"]["code"] == INVALID_PARAMS
    # The handler never runs — the schema gate rejects the now-unknown
    # ``slug`` property (and the absent required ``template_slug``).
    assert "inputSchema" in rejected["error"]["message"]


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
@pytest.mark.asyncio
async def test_list_templates_summaries_mirror_template_slug(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC #1612: list summaries carry ``template_slug`` == ``slug`` (round-trip key)."""
    client, op = client_with_operator
    await RunbookTemplateService().create_draft(
        tenant_id=op.tenant_id,
        operator_sub=op.sub,
        request=DraftTemplateRequest.model_validate({"slug": "cert-rotate", "body": _body()}),
    )

    payload = _result_payload(_call(client, "meho.runbook.list_templates", {}))
    summary = payload["templates"][0]
    assert summary["template_slug"] == summary["slug"] == "cert-rotate"
