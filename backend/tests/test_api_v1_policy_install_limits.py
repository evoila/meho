# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The outer limit in the install's policy file, with ``CUSTOM_POLICY`` on.

* A platform admin imports an install file with ``limits``: with a token it
  waits for a click in the web console (``POST /ui/approvals/{id}/approve``
  after a fresh console sign-in), in the web console it runs at once. The
  export writes ``limits``, and the exported file imports again with "No
  change.".
* An agent login, a service login or a hosted agent run is refused before
  anything waits; a person who is not a platform admin cannot import the
  install's file; a tenant file with ``limits`` is refused. Each refusal
  writes one audit row with the caller and the refused status.
* The audit rows of the import name the platform admins who asked and approved.
* Who may approve a waiting install import: only a platform admin.
* A wrong ``limits`` part gets one sentence with its place.
* A stored value that MEHO cannot read gives the strictest limit and one
  warning, and changes no call's answer.
* With no ``limits`` part the limit is ``safe``, and the approval table is
  the same as with the switch off.

Drives the production ``meho_backplane.main:app``. DB is the autouse
per-test SQLite + ``alembic upgrade head``.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
import structlog
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.delegation import actor_delegation
from meho_backplane.auth.operator import PrincipalKind
from meho_backplane.connectors.policy.apply import (
    IMPORT_NEEDS_PERSON,
    INSTALL_ASK_NEEDS_PLATFORM_ADMIN,
    INSTALL_NEEDS_PLATFORM_ADMIN,
    POLICY_APPLY_OP_ID,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import PolicyRevision, TenantPolicy
from meho_backplane.operations.dispatcher import dispatch
from meho_backplane.operations.typed_register import clear_typed_op_snapshot
from meho_backplane.policy.limits import (
    LIMITS_NEED_CUSTOM_POLICY,
    install_personal_agent_limit,
)
from meho_backplane.policy.service import NO_CHANGE
from meho_backplane.policy.store import connector_id_of, load_classification
from meho_backplane.settings import get_settings
from tests.approval_table import build_rows, register_all_descriptors

from . import _install_limits_helpers as h
from ._install_limits_helpers import (
    client_fixture,  # noqa: F401
    embedding_fixture,  # noqa: F401
    switch_on_fixture,  # noqa: F401
)
from ._oidc_jwt_helpers import make_rsa_keypair, mock_discovery_and_jwks, public_jwks
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)

