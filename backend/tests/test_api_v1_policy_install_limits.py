# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The outer limit in the install's policy file, with ``CUSTOM_POLICY`` on.

* A platform admin imports an install file with ``limits``: with a token it
  waits for a click in the web console, in the web console it runs at once
  after a fresh sign-in. The export writes ``limits``, and the exported file
  imports again with "No change.".
* An agent login, a service login or a hosted agent run is refused before
  anything waits; a person who is not a platform admin cannot import the
  install's file; a tenant file with ``limits`` is refused.
* The audit row of the import names the platform admin.
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
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
import structlog
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.delegation import actor_delegation
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.policy.apply import (
    IMPORT_NEEDS_PERSON,
    INSTALL_ASK_NEEDS_PLATFORM_ADMIN,
    INSTALL_NEEDS_PLATFORM_ADMIN,
    POLICY_APPLY_OP_ID,
    register_policy_apply_operation,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog, PolicyRevision, TenantPolicy
from meho_backplane.main import app
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.dispatcher import dispatch
from meho_backplane.operations.typed_register import (
    clear_typed_op_snapshot,
    register_typed_operation,
)
from meho_backplane.policy.document import file_sha256
from meho_backplane.policy.limits import (
    LIMITS_NEED_CUSTOM_POLICY,
    install_personal_agent_limit,
)
from meho_backplane.policy.service import NO_CHANGE, build_plan
from meho_backplane.policy.store import load_classification
from meho_backplane.settings import get_settings
from tests.approval_table import build_rows, register_all_descriptors

from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)
from ._vault_fakes import install_fake_vault

_TENANT = uuid.UUID(DEFAULT_TENANT_ID)
_CONNECTOR = "policy-registry-1.x"

_FILE_P = "policy: v1\nclassifications:\n  - {match: {connector: inslim-1.x}, category: network}\n"
_FILE_L = _FILE_P + "limits: {personal_agent_up_to: dangerous}\n"

_TENANT_SENTENCE = (
    "`limits` belongs to the install's policy file. Only a platform admin sets the "
    "outer limit; a tenant's file stays inside it."
)


@pytest.fixture
def switch_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("CUSTOM_POLICY", "true")
    monkeypatch.setenv("APPROVAL_SIGN_IN_LEVEL", "observe")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


async def _handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {"ran": True}


async def _setup(embedding: AsyncMock) -> None:
    await register_policy_apply_operation(embedding_service=embedding)
    for op_id, level in (("inslim.vm.list", "safe"), ("inslim.vm.restart", "caution")):
        await register_typed_operation(
            product="inslim",
            version="1.x",
            impl_id="inslim",
            op_id=op_id,
            handler=_handler,
            summary="Fixture operation.",
            description="Fixture operation.",
            parameter_schema={"type": "object", "properties": {}},
            group_key="compute",
            when_to_use="Fixture group.",
            safety_level=level,
            embedding_service=embedding,
        )


def _token(key: Any, *, sub: str = "admin-a", platform_admin: bool = True, **claims: Any) -> str:
    claims.setdefault("tenant_role", TenantRole.TENANT_ADMIN.value)
    return mint_token(key, sub=sub, tenant_id=str(_TENANT), platform_admin=platform_admin, **claims)


def _post(client: TestClient, path: str, token: str, content: str, **params: str) -> Any:
    return client.post(
        f"/api/v1/policy/{path}",
        params=params,
        content=content.encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/yaml"},
    )


def _export(client: TestClient, token: str) -> Any:
    return client.get(
        "/api/v1/policy/export",
        params={"scope": "install"},
        headers={"Authorization": f"Bearer {token}"},
    )


def _person(
    sub: str,
    *,
    platform_admin: bool = True,
    web: bool = True,
    kind: PrincipalKind = PrincipalKind.USER,
) -> Operator:
    return Operator(
        sub=sub,
        tenant_id=_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=kind,
        platform_admin=platform_admin,
        raw_jwt="header.payload.signature",
        web_session=web,
        acr="1",
        auth_time=int(time.time()),
    )


async def _stored_install() -> dict[str, Any] | None:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(select(TenantPolicy).where(TenantPolicy.tenant_id.is_(None)))
        ).scalar_one_or_none()
    return None if row is None else row.document


async def _store_install(document: dict[str, Any]) -> None:
    async with get_sessionmaker()() as session, session.begin():
        session.add(TenantPolicy(tenant_id=None, document=document, set_by_sub="fixture"))


