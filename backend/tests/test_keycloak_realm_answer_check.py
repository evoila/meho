# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every Keycloak operation checks which realm Keycloak answers with.

* Each operation first reads the target's managed realm and compares the
  ``realm`` field of the answer with the managed realm of the target.
* A different name, a different letter case, a space at the end, an accent,
  a missing name or an answer that is not an object gives an error. Nothing
  else is sent to Keycloak: no read, no write, no password read from Vault.
* The error names the target and says what to check. It holds no value from
  the answer.
* The same name lets the operation run.
* ``keycloak.realm.create`` and ``keycloak.realm.delete`` also run when the
  managed realm does not exist (HTTP 404). Other operations, and other read
  errors, still stop before anything more is sent.
* The check runs before the policy gate, so before the call waits for
  approval and before any approval card or preview reads Keycloak, with the
  cards switch on and off. No approval request is stored, and the call
  answer, the preview answer, the audit rows and the logs hold no value from
  the answer.
* ``keycloak.realm.update`` with a ``realm`` value goes on only when the
  answer for that realm carries exactly that name.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from sqlalchemy import select
from structlog.testing import capture_logs

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.keycloak import (
    KeycloakConnector,
    ops_login_check,
    ops_write,
    ops_write_realm_delete,
)
from meho_backplane.connectors.keycloak.realm_answer import KeycloakRealmAnswerError
from meho_backplane.connectors.keycloak.session import (
    KeycloakAdminCredentials,
    KeycloakClientCredentials,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog, EndpointDescriptor, Tenant
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import _park_check, dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations._request_preview import (
    _validate_params_envelope,
    preview_dispatch,
)
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import resolve_target

pytestmark = pytest.mark.usefixtures("real_keycloak_realm_check")

_CHECK_LOGIN = "keycloak.user.check_login"
_ALL_OPS = KeycloakConnector.operation_specs()
_HANDLER = {op.op_id: op.handler_attr for op in _ALL_OPS}
_MANAGED = "example"
_ANSWER_MARKER = "answer-realm-marker"


@dataclass
class _Target:
    name: str = "keycloak-a"
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=uuid4)
    extras: dict[str, Any] = field(default_factory=lambda: {"managed_realm": _MANAGED})


def _operator() -> Operator:
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt="operator.jwt.value",
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