# ---------------------------------------------------------------------------
# Import, export, import again
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_a_token_import_waits_then_export_and_import_again_is_no_change(
    client: TestClient, embedding: AsyncMock
) -> None:
    await h.setup_ops(embedding)
    key = make_rsa_keypair("kid-inslim-roundtrip")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        token = h.token(key)
        preview = h.post(client, "import", token, h.FILE_L, scope="install", preview="true")
        asked = h.post(client, "import", token, h.FILE_L, scope="install")
        assert preview.json()["ok"] is True, preview.text
        assert preview.json()["diff"]["limits"]["added"] == [
            {"key": "personal_agent_up_to", "value": "dangerous"}
        ]
        assert asked.status_code == 202, asked.text
        assert await h.stored_install() is None
        (request,) = await h.requests()
        assert request.rights_change is True

        clicked = await h.console_approve(
            client, key, uuid.UUID(asked.json()["approval_request_id"]), sub="admin-b"
        )
        assert clicked.status_code == 200, clicked.text
        (run_row,) = await h.audit_rows("DISPATCH", POLICY_APPLY_OP_ID)
        assert (run_row.operator_sub, run_row.status_code) == ("admin-b", 200)
        assert await h.pending_requests() == []

        exported = h.export(client, token)
        again = h.post(client, "import", token, exported.text, scope="install")
    assert exported.status_code == 200
    assert "personal_agent_up_to: dangerous" in exported.text
    assert again.status_code == 200, again.text
    assert again.json()["message"] == NO_CHANGE
    assert again.json()["diff"]["limits"] == {"added": [], "changed": [], "removed": []}
    assert len(await h.requests()) == 1
    assert await h.limit() == "dangerous"


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_a_platform_admin_in_the_web_console_runs_it_at_once(embedding: AsyncMock) -> None:
    await h.setup_ops(embedding)
    result = await h.dispatch_apply(h.person("admin-a", web=True), h.FILE_L)
    assert result.status == "ok", result
    assert await h.requests() == []
    assert (await h.stored_install() or {})["limits"] == {"personal_agent_up_to": "dangerous"}
    assert await h.limit() == "dangerous"


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_the_audit_row_of_the_import_names_the_platform_admin(
    client: TestClient, embedding: AsyncMock
) -> None:
    """Case 14: a token ask, a click in the web console by another platform admin."""
    await h.setup_ops(embedding)
    key = make_rsa_keypair("kid-inslim-audit")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        asked = h.post(client, "import", h.token(key, sub="platform-a"), h.FILE_L, scope="install")
        assert asked.status_code == 202, asked.text
        clicked = await h.console_approve(
            client, key, uuid.UUID(asked.json()["approval_request_id"]), sub="platform-b"
        )
    assert clicked.status_code == 200, clicked.text
    assert (await h.stored_install() or {})["limits"] == {"personal_agent_up_to": "dangerous"}
    (request_row,) = await h.audit_rows("APPROVAL", "approval.request")
    (decision_row,) = await h.audit_rows("APPROVAL", "approval.decision")
    (run_row,) = await h.audit_rows("DISPATCH", POLICY_APPLY_OP_ID)
    assert request_row.operator_sub == "platform-a"
    assert request_row.payload["op_id"] == POLICY_APPLY_OP_ID
    assert decision_row.operator_sub == "platform-b"
    assert decision_row.payload["approval_auth"]["channel"] == "web"
    assert (run_row.operator_sub, run_row.status_code) == ("platform-b", 200)
    async with get_sessionmaker()() as session:
        (revision,) = (await session.execute(select(PolicyRevision))).scalars().all()
    assert (revision.imported_by_sub, revision.approved_by_sub) == ("platform-a", "platform-b")


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_with_the_switch_turned_off_again_a_stored_limit_is_ignored(
    client: TestClient, embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await h.setup_ops(embedding)
    assert (await h.dispatch_apply(h.person("admin-a", web=True), h.FILE_L)).status == "ok"

    monkeypatch.delenv("CUSTOM_POLICY")
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true")
    get_settings.cache_clear()
    key = make_rsa_keypair("kid-inslim-off-again")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        token = h.token(key)
        exported = h.export(client, token)
        again = h.post(client, "import", token, exported.text, scope="install")
        refused = h.post(client, "validate", token, h.FILE_L, scope="install")
    assert exported.status_code == 200
    assert "limits" not in exported.text
    assert "inslim-1.x" in exported.text
    assert again.json()["message"] == NO_CHANGE, again.text
    assert "limits" not in again.json()["diff"]
    assert refused.json()["mistakes"] == [LIMITS_NEED_CUSTOM_POLICY]
    async with get_sessionmaker()() as session:
        taxonomy, entries = await load_classification(session, None)
        off = await install_personal_agent_limit(session, custom_policy=False)
    assert off == "safe"
    assert [entry.category for entry in entries] == ["network"]
    assert "network" in taxonomy.categories
    assert (await h.stored_install() or {})["limits"] == {"personal_agent_up_to": "dangerous"}


# ---------------------------------------------------------------------------
# Who may ask, and who may approve
# ---------------------------------------------------------------------------


async def _assert_refused_row(
    method: str, path: str, sub: str, status: int, actor: str | None = None
) -> None:
    """The refused attempt has one audit row with the caller and the status; nothing waits."""
    (row,) = [r for r in await h.audit_rows(method, path) if r.operator_sub == sub]
    assert (row.status_code, row.actor_sub) == (status, actor)
    assert await h.pending_requests() == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
@pytest.mark.parametrize(
    ("kind", "sub"),
    [(PrincipalKind.AGENT, "example-agent"), (PrincipalKind.SERVICE, "example-service")],
    ids=["9-agent", "10-service"],
)
async def test_an_agent_or_a_service_login_is_refused_before_anything_waits(
    client: TestClient, embedding: AsyncMock, kind: PrincipalKind, sub: str
) -> None:
    await h.setup_ops(embedding)
    key = make_rsa_keypair(f"kid-inslim-{kind.value}")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        bearer = h.token(key, sub=sub, principal_kind=kind.value)
        resp = h.post(client, "import", bearer, h.FILE_L, scope="install")
    assert resp.status_code == 403, resp.text
    await _assert_refused_row("POST", "/api/v1/policy/import", sub, 403)
    result = await h.dispatch_apply(h.person(sub, kind=kind, web=False), h.FILE_L)
    assert result.status == "denied", result
    assert IMPORT_NEEDS_PERSON in str(result.error)
    await _assert_refused_row("DISPATCH", POLICY_APPLY_OP_ID, sub, 403)
    assert await h.requests() == []
    assert await h.stored_install() is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_a_hosted_agent_run_is_refused_before_anything_waits(embedding: AsyncMock) -> None:
    """Case 11."""
    await h.setup_ops(embedding)
    with actor_delegation("agent:example"):
        result = await h.dispatch_apply(h.person("example-admin", web=False), h.FILE_L)
    assert result.status == "denied", result
    assert IMPORT_NEEDS_PERSON in str(result.error)
    await _assert_refused_row(
        "DISPATCH", POLICY_APPLY_OP_ID, "example-admin", 403, actor="agent:example"
    )
    assert await h.requests() == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_a_person_who_is_not_a_platform_admin_cannot_import_the_install_file(
    client: TestClient, embedding: AsyncMock
) -> None:
    """Case 12."""
    await h.setup_ops(embedding)
    sub = "example-tenant-admin"
    key = make_rsa_keypair("kid-inslim-tenant-admin")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        bearer = h.token(key, sub=sub, platform_admin=False)
        resp = h.post(client, "import", bearer, h.FILE_L, scope="install")
        export = h.export(client, bearer)
    assert resp.status_code == 403, resp.text
    assert export.status_code == 403
    await _assert_refused_row("POST", "/api/v1/policy/import", sub, 403)
    result = await h.dispatch_apply(h.person(sub, platform_admin=False, web=False), h.FILE_L)
    assert result.status == "denied", result
    assert INSTALL_ASK_NEEDS_PLATFORM_ADMIN in str(result.error)
    await _assert_refused_row("DISPATCH", POLICY_APPLY_OP_ID, sub, 403)
    web = await h.dispatch_apply(h.person(sub, platform_admin=False, web=True), h.FILE_L)
    assert web.status == "denied", web
    assert INSTALL_ASK_NEEDS_PLATFORM_ADMIN in str(web.error)
    assert await h.requests() == []
    assert await h.stored_install() is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_only_a_platform_admin_can_approve_a_waiting_install_import(
    embedding: AsyncMock,
) -> None:
    await h.setup_ops(embedding)
    asked = await h.dispatch_apply(h.person("admin-a", web=False), h.FILE_L)
    assert asked.status == "awaiting_approval", asked
    (request,) = await h.requests()

    result = await h.approve(request.id, h.person("tenant-b", platform_admin=False))
    assert result.status != "ok", result
    assert INSTALL_NEEDS_PLATFORM_ADMIN in str(result.error) + str(result.extras)
    assert await h.stored_install() is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_a_tenant_file_with_limits_is_refused_with_its_place(
    client: TestClient, embedding: AsyncMock
) -> None:
    """Case 13."""
    await h.setup_ops(embedding)
    key = make_rsa_keypair("kid-inslim-tenant-file")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        bearer = h.token(key, sub="example-admin")
        checked = h.post(client, "validate", bearer, h.FILE_L)
        resp = h.post(client, "import", bearer, h.FILE_L, scope="tenant")
    assert checked.json()["mistakes"] == [h.TENANT_SENTENCE]
    assert resp.status_code == 422, resp.text
    assert resp.json()["mistakes"] == [h.TENANT_SENTENCE]
    await _assert_refused_row("POST", "/api/v1/policy/import", "example-admin", 422)
    assert await h.requests() == []


# ---------------------------------------------------------------------------
# A wrong ``limits`` part, with the switch on
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("part", "mistakes"),
    [
        ("limits: {}", []),
        ("limits: null", []),
        ("limits: {personal_agent_up_to: null}", []),
        ("limits: {example_key: safe}", ["`limits.example_key` is not a known key."]),
        (
            "limits: {personal_agent_up_to: destructive}",
            [
                "`limits.personal_agent_up_to` is not a level a limit can have; use safe, "
                "caution or dangerous. A destructive operation always waits."
            ],
        ),
        ("limits: []", ["`limits` must be a mapping of named keys."]),
    ],
)
@pytest.mark.usefixtures("switch_on")
def test_a_wrong_limits_part_gets_one_sentence(
    client: TestClient, part: str, mistakes: list[str]
) -> None:
    key = make_rsa_keypair("kid-inslim-wrong")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = h.post(client, "validate", h.token(key), f"policy: v1\n{part}\n", scope="install")
        preview = h.post(
            client, "import", h.token(key), f"policy: v1\n{part}\n", scope="install", preview="true"
        )
    assert resp.json()["mistakes"] == mistakes, resp.text
    assert preview.json()["mistakes"] == mistakes, preview.text
    if not mistakes:
        # An empty part means the same as no part: no ``limits`` section in the diff.
        assert "limits" not in preview.json()["diff"]


