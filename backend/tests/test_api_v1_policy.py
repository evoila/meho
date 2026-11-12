# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``/api/v1/policy`` (the policy file, ``policy: v1``).

* Who may call: no token 401; another role 403; the install's policy needs
  a platform admin; a machine principal cannot import.
* With the ``POLICY_CLASSIFICATION`` switch off, the import is refused with
  one sentence.
* The preview shows the diff and writes nothing. The import makes a
  request that waits; nothing changes yet. The importer can never approve
  it; a second tenant admin can, and then the policy is stored and listed
  in the history.
* The same file again is "No change.": nothing waits.
* When the stored policy changed after the plan, the approved import does
  not run.
* After a failed run, the importer's own re-run writes nothing.
* ``policy.apply`` through ``dispatch`` (``call_operation``, the REST
  dispatch route, the CLI) refuses an agent, a hosted agent run, and an
  install-wide import from someone who is not a platform admin: no request
  waits and no plan is built. The run checks the recorded requester again.
  The preview gives the same ``deny``.

Drives the production ``meho_backplane.main:app``. DB is the autouse
per-test SQLite + ``alembic upgrade head``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.delegation import actor_delegation
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.policy import apply as policy_apply_module
from meho_backplane.connectors.policy.apply import (
    IMPORT_NEEDS_PERSON,
    IMPORT_NEEDS_SECOND_ADMIN,
    INSTALL_ASK_NEEDS_PLATFORM_ADMIN,
    POLICY_APPLY_OP_ID,
    SWITCH_OFF_IMPORT,
    register_policy_apply_operation,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, PolicyRevision, TenantPolicy
from meho_backplane.main import app
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.operations.approval_queue import (
    RightsChangeApprovalError,
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.approval_rerun import rerun_failed_request
from meho_backplane.operations.dispatcher import dispatch
from meho_backplane.operations.grant_approval import is_rights_change_op
from meho_backplane.operations.role_floors import role_floor_for
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.policy.service import NO_CHANGE, POLICY_CHANGED
from meho_backplane.settings import get_settings

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

_FILE = (
    "policy: v1\n"
    "description: Network team\n"
    "classifications:\n"
    "  - {match: {connector: polapi-1.x}, category: network}\n"
)
_OTHER_FILE = "policy: v1\nclassifications:\n  - {match: {op: polapi.*}, always_wait: true}\n"


@pytest.fixture
def switch(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true")
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
    return {}


async def _setup(embedding: AsyncMock) -> None:
    await register_policy_apply_operation(embedding_service=embedding)
    await register_typed_operation(
        product="polapi",
        version="1.x",
        impl_id="polapi",
        op_id="polapi.vm.list",
        handler=_handler,
        summary="Fixture operation.",
        description="Fixture operation.",
        parameter_schema={"type": "object", "properties": {}},
        group_key="compute",
        when_to_use="Fixture group.",
        embedding_service=embedding,
    )


def _token(key: Any, *, sub: str = "admin-a", **claims: Any) -> str:
    claims.setdefault("tenant_role", TenantRole.TENANT_ADMIN.value)
    return mint_token(key, sub=sub, tenant_id=str(_TENANT), **claims)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _post(client: TestClient, path: str, token: str, content: str, **params: str) -> Any:
    return client.post(
        f"/api/v1/policy/{path}",
        params=params or None,
        content=content.encode(),
        headers={**_auth(token), "Content-Type": "application/yaml"},
    )


def _admin(
    sub: str, *, kind: PrincipalKind = PrincipalKind.USER, platform_admin: bool = False
) -> Operator:
    return Operator(
        sub=sub,
        tenant_id=_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=kind,
        platform_admin=platform_admin,
        raw_jwt="header.payload.signature",
    )


async def _stored() -> dict[str, Any] | None:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(select(TenantPolicy).where(TenantPolicy.tenant_id == _TENANT))
        ).scalar_one_or_none()
    return None if row is None else row.document


async def _requests() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(ApprovalRequest))).scalars())


async def _approve(request_id: uuid.UUID, approver: Operator) -> Any:
    async with get_sessionmaker()() as session:
        request = await approve_request(session, request_id, operator=approver)
        await session.commit()
    return await resume_dispatch_after_approval(operator=approver, request=request)


# ---------------------------------------------------------------------------
# Who may call
# ---------------------------------------------------------------------------


