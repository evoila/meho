# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Change a protocol mapper of a client scope: ``keycloak.client_scope.protocol_mapper.update``.

Drives the ops through the real dispatch stack against a respx-mocked
Keycloak Admin REST API. No running Keycloak and no Vault.

What is proven:

* the op registers on the ``dangerous`` risk level, needs approval and sits in
  the ``protocol_mapper_write`` group;
* the mapper is read, the requested fields are changed, and every other
  field (also the other ``config`` keys) is written back as it was;
* names are looked up to ids; ids are read, and the result shows the
  names stored in Keycloak;
* an id and a name that do not belong together are refused before the
  wait for approval, and when the call runs;
* the approval card shows the ids and the stored names; the approved run
  acts on those ids and is refused when a name changed in between;
* a name that matches nothing is an error and nothing is written; a name
  that matches two is an error too;
* a vendor error is an error, not a success;
* the approval gate holds, and the audit row names the scope and the mapper;
* the op is a change of rights: no approval with a token counts, the
  approver is an admin in the web console, and no standing grant, run
  approval or agent grant clears the wait;
* the card shows the mapper as it is now (type, protocol, claim name, old
  values) next to the new values, and the approved run is refused when the
  mapper's content changed after approval;
* ``representation`` may change only ``name`` and ``config``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
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
from meho_backplane.db.models import (
    AgentPermission,
    ApprovalRequest,
    AuditLog,
    EndpointDescriptor,
    RunApproval,
    ServicePrincipalGrant,
    Tenant,
)
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.op_classes import needs_per_call_approval
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._audit import work_ref_var
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations.approval_queue import (
    RightsChangeApprovalError,
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.grant_approval import is_rights_change_op
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.operations.run_approval_schemas import RunApprovalCreate, RunApprovalEntry
from meho_backplane.operations.run_approvals import (
    RunApprovalService,
    RunApprovalValidationError,
)
from meho_backplane.operations.service_grant_schemas import ServiceGrantCreate
from meho_backplane.operations.service_grants import (
    GrantValidationError,
    ServicePrincipalGrantService,
)
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


def _mount_token(mock: respx.MockRouter) -> respx.Route:
    """Mount the admin login and the scope read; return the scope read."""
    mock.post(f"/realms/{_ADMIN_REALM}/protocol/openid-connect/token").respond(
        200, json={"access_token": "kc-admin-token", "expires_in": 300}
    )
    return mock.get(_SCOPE_PATH).respond(200, json={"id": _SCOPE_UUID, "name": _SCOPE_NAME})


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def test_op_registers_dangerous_and_approval_gated(kc: KeycloakConnector) -> None:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(select(EndpointDescriptor).where(EndpointDescriptor.op_id == _OP))
        ).scalar_one()
    assert row.safety_level == "dangerous"
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


async def test_change_by_ids_skips_the_lookups_and_shows_stored_names(
    kc: KeycloakConnector,
) -> None:
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
    assert result["result"]["scope_name"] == _SCOPE_NAME
    assert result["result"]["mapper_name"] == _MAPPER_NAME
    assert not scopes.called
    assert not mappers.called


async def test_a_rename_changes_the_name_and_keeps_config(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_MAPPER_PATH).mock(
            side_effect=[
                httpx.Response(200, json=_mapper()),
                httpx.Response(200, json={**_mapper(), "name": "capabilities-v2"}),
            ]
        )
        put = mock.put(_MAPPER_PATH).respond(204)
        result = await _run(
            _OP,
            {
                "scope_id": _SCOPE_UUID,
                "mapper_id": _MAPPER_UUID,
                "representation": {"name": "capabilities-v2"},
            },
        )
    assert result["status"] == "ok", result
    assert json.loads(put.calls.last.request.content) == {**_mapper(), "name": "capabilities-v2"}


