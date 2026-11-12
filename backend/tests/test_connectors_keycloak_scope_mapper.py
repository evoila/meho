# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Change a protocol mapper of a client scope: ``keycloak.client_scope.protocol_mapper.update``.

Drives the ops through the real dispatch stack against a respx-mocked
Keycloak Admin REST API. No running Keycloak and no Vault.

What is proven:

* the op registers on the ``caution`` risk level, needs approval and sits in
  the ``protocol_mapper_write`` group;
* the mapper is read, the requested fields are changed, and every other
  field (also the other ``config`` keys) is written back as it was;
* names are looked up to ids; ids are used as they are;
* a name that matches nothing is an error and nothing is written; a name
  that matches two is an error too;
* a vendor error is an error, not a success;
* the approval gate holds, and the audit row names the scope and the mapper.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.ops_write import WRITE_OPS
from meho_backplane.connectors.keycloak.session import (
    KeycloakAdminCredentials,
    KeycloakClientCredentials,
    KeycloakTargetLike,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import resolve_target

_CONNECTOR_ID = "keycloak-admin-26.x"
_TARGET_NAME = "kc-mapper-e2e"
_KC_HOST = "keycloak-mapper-e2e.test.invalid"
_KC_BASE_URL = f"https://{_KC_HOST}"
_REALM = "meho"
_ADMIN_REALM = "master"
_SCOPE_UUID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_MAPPER_UUID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
_OTHER_MAPPER_UUID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
_SCOPE_NAME = "docs-capabilities"
_MAPPER_NAME = "capabilities"
_SCOPE_PATH = f"/admin/realms/{_REALM}/client-scopes/{_SCOPE_UUID}"
_MAPPERS_PATH = f"{_SCOPE_PATH}/protocol-mappers/models"
_MAPPER_PATH = f"{_MAPPERS_PATH}/{_MAPPER_UUID}"
_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000005930")
_OP = "keycloak.client_scope.protocol_mapper.update"


def _mapper(value: str = '["meho-docs"]') -> dict[str, Any]:
    """A made-up hardcoded-claim mapper as Keycloak returns it."""
    return {
        "id": _MAPPER_UUID,
        "name": _MAPPER_NAME,
        "protocol": "openid-connect",
        "protocolMapper": "oidc-hardcoded-claim-mapper",
        "consentRequired": False,
        "config": {
            "claim.name": "capabilities",
            "claim.value": value,
            "jsonType.label": "JSON",
            "access.token.claim": "true",
        },
    }


_NEW_VALUE = '["meho-docs","meho-docs:team-a"]'
_CHANGE = {"config": {"claim.value": _NEW_VALUE}}


def _scope_rows() -> list[dict[str, Any]]:
    return [{"id": _SCOPE_UUID, "name": _SCOPE_NAME}, {"id": "x-other", "name": "other"}]


def _mapper_rows() -> list[dict[str, Any]]:
    return [_mapper(), {**_mapper(), "id": _OTHER_MAPPER_UUID, "name": "other-mapper"}]


def _operator(sub: str = "kc-mapper-requester", role: TenantRole = TenantRole.OPERATOR) -> Operator:
    return Operator(
        sub=sub,
        name="Keycloak Mapper Test Operator",
        email=None,
        raw_jwt="<kc-mapper-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=role,
        principal_kind=PrincipalKind.USER,
    )


def _creds_loader(_target: KeycloakTargetLike, _operator: Operator) -> Any:
    async def _load() -> KeycloakAdminCredentials:
        return KeycloakClientCredentials(client_id="meho-admin", client_secret="stub-secret")

    return _load()


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()


@pytest.fixture
async def kc() -> AsyncIterator[KeycloakConnector]:
    set_default_reducer(PassThroughReducer())
    await KeycloakConnector.register_operations()
    async with get_sessionmaker()() as session:
        session.add(
            TargetORM(
                tenant_id=_TENANT_ID,
                name=_TARGET_NAME,
                aliases=[],
                product="keycloak",
                host=_KC_HOST,
                port=443,
                fqdn=None,
                secret_ref="keycloak/admin",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={"managed_realm": _REALM, "admin_realm": _ADMIN_REALM},
                fingerprint={"version": "26.3.3"},
                notes="seeded by test_connectors_keycloak_user_enabled",
            )
        )
        await session.commit()
    connector = KeycloakConnector(credentials_loader=_creds_loader)
    _CONNECTOR_INSTANCE_CACHE[KeycloakConnector] = connector
    yield connector
    await connector.aclose()


async def _run(op_id: str, params: dict[str, Any]) -> dict[str, Any]:
    """Run the op as an already-approved call (the handler path)."""
    async with get_sessionmaker()() as session:
        target = await resolve_target(session, _TENANT_ID, _TARGET_NAME)
    result = await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=target,
        params=params,
        _approved=True,
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped


def _mount_token(mock: respx.MockRouter) -> None:
    mock.post(f"/realms/{_ADMIN_REALM}/protocol/openid-connect/token").respond(
        200, json={"access_token": "kc-admin-token", "expires_in": 300}
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def test_op_registers_caution_and_approval_gated(kc: KeycloakConnector) -> None:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(select(EndpointDescriptor).where(EndpointDescriptor.op_id == _OP))
        ).scalar_one()
    assert row.safety_level == "caution"
    assert row.requires_approval is True
    (op,) = [op for op in WRITE_OPS if op.op_id == _OP]
    assert op.group_key == "protocol_mapper_write"


@pytest.mark.parametrize(
    "params",
    [
        {"mapper_name": _MAPPER_NAME, "representation": _CHANGE},
        {"scope_name": _SCOPE_NAME, "representation": _CHANGE},
        {"scope_name": _SCOPE_NAME, "mapper_name": _MAPPER_NAME},
        {"scope_name": _SCOPE_NAME, "mapper_name": _MAPPER_NAME, "representation": {}},
        {"scope_name": _SCOPE_NAME, "mapper_name": _MAPPER_NAME, "representation": _CHANGE, "x": 1},
    ],
    ids=["no-scope", "no-mapper", "no-representation", "empty-representation", "extra-field"],
)
async def test_bad_params_are_invalid_params(kc: KeycloakConnector, params: dict[str, Any]) -> None:
    result = await _run(_OP, params)
    assert result["status"] != "ok"
    assert result["extras"]["error_code"] == "invalid_params"


async def test_changing_the_mapper_id_is_refused(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        put = mock.put(_MAPPER_PATH).respond(204)
        result = await _run(
            _OP,
            {
                "scope_id": _SCOPE_UUID,
                "mapper_id": _MAPPER_UUID,
                "representation": {"id": _OTHER_MAPPER_UUID},
            },
        )
    assert result["status"] != "ok"
    assert not put.called


# ---------------------------------------------------------------------------
# The handler
# ---------------------------------------------------------------------------


async def test_change_by_names_keeps_every_other_field(kc: KeycloakConnector) -> None:
    after = _mapper(_NEW_VALUE)
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        scopes = mock.get(f"/admin/realms/{_REALM}/client-scopes").respond(200, json=_scope_rows())
        mappers = mock.get(_MAPPERS_PATH).respond(200, json=_mapper_rows())
        mock.get(_MAPPER_PATH).mock(
            side_effect=[httpx.Response(200, json=_mapper()), httpx.Response(200, json=after)]
        )
        put = mock.put(_MAPPER_PATH).respond(204)
        result = await _run(
            _OP,
            {"scope_name": _SCOPE_NAME, "mapper_name": _MAPPER_NAME, "representation": _CHANGE},
        )
    assert result["status"] == "ok", result
    assert result["result"] == {
        "scope_id": _SCOPE_UUID,
        "scope_name": _SCOPE_NAME,
        "mapper_id": _MAPPER_UUID,
        "mapper_name": _MAPPER_NAME,
        "changed_fields": ["config"],
        "unrequested_changes": [],
        "read_back_checked": True,
    }
    assert scopes.call_count == 1
    assert mappers.call_count == 1
    assert put.call_count == 1
    # Only claim.value changed; the other config keys and fields are as read.
    assert json.loads(put.calls.last.request.content) == after
    assert _NEW_VALUE not in str(result)


async def test_change_by_ids_skips_the_lookups(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        scopes = mock.get(f"/admin/realms/{_REALM}/client-scopes").respond(200, json=[])
        mappers = mock.get(_MAPPERS_PATH).respond(200, json=[])
        mock.get(_MAPPER_PATH).mock(
            side_effect=[
                httpx.Response(200, json=_mapper()),
                httpx.Response(200, json=_mapper(_NEW_VALUE)),
            ]
        )
        mock.put(_MAPPER_PATH).respond(204)
        result = await _run(
            _OP,
            {"scope_id": _SCOPE_UUID, "mapper_id": _MAPPER_UUID, "representation": _CHANGE},
        )
    assert result["status"] == "ok", result
    assert result["result"]["scope_name"] is None
    assert result["result"]["mapper_name"] is None
    assert not scopes.called
    assert not mappers.called


async def test_a_top_level_field_changes_and_config_is_kept(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_MAPPER_PATH).mock(
            side_effect=[
                httpx.Response(200, json=_mapper()),
                httpx.Response(200, json={**_mapper(), "consentRequired": True}),
            ]
        )
        put = mock.put(_MAPPER_PATH).respond(204)
        result = await _run(
            _OP,
            {
                "scope_id": _SCOPE_UUID,
                "mapper_id": _MAPPER_UUID,
                "representation": {"consentRequired": True},
            },
        )
    assert result["status"] == "ok", result
    assert json.loads(put.calls.last.request.content) == {**_mapper(), "consentRequired": True}


async def test_same_change_twice_is_a_success_both_times(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_MAPPER_PATH).respond(200, json=_mapper(_NEW_VALUE))
        put = mock.put(_MAPPER_PATH).respond(204)
        params = {"scope_id": _SCOPE_UUID, "mapper_id": _MAPPER_UUID, "representation": _CHANGE}
        first = await _run(_OP, params)
        second = await _run(_OP, params)
    assert first["status"] == "ok", first
    assert second["status"] == "ok", second
    assert put.call_count == 2


async def test_unrequested_change_is_named_not_valued(kc: KeycloakConnector) -> None:
    after = {**_mapper(_NEW_VALUE), "protocol": "saml"}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_MAPPER_PATH).mock(
            side_effect=[httpx.Response(200, json=_mapper()), httpx.Response(200, json=after)]
        )
        mock.put(_MAPPER_PATH).respond(204)
        result = await _run(
            _OP,
            {"scope_id": _SCOPE_UUID, "mapper_id": _MAPPER_UUID, "representation": _CHANGE},
        )
    assert result["result"]["unrequested_changes"] == ["protocol"]
    assert "saml" not in str(result["result"])


async def test_a_failed_read_back_still_reports_the_write(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_MAPPER_PATH).mock(
            side_effect=[httpx.Response(200, json=_mapper()), httpx.Response(403, json={})]
        )
        mock.put(_MAPPER_PATH).respond(204)
        result = await _run(
            _OP,
            {"scope_id": _SCOPE_UUID, "mapper_id": _MAPPER_UUID, "representation": _CHANGE},
        )
    assert result["status"] == "ok", result
    assert result["result"]["read_back_checked"] is False


async def test_unknown_scope_name_is_not_found_and_nothing_is_written(
    kc: KeycloakConnector,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/client-scopes").respond(200, json=_scope_rows())
        put = mock.put(url__regex=r".*/protocol-mappers/.*").respond(204)
        result = await _run(
            _OP, {"scope_name": "nope", "mapper_name": _MAPPER_NAME, "representation": _CHANGE}
        )
    assert result["status"] != "ok"
    assert "KeycloakUserNotFoundError" in str(result)
    assert not put.called


async def test_unknown_mapper_name_is_not_found_and_nothing_is_written(
    kc: KeycloakConnector,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_MAPPERS_PATH).respond(200, json=_mapper_rows())
        put = mock.put(_MAPPER_PATH).respond(204)
        result = await _run(
            _OP, {"scope_id": _SCOPE_UUID, "mapper_name": "nope", "representation": _CHANGE}
        )
    assert result["status"] != "ok"
    assert "KeycloakUserNotFoundError" in str(result)
    assert not put.called


async def test_two_mappers_with_one_name_are_refused(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_MAPPERS_PATH).respond(
            200, json=[_mapper(), {**_mapper(), "id": _OTHER_MAPPER_UUID}]
        )
        put = mock.put(url__regex=r".*/protocol-mappers/.*").respond(204)
        result = await _run(
            _OP,
            {"scope_id": _SCOPE_UUID, "mapper_name": _MAPPER_NAME, "representation": _CHANGE},
        )
    assert result["status"] != "ok"
    assert not put.called


async def test_unreadable_scope_list_is_an_error_not_not_found(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/client-scopes").respond(500, json={"error": "boom"})
        put = mock.put(url__regex=r".*/protocol-mappers/.*").respond(204)
        result = await _run(
            _OP,
            {"scope_name": _SCOPE_NAME, "mapper_name": _MAPPER_NAME, "representation": _CHANGE},
        )
    assert result["status"] != "ok"
    assert "KeycloakUserNotFoundError" not in str(result)
    assert not put.called


async def test_unknown_mapper_id_is_not_found_and_nothing_is_written(
    kc: KeycloakConnector,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_MAPPER_PATH).respond(404, json={"error": "not found"})
        put = mock.put(_MAPPER_PATH).respond(204)
        result = await _run(
            _OP, {"scope_id": _SCOPE_UUID, "mapper_id": _MAPPER_UUID, "representation": _CHANGE}
        )
    assert result["status"] != "ok"
    assert "KeycloakUserNotFoundError" in str(result)
    assert not put.called


async def test_a_vendor_server_error_is_an_error_not_success(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_MAPPER_PATH).respond(200, json=_mapper())
        mock.put(_MAPPER_PATH).respond(500, json={"error": "boom"})
        result = await _run(
            _OP, {"scope_id": _SCOPE_UUID, "mapper_id": _MAPPER_UUID, "representation": _CHANGE}
        )
    assert result["status"] != "ok"


# ---------------------------------------------------------------------------
# The approval gate
# ---------------------------------------------------------------------------


async def test_change_waits_for_approval_then_runs_and_audit_names_both(
    kc: KeycloakConnector,
) -> None:
    requester = _operator("kc-mapper-requester")
    approver = _operator("kc-mapper-approver", TenantRole.TENANT_ADMIN)
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": _OP,
        "target": _TARGET_NAME,
        "params": {
            "scope_name": _SCOPE_NAME,
            "mapper_name": _MAPPER_NAME,
            "representation": _CHANGE,
        },
    }
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/client-scopes").respond(200, json=_scope_rows())
        mock.get(_MAPPERS_PATH).respond(200, json=_mapper_rows())
        mock.get(_MAPPER_PATH).respond(200, json=_mapper())
        put = mock.put(_MAPPER_PATH).respond(204)

        call = await call_operation(requester, args)
        assert call["status"] == "awaiting_approval", call
        assert not put.called  # nothing is written before approval
        request_id = uuid.UUID(call["extras"]["approval_request_id"])

        async with get_sessionmaker()() as session:
            approved = await approve_request(session, request_id, operator=approver, params=None)
            await session.commit()
        assert approved.status == "approved"

        async with get_sessionmaker()() as session:
            row = await session.get(ApprovalRequest, request_id)
            assert row is not None
            resumed = await resume_dispatch_after_approval(
                operator=approver, request=row, params=None
            )
    assert resumed.status == "ok", resumed
    assert put.call_count == 1
    assert json.loads(put.calls.last.request.content)["config"]["claim.value"] == _NEW_VALUE

    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    audit = json.dumps(
        [{c.name: getattr(r, c.name) for c in AuditLog.__table__.columns} for r in rows],
        default=str,
    )
    assert _SCOPE_NAME in audit
    assert _MAPPER_NAME in audit