def test_every_endpoint_needs_a_token(client: TestClient) -> None:
    assert client.get("/api/v1/policy/export").status_code == 401
    assert client.get("/api/v1/policy/history").status_code == 401
    assert client.post("/api/v1/policy/validate", content=b"x").status_code == 401
    assert client.post("/api/v1/policy/import", content=b"x").status_code == 401


def test_other_roles_are_refused(client: TestClient) -> None:
    key = make_rsa_keypair("kid-pol-role")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        token = _token(key, tenant_role=TenantRole.OPERATOR.value)
        assert client.get("/api/v1/policy/export", headers=_auth(token)).status_code == 403
        assert _post(client, "validate", token, _FILE).status_code == 403
        assert _post(client, "import", token, _FILE, preview="true").status_code == 403


def test_the_install_policy_needs_a_platform_admin(client: TestClient) -> None:
    key = make_rsa_keypair("kid-pol-install")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _post(client, "validate", _token(key), _FILE, scope="install")
        assert resp.status_code == 403
        assert resp.json()["detail"] == "The install's policy needs a platform admin."
        token = _token(key, platform_admin=True)
        assert _post(client, "validate", token, _FILE, scope="install").json()["ok"] is True
        machine = _token(key, platform_admin=True, principal_kind="service")
        assert _post(client, "validate", machine, _FILE, scope="install").status_code == 403


def test_a_machine_principal_cannot_import(client: TestClient) -> None:
    key = make_rsa_keypair("kid-pol-agent")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _post(client, "import", _token(key, principal_kind="agent"), _FILE)
    assert resp.status_code == 403
    assert "human_principal_required" in resp.json()["detail"]


def test_the_import_is_refused_with_the_switch_off(client: TestClient) -> None:
    key = make_rsa_keypair("kid-pol-off")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _post(client, "import", _token(key), _FILE)
    assert resp.status_code == 409
    assert resp.json()["detail"] == SWITCH_OFF_IMPORT


# ---------------------------------------------------------------------------
# Validate, preview, import, approval, history, export
# ---------------------------------------------------------------------------


