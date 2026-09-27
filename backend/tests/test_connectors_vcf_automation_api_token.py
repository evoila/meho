# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""VCFA API-token mint / revoke + the no-transit guarantee (#3890).

``vcfa.provider.api_token.create`` mints an org user's refresh token and
writes it straight to Vault. The load-bearing assertions here are that
the token value never reaches any audit-facing surface: the op result,
the durable audit row, the broadcast event, the park-time
``proposed_effect``, or a flight-recorder body. Also covers the flow
(user session → OAuth register → jwt-bearer grant), idempotency on the
token name, the System-org path, Vault patch→put fallback, revoke-on-
store-failure, login failure mapping, and the revoke op.

The Vault write runs on the caller-named ``vault_target`` (#3895): the KV
handlers receive that resolved Vault target, so the write executes under
its Vault role, and the park card carries a ``permission_preflight`` probed
under the same role.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import hvac.exceptions
import pytest
import respx
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors.vault import ops as vault_ops_module
from meho_backplane.connectors.vault.ops import register_vault_typed_operations
from meho_backplane.connectors.vcf_automation import VCFA_CONNECTOR_ID, VcfAutomationConnector
from meho_backplane.connectors.vcf_automation import _lookups as lookups_module
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentPermission,
    ApprovalRequest,
    AuditLog,
    ServicePrincipalGrant,
    Target,
)
from meho_backplane.operations import (
    PassThroughReducer,
    dispatch,
    reset_dispatcher_caches,
    set_default_reducer,
)
from meho_backplane.operations._audit import policy_decision_var
from meho_backplane.operations._handler_resolve import reset_handler_cache
from meho_backplane.operations._preview import PreviewContext, build_proposed_effect
from meho_backplane.redaction.flight_recorder import classify_body_exclusion
from tests._vcfa_provisioning_support import (
    BASE_URL,
    MINTED_TOKEN,
    OPERATOR,
    ORG,
    PASSWORD,
    TARGET_NAME,
    TENANT_ID,
    USER_JWT,
    basic_user,
    by_filter,
    mount_logins,
    page,
    resolved_target,
    run,
    seed_target,
    wire_connector,
)

_TOKENS = "/cloudapi/1.0.0/tokens"
_CLIENT_ID = "4c1e0a52-0000-4000-8000-00000000c11d"
_TOKEN_URN = f"urn:vcloud:token:{_CLIENT_ID}"
_TOKEN_SEG = "urn%3Avcloud%3Atoken%3A4c1e0a52-0000-4000-8000-00000000c11d"

_VAULT_TARGET = "example-vault-writer"
_CREATE_PARAMS = {
    "org": "example-org",
    "username": "org-admin",
    "token_name": "meho-tenant",
    "password_secret_ref": "example/vcfa-org-admin",
    "store_secret_ref": "example/vcfa-target",
    "vault_target": _VAULT_TARGET,
}
_REVOKE_PARAMS = {
    k: v for k, v in _CREATE_PARAMS.items() if k not in ("store_secret_ref", "vault_target")
}


async def _seed_vault_target() -> None:
    """A Vault-connector target whose (per-target) Vault role performs the write."""
    async with get_sessionmaker()() as session:
        session.add(
            Target(
                tenant_id=TENANT_ID,
                name=_VAULT_TARGET,
                aliases=[],
                product="vault",
                host="vault.test.invalid",
                port=8200,
                auth_model="shared_service_account",
                vpn_required=False,
                extras={"vault_role": "example-writer-role"},
                notes="seeded by test_connectors_vcf_automation_api_token.py",
            )
        )
        await session.commit()


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from meho_backplane.settings import get_settings

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    captured: list[Any] = []

    async def _capture(event: Any) -> None:
        captured.append(event)

    monkeypatch.setattr("meho_backplane.operations._audit.publish_event", _capture)
    return captured


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch, events: list[Any]) -> Iterator[None]:
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )

    async def _load(_target: Any, _operator: Any, *, mount: str = "secret") -> dict[str, Any]:
        return {"password": PASSWORD}

    monkeypatch.setattr(lookups_module, "load_vault_secret_data", _load)
    reset_dispatcher_caches()
    reset_handler_cache()
    yield
    reset_dispatcher_caches()
    reset_handler_cache()


