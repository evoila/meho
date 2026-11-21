# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""One person alone cannot change the sign-in that guards a change of rights.

In MEHO's own login realm, a Keycloak write that can change the sign-in
MEHO asks for before an approval (a client of that sign-in, a mapper of
its token claims, a realm setting outside a short list):

* waits, also for a platform admin in the web console with a fresh sign-in,
  and nothing is written;
* is never approved by the login that asked, also not in the web console;
  a second platform admin approves it.

After a change of rights that ran alone, the next one still needs the
approval sign-in level. The marker of where a change of rights was asked
stays inside MEHO: no approval response returns it. Fixture values are
made up.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import respx

from meho_backplane.auth.operator import Operator, PrincipalKind
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus
from meho_backplane.operations import dispatch
from meho_backplane.operations._lookup import lookup_descriptor
from meho_backplane.operations._preview import (
    INTERNAL_EFFECT_KEYS,
    PreviewContext,
    build_proposed_effect,
    proposed_effect_for_agent,
)
from meho_backplane.operations._validate import compute_params_hash, policy_gate
from meho_backplane.operations.approval_queue import (
    ApprovalRoleFloorError,
    approve_request,
    create_pending_request,
)
from meho_backplane.operations.approval_rights import approve_right
from meho_backplane.operations.approval_sign_in_writes import approval_sign_in_check
from meho_backplane.operations.grant_approval import ASKED_IN_WEB_CONSOLE
from meho_backplane.targets.resolver import resolve_target
from tests.test_keycloak_login_realm_every_write import (
    _MAPPER_ID,
    _SCOPE_ID,
    _ctx,
    _FakeKeycloak,
    _fill,
    _Write,
)
from tests.test_rights_change_console_run import (
    _APPROVAL_OPERATOR,
    _CONNECTOR_ID,
    _KC_BASE_URL,
    _PLAIN_GROUP,
    _REALM,
    _TARGET_NAME,
    _TENANT_ID,
    _add,
    _approval_sign_in,
    _mount,
    _person,
    _required_settings_env,  # noqa: F401 -- autouse fixture: pins the env these tests need
    _run_rows,
    keycloak,  # noqa: F401 -- fixture: registers the Keycloak operations
)
from tests.test_rights_change_login_realm_platform_admin_alone import _login

_CLIENT_UUID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
_SIGN_IN_TEXT = "can change the sign-in that MEHO asks for before an approval"


class _ClientKeycloak(_FakeKeycloak):
    """The fake Keycloak, whose client reads answer with *client_id*."""

    def __init__(self, client_id: str) -> None:
        super().__init__()
        self.client_id = client_id

    def connector(self) -> KeycloakConnector:
        connector = super().connector()
        read = connector._get_admin_json
        client_id = self.client_id

        async def _get_admin_json(
            target: Any, path: str, *, operator: Any, params: Any = None
        ) -> dict[str, Any]:
            answer = await read(target, path, operator=operator, params=params)
            if isinstance(answer, dict) and "clientId" in answer:
                return {**answer, "clientId": client_id}
            return answer

        connector._get_admin_json = _get_admin_json  # type: ignore[method-assign]
        return connector


def _mapper(claim: str, kind: str = "oidc-hardcoded-claim-mapper") -> dict[str, Any]:
    return {
        "name": "a-claim",
        "protocol": "openid-connect",
        "protocolMapper": kind,
        "config": {"claim.name": claim, "claim.value": "x"},
    }