# ---------------------------------------------------------------------------
# A stored limit that MEHO cannot read
# ---------------------------------------------------------------------------

_UNREADABLE = [
    pytest.param({"limits": ["dangerous"]}, "install:limits", id="list"),
    pytest.param({"limits": "dangerous"}, "install:limits", id="text"),
    pytest.param({"limits": 3}, "install:limits", id="number"),
    pytest.param(
        {"limits": {"personal_agent_up_to": "destructive"}},
        "install:limits.personal_agent_up_to",
        id="unknown-level",
    ),
    pytest.param(
        {"limits": {"personal_agent_up_to": ["dangerous"]}},
        "install:limits.personal_agent_up_to",
        id="level-list",
    ),
]


async def _answers(embedding: AsyncMock) -> list[tuple[str, str, str]]:
    """The answer of each fixture call, for a person's token and an agent login."""
    out = []
    for kind in (PrincipalKind.USER, PrincipalKind.AGENT):
        for op_id in ("inslim.vm.list", "inslim.vm.restart"):
            result = await dispatch(
                operator=h.person("caller", platform_admin=False, web=False, kind=kind),
                connector_id="inslim-1.x",
                op_id=op_id,
                target=None,
                params={},
            )
            out.append((kind.value, op_id, result.status))
    return out


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
@pytest.mark.parametrize(("stored", "place"), _UNREADABLE)
async def test_a_limit_that_cannot_be_read_is_the_strictest_and_changes_no_answer(
    embedding: AsyncMock, stored: dict[str, Any], place: str
) -> None:
    await h.setup_ops(embedding)
    before = await _answers(embedding)
    assert await h.limit() == "safe"

    async with get_sessionmaker()() as session, session.begin():
        await session.execute(TenantPolicy.__table__.delete())
    await h.store_install(stored)
    with structlog.testing.capture_logs() as logs:
        limit = await h.limit()
    after = await _answers(embedding)

    assert limit == "safe"
    warnings = [e for e in logs if e["event"] == "policy_limit_not_readable"]
    assert [(w["log_level"], w["place"]) for w in warnings] == [("warning", place)]
    assert after == before