@pytest.mark.parametrize(
    "representation",
    [{"protocolMapper": "oidc-usermodel-attribute-mapper"}, {"protocol": "saml"}],
    ids=["type", "protocol"],
)
async def test_a_change_of_type_or_protocol_is_refused(
    kc: KeycloakConnector, representation: dict[str, Any]
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_MAPPER_PATH).respond(200, json=_mapper())
        put = mock.put(_MAPPER_PATH).respond(204)
        result = await _run(
            _OP,
            {"scope_id": _SCOPE_UUID, "mapper_id": _MAPPER_UUID, "representation": representation},
        )
    assert result["status"] != "ok"
    assert result["extras"]["error_code"] == "invalid_params"
    assert not put.called


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
    approver = _operator("kc-mapper-approver", TenantRole.TENANT_ADMIN).model_copy(
        update={"web_session": True}
    )
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


async def _park(params: dict[str, Any]) -> dict[str, Any]:
    return await call_operation(
        _operator("kc-mapper-requester"),
        {"connector_id": _CONNECTOR_ID, "op_id": _OP, "target": _TARGET_NAME, "params": params},
    )


async def _card(request_id: uuid.UUID) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        pending = await session.get(ApprovalRequest, request_id)
        assert pending is not None
        preview: dict[str, Any] = pending.proposed_effect["preview"]
    return preview


async def _approve_and_resume(request_id: uuid.UUID) -> Any:
    approver = _operator("kc-mapper-approver", TenantRole.TENANT_ADMIN).model_copy(
        update={"web_session": True}
    )
    async with get_sessionmaker()() as session:
        await approve_request(session, request_id, operator=approver, params=None)
        await session.commit()
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        return await resume_dispatch_after_approval(operator=approver, request=row, params=None)


def _mount_all(mock: respx.MockRouter) -> respx.Route:
    """Mount every read and the write; return the write."""
    _mount_token(mock)
    mock.get(f"/admin/realms/{_REALM}/client-scopes").respond(200, json=_scope_rows())
    mock.get(_MAPPERS_PATH).respond(200, json=_mapper_rows())
    mock.get(_MAPPER_PATH).respond(200, json=_mapper())
    return mock.put(url__regex=r".*/protocol-mappers/.*").respond(204)


async def _stored_requests() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(ApprovalRequest))).scalars().all())