#: (write, the clientId Keycloak stores for the client it names)
_SIGN_IN_WRITES: list[tuple[_Write, str]] = [
    (
        _Write(
            "keycloak.client.update",
            {
                "client_id": "meho-web-approve",
                "representation": {"authenticationFlowBindingOverrides": {"browser": ""}},
            },
        ),
        "meho-web-approve",
    ),
    (
        _Write(
            "keycloak.client.update",
            {"id": _CLIENT_UUID, "representation": {"directAccessGrantsEnabled": True}},
        ),
        "MEHO-CLI-Approve",
    ),
    (
        _Write(
            "keycloak.protocol_mapper.create",
            {"client_id": "meho-web-approve", "representation": _mapper("team")},
        ),
        "meho-web-approve",
    ),
    (
        _Write(
            "keycloak.protocol_mapper.create",
            {"client_id": "example-app", "representation": _mapper("acr")},
        ),
        "example-app",
    ),
    (
        _Write(
            "keycloak.protocol_mapper.create",
            {"client_id": "example-app", "representation": _mapper("level", "oidc-acr-mapper")},
        ),
        "example-app",
    ),
    (
        _Write(
            "keycloak.client_scope.protocol_mapper.update",
            {
                "scope_id": _SCOPE_ID,
                "mapper_id": _MAPPER_ID,
                "representation": {"config": {"claim.name": "auth_time"}},
            },
        ),
        "example-app",
    ),
]
_IDS = [
    "approval-client-flow",
    "approval-client-by-id",
    "mapper-on-approval-client",
    "acr-claim-mapper",
    "level-mapper",
    "scope-mapper-to-sign-in-claim",
]
_REALM_LEVEL_MAP = _Write(
    "keycloak.realm.update",
    {"realm": "{realm}", "representation": {"attributes": {"acr.loa.map": "{}"}}},
)