@pytest.fixture
def vault_writes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Stub the governed KV handlers the mint writes through."""
    writes: list[tuple[str, dict[str, Any]]] = []

    async def _patch(_operator: Any, target: Any, params: dict[str, Any]) -> dict[str, Any]:
        # Dispatched on the named Vault target -> its Vault role, not the caller's.
        assert (target.name, target.product) == (_VAULT_TARGET, "vault")
        writes.append(("patch", params))
        return {"version": 4}

    async def _put(_operator: Any, target: Any, params: dict[str, Any]) -> dict[str, Any]:
        assert (target.name, target.product) == (_VAULT_TARGET, "vault")
        writes.append(("put", params))
        return {"version": 1}

    monkeypatch.setattr(vault_ops_module, "vault_kv_patch", _patch)
    monkeypatch.setattr(vault_ops_module, "vault_kv_put", _put)
    return writes


@pytest.fixture
async def vcfa() -> AsyncIterator[VcfAutomationConnector]:
    set_default_reducer(PassThroughReducer())
    await VcfAutomationConnector.register_typed_operations()
    await register_vault_typed_operations()  # the policy gate reads vault.kv.patch/put
    await seed_target()
    await _seed_vault_target()
    connector = wire_connector()
    yield connector
    await connector.aclose()


def _mount_user_flow(
    mock: respx.MockRouter, *, context: str = "tenant/example-org", tokens: list[Any] | None = None
) -> dict[str, respx.Route]:
    session_path = (
        "/cloudapi/1.0.0/sessions/provider" if context == "provider" else "/cloudapi/1.0.0/sessions"
    )
    return {
        "session": mock.post(session_path).respond(
            200,
            headers={
                "X-VMWARE-VCLOUD-ACCESS-TOKEN": USER_JWT,
                "Set-Cookie": "vcloud_session_id=user-cookie-sentinel; Path=/",
            },
        ),
        "tokens": mock.get(_TOKENS).respond(200, json=page(tokens or [])),
        "register": mock.post(f"/oauth/{context}/register").respond(
            200, json={"client_id": _CLIENT_ID, "client_name": "meho-tenant"}
        ),
        "grant": mock.post(f"/oauth/{context}/token").respond(
            200,
            json={
                "access_token": "access-sentinel",
                "refresh_token": MINTED_TOKEN,
                "token_type": "Bearer",
                "expires_in": 3600,
            },
        ),
        "delete": mock.delete(f"{_TOKENS}/{_TOKEN_SEG}").respond(204),
        "logout": mock.delete("/cloudapi/1.0.0/sessions/current").respond(204),
    }


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_create_mints_as_the_user_and_stores_in_vault(
    vcfa: VcfAutomationConnector, vault_writes: list[tuple[str, dict[str, Any]]]
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        routes = _mount_user_flow(mock)
        result = await run("vcfa.provider.api_token.create", _CREATE_PARAMS)
    assert result["status"] == "ok", json.dumps(result)
    out = result["result"]
    assert out["status"] == "created"
    assert out["client_id"] == _CLIENT_ID
    assert out["stored"] == {
        "vault_target": _VAULT_TARGET,
        "mount": "secret",
        "secret_ref": "example/vcfa-target",
        "field": "refresh_token",
        "version": 4,
        "value_sha256": hashlib.sha256(MINTED_TOKEN.encode()).hexdigest(),
        "length": len(MINTED_TOKEN),
    }
    # The session is the org user's own (Basic user@org), not the target's.
    assert basic_user(routes["session"].calls.last.request) == "org-admin@example-org"
    register = routes["register"].calls.last.request
    assert register.headers["Authorization"] == f"Bearer {USER_JWT}"
    assert json.loads(register.content) == {"client_name": "meho-tenant"}
    form = parse_qs(routes["grant"].calls.last.request.content.decode())
    assert form == {
        "grant_type": ["urn:ietf:params:oauth:grant-type:jwt-bearer"],
        "assertion": [USER_JWT],
        "client_id": [_CLIENT_ID],
    }
    # The grant carries the session JWT as Bearer too (the proven live shape).
    assert routes["grant"].calls.last.request.headers["Authorization"] == f"Bearer {USER_JWT}"
    token_filter = routes["tokens"].calls.last.request.url.params["filter"]
    assert token_filter == "(name==meho-tenant;(type==PROXY,type==REFRESH))"
    # The user session is logged out and never touched the pooled client's jar.
    assert routes["logout"].called
    assert routes["logout"].calls.last.request.headers["Authorization"] == f"Bearer {USER_JWT}"
    pooled = await vcfa._http_client(await resolved_target())
    assert "user-cookie-sentinel" not in str(pooled.cookies.jar)
    assert vault_writes == [
        (
            "patch",
            {
                "mount": "secret",
                "path": "example/vcfa-target",
                "data": {"refresh_token": MINTED_TOKEN},
            },
        )
    ]
    assert not routes["delete"].called


async def test_token_never_reaches_result_audit_or_broadcast(
    vcfa: VcfAutomationConnector,
    vault_writes: list[tuple[str, dict[str, Any]]],
    events: list[Any],
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        _mount_user_flow(mock)
        result = await run("vcfa.provider.api_token.create", _CREATE_PARAMS)
    assert result["result"]["status"] == "created"
    for secret in (MINTED_TOKEN, PASSWORD, USER_JWT, "access-sentinel"):
        assert secret not in json.dumps(result)
    # Durable audit row: params hash + status only, no token.
    async with get_sessionmaker()() as session:
        rows = [
            row
            for row in (await session.execute(select(AuditLog))).scalars().all()
            if row.payload.get("op_id") == "vcfa.provider.api_token.create"
        ]
    assert rows, "the approved dispatch must write an audit row"
    for row in rows:
        assert MINTED_TOKEN not in json.dumps(row.payload, default=str)
    # Broadcast: credential_mint collapses to aggregate-only.
    assert classify_op("vcfa.provider.api_token.create") == "credential_mint"
    mint_events = [e for e in events if e.op_id == "vcfa.provider.api_token.create"]
    assert mint_events
    for event in mint_events:
        assert event.payload == {"op_class": "credential_mint", "result_status": "ok"}
        assert MINTED_TOKEN not in event.model_dump_json()


@pytest.mark.parametrize(
    ("op_id", "op_class"),
    [
        ("vcfa.provider.api_token.create", "credential_mint"),
        ("vcfa.provider.api_token.revoke", "credential_write"),
        ("vcfa.provider.user.create", "credential_write"),
        ("vcfa.tenant.login.test", None),
    ],
)
def test_flight_recorder_never_records_secret_bearing_bodies(
    op_id: str, op_class: str | None
) -> None:
    if op_class is not None:
        assert classify_op(op_id) == op_class
    exclusion = classify_body_exclusion(op_id)
    assert exclusion.excluded
    assert exclusion.family == "secret-bearing"


async def test_create_system_org_uses_the_provider_endpoints(
    vcfa: VcfAutomationConnector, vault_writes: list[tuple[str, dict[str, Any]]]
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        routes = _mount_user_flow(mock, context="provider")
        result = await run(
            "vcfa.provider.api_token.create",
            {**_CREATE_PARAMS, "org": "System", "username": "admin"},
        )
    assert result["result"]["status"] == "created", result
    assert basic_user(routes["session"].calls.last.request) == "admin@System"
    assert routes["grant"].called


async def test_create_unchanged_when_token_name_exists(
    vcfa: VcfAutomationConnector, vault_writes: list[tuple[str, dict[str, Any]]]
) -> None:
    existing = {
        "id": _TOKEN_URN,
        "name": "meho-tenant",
        "type": "REFRESH",
        "owner": {"name": "org-admin"},
    }
    someone_elses = {**existing, "id": "urn:vcloud:token:other", "owner": {"name": "other"}}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        routes = _mount_user_flow(mock, tokens=[someone_elses, existing])
        result = await run("vcfa.provider.api_token.create", _CREATE_PARAMS)
    out = result["result"]
    assert out["status"] == "unchanged"
    assert out["client_id"] == _CLIENT_ID
    assert not routes["register"].called
    assert vault_writes == []


async def test_create_falls_back_to_put_when_the_vault_path_is_new(
    vcfa: VcfAutomationConnector,
    vault_writes: list[tuple[str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _missing(_operator: Any, _target: Any, _params: dict[str, Any]) -> dict[str, Any]:
        raise hvac.exceptions.InvalidPath("no secret at path")

    monkeypatch.setattr(vault_ops_module, "vault_kv_patch", _missing)
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        _mount_user_flow(mock)
        result = await run(
            "vcfa.provider.api_token.create", {**_CREATE_PARAMS, "store_field": "api_token"}
        )
    assert result["result"]["stored"]["version"] == 1
    assert vault_writes == [
        (
            "put",
            {"mount": "secret", "path": "example/vcfa-target", "data": {"api_token": MINTED_TOKEN}},
        )
    ]


async def test_create_revokes_the_token_when_vault_write_fails(
    vcfa: VcfAutomationConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _denied(_operator: Any, _target: Any, _params: dict[str, Any]) -> dict[str, Any]:
        raise hvac.exceptions.Forbidden("permission denied")

    monkeypatch.setattr(vault_ops_module, "vault_kv_patch", _denied)
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        routes = _mount_user_flow(mock)
        result = await run("vcfa.provider.api_token.create", _CREATE_PARAMS)
    assert result["status"] == "error"
    assert result["extras"]["error_code"] == "connector_error"
    assert routes["delete"].called
    assert routes["delete"].calls.last.request.headers["Authorization"] == f"Bearer {USER_JWT}"
    assert "was revoked" in json.dumps(result)
    assert f"through Vault target '{_VAULT_TARGET}'" in json.dumps(result)
    assert MINTED_TOKEN not in json.dumps(result)
    assert routes["logout"].called


async def test_create_writes_under_the_vault_targets_role(
    vcfa: VcfAutomationConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unstubbed KV handler logs in under the named target's Vault role."""
    roles: list[str | None] = []

    class _Client:
        class secrets:  # noqa: N801 -- hvac client shape
            class kv:  # noqa: N801
                class v2:  # noqa: N801
                    @staticmethod
                    def patch(**_kw: Any) -> dict[str, Any]:
                        return {"data": {"version": 7}}

    class _Ctx:
        async def __aenter__(self) -> Any:
            return _Client()

        async def __aexit__(self, *_exc: object) -> None:
            return None

    def _client_for_operator(_operator: Any, *, role: Any = None, mount_path: Any = None) -> Any:
        roles.append(role)
        return _Ctx()

    import meho_backplane.auth.vault as auth_vault_module

    monkeypatch.setattr(auth_vault_module, "vault_client_for_operator", _client_for_operator)
    # The tenant-scope guard is the handler's own (tested with the Vault ops).
    monkeypatch.setattr(vault_ops_module, "enforce_tenant_scope", lambda *_a, **_k: None)
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        _mount_user_flow(mock)
        result = await run("vcfa.provider.api_token.create", _CREATE_PARAMS)
    assert result["status"] == "ok", json.dumps(result)
    assert result["result"]["status"] == "created", result
    assert result["result"]["stored"]["version"] == 7
    assert roles == ["example-writer-role"]