@pytest.mark.parametrize(
    ("ids", "message"),
    [
        (
            {"scope_id": _SCOPE_UUID, "scope_name": "other", "mapper_name": _MAPPER_NAME},
            "'scope_id' and 'scope_name' name different client scopes",
        ),
        (
            {"scope_name": _SCOPE_NAME, "mapper_id": _MAPPER_UUID, "mapper_name": "other-mapper"},
            "'mapper_id' and 'mapper_name' name different protocol mappers",
        ),
    ],
    ids=["scope", "mapper"],
)
async def test_id_and_name_of_different_objects_is_refused_before_approval(
    kc: KeycloakConnector, ids: dict[str, Any], message: str
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        put = _mount_all(mock)
        call = await _park({**ids, "representation": _CHANGE})
        result = await _run(_OP, {**ids, "representation": _CHANGE})
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "refused_before_approval"
    assert message in call["error"]
    assert await _stored_requests() == []
    # The same request is refused when it runs.
    assert result["status"] != "ok"
    assert message in str(result)
    assert not put.called


async def test_id_and_name_of_the_same_objects_waits_for_approval(kc: KeycloakConnector) -> None:
    params = {
        "scope_id": _SCOPE_UUID,
        "scope_name": _SCOPE_NAME,
        "mapper_id": _MAPPER_UUID,
        "mapper_name": _MAPPER_NAME,
        "representation": _CHANGE,
    }
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        put = _mount_all(mock)
        call = await _park(params)
        assert call["status"] == "awaiting_approval", call
        resumed = await _approve_and_resume(uuid.UUID(call["extras"]["approval_request_id"]))
    assert resumed.status == "ok", resumed
    assert put.call_count == 1


@pytest.mark.parametrize(
    "ids",
    [
        {"scope_id": _SCOPE_UUID, "mapper_id": _MAPPER_UUID},
        {"scope_name": _SCOPE_NAME, "mapper_name": _MAPPER_NAME},
    ],
    ids=["ids-only", "names-only"],
)
async def test_card_shows_the_ids_and_the_stored_names(
    kc: KeycloakConnector, ids: dict[str, Any]
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        put = _mount_all(mock)
        call = await _park({**ids, "representation": _CHANGE})
        assert call["status"] == "awaiting_approval", call
        card = await _card(uuid.UUID(call["extras"]["approval_request_id"]))
    assert card["scope_id"] == _SCOPE_UUID
    assert card["scope_name"] == _SCOPE_NAME
    assert card["mapper_id"] == _MAPPER_UUID
    assert card["mapper_name"] == _MAPPER_NAME
    assert card["realm"] == _REALM
    assert card["changed_fields"] == ["config"]
    assert card["current_available"] is True
    assert not put.called


async def test_approved_run_acts_on_the_mapper_id_on_the_card(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        put = _mount_all(mock)
        call = await _park(
            {"scope_name": _SCOPE_NAME, "mapper_name": _MAPPER_NAME, "representation": _CHANGE}
        )
        # While the request waits, the lookups would find other objects.
        scopes = mock.get(f"/admin/realms/{_REALM}/client-scopes").respond(200, json=[])
        mappers = mock.get(_MAPPERS_PATH).respond(200, json=[])
        mock.reset()  # count only the calls of the approved run
        resumed = await _approve_and_resume(uuid.UUID(call["extras"]["approval_request_id"]))
    assert resumed.status == "ok", resumed
    assert not scopes.called
    assert not mappers.called
    assert put.call_count == 1
    assert put.calls.last.request.url.path == _MAPPER_PATH
    assert resumed.result["mapper_name"] == _MAPPER_NAME


@pytest.mark.parametrize("renamed", ["scope", "mapper"])
async def test_approved_run_is_refused_when_a_name_changed_after_approval(
    kc: KeycloakConnector, renamed: str
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        put = _mount_all(mock)
        call = await _park(
            {"scope_name": _SCOPE_NAME, "mapper_name": _MAPPER_NAME, "representation": _CHANGE}
        )
        if renamed == "scope":
            mock.get(_SCOPE_PATH).respond(200, json={"id": _SCOPE_UUID, "name": "renamed"})
        else:
            mock.get(_MAPPER_PATH).respond(200, json={**_mapper(), "name": "renamed"})
        resumed = await _approve_and_resume(uuid.UUID(call["extras"]["approval_request_id"]))
    assert resumed.status != "ok"
    assert "on the approval card has another name now" in str(resumed.model_dump(mode="json"))
    assert not put.called


async def test_approved_run_is_refused_when_the_card_has_no_ids(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        put = _mount_all(mock)
        scopes = mock.get(f"/admin/realms/{_REALM}/client-scopes").respond(500)
        call = await _park(
            {"scope_name": _SCOPE_NAME, "mapper_name": _MAPPER_NAME, "representation": _CHANGE}
        )
        assert call["status"] == "awaiting_approval", call
        request_id = uuid.UUID(call["extras"]["approval_request_id"])
        card = await _card(request_id)
        scopes.respond(200, json=_scope_rows())
        resumed = await _approve_and_resume(request_id)
    assert card["current_available"] is False
    assert card["scope_id"] is None
    assert resumed.status != "ok"
    assert "approval card does not show the client scope and the mapper" in str(
        resumed.model_dump(mode="json")
    )
    assert not put.called


# ---------------------------------------------------------------------------
# A change of rights
# ---------------------------------------------------------------------------

_BY_NAMES = {"scope_name": _SCOPE_NAME, "mapper_name": _MAPPER_NAME, "representation": _CHANGE}


def _principal(sub: str, kind: PrincipalKind) -> Operator:
    return _operator(sub).model_copy(update={"principal_kind": kind})


async def _call(operator: Operator, params: dict[str, Any]) -> dict[str, Any]:
    return await call_operation(
        operator,
        {"connector_id": _CONNECTOR_ID, "op_id": _OP, "target": _TARGET_NAME, "params": params},
    )


async def _target_id() -> uuid.UUID:
    async with get_sessionmaker()() as session:
        target = await resolve_target(session, _TENANT_ID, _TARGET_NAME)
    target_id: uuid.UUID = target.id
    return target_id


def test_op_is_a_change_of_rights_approved_call_by_call() -> None:
    assert is_rights_change_op(_OP)
    assert needs_per_call_approval(_OP)


@pytest.mark.parametrize(
    "requester",
    [
        _operator("kc-mapper-admin", TenantRole.TENANT_ADMIN),
        _operator("kc-mapper-admin").model_copy(update={"self_approver": True}),
    ],
    ids=["tenant-admin", "sa-operator"],
)
async def test_the_requester_cannot_approve_their_own_request_with_a_token(
    kc: KeycloakConnector, requester: Operator, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APPROVAL_ALLOW_SELF_APPROVAL", "true")
    get_settings.cache_clear()
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        put = _mount_all(mock)
        call = await _call(requester, _BY_NAMES)
        assert call["status"] == "awaiting_approval", call
        request_id = uuid.UUID(call["extras"]["approval_request_id"])
        async with get_sessionmaker()() as session:
            assert (await session.get(ApprovalRequest, request_id)).rights_change is True  # type: ignore[union-attr]
            with pytest.raises(RightsChangeApprovalError):
                await approve_request(session, request_id, operator=requester, params=None)
            await session.rollback()
    assert not put.called


async def test_an_sa_operator_who_is_not_an_admin_cannot_approve(kc: KeycloakConnector) -> None:
    reviewer = _operator("kc-mapper-sa").model_copy(update={"self_approver": True})
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        put = _mount_all(mock)
        call = await _park(_BY_NAMES)
        request_id = uuid.UUID(call["extras"]["approval_request_id"])
        async with get_sessionmaker()() as session:
            with pytest.raises(RightsChangeApprovalError):
                await approve_request(session, request_id, operator=reviewer, params=None)
            await session.rollback()
    assert not put.called


async def test_a_standing_grant_does_not_clear_the_wait(kc: KeycloakConnector) -> None:
    service = _principal("svc-kc-mapper", PrincipalKind.SERVICE)
    with pytest.raises(GrantValidationError, match="a person approves each call"):
        await ServicePrincipalGrantService().create(
            _TENANT_ID,
            "kc-mapper-admin",
            ServiceGrantCreate(
                principal_sub=service.sub,
                op_id=_OP,
                connector_id=_CONNECTOR_ID,
                reason="fixture: try to grant the mapper change",
            ),
        )
    # A row stored past the create check, as an older row would be.
    async with get_sessionmaker()() as session:
        session.add(
            ServicePrincipalGrant(
                id=uuid.uuid4(),
                tenant_id=_TENANT_ID,
                principal_sub=service.sub,
                op_id=_OP,
                connector_id=_CONNECTOR_ID,
                target_id=None,
                reason="fixture: made before the per-call mark",
                created_by_sub="kc-mapper-admin",
            )
        )
        await session.commit()
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        put = _mount_all(mock)
        call = await _call(service, _BY_NAMES)
    assert call["status"] == "awaiting_approval", call
    assert not put.called


async def test_a_run_approval_does_not_clear_the_wait(kc: KeycloakConnector) -> None:
    service = _principal("svc-kc-mapper", PrincipalKind.SERVICE)
    run_ref = "run-kc-mapper-0001"
    with pytest.raises(RunApprovalValidationError, match="a person approves each call"):
        await RunApprovalService().request(
            service,
            RunApprovalCreate(
                work_ref=run_ref,
                entries=[RunApprovalEntry(connector_id=_CONNECTOR_ID, op_id=_OP)],
                reason="fixture: try to list the mapper change",
            ),
        )
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session:
        session.add(
            RunApproval(
                id=uuid.uuid4(),
                tenant_id=_TENANT_ID,
                principal_sub=service.sub,
                work_ref=run_ref,
                entries=[
                    {
                        "connector_id": _CONNECTOR_ID,
                        "op_id": _OP,
                        "target_id": str(await _target_id()),
                    }
                ],
                reason="fixture: stored past the request check",
                requested_at=now,
                expires_at=now + timedelta(hours=1),
                approved_at=now,
                approved_by_sub="kc-mapper-admin",
            )
        )
        await session.commit()
    token = work_ref_var.set(run_ref)
    try:
        with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
            put = _mount_all(mock)
            call = await _call(service, _BY_NAMES)
    finally:
        work_ref_var.reset(token)
    assert call["status"] == "awaiting_approval", call
    assert not put.called


async def test_an_agent_grant_does_not_clear_the_wait(kc: KeycloakConnector) -> None:
    agent = _principal("agent-kc-mapper", PrincipalKind.AGENT)
    async with get_sessionmaker()() as session:
        if await session.get(Tenant, _TENANT_ID) is None:
            session.add(Tenant(id=_TENANT_ID, slug="t-kc-mapper", name="Tenant kc mapper"))
            await session.flush()
        session.add(
            AgentPermission(
                tenant_id=_TENANT_ID,
                principal_sub=agent.sub,
                op_pattern=_OP,
                verdict="auto-execute",
                created_by_sub="kc-mapper-admin",
            )
        )
        await session.commit()
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        put = _mount_all(mock)
        call = await _call(agent, _BY_NAMES)
    assert call["status"] == "awaiting_approval", call
    assert not put.called


# ---------------------------------------------------------------------------
# The card shows the mapper as it is now; the run checks it did not change
# ---------------------------------------------------------------------------


async def test_card_shows_the_mapper_now_and_the_old_and_new_values(
    kc: KeycloakConnector,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        mapper = {**_mapper(), "config": {**_mapper()["config"], "secret": "s3cr3t-old"}}
        _mount_all(mock)
        mock.get(_MAPPER_PATH).respond(200, json=mapper)
        change = {"config": {"claim.value": _NEW_VALUE, "secret": "s3cr3t-new"}}
        call = await _park({**_BY_NAMES, "representation": change})
        assert call["status"] == "awaiting_approval", call
        card = await _card(uuid.UUID(call["extras"]["approval_request_id"]))
    assert card["current"]["protocolMapper"] == "oidc-hardcoded-claim-mapper"
    assert card["current"]["protocol"] == "openid-connect"
    assert card["current"]["claim_name"] == "capabilities"
    assert card["current"]["before"]["config"]["claim.value"] == '["meho-docs"]'
    assert card["representation"]["config"]["claim.value"] == _NEW_VALUE
    assert "s3cr3t-old" not in json.dumps(card)
    assert "s3cr3t-new" not in json.dumps(card)
    assert card["mapper_content_hash"]


@pytest.mark.parametrize(
    "changed",
    [
        {"config": {**_mapper()["config"], "claim.value": '["meho-docs","other"]'}},
        {"protocolMapper": "oidc-usermodel-attribute-mapper"},
        {"protocol": "saml"},
    ],
    ids=["config", "type", "protocol"],
)
async def test_approved_run_is_refused_when_the_mapper_changed_after_approval(
    kc: KeycloakConnector, changed: dict[str, Any]
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        put = _mount_all(mock)
        call = await _park(_BY_NAMES)
        assert call["status"] == "awaiting_approval", call
        mock.get(_MAPPER_PATH).respond(200, json={**_mapper(), **changed})
        resumed = await _approve_and_resume(uuid.UUID(call["extras"]["approval_request_id"]))
    assert resumed.status != "ok"
    dumped = str(resumed.model_dump(mode="json"))
    assert "the mapper changed after the approval" in dumped
    assert "A new request is needed" in dumped
    assert not put.called