async def _card(write: _Write, client_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    fake = _ClientKeycloak(client_id)
    fake.realm = _REALM
    params = _fill(copy.deepcopy(write.params), _REALM)
    ctx: PreviewContext = _ctx(write.op_id, params, realm=_REALM, connector=fake.connector())
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    assert effect["preview"]["grant"]["login_realm"] is True
    return effect, params


async def _gate(
    monkeypatch: pytest.MonkeyPatch, write: _Write, client_id: str, operator: Operator
) -> str:
    effect, params = await _card(write, client_id)
    monkeypatch.setattr(
        "meho_backplane.operations.approval_object_check.live_card",
        AsyncMock(return_value=effect),
    )
    descriptor = await lookup_descriptor(
        tenant_id=_TENANT_ID,
        product="keycloak",
        version="26.x",
        impl_id="keycloak-admin",
        op_id=write.op_id,
    )
    assert descriptor is not None
    verdict, _reason = await policy_gate(
        operator=operator,
        descriptor=descriptor,
        target=None,
        connector_id=_CONNECTOR_ID,
        params=params,
    )
    return verdict.value


async def _park(write: _Write, client_id: str, requester: Operator) -> uuid.UUID:
    effect, params = await _card(write, client_id)
    async with get_sessionmaker()() as session:
        request = await create_pending_request(
            session,
            operator=requester,
            connector_id=_CONNECTOR_ID,
            op_id=write.op_id,
            target=None,
            params=params,
            params_hash=compute_params_hash(params),
            proposed_effect=effect,
        )
        await session.commit()
        return request.id


async def _row(request_id: uuid.UUID) -> ApprovalRequest:
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        return row


async def _approve(request_id: uuid.UUID, approver: Operator) -> None:
    row = await _row(request_id)
    async with get_sessionmaker()() as session:
        await approve_request(session, request_id, operator=approver, params=row.params)
        await session.commit()


# -- the write waits, also for a platform admin with a fresh sign-in ------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("write", "client_id"), _SIGN_IN_WRITES, ids=_IDS)
async def test_a_platform_admin_with_a_fresh_sign_in_does_not_run_it_alone(
    keycloak: KeycloakConnector,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    write: _Write,
    client_id: str,
) -> None:
    assert await _gate(monkeypatch, write, client_id, _login()) == "needs-approval"


@pytest.mark.asyncio
async def test_a_mapper_of_another_claim_on_another_client_still_runs_alone(
    keycloak: KeycloakConnector,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write = _Write(
        "keycloak.protocol_mapper.create",
        {"client_id": "example-app", "representation": _mapper("team")},
    )

    assert await _gate(monkeypatch, write, "example-app", _login()) == "auto-execute"


@pytest.mark.asyncio
async def test_a_platform_admin_s_update_of_the_approval_client_writes_nothing(
    keycloak: KeycloakConnector,  # noqa: F811
) -> None:
    async with get_sessionmaker()() as session:
        target = await resolve_target(session, _TENANT_ID, _TARGET_NAME)
    client_path = f"/admin/realms/{_REALM}/clients/{_CLIENT_UUID}"
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount(mock, group=_PLAIN_GROUP)
        mock.get(client_path).respond(
            200,
            json={
                "id": _CLIENT_UUID,
                "clientId": "meho-web-approve",
                "description": "Approval sign-in",
            },
        )
        put = mock.put(client_path).respond(204)
        result = await dispatch(
            operator=_person(platform_admin=True),
            connector_id=_CONNECTOR_ID,
            op_id="keycloak.client.update",
            target=target,
            params={
                "id": _CLIENT_UUID,
                "representation": {"description": "The approval sign-in of the web console"},
            },
        )

    dumped = result.model_dump(mode="json")
    assert dumped["status"] == "awaiting_approval", dumped
    assert not put.called
    assert await _run_rows() == []


class _UnreadableClientKeycloak(_FakeKeycloak):
    """The fake Keycloak, whose client reads fail."""

    def connector(self) -> KeycloakConnector:
        connector = super().connector()
        read = connector._get_admin_json

        async def _get_admin_json(
            target: Any, path: str, *, operator: Any, params: Any = None
        ) -> dict[str, Any]:
            if "/clients/" in path:
                raise TimeoutError("the client read timed out")
            answer: dict[str, Any] = await read(target, path, operator=operator, params=params)
            return answer

        connector._get_admin_json = _get_admin_json  # type: ignore[method-assign]
        return connector


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "representation"),
    [
        ("keycloak.client.update", {"authenticationFlowBindingOverrides": {"browser": ""}}),
        ("keycloak.protocol_mapper.create", _mapper("team")),
    ],
)
async def test_a_failed_client_read_never_trusts_the_client_id_of_the_params(
    keycloak: KeycloakConnector,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    representation: dict[str, Any],
) -> None:
    fake = _UnreadableClientKeycloak()
    fake.realm = _REALM
    params = {"id": _CLIENT_UUID, "client_id": "example-app", "representation": representation}
    ctx: PreviewContext = _ctx(op_id, params, realm=_REALM, connector=fake.connector())
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    assert effect["preview"]["client_id"] is None
    assert effect["preview"]["id"] == _CLIENT_UUID

    request = ApprovalRequest(
        id=uuid.uuid4(),
        tenant_id=_TENANT_ID,
        principal_sub="platform-admin-a",
        op_id=op_id,
        connector_id=_CONNECTOR_ID,
        params=params,
        proposed_effect=effect,
    )
    assert approval_sign_in_check(request) is True

    monkeypatch.setattr(
        "meho_backplane.operations.approval_object_check.live_card",
        AsyncMock(return_value=effect),
    )
    descriptor = await lookup_descriptor(
        tenant_id=_TENANT_ID,
        product="keycloak",
        version="26.x",
        impl_id="keycloak-admin",
        op_id=op_id,
    )
    assert descriptor is not None
    verdict, _reason = await policy_gate(
        operator=_login(),
        descriptor=descriptor,
        target=None,
        connector_id=_CONNECTOR_ID,
        params=params,
    )
    assert verdict.value == "needs-approval"

    async with get_sessionmaker()() as session:
        parked = await create_pending_request(
            session,
            operator=_login(),
            connector_id=_CONNECTOR_ID,
            op_id=op_id,
            target=None,
            params=params,
            params_hash=compute_params_hash(params),
            proposed_effect=effect,
        )
        await session.commit()
        request_id = parked.id
    with pytest.raises(ApprovalRoleFloorError) as exc:
        await _approve(request_id, _login())
    assert _SIGN_IN_TEXT in exc.value.reason_text
    assert (await _row(request_id)).status == ApprovalRequestStatus.PENDING.value