@pytest.mark.parametrize(
    ("vault_target", "reason"),
    [
        ("no-such-vault", "no such target"),
        (TARGET_NAME, "not a Vault-connector target"),
        ("   ", "vault_target is required"),
    ],
)
async def test_create_bad_vault_target_is_invalid_request_before_the_appliance(
    vcfa: VcfAutomationConnector,
    vault_writes: list[tuple[str, dict[str, Any]]],
    vault_target: str,
    reason: str,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        routes = _mount_user_flow(mock)
        result = await run(
            "vcfa.provider.api_token.create", {**_CREATE_PARAMS, "vault_target": vault_target}
        )
    assert result["status"] == "ok", json.dumps(result)
    assert result["result"]["status"] == "invalid_request"
    assert reason in result["result"]["guidance"]
    assert not any(route.called for route in routes.values())
    assert vault_writes == []


async def test_create_without_vault_target_is_refused_before_the_appliance(
    vcfa: VcfAutomationConnector, vault_writes: list[tuple[str, dict[str, Any]]]
) -> None:
    from meho_backplane.connectors.vcf_automation import _api_token

    params = {k: v for k, v in _CREATE_PARAMS.items() if k != "vault_target"}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        routes = _mount_user_flow(mock)
        dispatched = await run("vcfa.provider.api_token.create", params)
        direct = await _api_token.provider_api_token_create(
            vcfa, OPERATOR, await resolved_target(), params
        )
    assert dispatched["status"] != "ok"  # schema-required
    assert direct["status"] == "invalid_request"
    assert "vault_target is required" in direct["guidance"]
    assert not any(route.called for route in routes.values())
    assert vault_writes == []


async def test_vault_target_resolution_keeps_the_audit_target_binding() -> None:
    import structlog

    from meho_backplane.connectors.vcf_automation._api_token import resolve_store_vault_target

    await seed_target()
    await _seed_vault_target()
    structlog.contextvars.bind_contextvars(target_id="vcfa-row", target_name=TARGET_NAME)
    try:
        target, problem = await resolve_store_vault_target(OPERATOR, _VAULT_TARGET)
        bound = structlog.contextvars.get_contextvars()
    finally:
        structlog.contextvars.unbind_contextvars("target_id", "target_name")
    assert problem is None and target.name == _VAULT_TARGET
    assert (bound["target_id"], bound["target_name"]) == ("vcfa-row", TARGET_NAME)


# ---------------------------------------------------------------------------
# Policy gate for the Vault write on the named target (#3895 review M1)
# ---------------------------------------------------------------------------


async def _vault_target_id() -> Any:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(select(Target).where(Target.name == _VAULT_TARGET))
        ).scalar_one()
    return row.id