def test_validate_lists_every_mistake(client: TestClient) -> None:
    content = "policy: v1\nclassifications:\n  - {match: {op: x}, category: cars, colour: red}\n"
    key = make_rsa_keypair("kid-pol-validate")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        body = _post(client, "validate", _token(key), content).json()
    assert body["ok"] is False
    assert body["mistakes"] == ["`classifications[0].colour` is not a known key."]


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch")
async def test_import_waits_and_only_a_second_admin_applies_it(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _setup(embedding)
    assert role_floor_for(POLICY_APPLY_OP_ID) == TenantRole.TENANT_ADMIN
    assert is_rights_change_op(POLICY_APPLY_OP_ID)
    key = make_rsa_keypair("kid-pol-flow")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        token = _token(key)
        preview = _post(client, "import", token, _FILE, preview="true").json()
        assert preview["ok"] is True and preview["preview"] is True
        added = preview["diff"]["classifications"]["added"]
        assert [(a["matches"], a["changes"]) for a in added] == [(1, 1)]

        resp = _post(client, "import", token, _FILE)
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["based_on"] == preview["based_on"]
        assert body["message"].startswith("The import waits until a second admin approves it.")
    assert await _stored() is None

    request_id = uuid.UUID(body["approval_request_id"])
    (request,) = await _requests()
    assert request.id == request_id and request.rights_change is True
    plan = (request.proposed_effect or {}).get("preview") or {}
    assert plan["file_sha256"] == body["file_sha256"]
    assert plan["diff"]["classifications"]["added"][0]["matches"] == 1

    async with get_sessionmaker()() as session:
        with pytest.raises(RightsChangeApprovalError):
            await approve_request(session, request_id, operator=_admin("admin-a"))
        await session.rollback()
    result = await _approve(request_id, _admin("admin-b"))
    assert result.status == "ok", result
    assert result.result["applied"] is True
    assert (await _stored() or {})["description"] == "Network team"

    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        history = client.get("/api/v1/policy/history", headers=_auth(token)).json()
        exported = client.get("/api/v1/policy/export", headers=_auth(token))
        again = _post(client, "import", token, exported.text)
    assert [(h["imported_by_sub"], h["approved_by_sub"]) for h in history] == [
        ("admin-a", "admin-b")
    ]
    assert history[0]["file"] == _FILE
    assert exported.text.startswith("policy: v1\n")
    assert again.status_code == 200
    assert again.json()["message"] == NO_CHANGE
    assert len(await _requests()) == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch")
async def test_a_file_with_mistakes_is_422_and_nothing_waits(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _setup(embedding)
    key = make_rsa_keypair("kid-pol-422")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _post(client, "import", _token(key), "policy: v1\nrouting: []\n")
    assert resp.status_code == 422
    assert resp.json()["mistakes"] == ["`routing` is not a known key."]
    assert await _requests() == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch")
async def test_an_import_planned_on_an_old_policy_does_not_run(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _setup(embedding)
    key = make_rsa_keypair("kid-pol-moved")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        first = _post(client, "import", _token(key), _FILE).json()
        second = _post(client, "import", _token(key, sub="admin-c"), _OTHER_FILE).json()
    assert (
        await _approve(uuid.UUID(first["approval_request_id"]), _admin("admin-b"))
    ).status == "ok"
    result = await _approve(uuid.UUID(second["approval_request_id"]), _admin("admin-b"))
    assert result.status != "ok"
    assert POLICY_CHANGED in str(result.error) + str(result.extras)
    async with get_sessionmaker()() as session:
        assert len((await session.execute(select(PolicyRevision))).scalars().all()) == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch")
async def test_the_importer_cannot_rerun_a_failed_approved_import(
    client: TestClient, embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _setup(embedding)
    key = make_rsa_keypair("kid-pol-rerun")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        body = _post(client, "import", _token(key), _FILE).json()
    request_id = uuid.UUID(body["approval_request_id"])

    async def _fails(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("The first run failed.")

    with monkeypatch.context() as m:
        m.setattr(policy_apply_module, "apply_policy", _fails)
        first = await _approve(request_id, _admin("admin-b"))
    assert first.status != "ok"

    outcome = await rerun_failed_request(operator=_admin("admin-a"), request_id=request_id)
    assert outcome.result.status != "ok"
    assert IMPORT_NEEDS_SECOND_ADMIN in str(outcome.result.error) + str(outcome.result.extras)
    assert await _stored() is None
    async with get_sessionmaker()() as session:
        assert (await session.execute(select(PolicyRevision))).scalars().all() == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch")
async def test_a_second_admin_replaces_a_stored_policy_that_is_not_valid(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _setup(embedding)
    async with get_sessionmaker()() as session:
        session.add(
            TenantPolicy(
                tenant_id=_TENANT,
                document={"classifications": [{"match": {"op": "x"}, "risk": "huge"}]},
                set_by_sub="fixture",
            )
        )
        await session.commit()
    key = make_rsa_keypair("kid-pol-repair")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _post(client, "import", _token(key), _FILE)
    assert resp.status_code == 202, resp.text
    request_id = uuid.UUID(resp.json()["approval_request_id"])

    async with get_sessionmaker()() as session:
        with pytest.raises(RightsChangeApprovalError):
            await approve_request(session, request_id, operator=_admin("admin-a"))
        await session.rollback()
    result = await _approve(request_id, _admin("admin-b"))
    assert result.status == "ok", result
    assert (await _stored() or {})["description"] == "Network team"


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch")
async def test_the_same_rules_in_another_order_do_not_wait(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _setup(embedding)
    async with get_sessionmaker()() as session:
        session.add(
            TenantPolicy(
                tenant_id=_TENANT,
                document={
                    "classifications": [
                        {"match": {"connector": "polapi-1.x"}, "category": "network"},
                        {"match": {"op": "polapi.*"}, "always_wait": True},
                    ]
                },
                set_by_sub="fixture",
            )
        )
        await session.commit()
    reordered = (
        "policy: v1\n"
        "classifications:\n"
        "  - {match: {op: polapi.*}, always_wait: true}\n"
        "  - {match: {connector: polapi-1.x}, category: network}\n"
    )
    key = make_rsa_keypair("kid-pol-order")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _post(client, "import", _token(key), reordered)
    assert resp.status_code == 200, resp.text
    assert resp.json()["message"] == NO_CHANGE
    assert await _requests() == []
    async with get_sessionmaker()() as session:
        assert (await session.execute(select(PolicyRevision))).scalars().all() == []


# ---------------------------------------------------------------------------
# policy.apply through dispatch: who may ask, checked again at the run
# ---------------------------------------------------------------------------


def _apply_params(scope: str) -> dict[str, Any]:
    return {"scope": scope, "file": _FILE, "file_sha256": "0" * 64, "based_on": "0" * 64}


async def _dispatch_apply(operator: Operator, scope: str = "tenant") -> Any:
    return await dispatch(
        operator=operator,
        connector_id="policy-registry-1.x",
        op_id=POLICY_APPLY_OP_ID,
        target=None,
        params=_apply_params(scope),
    )


@pytest.fixture
def no_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fails(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no plan is built for a refused requester")

    monkeypatch.setattr(policy_apply_module, "build_plan", _fails)


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch", "no_plan")
@pytest.mark.parametrize(
    ("kind", "scope", "reason"),
    [
        (PrincipalKind.AGENT, "tenant", IMPORT_NEEDS_PERSON),
        (PrincipalKind.SERVICE, "tenant", IMPORT_NEEDS_PERSON),
        (PrincipalKind.USER, "install", INSTALL_ASK_NEEDS_PLATFORM_ADMIN),
    ],
)
async def test_dispatch_refuses_a_requester_who_may_not_ask(
    embedding: AsyncMock, kind: PrincipalKind, scope: str, reason: str
) -> None:
    await _setup(embedding)
    result = await _dispatch_apply(_admin("caller", kind=kind), scope)
    assert result.status == "denied", result
    assert reason in str(result.error)
    assert await _requests() == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch", "no_plan")
async def test_dispatch_refuses_a_hosted_agent_run(embedding: AsyncMock) -> None:
    await _setup(embedding)
    with actor_delegation("agent:helper"):
        result = await _dispatch_apply(_admin("admin-a"))
    assert result.status == "denied", result
    assert IMPORT_NEEDS_PERSON in str(result.error)
    assert await _requests() == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch")
async def test_a_platform_admin_imports_the_install_policy(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _setup(embedding)
    key = make_rsa_keypair("kid-pol-install-run")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _post(client, "import", _token(key, platform_admin=True), _FILE, scope="install")
    assert resp.status_code == 202, resp.text
    (request,) = await _requests()
    record = request.proposed_effect["permission_preflight"]["requester"]  # type: ignore[index]
    assert record["platform_admin"] is True and record["principal_kind"] == "user"
    result = await _approve(request.id, _admin("admin-b", platform_admin=True))
    assert result.status == "ok", result
    assert result.result["applied"] is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch")
@pytest.mark.parametrize(
    "record",
    [None, {"sub": "admin-a", "principal_kind": "agent", "platform_admin": False, "act": None}],
)
async def test_the_run_checks_the_recorded_requester_again(
    client: TestClient, embedding: AsyncMock, record: dict[str, Any] | None
) -> None:
    await _setup(embedding)
    key = make_rsa_keypair("kid-pol-record")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        body = _post(client, "import", _token(key), _FILE).json()
    request_id = uuid.UUID(body["approval_request_id"])
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        effect = dict(row.proposed_effect)
        effect["permission_preflight"] = None if record is None else {"requester": record}
        row.proposed_effect = effect
        await session.commit()
    result = await _approve(request_id, _admin("admin-b"))
    assert result.status != "ok"
    assert IMPORT_NEEDS_PERSON in str(result.error) + str(result.extras)
    assert await _stored() is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch", "no_plan")
async def test_the_preview_denies_what_dispatch_denies(embedding: AsyncMock) -> None:
    await _setup(embedding)
    operator = _admin("caller")
    preview = await preview_dispatch(
        operator=operator,
        connector_id="policy-registry-1.x",
        op_id=POLICY_APPLY_OP_ID,
        target=None,
        params=_apply_params("install"),
    )
    assert preview["policy_verdict"] == "deny", preview
    assert INSTALL_ASK_NEEDS_PLATFORM_ADMIN in str(preview["policy_reason"])
    result = await _dispatch_apply(operator, "install")
    assert result.status == "denied", result
    assert INSTALL_ASK_NEEDS_PLATFORM_ADMIN in str(result.error)
    assert await _requests() == []