# -- the login that asked never approves it --------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("write", "client_id"),
    [*_SIGN_IN_WRITES, (_REALM_LEVEL_MAP, "example-app")],
    ids=[*_IDS, "realm-level-map"],
)
async def test_the_login_that_asked_never_approves_it(
    keycloak: KeycloakConnector,  # noqa: F811
    write: _Write,
    client_id: str,
) -> None:
    request_id = await _park(write, client_id, _login())
    assert ((await _row(request_id)).proposed_effect or {})[ASKED_IN_WEB_CONSOLE] is True

    with pytest.raises(ApprovalRoleFloorError) as exc:
        await _approve(request_id, _login())
    assert _SIGN_IN_TEXT in exc.value.reason_text
    assert approve_right(_login(), await _row(request_id)).may_approve is False
    assert (await _row(request_id)).status == ApprovalRequestStatus.PENDING.value

    await _approve(request_id, _login("platform-admin-b"))
    assert (await _row(request_id)).status == ApprovalRequestStatus.APPROVED.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "op_id",
    [
        "keycloak.client.update",
        "keycloak.protocol_mapper.create",
        "keycloak.client_scope.protocol_mapper.update",
        "keycloak.realm.update",
    ],
)
async def test_a_card_that_does_not_say_what_changes_counts_as_such_a_write(
    keycloak: KeycloakConnector,  # noqa: F811
    op_id: str,
) -> None:
    request = ApprovalRequest(
        id=uuid.uuid4(),
        tenant_id=_TENANT_ID,
        principal_sub="platform-admin-a",
        op_id=op_id,
        connector_id=_CONNECTOR_ID,
        params=None,
        proposed_effect={"preview": {"grant": {"login_realm": True}}},
    )

    assert approval_sign_in_check(request) is True


# -- the next change still needs the approval sign-in level ---------------------


@pytest.mark.asyncio
async def test_after_a_change_alone_the_next_one_still_needs_the_approval_level(
    keycloak: KeycloakConnector,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meho_backplane.settings import get_settings

    monkeypatch.setenv("APPROVAL_SIGN_IN_LEVEL", "required")
    get_settings.cache_clear()
    operator = _person(platform_admin=True).model_copy(update={"web_session_id": uuid.uuid4()})

    with (
        patch(
            _APPROVAL_OPERATOR,
            new_callable=AsyncMock,
            return_value=_approval_sign_in(acr="meho-approve"),
        ),
        respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock,
    ):
        put = _mount(mock, group=_PLAIN_GROUP)
        first = await _add(operator)
    assert first["status"] == "ok", first
    assert put.called

    with (
        patch(_APPROVAL_OPERATOR, new_callable=AsyncMock, return_value=_approval_sign_in(acr="1")),
        respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock,
    ):
        put = _mount(mock, group=_PLAIN_GROUP)
        second = await _add(operator)
    assert second["status"] == "awaiting_approval", second
    assert not put.called


# -- the marker of where a change was asked stays inside MEHO --------------------


def test_the_marker_is_an_internal_key() -> None:
    assert ASKED_IN_WEB_CONSOLE in INTERNAL_EFFECT_KEYS


@pytest.mark.asyncio
@pytest.mark.parametrize("web", [True, False], ids=["asked-in-console", "asked-with-a-token"])
async def test_no_approval_response_returns_the_marker(
    keycloak: KeycloakConnector,  # noqa: F811
    web: bool,
) -> None:
    from meho_backplane.api.v1.approvals import _proposed_effect_for

    write, client_id = _SIGN_IN_WRITES[0]
    request_id = await _park(write, client_id, _login(web=web))
    row = await _row(request_id)
    stored = dict(row.proposed_effect or {})
    assert stored[ASKED_IN_WEB_CONSOLE] is web
    without_marker = {key: value for key, value in stored.items() if key != ASKED_IN_WEB_CONSOLE}

    agent_view = proposed_effect_for_agent(
        row.op_id,
        row.proposed_effect,
        params=row.params,
        connector_id=row.connector_id,
        tenant=str(row.tenant_id),
    )
    agent = _login(web=False).model_copy(update={"principal_kind": PrincipalKind.AGENT})
    for shown in (
        agent_view,
        _proposed_effect_for(_login(), row),
        _proposed_effect_for(agent, row),
    ):
        assert isinstance(shown, dict)
        assert ASKED_IN_WEB_CONSOLE not in shown
    assert _proposed_effect_for(_login(), row) == without_marker