class _Recorder:
    """Records every request the connector would send to Keycloak."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.reads: list[str] = []
        self.sent: list[str] = []

    def install(self, connector: KeycloakConnector, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _get_json(_target: Any, path: str, **_kw: Any) -> Any:
            self.reads.append(path)
            return self.answer

        async def _other(*args: Any, **_kw: Any) -> Any:
            self.sent.append(str(args))
            raise AssertionError("a request was sent after the realm check refused")

        monkeypatch.setattr(connector, "_get_admin_json", _get_json)
        monkeypatch.setattr(connector, "_get_admin_list", _other)
        monkeypatch.setattr(connector, "_write_admin", _other)
        monkeypatch.setattr(connector, "_http_client", _other)


def test_every_registered_operation_checks_the_realm_answer() -> None:
    registered: list[str] = []

    async def _keep(**op: Any) -> None:
        registered.append(op["op_id"])

    with patch("meho_backplane.operations.typed_register.register_typed_operation", _keep):
        asyncio.run(KeycloakConnector.register_operations())
    assert registered == [op.op_id for op in _ALL_OPS]
    for op in _ALL_OPS:
        handler = getattr(KeycloakConnector, op.handler_attr)
        assert getattr(handler, "realm_answer_checked", False), op.op_id
        assert op.op_id in _park_check._GATE_CHECKS, op.op_id


@pytest.mark.parametrize("op", _ALL_OPS, ids=[op.op_id for op in _ALL_OPS])
@pytest.mark.parametrize(
    "answer",
    [
        {"realm": _ANSWER_MARKER},
        {"realm": "Example"},
        {"realm": "example "},
        {"enabled": True},
        {"realm": None},
        ["example"],
    ],
    ids=["other-name", "other-case", "trailing-space", "no-name", "null-name", "not-object"],
)
async def test_mismatch_refuses_and_sends_nothing_more(
    op: Any, answer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    connector = KeycloakConnector()
    recorder = _Recorder(answer)
    recorder.install(connector, monkeypatch)
    handler = getattr(connector, op.handler_attr)
    with pytest.raises(KeycloakRealmAnswerError) as caught:
        await handler(_operator(), _Target(), {})
    text = str(caught.value)
    assert "keycloak-a" in text
    assert "managed_realm" in text
    assert _ANSWER_MARKER not in text
    assert recorder.reads == [f"/admin/realms/{_MANAGED}"]
    assert recorder.sent == []


async def test_same_name_lets_the_operation_run(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = KeycloakConnector()
    recorder = _Recorder({"realm": _MANAGED, "enabled": True})
    recorder.install(connector, monkeypatch)
    result = await connector.realm_get(_operator(), _Target(), {})
    assert recorder.reads[0] == f"/admin/realms/{_MANAGED}"
    assert result is not None


_ABSENT_OK = ("keycloak.realm.create", "keycloak.realm.delete")


class _ReadFails(_Recorder):
    """The read of the managed realm answers with an HTTP error status."""

    def __init__(self, status: int) -> None:
        super().__init__(None)
        self.status = status

    def install(self, connector: KeycloakConnector, monkeypatch: pytest.MonkeyPatch) -> None:
        super().install(connector, monkeypatch)

        async def _get_json(_target: Any, path: str, **_kw: Any) -> Any:
            self.reads.append(path)
            request = httpx.Request("GET", f"https://keycloak.example.test{path}")
            response = httpx.Response(self.status, request=request)
            raise httpx.HTTPStatusError("read failed", request=request, response=response)

        monkeypatch.setattr(connector, "_get_admin_json", _get_json)


def _stub_realm_writes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    ran: list[str] = []

    async def _create(*_args: Any) -> dict[str, Any]:
        ran.append("keycloak.realm.create")
        return {}

    async def _delete(*_args: Any) -> dict[str, Any]:
        ran.append("keycloak.realm.delete")
        return {}

    monkeypatch.setattr(ops_write, "keycloak_realm_create", _create)
    monkeypatch.setattr(ops_write_realm_delete, "keycloak_realm_delete", _delete)
    return ran


_OTHER_OPS = [op for op in _ALL_OPS if op.op_id not in _ABSENT_OK]


@pytest.mark.parametrize("op", _OTHER_OPS, ids=[op.op_id for op in _OTHER_OPS])
async def test_absent_managed_realm_refuses_other_ops(
    op: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    connector = KeycloakConnector()
    recorder = _ReadFails(404)
    recorder.install(connector, monkeypatch)
    with pytest.raises(httpx.HTTPStatusError):
        await getattr(connector, op.handler_attr)(_operator(), _Target(), {})
    assert recorder.reads == [f"/admin/realms/{_MANAGED}"]
    assert recorder.sent == []


@pytest.mark.parametrize("op_id", _ABSENT_OK)
async def test_absent_managed_realm_lets_realm_create_and_delete_run(
    op_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    connector = KeycloakConnector()
    recorder = _ReadFails(404)
    recorder.install(connector, monkeypatch)
    ran = _stub_realm_writes(monkeypatch)
    await getattr(connector, _HANDLER[op_id])(_operator(), _Target(), {})
    assert recorder.reads == [f"/admin/realms/{_MANAGED}"]
    assert ran == [op_id]


@pytest.mark.parametrize("op_id", _ABSENT_OK)
@pytest.mark.parametrize("status", [401, 403, 500])
async def test_realm_create_and_delete_refuse_other_read_errors(
    op_id: str, status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    connector = KeycloakConnector()
    recorder = _ReadFails(status)
    recorder.install(connector, monkeypatch)
    ran = _stub_realm_writes(monkeypatch)
    with pytest.raises(httpx.HTTPStatusError):
        await getattr(connector, _HANDLER[op_id])(_operator(), _Target(), {})
    assert ran == []
    assert recorder.sent == []


@pytest.mark.parametrize("op_id", _ABSENT_OK)
async def test_realm_create_and_delete_refuse_a_mismatch(
    op_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    connector = KeycloakConnector()
    _Recorder({"realm": _ANSWER_MARKER}).install(connector, monkeypatch)
    ran = _stub_realm_writes(monkeypatch)
    with pytest.raises(KeycloakRealmAnswerError):
        await getattr(connector, _HANDLER[op_id])(_operator(), _Target(), {})
    assert ran == []


# ---------------------------------------------------------------------------
# Before the wait for approval: the dispatcher and preview_operation
# ---------------------------------------------------------------------------

_CONNECTOR_ID = "keycloak-admin-26.x"
_KC_HOST = "keycloak-answer.test.invalid"
_KC_BASE_URL = f"https://{_KC_HOST}"
_TARGET_NAME = "keycloak-answer"
_REALM_PATH = f"/admin/realms/{_MANAGED}"
_ID = "0a0b0c0d-0000-0000-0000-000000000001"
_GROUP_ID = "77777777-7777-7777-7777-777777777777"
_SCOPE_ID = "88888888-8888-8888-8888-888888888888"
_MAPPER_ID = "99999999-9999-9999-9999-999999999999"
_VAULT = "vault:secret/meho/example#password"
_USER = {"id": _ID}

#: Valid params of every operation, so each call passes the params check.
_PARAMS: dict[str, dict[str, Any]] = {
    "keycloak.client.get": {"id": _ID},
    "keycloak.role_mapping.get": {"id": _ID},
    "keycloak.role.users": {"name": "example-role"},
    "keycloak.group.member.list": {"id": _ID},
    "keycloak.user.get": {"id": _ID},
    "keycloak.user.sessions": {"id": _ID},
    "keycloak.user.lock_status": {"id": _ID},
    "keycloak.user.otp_status": {"username": "alice"},
    "keycloak.authflow.ensure_approval_flow": {"client_id": "meho-console"},
    "keycloak.authflow.ensure_approval_touch": {"client_id": "meho-web"},
    "keycloak.client.create": {"representation": {"clientId": "new-app"}},
    "keycloak.client.delete": {"client_id": "old-app"},
    "keycloak.client.update": {
        "client_id": "meho-console",
        "representation": {"redirectUris": ["https://x.example/*"]},
    },
    "keycloak.client_scope.create": {"representation": {"name": "new-scope"}},
    "keycloak.client_scope.protocol_mapper.update": {
        "scope_id": _SCOPE_ID,
        "mapper_id": _MAPPER_ID,
        "representation": {"config": {"claim.value": "y"}},
    },
    "keycloak.group.create": {"name": "ops"},
    "keycloak.group.delete": {"id": _GROUP_ID, "delete_subgroups": True},
    "keycloak.group.member.add": {"group_id": _GROUP_ID, "user_id": _ID},
    "keycloak.group.member.remove": {"group_id": _GROUP_ID, "user_id": _ID},
    "keycloak.group.update_attributes": {"id": _GROUP_ID, "attributes": {"team": ["a"]}},
    "keycloak.protocol_mapper.create": {
        "client_id": "meho-console",
        "representation": {
            "name": "a-claim",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-hardcoded-claim-mapper",
            "config": {"claim.name": "team", "claim.value": "x"},
        },
    },
    "keycloak.realm.create": {"representation": {"realm": _MANAGED}},
    "keycloak.realm.delete": {"realm": _MANAGED},
    "keycloak.realm.update": {"realm": _MANAGED, "representation": {"displayName": "New"}},
    "keycloak.role_mapping.assign": {**_USER, "roles": ["viewer"]},
    "keycloak.role_mapping.client.assign": {**_USER, "client_id": "app", "roles": ["v"]},
    "keycloak.role_mapping.client.remove": {**_USER, "client_id": "app", "roles": ["v"]},
    "keycloak.user.check_login": {"username": "alice", "password_secret_ref": _VAULT},
    "keycloak.user.create": {"representation": {"username": "new-login"}},
    "keycloak.user.credential.delete": {**_USER, "credential_type": "otp"},
    "keycloak.user.delete": dict(_USER),
    "keycloak.user.disable": dict(_USER),
    "keycloak.user.enable": dict(_USER),
    "keycloak.user.logout": dict(_USER),
    "keycloak.user.required_actions.update": {**_USER, "add": ["CONFIGURE_TOTP"]},
    "keycloak.user.reset_password": {**_USER, "password_secret_ref": _VAULT},
    "keycloak.user.update": {**_USER, "changes": {"email": "a@b.example"}},
}
_ALL_OP_IDS = [op.op_id for op in _ALL_OPS]

#: The operations whose approval card or preview reads Keycloak.
_CARD_OPS = [
    "keycloak.client.delete",
    "keycloak.client.update",
    "keycloak.client_scope.protocol_mapper.update",
    "keycloak.group.delete",
    "keycloak.group.member.add",
    "keycloak.group.member.remove",
    "keycloak.group.update_attributes",
    "keycloak.protocol_mapper.create",
    "keycloak.realm.update",
    "keycloak.role_mapping.assign",
    "keycloak.role_mapping.client.assign",
    "keycloak.role_mapping.client.remove",
    "keycloak.user.disable",
    "keycloak.user.enable",
    "keycloak.user.logout",
    "keycloak.user.required_actions.update",
    "keycloak.user.reset_password",
    "keycloak.user.update",
]

#: Answers for the managed realm that are not exactly the managed realm.
_WRONG_ANSWERS: list[Any] = [
    {"realm": "Example", "displayName": _ANSWER_MARKER},
    {"realm": "exämple", "displayName": _ANSWER_MARKER},
    {"displayName": _ANSWER_MARKER},
    {},
    [_ANSWER_MARKER],
]


def _stub_loader(_target: Any, _operator: Operator) -> Any:
    async def _load() -> KeycloakAdminCredentials:
        return KeycloakClientCredentials(client_id="admin-cli", client_secret="stub-secret")

    return _load()


@pytest.fixture
def cards(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[bool]:
    """The cards switch, on or off (``indirect`` param), and the settings a dispatch needs."""
    on = bool(getattr(request, "param", False))
    monkeypatch.setenv("KEYCLOAK_RIGHTS_CHANGE_CARDS", "true" if on else "false")
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_dispatcher_caches()
    yield on
    reset_dispatcher_caches()
    get_settings.cache_clear()


@pytest.fixture
async def seeded(cards: bool) -> AsyncIterator[Operator]:
    """Register the ops, seed a tenant and a Keycloak target that manages one realm."""
    set_default_reducer(PassThroughReducer())
    await KeycloakConnector.register_operations()
    tenant_id = uuid4()
    async with get_sessionmaker()() as session:
        session.add(Tenant(id=tenant_id, slug=f"kc-answer-{tenant_id.hex[:8]}", name="Answer"))
        await session.commit()
    async with get_sessionmaker()() as session:
        session.add(
            TargetORM(
                tenant_id=tenant_id,
                name=_TARGET_NAME,
                aliases=[],
                product="keycloak",
                host=_KC_HOST,
                port=443,
                fqdn=None,
                secret_ref="example/keycloak/admin",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={"managed_realm": _MANAGED},
                fingerprint={"version": "26.0.5"},
                notes="seeded by test_keycloak_realm_answer_check",
            )
        )
        await session.commit()
    connector = KeycloakConnector(credentials_loader=_stub_loader)
    _CONNECTOR_INSTANCE_CACHE[KeycloakConnector] = connector  # type: ignore[assignment]
    yield Operator(
        sub="keycloak-answer-operator",
        name="Keycloak answer operator",
        email=None,
        raw_jwt="<keycloak-answer-raw-jwt>",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )
    await connector.aclose()


class _Keycloak:
    """A fake Keycloak: the managed realm read answers *answer*, every other read the marker."""

    def __init__(self, answer: Any, *, status: int = 200) -> None:
        self.answer = answer
        self.status = status
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/protocol/openid-connect/token") and "/realms/master/" in path:
            return httpx.Response(200, json={"access_token": "kc-token", "expires_in": 300})
        self.calls.append(f"{request.method} {path}")
        if request.method == "GET" and path == _REALM_PATH:
            if self.status != 200:
                return httpx.Response(self.status)
            return httpx.Response(200, json=self.answer)
        if request.method == "GET":
            return httpx.Response(200, json={"id": _ID, "name": _ANSWER_MARKER, "realm": _MANAGED})
        return httpx.Response(204)


async def _resolved_target(operator: Operator) -> Any:
    async with get_sessionmaker()() as session:
        return await resolve_target(session, operator.tenant_id, _TARGET_NAME)


async def _call(operator: Operator, op_id: str, keycloak: _Keycloak) -> Any:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        mock.route().mock(side_effect=keycloak)
        return await dispatch(
            operator=operator,
            connector_id=_CONNECTOR_ID,
            op_id=op_id,
            target=await _resolved_target(operator),
            params=dict(_PARAMS.get(op_id, {})),
        )


async def _preview(operator: Operator, op_id: str, keycloak: _Keycloak) -> dict[str, Any]:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        mock.route().mock(side_effect=keycloak)
        return await preview_dispatch(
            operator=operator,
            connector_id=_CONNECTOR_ID,
            op_id=op_id,
            target=await _resolved_target(operator),
            params=dict(_PARAMS.get(op_id, {})),
        )


async def _descriptor(op_id: str) -> EndpointDescriptor:
    async with get_sessionmaker()() as session:
        return (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == op_id).limit(1)
            )
        ).scalar_one()


async def _approval_rows() -> list[Any]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(ApprovalRequest))).scalars().all())


async def _audit_text(operator: Operator) -> str:
    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.tenant_id == operator.tenant_id)
                )
            )
            .scalars()
            .all()
        )
    return json.dumps(
        [{c.name: getattr(row, c.name) for c in row.__table__.columns} for row in rows],
        default=str,
    )


_REFUSAL = "did not answer with the realm this target manages"


def _assert_refused(result_text: str, error: str | None, keycloak: _Keycloak) -> None:
    assert error is not None
    assert _REFUSAL in error
    assert _ANSWER_MARKER not in result_text
    # Only the check read reached Keycloak: no read of the card, the preview or
    # the policy gate, and no write.
    assert keycloak.calls == [f"GET {_REALM_PATH}"]


@pytest.mark.parametrize("cards", [False, True], ids=["cards-off", "cards-on"], indirect=True)
@pytest.mark.parametrize("op_id", _ALL_OP_IDS)
async def test_a_wrong_realm_answer_is_refused_before_the_wait_for_approval(
    seeded: Operator, op_id: str
) -> None:
    keycloak = _Keycloak({"realm": _ANSWER_MARKER, "displayName": _ANSWER_MARKER})
    assert (
        _validate_params_envelope(
            descriptor=await _descriptor(op_id),
            connector_id=_CONNECTOR_ID,
            op_id=op_id,
            params=dict(_PARAMS.get(op_id, {})),
        )
        is None
    ), "the test params must pass the params check"
    with capture_logs() as logs:
        result = await _call(seeded, op_id, keycloak)
    dumped = json.dumps(result.model_dump(mode="json"), default=str)
    assert result.status == "denied"
    assert result.extras["error_code"] == "refused_before_approval"
    _assert_refused(dumped, result.error, keycloak)
    assert await _approval_rows() == []
    assert _ANSWER_MARKER not in await _audit_text(seeded)
    assert _ANSWER_MARKER not in json.dumps(logs, default=str)


@pytest.mark.parametrize("cards", [False, True], ids=["cards-off", "cards-on"], indirect=True)
@pytest.mark.parametrize("op_id", _ALL_OP_IDS)
async def test_the_preview_gives_the_same_refusal(seeded: Operator, op_id: str) -> None:
    keycloak = _Keycloak({"realm": _ANSWER_MARKER, "displayName": _ANSWER_MARKER})
    envelope = await _preview(seeded, op_id, keycloak)
    assert envelope["extras"]["error_code"] == "refused_before_approval"
    assert envelope["policy_verdict"] == "deny"
    _assert_refused(json.dumps(envelope, default=str), envelope["error"], keycloak)
    assert await _approval_rows() == []


@pytest.mark.parametrize("cards", [True], ids=["cards-on"], indirect=True)
@pytest.mark.parametrize("op_id", _CARD_OPS)
async def test_every_wrong_answer_stops_the_card_and_the_preview(
    seeded: Operator, op_id: str
) -> None:
    for answer in _WRONG_ANSWERS:
        keycloak = _Keycloak(answer)
        result = await _call(seeded, op_id, keycloak)
        assert result.extras["error_code"] == "refused_before_approval", answer
        _assert_refused(json.dumps(result.model_dump(mode="json")), result.error, keycloak)
        keycloak = _Keycloak(answer)
        envelope = await _preview(seeded, op_id, keycloak)
        assert envelope["extras"]["error_code"] == "refused_before_approval", answer
        _assert_refused(json.dumps(envelope, default=str), envelope["error"], keycloak)
    assert await _approval_rows() == []


@pytest.mark.parametrize("cards", [False, True], ids=["cards-off", "cards-on"], indirect=True)
@pytest.mark.parametrize("op_id", sorted(_ABSENT_OK))
async def test_an_absent_managed_realm_lets_realm_create_and_delete_build_their_card(
    seeded: Operator, op_id: str
) -> None:
    keycloak = _Keycloak(None, status=404)
    result = await _call(seeded, op_id, keycloak)
    assert _REFUSAL not in (result.error or "")
    assert result.extras.get("error_code") != "refused_before_approval" or (
        _REFUSAL not in str(result.extras.get("reason"))
    )
    if op_id == "keycloak.realm.create":
        assert result.status == "awaiting_approval", result.error
        assert len(await _approval_rows()) == 1


@pytest.mark.parametrize("cards", [False, True], ids=["cards-off", "cards-on"], indirect=True)
async def test_the_right_realm_lets_the_card_hold_the_values_it_reads(
    seeded: Operator, cards: bool
) -> None:
    keycloak = _Keycloak({"realm": _MANAGED, "displayName": "value-now"})
    result = await _call(seeded, "keycloak.realm.update", keycloak)
    assert result.status == "awaiting_approval", result.error
    (row,) = await _approval_rows()
    assert "value-now" in json.dumps(row.proposed_effect, default=str)
    # The check read runs first, before the reads of the policy gate and the card.
    assert keycloak.calls[0] == f"GET {_REALM_PATH}"


# ---------------------------------------------------------------------------
# keycloak.realm.update with a ``realm`` value
# ---------------------------------------------------------------------------


class _RealmUpdateKeycloak(_Keycloak):
    """The first read of the realm answers with the managed realm, the next ones with *later*."""

    def __init__(self, later: Any) -> None:
        super().__init__({"realm": _MANAGED})
        self.later = later
        self.reads = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == _REALM_PATH:
            self.calls.append(f"GET {request.url.path}")
            self.reads += 1
            return httpx.Response(200, json=self.answer if self.reads == 1 else self.later)
        return super().__call__(request)


async def _approved_update(operator: Operator, params: dict[str, Any], keycloak: _Keycloak) -> Any:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        mock.route().mock(side_effect=keycloak)
        return await dispatch(
            operator=operator,
            connector_id=_CONNECTOR_ID,
            op_id="keycloak.realm.update",
            target=await _resolved_target(operator),
            params=params,
            _approved=True,
        )


def _puts(keycloak: _Keycloak) -> list[str]:
    return [call for call in keycloak.calls if call.startswith("PUT ")]


@pytest.mark.parametrize("cards", [False], ids=["cards-off"], indirect=True)
async def test_a_realm_value_that_names_another_realm_is_refused_before_approval(
    seeded: Operator,
) -> None:
    keycloak = _Keycloak({"realm": _MANAGED})
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        mock.route().mock(side_effect=keycloak)
        result = await dispatch(
            operator=seeded,
            connector_id=_CONNECTOR_ID,
            op_id="keycloak.realm.update",
            target=await _resolved_target(seeded),
            params={"realm": _ANSWER_MARKER, "representation": {"displayName": "New"}},
        )
    assert result.extras["error_code"] == "refused_before_approval"
    assert _ANSWER_MARKER not in (result.error or "")
    assert _puts(keycloak) == []
    assert await _approval_rows() == []


@pytest.mark.parametrize("cards", [False], ids=["cards-off"], indirect=True)
@pytest.mark.parametrize(
    "later",
    [
        {"realm": _ANSWER_MARKER, "displayName": _ANSWER_MARKER},
        {"realm": "Example", "displayName": _ANSWER_MARKER},
        {"displayName": _ANSWER_MARKER},
        [_ANSWER_MARKER],
    ],
    ids=["other-name", "other-case", "no-name", "not-object"],
)
async def test_the_named_realm_must_answer_with_its_own_name(seeded: Operator, later: Any) -> None:
    keycloak = _RealmUpdateKeycloak(later)
    params = {"realm": _MANAGED, "representation": {"displayName": "New"}}
    result = await _approved_update(seeded, params, keycloak)
    assert result.status == "error"
    assert _ANSWER_MARKER not in json.dumps(result.model_dump(mode="json"), default=str)
    assert _puts(keycloak) == []


@pytest.mark.parametrize("cards", [False], ids=["cards-off"], indirect=True)
@pytest.mark.parametrize(
    "params",
    [
        {"realm": _MANAGED, "representation": {"displayName": "New"}},
        {"representation": {"displayName": "New"}},
    ],
    ids=["named-realm", "no-realm-value"],
)
async def test_a_realm_that_answers_with_its_own_name_is_updated_once(
    seeded: Operator, params: dict[str, Any]
) -> None:
    keycloak = _RealmUpdateKeycloak({"realm": _MANAGED, "displayName": "Old"})
    result = await _approved_update(seeded, params, keycloak)
    assert result.status == "ok", result.error
    assert _puts(keycloak) == [f"PUT {_REALM_PATH}"]


@pytest.mark.parametrize("cards", [True], ids=["cards-on"], indirect=True)
async def test_the_realm_update_card_holds_no_value_of_another_realm(seeded: Operator) -> None:
    keycloak = _RealmUpdateKeycloak({"realm": "Example", "displayName": _ANSWER_MARKER})
    result = await _call(seeded, "keycloak.realm.update", keycloak)
    assert result.status == "awaiting_approval", result.error
    (row,) = await _approval_rows()
    assert _ANSWER_MARKER not in json.dumps(row.proposed_effect, default=str)


# ---------------------------------------------------------------------------
# keycloak.user.check_login reads no password for a wrong realm answer
# ---------------------------------------------------------------------------


async def test_check_login_reads_no_password_and_sends_no_login_for_a_wrong_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = KeycloakConnector()
    recorder = _Recorder({"realm": "Example"})
    recorder.install(connector, monkeypatch)
    vault_reads: list[str] = []

    async def _password(*_args: Any, **_kw: Any) -> str:
        vault_reads.append("read")
        return "not-used"

    monkeypatch.setattr(ops_login_check, "_read_password_from_vault", _password)
    with pytest.raises(KeycloakRealmAnswerError):
        await connector.user_check_login(_operator(), _Target(), _PARAMS[_CHECK_LOGIN])
    assert vault_reads == []
    assert recorder.reads == [f"/admin/realms/{_MANAGED}"]
    assert recorder.sent == []
