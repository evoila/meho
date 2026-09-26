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

from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors.vault import ops as vault_ops_module
from meho_backplane.connectors.vcf_automation import VcfAutomationConnector
from meho_backplane.connectors.vcf_automation import _lookups as lookups_module
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations import (
    PassThroughReducer,
    reset_dispatcher_caches,
    set_default_reducer,
)
from meho_backplane.operations._handler_resolve import reset_handler_cache
from meho_backplane.operations._preview import PreviewContext, build_proposed_effect
from meho_backplane.redaction.flight_recorder import classify_body_exclusion
from tests._vcfa_provisioning_support import (
    BASE_URL,
    MINTED_TOKEN,
    OPERATOR,
    ORG,
    PASSWORD,
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

_CREATE_PARAMS = {
    "org": "example-org",
    "username": "org-admin",
    "token_name": "meho-tenant",
    "password_secret_ref": "example/vcfa-org-admin",
    "store_secret_ref": "example/vcfa-target",
}
_REVOKE_PARAMS = {k: v for k, v in _CREATE_PARAMS.items() if k != "store_secret_ref"}


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
        assert target is None  # the deployment Vault, under the operator's identity
        writes.append(("patch", params))
        return {"version": 4}

    async def _put(_operator: Any, _target: Any, params: dict[str, Any]) -> dict[str, Any]:
        writes.append(("put", params))
        return {"version": 1}

    monkeypatch.setattr(vault_ops_module, "vault_kv_patch", _patch)
    monkeypatch.setattr(vault_ops_module, "vault_kv_put", _put)
    return writes


@pytest.fixture
async def vcfa() -> AsyncIterator[VcfAutomationConnector]:
    set_default_reducer(PassThroughReducer())
    await VcfAutomationConnector.register_typed_operations()
    await seed_target()
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
            200, headers={"X-VMWARE-VCLOUD-ACCESS-TOKEN": USER_JWT}
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
    assert result["status"] == "ok", result
    out = result["result"]
    assert out["status"] == "created"
    assert out["client_id"] == _CLIENT_ID
    assert out["stored"] == {
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
    token_filter = routes["tokens"].calls.last.request.url.params["filter"]
    assert token_filter == "(name==meho-tenant;owner.name==org-admin;(type==PROXY,type==REFRESH))"
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
    existing = {"id": _TOKEN_URN, "name": "meho-tenant", "type": "REFRESH"}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        routes = _mount_user_flow(mock, tokens=[existing])
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
    assert MINTED_TOKEN not in json.dumps(result)


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
    vcfa: VcfAutomationConnector,
) -> None:
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


async def test_token_previews_echo_refs_only(vcfa: VcfAutomationConnector) -> None:
    effect = await _preview("vcfa.provider.api_token.create", _CREATE_PARAMS, None)
    assert effect is not None
    dumped = json.dumps(effect)
    assert "mint_api_token" in dumped
    assert "example/vcfa-target" in dumped
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
