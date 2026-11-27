# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An Admin's change of rights as a step of a run approved in the web console.

``keycloak.group.member.add`` runs at once when an Admin calls it in the web
console with a fresh sign-in (``test_rights_change_console_run.py``). Here
it is the second step of a multi-step operation: a service login starts
the run, its first step waits, and a tenant Admin (outside MEHO's own login
realm) or a platform Admin approves that step in the web console
(``POST /ui/approvals/{id}/approve``). The run goes on as the route starts
it, and reaches the change of rights through the step gate and as a child
call, against a stand-in of the identity system.

With ``CUSTOM_POLICY`` on, the step is not the Admin's own change: it waits
for its own approval, and nothing is written to the identity system. With
the switch off, the test asserts the answer of the code with the switch off.

All names and values are made up.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.session import (
    KeycloakAdminCredentials,
    KeycloakClientCredentials,
    KeycloakTargetLike,
)
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import grant_approval
from meho_backplane.operations.composite import (
    DispatchChild,
    composite_resume_state,
    enforce_subop_policy,
)
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.targets.resolver import resolve_target
from tests import _composite_wait_audit_helpers as h
from tests import _custom_policy_run_steps_helpers as c
from tests._custom_policy_run_steps_helpers import Step

pytestmark = pytest.mark.asyncio

_CONNECTOR_ID = "keycloak-admin-26.x"
_ADD = Step("keycloak.group.member.add", "caution", False)
_TARGET_NAME = "probe-identity"
_KC_HOST = "probe-identity.test.invalid"
_KC_BASE_URL = f"https://{_KC_HOST}"
_REALM = "probe-realm"
#: The login ``user-probe-9`` and the group ``group-probe-4``; the connector takes their ids.
_USER = "99999999-0000-4000-8000-000000000009"
_GROUP = "44444444-0000-4000-8000-000000000004"
_PARAMS: dict[str, Any] = {"group_id": _GROUP, "user_id": _USER}
_LOGIN_REALMS = "meho_backplane.connectors.keycloak.ops_write_grant._backplane_login_realms"
_STEP_RUN = "vault.composite.probe_rights_step"
_CHILD_RUN = "vault.composite.probe_rights_child"
_RIGHTS_CHANGE_OPS = grant_approval._RIGHTS_CHANGE_OPS
_PUT_URL = f"{_KC_BASE_URL}/admin/realms/{_REALM}/users/{_USER}/groups/{_GROUP}"


async def _target() -> Any:
    async with get_sessionmaker()() as session:
        return await resolve_target(session, h.TENANT_ID, _TARGET_NAME)


async def _first_step(operator: Operator, composite_op_id: str) -> Any:
    if composite_resume_state(composite_op_id) is not None:
        return None
    gated = await h.gate(operator, None, h.STEP_A)
    if gated is None:
        c.ran.append(h.STEP_A)
    return gated