# ---------------------------------------------------------------------------
# No ``limits`` part: the limit is ``safe`` and the approval table is the same
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_no_limits_part_the_limit_is_safe_and_the_table_is_the_same(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_approval_table_snapshot import _use_built_in_settings

    _use_built_in_settings(monkeypatch)
    monkeypatch.delenv("POLICY_CLASSIFICATION", raising=False)
    monkeypatch.setenv("CUSTOM_POLICY", "true")
    get_settings.cache_clear()
    clear_typed_op_snapshot()
    try:
        await h.store_install({"description": "No limits"})
        assert await h.limit() == "safe"
        descriptors = await register_all_descriptors()
        rows = build_rows(descriptors)
    finally:
        clear_typed_op_snapshot()
        get_settings.cache_clear()
    table = Path(__file__).parent / "snapshots" / "approval_table.json"
    expected = json.loads(table.read_text(encoding="utf-8"))
    # With the switch on, an operation that is hard to undo has a delete sign;
    # the snapshot is made with the switch off.
    hard_to_undo = {
        (d.op_id, connector_id_of(d))
        for d in descriptors
        if d.tenant_id is None and d.hard_to_undo is not None
    }
    assert hard_to_undo
    for row in expected:
        if (row["op_id"], row["connector_id"]) in hard_to_undo and row["never_covered"] is None:
            row["never_covered"] = "delete_shaped"
    assert rows == expected