def _principal(kind: PrincipalKind, sub: str) -> Operator:
    return Operator(
        sub=sub,
        name=sub,
        email=None,
        raw_jwt="<vcfa-prov-raw-jwt>",
        tenant_id=TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
        client_id=sub if kind is PrincipalKind.AGENT else None,
    )


async def _dispatch_as(operator: Operator, *, approved: bool) -> dict[str, Any]:
    result = await dispatch(
        operator=operator,
        connector_id=VCFA_CONNECTOR_ID,
        op_id="vcfa.provider.api_token.create",
        target=await resolved_target(),
        params=_CREATE_PARAMS,
        _approved=approved,
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped


async def _seed_service_grant(sub: str, op_id: str, connector_id: str, target_id: Any) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            ServicePrincipalGrant(
                tenant_id=TENANT_ID,
                principal_sub=sub,
                op_id=op_id,
                connector_id=connector_id,
                target_id=target_id,
                reason="unattended tenant bootstrap",
                created_by_sub="op-admin",
            )
        )
        await session.commit()


async def _seed_service_op_grant(sub: str) -> None:
    """A standing grant for the vcfa op itself: the dispatch auto-executes."""
    await _seed_service_grant(
        sub,
        "vcfa.provider.api_token.create",
        VCFA_CONNECTOR_ID,
        (await resolved_target()).id,
    )