async def _requests() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(ApprovalRequest))).scalars())


async def _approve(request_id: uuid.UUID, approver: Operator) -> Any:
    async with get_sessionmaker()() as session:
        request = await approve_request(session, request_id, operator=approver)
        await session.commit()
    return await resume_dispatch_after_approval(operator=approver, request=request)


async def _limit() -> str:
    async with get_sessionmaker()() as session:
        return await install_personal_agent_limit(session, custom_policy=True)


async def _apply_params(content: str) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        plan = await build_plan(
            session,
            scope="install",
            tenant_id=None,
            content=content.encode(),
            custom_policy=True,
        )
    return {
        "scope": "install",
        "file": content,
        "file_sha256": file_sha256(content.encode()),
        "based_on": plan.based_on,
    }


async def _dispatch_apply(operator: Operator, content: str) -> Any:
    return await dispatch(
        operator=operator,
        connector_id=_CONNECTOR,
        op_id=POLICY_APPLY_OP_ID,
        target=None,
        params=await _apply_params(content),
    )


# ---------------------------------------------------------------------------
# Import, export, import again
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_a_token_import_waits_then_export_and_import_again_is_no_change(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _setup(embedding)
    key = make_rsa_keypair("kid-inslim-roundtrip")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        token = _token(key)
        preview = _post(client, "import", token, _FILE_L, scope="install", preview="true")
        asked = _post(client, "import", token, _FILE_L, scope="install")
        assert preview.json()["ok"] is True, preview.text
        assert preview.json()["diff"]["limits"]["added"] == [
            {"key": "personal_agent_up_to", "value": "dangerous"}
        ]
        assert asked.status_code == 202, asked.text
        assert await _stored_install() is None
        (request,) = await _requests()
        assert request.rights_change is True

        result = await _approve(uuid.UUID(asked.json()["approval_request_id"]), _person("b"))
        assert result.status == "ok", result
        assert result.result["summary"]["limits_added"] == 1

        exported = _export(client, token)
        again = _post(client, "import", token, exported.text, scope="install")
    assert exported.status_code == 200
    assert "personal_agent_up_to: dangerous" in exported.text
    assert again.status_code == 200, again.text
    assert again.json()["message"] == NO_CHANGE
    assert again.json()["diff"]["limits"] == {"added": [], "changed": [], "removed": []}
    assert len(await _requests()) == 1
    assert await _limit() == "dangerous"


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_a_platform_admin_in_the_web_console_runs_it_at_once(embedding: AsyncMock) -> None:
    await _setup(embedding)
    result = await _dispatch_apply(_person("admin-a", web=True), _FILE_L)
    assert result.status == "ok", result
    assert await _requests() == []
    assert (await _stored_install() or {})["limits"] == {"personal_agent_up_to": "dangerous"}
    assert await _limit() == "dangerous"


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_the_audit_row_of_the_import_names_the_platform_admin(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _setup(embedding)
    key = make_rsa_keypair("kid-inslim-audit")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        asked = _post(client, "import", _token(key, sub="platform-a"), _FILE_L, scope="install")
    assert asked.status_code == 202, asked.text
    result = await _approve(uuid.UUID(asked.json()["approval_request_id"]), _person("platform-b"))
    assert result.status == "ok", result
    async with get_sessionmaker()() as session:
        rows = list((await session.execute(select(AuditLog))).scalars().all())
    by_path = {(row.method, row.path): row for row in rows}
    request_row = by_path[("APPROVAL", "approval.request")]
    run_row = by_path[("DISPATCH", POLICY_APPLY_OP_ID)]
    assert request_row.operator_sub == "platform-a"
    assert request_row.payload["op_id"] == POLICY_APPLY_OP_ID
    assert by_path[("APPROVAL", "approval.decision")].operator_sub == "platform-b"
    assert (run_row.operator_sub, run_row.status_code) == ("platform-b", 200)
    async with get_sessionmaker()() as session:
        (revision,) = (await session.execute(select(PolicyRevision))).scalars().all()
    assert (revision.imported_by_sub, revision.approved_by_sub) == ("platform-a", "platform-b")


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_with_the_switch_turned_off_again_a_stored_limit_is_ignored(
    client: TestClient, embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _setup(embedding)
    assert (await _dispatch_apply(_person("admin-a", web=True), _FILE_L)).status == "ok"

    monkeypatch.delenv("CUSTOM_POLICY")
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true")
    get_settings.cache_clear()
    key = make_rsa_keypair("kid-inslim-off-again")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        token = _token(key)
        exported = _export(client, token)
        again = _post(client, "import", token, exported.text, scope="install")
        refused = _post(client, "validate", token, _FILE_L, scope="install")
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
    assert (await _stored_install() or {})["limits"] == {"personal_agent_up_to": "dangerous"}


# ---------------------------------------------------------------------------
# Who may ask, and who may approve
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
@pytest.mark.parametrize("kind", [PrincipalKind.AGENT, PrincipalKind.SERVICE])
async def test_an_agent_or_a_service_login_is_refused_before_anything_waits(
    client: TestClient, embedding: AsyncMock, kind: PrincipalKind
) -> None:
    await _setup(embedding)
    key = make_rsa_keypair(f"kid-inslim-{kind.value}")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _post(
            client,
            "import",
            _token(key, sub="machine", principal_kind=kind.value),
            _FILE_L,
            scope="install",
        )
    assert resp.status_code == 403, resp.text
    result = await _dispatch_apply(_person("machine", kind=kind, web=False), _FILE_L)
    assert result.status == "denied", result
    assert IMPORT_NEEDS_PERSON in str(result.error)
    assert await _requests() == []
    assert await _stored_install() is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_a_hosted_agent_run_is_refused_before_anything_waits(embedding: AsyncMock) -> None:
    await _setup(embedding)
    with actor_delegation("agent:helper"):
        result = await _dispatch_apply(_person("admin-a", web=False), _FILE_L)
    assert result.status == "denied", result
    assert IMPORT_NEEDS_PERSON in str(result.error)
    assert await _requests() == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_a_person_who_is_not_a_platform_admin_cannot_import_the_install_file(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _setup(embedding)
    key = make_rsa_keypair("kid-inslim-tenant-admin")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        token = _token(key, platform_admin=False)
        resp = _post(client, "import", token, _FILE_L, scope="install")
        export = _export(client, token)
    assert resp.status_code == 403, resp.text
    assert export.status_code == 403
    for web in (True, False):
        result = await _dispatch_apply(_person("tenant-a", platform_admin=False, web=web), _FILE_L)
        assert result.status == "denied", result
        assert INSTALL_ASK_NEEDS_PLATFORM_ADMIN in str(result.error)
    assert await _requests() == []
    assert await _stored_install() is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_only_a_platform_admin_can_approve_a_waiting_install_import(
    embedding: AsyncMock,
) -> None:
    await _setup(embedding)
    asked = await _dispatch_apply(_person("admin-a", web=False), _FILE_L)
    assert asked.status == "awaiting_approval", asked
    (request,) = await _requests()

    result = await _approve(request.id, _person("tenant-b", platform_admin=False))
    assert result.status != "ok", result
    assert INSTALL_NEEDS_PLATFORM_ADMIN in str(result.error) + str(result.extras)
    assert await _stored_install() is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_a_tenant_file_with_limits_is_refused_with_its_place(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _setup(embedding)
    key = make_rsa_keypair("kid-inslim-tenant-file")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        token = _token(key)
        checked = _post(client, "validate", token, _FILE_L)
        resp = _post(client, "import", token, _FILE_L)
    assert checked.json()["mistakes"] == [_TENANT_SENTENCE]
    assert resp.status_code == 422, resp.text
    assert resp.json()["mistakes"] == [_TENANT_SENTENCE]
    assert await _requests() == []


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
        resp = _post(client, "validate", _token(key), f"policy: v1\n{part}\n", scope="install")
        preview = _post(
            client, "import", _token(key), f"policy: v1\n{part}\n", scope="install", preview="true"
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
                operator=_person("caller", platform_admin=False, web=False, kind=kind),
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
    await _setup(embedding)
    before = await _answers(embedding)
    assert await _limit() == "safe"

    async with get_sessionmaker()() as session, session.begin():
        await session.execute(TenantPolicy.__table__.delete())
    await _store_install(stored)
    with structlog.testing.capture_logs() as logs:
        limit = await _limit()
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
        await _store_install({"description": "No limits"})
        assert await _limit() == "safe"
        rows = build_rows(await register_all_descriptors())
    finally:
        clear_typed_op_snapshot()
        get_settings.cache_clear()
    table = Path(__file__).parent / "snapshots" / "approval_table.json"
    assert rows == json.loads(table.read_text(encoding="utf-8"))