async def rights_step_run(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    """A step that waits, then the change of rights through the step gate, then its write."""
    gated = await _first_step(operator, _STEP_RUN)
    if gated is not None:
        return gated
    gated = await enforce_subop_policy(
        operator=operator,
        connector_id=_CONNECTOR_ID,
        op_id=_ADD.op_id,
        safety_level=_ADD.safety_level,
        requires_approval=_ADD.requires_approval,
        target=await _target(),
        params=_PARAMS,
        resume_state={"at": "rights"},
        after_main_effect=True,
    )
    if gated is not None:
        return gated
    async with httpx.AsyncClient() as client:
        await client.put(_PUT_URL)
    c.ran.append(_ADD.op_id)
    return {"done": True}


async def rights_child_run(
    operator: Operator, target: Any, params: dict[str, Any], dispatch_child: DispatchChild
) -> Any:
    """A step that waits, then the change of rights as a child call."""
    gated = await _first_step(operator, _CHILD_RUN)
    if gated is not None:
        return gated
    result = await dispatch_child(
        connector_id=_CONNECTOR_ID, op_id=_ADD.op_id, params=_PARAMS, target=await _target()
    )
    if result.status == "ok":
        c.ran.append(_ADD.op_id)
    return result


def _stub_loader(_target: KeycloakTargetLike, _operator: Operator) -> Any:
    async def _load() -> KeycloakAdminCredentials:
        return KeycloakClientCredentials(client_id="probe-admin", client_secret="probe-value")

    return _load()


@pytest.fixture(autouse=True)
def _state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    reset = c.reset(monkeypatch)
    next(reset)
    monkeypatch.setattr(grant_approval, "_RIGHTS_CHANGE_OPS", _RIGHTS_CHANGE_OPS | {c.RIGHTS.op_id})
    c.console_env(monkeypatch)
    yield
    next(reset, None)


@pytest.fixture
async def keycloak(stub_embedding_service: AsyncMock) -> AsyncIterator[None]:
    set_default_reducer(PassThroughReducer())
    register_connector_v2(
        product="keycloak", version="26.x", impl_id="keycloak-admin", cls=KeycloakConnector
    )
    await KeycloakConnector.register_operations()
    await h.register_child(stub_embedding_service)
    async with get_sessionmaker()() as session:
        session.add(
            TargetORM(
                tenant_id=h.TENANT_ID,
                name=_TARGET_NAME,
                aliases=[],
                product="keycloak",
                host=_KC_HOST,
                port=443,
                fqdn=None,
                secret_ref="probe-site/identity/admin",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={"managed_realm": _REALM},
                fingerprint={"version": "26.0.5"},
                notes="seeded by test_custom_policy_run_steps_console_rights",
            )
        )
        await session.commit()
    from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE

    instance = KeycloakConnector(credentials_loader=_stub_loader)
    _CONNECTOR_INSTANCE_CACHE[KeycloakConnector] = instance
    yield
    await instance.aclose()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = h.EMBEDDING
    service.encode.return_value = [h.EMBEDDING]
    service.dimension = 384
    return service


def _mount(mock: respx.MockRouter) -> respx.Route:
    """The identity system answers; returns the write of the new member."""
    mock.post("/realms/master/protocol/openid-connect/token").respond(
        200, json={"access_token": "probe-admin-token", "expires_in": 300}
    )
    mock.get(f"/admin/realms/{_REALM}").respond(200, json={"realm": _REALM})
    group_path = f"/admin/realms/{_REALM}/groups/{_GROUP}"
    group = {"id": _GROUP, "name": "group-probe-4", "path": "/group-probe-4", "attributes": {}}
    mock.get(group_path).respond(200, json=group)
    mock.get(f"{group_path}/members").respond(200, json=[])
    mock.get(f"/admin/realms/{_REALM}/users/{_USER}").respond(
        200, json={"id": _USER, "username": "user-probe-9"}
    )
    mock.get(f"/admin/realms/{_REALM}/users/{_USER}/groups").respond(200, json=[])
    return mock.put(f"/admin/realms/{_REALM}/users/{_USER}/groups/{_GROUP}").respond(204)


_ADMINS = pytest.mark.parametrize(
    ("admin", "login_realm"),
    [
        (
            c.console_person(sub="admin-probe-5", role=TenantRole.TENANT_ADMIN),
            False,
        ),
        (
            c.console_person(
                sub="platform-probe-6", role=TenantRole.TENANT_ADMIN, platform_admin=True
            ),
            True,
        ),
    ],
    ids=["tenant-admin-outside-the-login-realm", "platform-admin-in-the-login-realm"],
)


@_ADMINS
@pytest.mark.parametrize("on", [True, False], ids=["switch-on", "switch-off"])
@pytest.mark.parametrize("child", [False, True], ids=["step", "child-call"])
@pytest.mark.usefixtures("keycloak")
async def test_an_admin_s_console_approval_of_the_run_does_not_run_the_change_of_rights(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
    admin: Operator,
    login_realm: bool,
    on: bool,
    child: bool,
) -> None:
    c.switch(monkeypatch, on=on)
    c.console_env(monkeypatch)
    monkeypatch.setattr(_LOGIN_REALMS, lambda: {_REALM} if login_realm else {"other-realm"})
    run_op, handler = (_CHILD_RUN, "rights_child_run") if child else (_STEP_RUN, "rights_step_run")
    await h.register_composite(session, run_op, handler, module=__name__)
    c.current.append(_ADD)
    request_id = await c.start_run(run_op)

    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        put = _mount(mock)
        await c.approve_in_console(request_id, admin.model_copy(update={"sid": str(uuid.uuid4())}))

    if on:
        assert not put.called
        assert c.ran == [h.STEP_A]
        _, own = await c.requests()
        assert own.op_id == _ADD.op_id
        assert own.rights_change is True
        last = (await h.rows(run_op))[-1]
        assert last.status_code == 202
        assert last.payload["result_status"] == "awaiting_approval"
        assert last.payload["error"]["approval_request_id"] == str(own.id)
    else:
        # The code with the switch off: the step runs under the Admin's approval.
        assert put.called
        assert c.ran == [h.STEP_A, _ADD.op_id]
        assert len(await c.requests()) == 1