async def test_service_grant_on_the_op_alone_does_not_confer_the_vault_write(
    vcfa: VcfAutomationConnector, vault_writes: list[tuple[str, dict[str, Any]]]
) -> None:
    sub = "svc-bootstrap-no-vault-grant"
    await _seed_service_op_grant(sub)
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        routes = _mount_user_flow(mock)
        result = await _dispatch_as(_principal(PrincipalKind.SERVICE, sub), approved=False)
    assert result["status"] == "ok", result  # auto-executed by the standing grant
    out = result["result"]
    assert out["status"] == "invalid_request"
    assert out["guidance"].startswith("policy_denied: vault.kv.patch")
    assert f"Vault target '{_VAULT_TARGET}'" in out["guidance"]
    assert "needs approval" in out["guidance"]
    assert not any(route.called for route in routes.values())  # nothing minted
    assert vault_writes == []


async def test_service_grant_covering_the_vault_write_proceeds(
    vcfa: VcfAutomationConnector, vault_writes: list[tuple[str, dict[str, Any]]]
) -> None:
    sub = "svc-bootstrap-with-vault-grant"
    await _seed_service_op_grant(sub)
    vault_id = await _vault_target_id()
    for op_id in ("vault.kv.patch", "vault.kv.put"):
        await _seed_service_grant(sub, op_id, "vault-1.x", vault_id)
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        routes = _mount_user_flow(mock)
        result = await _dispatch_as(_principal(PrincipalKind.SERVICE, sub), approved=False)
    assert result["result"]["status"] == "created", result
    assert routes["grant"].called
    assert [kind for kind, _ in vault_writes] == ["patch"]


async def test_agent_denied_vault_write_is_refused_before_mint(
    vcfa: VcfAutomationConnector, vault_writes: list[tuple[str, dict[str, Any]]]
) -> None:
    sub = "agent:tenant-bootstrap"
    async with get_sessionmaker()() as session:
        session.add(
            AgentPermission(
                tenant_id=TENANT_ID,
                principal_sub=sub,
                op_pattern="vault.kv.patch",
                target_scope=str(await _vault_target_id()),
                verdict="deny",
                created_by_sub="op-admin",
            )
        )
        await session.commit()
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        routes = _mount_user_flow(mock)
        # Even on the approved re-dispatch of this op, a deny on the write refuses.
        result = await _dispatch_as(_principal(PrincipalKind.AGENT, sub), approved=True)
    out = result["result"]
    assert out["status"] == "invalid_request", result
    assert out["guidance"].startswith("policy_denied: vault.kv.patch")
    assert _VAULT_TARGET in out["guidance"]
    assert not any(route.called for route in routes.values())
    assert vault_writes == []


async def test_agent_needs_approval_on_the_write_is_satisfied_by_the_ops_park(
    vcfa: VcfAutomationConnector, vault_writes: list[tuple[str, dict[str, Any]]]
) -> None:
    agent = _principal(PrincipalKind.AGENT, "agent:tenant-bootstrap-default")
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        routes = _mount_user_flow(mock)
        result = await _dispatch_as(agent, approved=True)
    assert result["result"]["status"] == "created", result
    assert routes["grant"].called


async def test_handler_outside_an_approved_dispatch_is_refused(
    vcfa: VcfAutomationConnector, vault_writes: list[tuple[str, dict[str, Any]]]
) -> None:
    from meho_backplane.connectors.vcf_automation import _api_token

    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        routes = _mount_user_flow(mock)
        out = await _api_token.provider_api_token_create(
            vcfa, OPERATOR, await resolved_target(), _CREATE_PARAMS
        )
    assert out["status"] == "invalid_request"
    assert "needs approval" in out["guidance"]
    assert not any(route.called for route in routes.values())


async def test_create_names_the_recovery_when_store_and_revoke_both_fail(
    vcfa: VcfAutomationConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _denied(_operator: Any, _target: Any, _params: dict[str, Any]) -> dict[str, Any]:
        raise hvac.exceptions.Forbidden("permission denied")

    monkeypatch.setattr(vault_ops_module, "vault_kv_patch", _denied)
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        routes = _mount_user_flow(mock)
        routes["delete"].respond(500)
        result = await run("vcfa.provider.api_token.create", _CREATE_PARAMS)
    dumped = json.dumps(result)
    assert result["status"] == "error"
    assert "could NOT be revoked" in dumped
    assert "vcfa.provider.api_token.revoke" in dumped
    assert _CLIENT_ID in dumped  # the bare client id survives boundary redaction
    assert MINTED_TOKEN not in dumped


async def test_create_revokes_the_client_when_the_grant_fails(
    vcfa: VcfAutomationConnector, vault_writes: list[tuple[str, dict[str, Any]]]
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        routes = _mount_user_flow(mock)
        routes["grant"].respond(500, json={"message": "grant exploded"})
        result = await run("vcfa.provider.api_token.create", _CREATE_PARAMS)
    assert result["status"] == "error"
    assert result["extras"]["http_status"] == 500
    assert routes["delete"].called  # no orphaned client for a re-run to call 'unchanged'
    assert vault_writes == []
    assert routes["logout"].called


async def test_cancellation_between_mint_and_store_still_revokes(
    vcfa: VcfAutomationConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from meho_backplane.connectors.vcf_automation import _api_token

    async def _cancelled(_operator: Any, _target: Any, _params: dict[str, Any]) -> dict[str, Any]:
        raise asyncio.CancelledError

    monkeypatch.setattr(vault_ops_module, "vault_kv_patch", _cancelled)
    connector = vcfa
    target = await resolved_target()
    token = policy_decision_var.set("needs-approval")  # the approved re-dispatch
    try:
        with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
            routes = _mount_user_flow(mock)
            with pytest.raises(asyncio.CancelledError):
                await _api_token.provider_api_token_create(
                    connector, OPERATOR, target, _CREATE_PARAMS
                )
    finally:
        policy_decision_var.reset(token)
    assert routes["delete"].called
    assert routes["logout"].called


async def test_create_user_login_401_is_connector_auth_failed(
    vcfa: VcfAutomationConnector, vault_writes: list[tuple[str, dict[str, Any]]]
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mock.post("/cloudapi/1.0.0/sessions").respond(401)
        register = mock.post("/oauth/tenant/example-org/register").respond(200, json={})
        result = await run("vcfa.provider.api_token.create", _CREATE_PARAMS)
    assert result["status"] == "error"
    assert result["extras"]["error_code"] == "connector_auth_failed"
    assert "example/vcfa-org-admin" in json.dumps(result)  # remediation names the password ref
    assert PASSWORD not in json.dumps(result)
    assert not register.called


async def test_create_register_403_is_an_upstream_error(
    vcfa: VcfAutomationConnector, vault_writes: list[tuple[str, dict[str, Any]]]
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        routes = _mount_user_flow(mock)
        routes["register"].respond(403, json={"message": "Missing right API Tokens: Manage"})
        result = await run("vcfa.provider.api_token.create", _CREATE_PARAMS)
    # An upstream 403 is the dispatcher's authorization-denied envelope.
    assert result["extras"]["error_code"] == "connector_http_403"
    assert "API Tokens: Manage" in result["extras"]["upstream_message"]
    assert vault_writes == []


async def test_create_missing_password_is_invalid_request(
    vcfa: VcfAutomationConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _empty(_target: Any, _operator: Any, *, mount: str = "secret") -> dict[str, Any]:
        return {"username": "org-admin"}

    monkeypatch.setattr(lookups_module, "load_vault_secret_data", _empty)
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        session = mock.post("/cloudapi/1.0.0/sessions").respond(200)
        result = await run("vcfa.provider.api_token.create", _CREATE_PARAMS)
    assert result["result"]["status"] == "invalid_request"
    assert not session.called


# ---------------------------------------------------------------------------
# Revoke
# ---------------------------------------------------------------------------


async def test_revoke_deletes_the_named_token(vcfa: VcfAutomationConnector) -> None:
    existing = {"id": _TOKEN_URN, "name": "meho-tenant", "type": "REFRESH"}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        routes = _mount_user_flow(mock, tokens=[existing])
        result = await run("vcfa.provider.api_token.revoke", _REVOKE_PARAMS)
    assert routes["logout"].called
    out = result["result"]
    assert out["status"] == "revoked", result
    assert out["client_id"] == _CLIENT_ID
    assert routes["delete"].called


async def test_revoke_unknown_token_is_unchanged(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        routes = _mount_user_flow(mock, tokens=[])
        result = await run("vcfa.provider.api_token.revoke", _REVOKE_PARAMS)
    assert result["result"]["status"] == "unchanged"
    assert not routes["delete"].called


# ---------------------------------------------------------------------------
# Approval park + previews
# ---------------------------------------------------------------------------


async def test_unapproved_dispatch_parks_without_touching_the_appliance(
    vcfa: VcfAutomationConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    _capabilities(monkeypatch, ["create", "update"])
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        routes = _mount_user_flow(mock)
        result = await run("vcfa.provider.api_token.create", _CREATE_PARAMS, approved=False)
    assert result["status"] != "ok"
    assert "approval" in json.dumps(result).lower()
    assert not any(route.called for route in routes.values())


async def _preview(op_id: str, params: dict[str, Any], connector: Any) -> dict[str, Any] | None:
    from meho_backplane.db.models import EndpointDescriptor

    async with get_sessionmaker()() as session:
        descriptor = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == op_id)
            )
        ).scalar_one()
    return await build_proposed_effect(
        PreviewContext(
            descriptor=descriptor,
            connector_instance=connector,
            operator=OPERATOR,
            target=await resolved_target(),
            params=params,
            connector_id="vcfa-rest-9.0",
        )
    )


def _capabilities(
    monkeypatch: pytest.MonkeyPatch, granted: list[str]
) -> list[tuple[Any, list[str]]]:
    """Stub the Vault capability probe; record (target, paths) per probe."""
    probes: list[tuple[Any, list[str]]] = []

    class _Sys:
        def __init__(self, target: Any) -> None:
            self._target = target

        def get_capabilities(self, *, paths: list[str]) -> dict[str, Any]:
            probes.append((self._target, paths))
            return {paths[0]: granted}

    class _Ctx:
        def __init__(self, target: Any) -> None:
            self._target = target

        async def __aenter__(self) -> Any:
            client = type("_Client", (), {})()
            client.sys = _Sys(self._target)
            return client

        async def __aexit__(self, *_exc: object) -> None:
            return None

    monkeypatch.setattr(
        vault_ops_module, "vault_client_for_target", lambda _operator, target: _Ctx(target)
    )
    return probes


async def _parked_effect() -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(ApprovalRequest).where(
                        ApprovalRequest.op_id == "vcfa.provider.api_token.create"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    return dict(rows[0].proposed_effect)


async def test_park_card_carries_the_vault_write_preflight(
    vcfa: VcfAutomationConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    probes = _capabilities(monkeypatch, ["read"])
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        routes = _mount_user_flow(mock)
        await run("vcfa.provider.api_token.create", _CREATE_PARAMS, approved=False)
    assert not any(route.called for route in routes.values())
    effect = await _parked_effect()
    preflight = effect["permission_preflight"]
    assert preflight["vault_target"] == _VAULT_TARGET
    assert preflight["path"] == "secret/data/example/vcfa-target"
    assert preflight["required"] == ["create", "update"]
    assert preflight["will_be_denied"] is True
    assert effect["write_capability_warning"] == "connector_identity_may_lack_write"
    # Probed under the named Vault target (its role), not the caller's identity.
    assert [(t.name, paths) for t, paths in probes] == [
        (_VAULT_TARGET, ["secret/data/example/vcfa-target"])
    ]
    store = effect["preview"]["store"]
    assert store["vault_target"] == _VAULT_TARGET
    for secret in (MINTED_TOKEN, PASSWORD, USER_JWT):
        assert secret not in json.dumps(effect)


async def test_park_card_preflight_passes_when_the_target_role_may_write(
    vcfa: VcfAutomationConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    _capabilities(monkeypatch, ["create", "read", "update"])
    await run("vcfa.provider.api_token.create", _CREATE_PARAMS, approved=False)
    effect = await _parked_effect()
    assert effect["permission_preflight"]["will_be_denied"] is False
    assert "write_capability_warning" not in effect


async def test_park_card_reports_a_failed_probe(
    vcfa: VcfAutomationConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _RoleRefusedError(Exception):
        pass

    class _Ctx:
        async def __aenter__(self) -> Any:
            raise _RoleRefusedError("role login refused")

        async def __aexit__(self, *_exc: object) -> None:
            return None

    monkeypatch.setattr(vault_ops_module, "vault_client_for_target", lambda _o, _t: _Ctx())
    await run("vcfa.provider.api_token.create", _CREATE_PARAMS, approved=False)
    effect = await _parked_effect()
    preflight = effect["permission_preflight"]
    assert preflight["will_be_denied"] is True
    assert preflight["reason"] == "probe_failed:_RoleRefusedError"
    assert preflight["vault_target"] == _VAULT_TARGET
    assert effect["write_capability_warning"] == "connector_identity_may_lack_write"


async def test_park_card_flags_a_non_vault_target(
    vcfa: VcfAutomationConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    probes = _capabilities(monkeypatch, ["create", "update"])
    await run(
        "vcfa.provider.api_token.create",
        {**_CREATE_PARAMS, "vault_target": TARGET_NAME},
        approved=False,
    )
    preflight = (await _parked_effect())["permission_preflight"]
    assert preflight["will_be_denied"] is True
    assert "not a Vault-connector target" in preflight["reason"]
    assert probes == []


async def test_token_previews_echo_refs_only(vcfa: VcfAutomationConnector) -> None:
    effect = await _preview("vcfa.provider.api_token.create", _CREATE_PARAMS, None)
    assert effect is not None
    dumped = json.dumps(effect)
    assert "mint_api_token" in dumped
    assert "example/vcfa-target" in dumped
    assert _VAULT_TARGET in dumped
    assert '"token_value_returned": false' in dumped
    assert PASSWORD not in dumped
    revoke = await _preview("vcfa.provider.api_token.revoke", _REVOKE_PARAMS, None)
    assert revoke is not None and "revoke_api_token" in json.dumps(revoke)


async def test_org_create_preview_reads_existence_at_park_time(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get("/cloudapi/1.0.0/orgs").mock(side_effect=by_filter({"name==example-org": [ORG]}))
        post = mock.post("/cloudapi/1.0.0/orgs").respond(201, json=ORG)
        parked = await _preview("vcfa.provider.org.create", {"name": "example-org"}, vcfa)
        absent = await _preview("vcfa.provider.org.create", {"name": "other-org"}, vcfa)
    egress_free = await _preview("vcfa.provider.org.create", {"name": "other-org"}, None)
    assert json.dumps(parked).count('"would": "unchanged"') == 1
    assert json.dumps(absent).count('"would": "create"') == 1
    assert json.dumps(egress_free).count('"would": "create_if_absent"') == 1
    assert not post.called


async def test_user_create_preview_names_the_password_ref_not_the_password(
    vcfa: VcfAutomationConnector,
) -> None:
    params = {
        "org": "example-org",
        "username": "org-admin",
        "role": "Custom Org Admin",
        "password_secret_ref": "example/vcfa-org-admin",
    }
    effect = await _preview("vcfa.provider.user.create", params, None)
    dumped = json.dumps(effect)
    assert "example/vcfa-org-admin" in dumped
    assert PASSWORD not in dumped
