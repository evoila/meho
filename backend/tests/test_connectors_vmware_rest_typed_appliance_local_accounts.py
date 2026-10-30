# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the vCenter appliance local-account typed operations.

The four operations read the appliance's own operating-system accounts
(list, get), set a new password from a Vault path (password.set) and change
the password expiry rules (policy.set). The handler tests use a fake
connector that records every call. No live vCenter is used. The full
password change through the dispatcher, the approval queue and the real
connector is in ``test_connectors_vmware_rest_appliance_password_e2e.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors.base import ConnectorResourceNotFoundError, ConnectorUpstreamError
from meho_backplane.connectors.vmware_rest import appliance_local_account_password as password_mod
from meho_backplane.connectors.vmware_rest import appliance_local_accounts as accounts
from meho_backplane.connectors.vmware_rest.appliance_local_account_password import (
    PASSWORD_SET_OP_ID,
    LocalAccountPasswordSecretError,
    change_error,
    password_set_preview,
    sanitized_status_error,
)
from meho_backplane.connectors.vmware_rest.appliance_local_accounts import (
    ACCOUNT_FIELDS,
    GET_OP_ID,
    LIST_OP_ID,
    POLICY_SET_OP_ID,
    expiry_status,
    local_account_get_impl,
    local_account_list_impl,
    local_account_password_set_impl,
    local_account_policy_set_impl,
    policy_body,
)
from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
from meho_backplane.connectors.vmware_rest.typed_ops import (
    VMWARE_TYPED_OPS,
    VMWARE_TYPED_WHEN_TO_USE_BY_GROUP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_appliance_local_accounts import (
    APPLIANCE_LOCAL_ACCOUNTS_GROUP_KEY,
    APPLIANCE_LOCAL_ACCOUNTS_OPS,
)
from meho_backplane.operations._errors import result_connector_error
from meho_backplane.operations._preview import _PREVIEW_BUILDERS, PreviewContext
from meho_backplane.operations._validate import validate_params
from meho_backplane.redaction.flight_recorder import SECRET_FAMILY_OMITTED_MARKER
from meho_backplane.redaction.flight_recorder.span import redact_span

_MOUNT = "/api"
_LIST = "/api/appliance/local-accounts"
_ROOT = "/api/appliance/local-accounts/root"
_ALICE = "/api/appliance/local-accounts/alice"
_NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
#: A made-up new password with characters that JSON and URLs escape.
_NEW_PASSWORD = 'Neu-Pa"ss/w0rd+&<x>'
_REF = "<vault-path-of-the-new-password>"

_ROOT_INFO: dict[str, Any] = {
    "roles": ["superAdmin"],
    "enabled": True,
    "has_password": True,
    "last_password_change": "2026-07-25T00:00:00.000Z",
    "password_expires_at": "2026-10-23T00:00:00.000Z",
    "inactive_at": "2026-10-30T00:00:00.000Z",
    "min_days_between_password_change": 0,
    "max_days_between_password_change": 90,
    "warn_days_before_password_expiration": 7,
}
_GROUP = "SystemConfiguration.BashShellAdministrators"
#: What vCenter answers to a caller without the SSO group.
_UNAUTHORIZED_BODY: dict[str, Any] = {
    "error_type": "UNAUTHORIZED",
    "messages": [
        {
            "default_message": "Unable to authorize user",
            "id": "vapi.security.authorization.invalid",
        }
    ],
}
_FAILED_FETCH_USER_BODY: dict[str, Any] = {
    "error_type": "ERROR",
    "messages": [
        {
            "default_message": "Failed to get user details for root.",
            "id": "com.vmware.appliance.failed_fetch_user",
        }
    ],
}
_ALICE_INFO: dict[str, Any] = {
    "roles": ["operator"],
    "enabled": True,
    "has_password": True,
    "fullname": "Alice Example",
}


@pytest.fixture(autouse=True)
def _fixed_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(accounts, "_now", lambda: _NOW)


def _http_error(status: int, verb: str, path: str, body: Any = None) -> httpx.HTTPStatusError:
    request = httpx.Request(verb, f"https://vc.test.invalid{path}")
    if body is None:
        response = httpx.Response(status, request=request)
    else:
        response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


class _InOrder(list[Any]):
    """Answers for one ``(verb, path)`` that the fake gives out one per call."""


@dataclass
class _FakeConnector:
    """Answers per ``(verb, path)``; an :class:`_InOrder` is used in order; records every call."""

    answers: dict[tuple[str, str], Any] = field(default_factory=dict)
    calls: list[tuple[str, str, Any]] = field(default_factory=list)

    async def mount_op_path(self, target: Any, path: str, operator: Any) -> str:
        return f"{_MOUNT}{path}"

    async def _get_json(self, target: Any, path: str, *, operator: Any, params: Any = None) -> Any:
        self.calls.append(("GET", path, None))
        return self._answer("GET", path)

    async def _post_json(
        self, target: Any, path: str, *, operator: Any, verb: str = "POST", json: Any = None
    ) -> Any:
        self.calls.append((verb, path, json))
        return self._answer(verb, path)

    def _answer(self, verb: str, path: str) -> Any:
        answer = self.answers.get((verb, path), {})
        if isinstance(answer, _InOrder):
            answer = answer.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


@dataclass
class _Target:
    name: str = "vc-test"
    host: str = "vc.test.invalid"
    port: int | None = 443
    secret_ref: str | None = "targets/vc-test"
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=7))


def _operator() -> Operator:
    return Operator(
        sub="op-local-accounts",
        name="Local Accounts Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID(int=7),
        tenant_role=TenantRole.OPERATOR,
    )


def _vault(monkeypatch: pytest.MonkeyPatch, data: dict[str, Any]) -> list[dict[str, Any]]:
    """Replace the Vault read; return the list of recorded reads."""
    reads: list[dict[str, Any]] = []

    async def _load(target: Any, operator: Any, *, param: str, mount: str) -> dict[str, Any]:
        reads.append({"secret_ref": target.secret_ref, "param": param, "mount": mount})
        return data

    monkeypatch.setattr(password_mod, "load_param_secret_data", _load)
    return reads


def _op(op_id: str) -> Any:
    return next(op for op in APPLIANCE_LOCAL_ACCOUNTS_OPS if op.op_id == op_id)


def _password_change_error(exc: httpx.HTTPStatusError) -> Exception:
    return change_error(
        exc,
        op_id=PASSWORD_SET_OP_ID,
        action="set the new password",
        username="root",
        secrets=(_NEW_PASSWORD,),
    )


# ---------------------------------------------------------------------------
# Registration, risk levels and schemas
# ---------------------------------------------------------------------------


def test_the_four_ops_register_with_their_risk_levels() -> None:
    by_id = {op.op_id: op for op in VMWARE_TYPED_OPS}
    for op_id in (LIST_OP_ID, GET_OP_ID):
        assert by_id[op_id].safety_level == "safe"
        assert by_id[op_id].requires_approval is False
    for op_id in (PASSWORD_SET_OP_ID, POLICY_SET_OP_ID):
        assert by_id[op_id].safety_level == "dangerous"
        assert by_id[op_id].requires_approval is True
    for op in APPLIANCE_LOCAL_ACCOUNTS_OPS:
        assert op.group_key == APPLIANCE_LOCAL_ACCOUNTS_GROUP_KEY
        assert callable(getattr(VmwareRestConnector, op.handler_attr))
    assert VMWARE_TYPED_WHEN_TO_USE_BY_GROUP[APPLIANCE_LOCAL_ACCOUNTS_GROUP_KEY]


def test_only_the_password_change_is_a_credential_write() -> None:
    assert classify_op(PASSWORD_SET_OP_ID) == "credential_write"
    assert classify_op(POLICY_SET_OP_ID) != "credential_write"
    assert classify_op(LIST_OP_ID) == "read"
    assert classify_op(GET_OP_ID) == "read"


def test_no_parameter_can_carry_the_password_itself() -> None:
    schema = _op(PASSWORD_SET_OP_ID).parameter_schema
    assert set(schema["properties"]) == {
        "username",
        "password_secret_ref",
        "password_secret_mount",
        "password_secret_key",
    }
    assert schema["additionalProperties"] is False
    assert validate_params(schema, {"username": "root", "password": _NEW_PASSWORD})
    response_fields = set(_op(PASSWORD_SET_OP_ID).response_schema["properties"])
    assert response_fields == {"username", "password_set", "password_expires_at", "read_note"}


def _op_texts(op: Any) -> str:
    return " ".join([op.summary, op.description, json.dumps(op.llm_instructions)])


@pytest.mark.parametrize("op", APPLIANCE_LOCAL_ACCOUNTS_OPS, ids=lambda op: op.op_id)
def test_every_op_text_names_the_sso_group(op: Any) -> None:
    texts = _op_texts(op)
    assert _GROUP in texts
    assert "for this operation" in texts
    assert "Changes need" not in texts
    assert "for this change" not in texts


@pytest.mark.parametrize("op", APPLIANCE_LOCAL_ACCOUNTS_OPS, ids=lambda op: op.op_id)
def test_every_example_validates(op: Any) -> None:
    for example in op.parameter_schema["examples"]:
        assert validate_params(op.parameter_schema, example) == []


@pytest.mark.parametrize("username", ["", "..", ".root", "root/x", "a b", "root%2F", "x" * 65])
def test_bad_user_names_are_refused_by_the_schema(username: str) -> None:
    schema = _op(GET_OP_ID).parameter_schema
    assert validate_params(schema, {"username": username})


@pytest.mark.parametrize(
    "params",
    [
        {"username": "root"},
        {"username": "root", "max_days_between_password_change": 100000},
        {"username": "root", "warn_days_before_password_expiration": -2},
        {"username": "root", "password_expires": "no"},
        {"username": "root", "password": "x"},
    ],
)
def test_bad_policy_calls_are_refused_by_the_schema(params: dict[str, Any]) -> None:
    assert validate_params(_op(POLICY_SET_OP_ID).parameter_schema, params)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_reads_every_account_and_sorts_them() -> None:
    connector = _FakeConnector(
        answers={
            ("GET", _LIST): ["root", "alice", "root"],
            ("GET", _ROOT): _ROOT_INFO,
            ("GET", _ALICE): {"value": _ALICE_INFO},
        }
    )
    result = await local_account_list_impl(connector, _operator(), _Target(), {})  # type: ignore[arg-type]

    rows = result["accounts"]
    assert [row["username"] for row in rows] == ["alice", "root"]
    root = rows[1]
    assert list(root) == [*ACCOUNT_FIELDS, "read_note"]
    assert root["roles"] == ["superAdmin"]
    assert root["password_expires_at"] == "2026-10-23T00:00:00.000Z"
    assert root["password_expired"] is True
    assert root["days_until_password_expires"] == -2
    assert root["max_days_between_password_change"] == 90
    assert root["read_note"] is None
    alice = rows[0]
    assert alice["password_expired"] is False
    assert alice["days_until_password_expires"] is None
    assert "fullname" not in alice
    assert [call[1] for call in connector.calls] == [_LIST, _ALICE, _ROOT]


@pytest.mark.asyncio
async def test_list_keeps_going_when_one_account_read_fails() -> None:
    error = _http_error(500, "GET", _ROOT, {"error_type": "ERROR", "messages": []})
    connector = _FakeConnector(
        answers={
            ("GET", _LIST): {"value": ["root", "alice"]},
            ("GET", _ROOT): error,
            ("GET", _ALICE): _ALICE_INFO,
        }
    )
    result = await local_account_list_impl(connector, _operator(), _Target(), {})  # type: ignore[arg-type]

    root = next(row for row in result["accounts"] if row["username"] == "root")
    assert root["read_note"] == "the read failed: HTTP 500 (ERROR)"
    assert root["password_expired"] is None
    assert root["roles"] == []


@pytest.mark.asyncio
async def test_list_refused_names_the_sso_group_and_keeps_the_403() -> None:
    refusal = _http_error(403, "GET", _LIST, _UNAUTHORIZED_BODY)
    connector = _FakeConnector(answers={("GET", _LIST): refusal})
    with pytest.raises(ConnectorUpstreamError) as caught:
        await local_account_list_impl(connector, _operator(), _Target(), {})  # type: ignore[arg-type]
    error = caught.value
    assert error.http_status == 403
    assert str(error) == (
        f"{LIST_OP_ID}: vCenter answered HTTP 403 when MEHO tried to list the local accounts. "
        "vCenter refused the read. The service account needs the appliance super "
        f"administrator level: membership in the SSO group {_GROUP}. After a group change, "
        "MEHO's next login to vCenter (by default within 20 minutes) carries the new group."
    )
    assert error.upstream_message == "UNAUTHORIZED; Unable to authorize user"
    assert error.__context__ is None
    assert [call[1] for call in connector.calls] == [_LIST]


@pytest.mark.parametrize("status", [401, 500, 503])
@pytest.mark.asyncio
async def test_list_other_errors_are_raised_as_they_are(status: int) -> None:
    original = _http_error(status, "GET", _LIST, {"error_type": "ERROR", "messages": []})
    connector = _FakeConnector(answers={("GET", _LIST): original})
    with pytest.raises(httpx.HTTPStatusError) as caught:
        await local_account_list_impl(connector, _operator(), _Target(), {})  # type: ignore[arg-type]
    assert caught.value is original


@pytest.mark.parametrize(
    ("status", "body", "hint"),
    [
        (403, _UNAUTHORIZED_BODY, "vCenter refused the read. The service account needs"),
        (500, _FAILED_FETCH_USER_BODY, "The service account may lack"),
    ],
)
@pytest.mark.asyncio
async def test_list_row_of_a_refused_account_read_names_the_sso_group(
    status: int, body: dict[str, Any], hint: str
) -> None:
    connector = _FakeConnector(
        answers={
            ("GET", _LIST): ["root", "alice"],
            ("GET", _ROOT): _http_error(status, "GET", _ROOT, body),
            ("GET", _ALICE): _ALICE_INFO,
        }
    )
    result = await local_account_list_impl(connector, _operator(), _Target(), {})  # type: ignore[arg-type]

    root = next(row for row in result["accounts"] if row["username"] == "root")
    note = root["read_note"]
    assert note.startswith(f"the read failed: HTTP {status} ({body['error_type']}). {hint}")
    assert _GROUP in note
    alice = next(row for row in result["accounts"] if row["username"] == "alice")
    assert alice["read_note"] is None


@pytest.mark.asyncio
async def test_get_returns_one_row() -> None:
    connector = _FakeConnector(answers={("GET", _ROOT): _ROOT_INFO})
    row = await local_account_get_impl(
        connector,  # type: ignore[arg-type]
        _operator(),
        _Target(),
        {"username": "root"},
    )
    assert list(row) == list(ACCOUNT_FIELDS)
    assert row["username"] == "root"
    assert row["has_password"] is True
    assert row["inactive_at"] == "2026-10-30T00:00:00.000Z"


@pytest.mark.asyncio
async def test_get_of_a_missing_account_is_not_found() -> None:
    connector = _FakeConnector(answers={("GET", _ROOT): _http_error(404, "GET", _ROOT)})
    with pytest.raises(ConnectorResourceNotFoundError) as caught:
        await local_account_get_impl(
            connector,  # type: ignore[arg-type]
            _operator(),
            _Target(),
            {"username": "root"},
        )
    assert caught.value.resource_ids == ["root"]


@pytest.mark.parametrize(
    ("status", "body", "hint", "upstream"),
    [
        (
            403,
            _UNAUTHORIZED_BODY,
            "vCenter refused the read. The service account needs",
            "UNAUTHORIZED; Unable to authorize user",
        ),
        (
            500,
            _FAILED_FETCH_USER_BODY,
            "The service account may lack",
            "ERROR; Failed to get user details for root.",
        ),
    ],
)
@pytest.mark.asyncio
async def test_get_refused_names_the_sso_group_and_keeps_the_status(
    status: int, body: dict[str, Any], hint: str, upstream: str
) -> None:
    connector = _FakeConnector(answers={("GET", _ROOT): _http_error(status, "GET", _ROOT, body)})
    with pytest.raises(ConnectorUpstreamError) as caught:
        await local_account_get_impl(
            connector,  # type: ignore[arg-type]
            _operator(),
            _Target(),
            {"username": "root"},
        )
    error = caught.value
    assert error.http_status == status
    assert str(error).startswith(
        f"{GET_OP_ID}: vCenter answered HTTP {status} when MEHO tried to read the local "
        f"account 'root'. {hint}"
    )
    assert _GROUP in str(error)
    assert error.upstream_message == upstream
    assert error.__context__ is None


def test_a_refusal_reaches_the_caller_with_the_group_and_the_status() -> None:
    """The dispatcher cuts long messages; ``remediation`` keeps the group hint whole."""
    long_name = "a" * 64
    read_error = accounts._read_error(long_name, op_id=GET_OP_ID)(
        _http_error(500, "GET", _ROOT, _FAILED_FETCH_USER_BODY)
    )
    change = _password_change_error(_http_error(403, "PATCH", _ROOT, _UNAUTHORIZED_BODY))
    for op_id, error, status in ((GET_OP_ID, read_error, 500), (PASSWORD_SET_OP_ID, change, 403)):
        result = result_connector_error(op_id, error, 1.0)
        assert result.extras["http_status"] == status
        assert _GROUP in result.extras["remediation"]


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (401, _UNAUTHORIZED_BODY),
        (500, {"error_type": "ERROR", "messages": [{"default_message": "x", "id": "other"}]}),
        (503, None),
    ],
)
def test_get_other_errors_are_raised_as_they_are(status: int, body: Any) -> None:
    original = _http_error(status, "GET", _ROOT, body)
    assert accounts._read_error("root", op_id=GET_OP_ID)(original) is original


def test_a_legacy_rest_failed_fetch_user_answer_is_seen() -> None:
    body = {"type": "ERROR", "value": {"messages": _FAILED_FETCH_USER_BODY["messages"]}}
    error = accounts._read_error("root", op_id=GET_OP_ID)(_http_error(500, "GET", _ROOT, body))
    assert isinstance(error, ConnectorUpstreamError)
    assert "may lack" in str(error)


@pytest.mark.asyncio
async def test_a_bad_user_name_sends_no_request() -> None:
    connector = _FakeConnector()
    with pytest.raises(ValueError, match="username"):
        await local_account_get_impl(
            connector,  # type: ignore[arg-type]
            _operator(),
            _Target(),
            {"username": "../root"},
        )
    assert connector.calls == []


@pytest.mark.parametrize(
    ("expires_at", "expired", "days"),
    [
        (None, False, None),
        ("2026-10-23T00:00:00.000Z", True, -2),
        ("2026-10-24T12:00:00+00:00", True, 0),
        ("2026-10-31T13:00:00Z", False, 7),
        ("2026-10-31T13:00:00", False, 7),
        ("not a date", None, None),
    ],
)
def test_expiry_status(expires_at: str | None, expired: bool | None, days: int | None) -> None:
    assert expiry_status(expires_at, _NOW) == {
        "password_expired": expired,
        "days_until_password_expires": days,
    }


# ---------------------------------------------------------------------------
# Policy change
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_policy_set_sends_only_the_given_fields() -> None:
    after = {**_ROOT_INFO, "max_days_between_password_change": 9999}
    after["password_expires_at"] = "2054-03-11T00:00:00.000Z"
    connector = _FakeConnector(answers={("GET", _ROOT): _InOrder([_ROOT_INFO, after])})
    params = {
        "username": "root",
        "max_days_between_password_change": 9999,
        "password_expires": True,
    }

    result = await local_account_policy_set_impl(connector, _operator(), _Target(), params)  # type: ignore[arg-type]

    assert connector.calls == [
        ("GET", _ROOT, None),
        ("PATCH", _ROOT, {"password_expires": True, "max_days_between_password_change": 9999}),
        ("GET", _ROOT, None),
    ]
    assert result["requested"] == {
        "password_expires": True,
        "max_days_between_password_change": 9999,
    }
    assert result["before"]["max_days_between_password_change"] == 90
    assert result["after"]["max_days_between_password_change"] == 9999
    assert result["after"]["password_expired"] is False
    assert result["read_note"] is None


@pytest.mark.asyncio
async def test_policy_set_on_a_missing_account_changes_nothing() -> None:
    connector = _FakeConnector(answers={("GET", _ROOT): _http_error(404, "GET", _ROOT)})
    with pytest.raises(ConnectorResourceNotFoundError):
        await local_account_policy_set_impl(
            connector,  # type: ignore[arg-type]
            _operator(),
            _Target(),
            {"username": "root", "password_expires": False},
        )
    assert [call[0] for call in connector.calls] == ["GET"]


@pytest.mark.parametrize("status", [400, 401, 403, 500])
@pytest.mark.asyncio
async def test_policy_set_errors_hold_no_raw_vendor_bytes(status: int) -> None:
    body = {
        "error_type": "INVALID_ARGUMENT",
        "messages": [{"default_message": "Value out of range."}],
        "message": "raw vendor text",
    }
    original = _http_error(status, "PATCH", _ROOT, body)
    connector = _FakeConnector(answers={("GET", _ROOT): _ROOT_INFO, ("PATCH", _ROOT): original})
    with pytest.raises((httpx.HTTPStatusError, ConnectorUpstreamError)) as caught:
        await local_account_policy_set_impl(
            connector,  # type: ignore[arg-type]
            _operator(),
            _Target(),
            {"username": "root", "min_days_between_password_change": 1},
        )
    error = caught.value
    assert error is not original
    assert error.__cause__ is None
    assert error.__context__ is None
    assert "raw vendor text" not in str(error)
    if status == 401:
        # Still an HTTPStatusError with status 401, so the dispatcher logs in again.
        assert isinstance(error, httpx.HTTPStatusError)
        assert error.response.status_code == 401
        assert error.response.json() == {
            "error_type": "INVALID_ARGUMENT",
            "messages": [{"default_message": "Value out of range."}],
        }
        assert error.request.content == b""
    else:
        assert isinstance(error, ConnectorUpstreamError)
        assert error.http_status == status
        assert error.upstream_message == "INVALID_ARGUMENT; Value out of range."
        assert "change the expiry rules" in str(error)


@pytest.mark.asyncio
async def test_policy_set_with_a_refused_first_read_sends_no_change() -> None:
    refusal = _http_error(403, "GET", _ROOT, {"error_type": "UNAUTHORIZED"})
    connector = _FakeConnector(answers={("GET", _ROOT): refusal})
    with pytest.raises(ConnectorUpstreamError) as caught:
        await local_account_policy_set_impl(
            connector,  # type: ignore[arg-type]
            _operator(),
            _Target(),
            {"username": "root", "password_expires": False},
        )
    assert "read the account before the change" in str(caught.value)
    assert "SystemConfiguration.BashShellAdministrators" in str(caught.value)
    assert caught.value.__context__ is None
    assert [call[0] for call in connector.calls] == ["GET"]


def test_policy_body_refuses_an_empty_call_and_days_out_of_range() -> None:
    with pytest.raises(ValueError, match="at least one"):
        policy_body({"username": "root"})
    with pytest.raises(ValueError, match="between -1 and 99999"):
        policy_body({"username": "root", "days_after_password_expiration": 100000})
    assert policy_body({"username": "root", "inactive_after_password_expiration": False}) == {
        "inactive_after_password_expiration": False
    }


# ---------------------------------------------------------------------------
# Password change
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_password_set_reads_vault_and_sends_the_password_only_in_the_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads = _vault(monkeypatch, {"password": f"{_NEW_PASSWORD}\n"})
    after = {**_ROOT_INFO, "password_expires_at": "2027-01-22T00:00:00.000Z"}
    connector = _FakeConnector(answers={("GET", _ROOT): after})

    result = await local_account_password_set_impl(
        connector,  # type: ignore[arg-type]
        _operator(),
        _Target(),
        {"username": "root", "password_secret_ref": _REF},
    )

    assert reads == [{"secret_ref": _REF, "param": "password_secret_ref", "mount": "secret"}]
    assert connector.calls == [
        ("PATCH", _ROOT, {"password": _NEW_PASSWORD}),
        ("GET", _ROOT, None),
    ]
    assert result == {
        "username": "root",
        "password_set": True,
        "password_expires_at": "2027-01-22T00:00:00.000Z",
        "read_note": None,
    }
    assert _NEW_PASSWORD not in json.dumps(result)


@pytest.mark.asyncio
async def test_password_set_uses_the_mount_and_key_the_call_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads = _vault(monkeypatch, {"root_password": _NEW_PASSWORD, "password": "other"})
    connector = _FakeConnector(answers={("GET", _ROOT): _ROOT_INFO})
    await local_account_password_set_impl(
        connector,  # type: ignore[arg-type]
        _operator(),
        _Target(),
        {
            "username": "root",
            "password_secret_ref": _REF,
            "password_secret_mount": "kv",
            "password_secret_key": "root_password",
        },
    )
    assert reads[0]["mount"] == "kv"
    assert connector.calls[0] == ("PATCH", _ROOT, {"password": _NEW_PASSWORD})


@pytest.mark.parametrize("data", [{}, {"password": ""}, {"password": "  \n"}, {"password": 42}])
@pytest.mark.asyncio
async def test_password_set_without_a_password_in_vault_sends_nothing(
    monkeypatch: pytest.MonkeyPatch, data: dict[str, Any]
) -> None:
    _vault(monkeypatch, data)
    connector = _FakeConnector()
    with pytest.raises(LocalAccountPasswordSecretError) as caught:
        await local_account_password_set_impl(
            connector,  # type: ignore[arg-type]
            _operator(),
            _Target(),
            {"username": "root", "password_secret_ref": _REF},
        )
    assert _REF in str(caught.value)
    assert "'password'" in str(caught.value)
    assert connector.calls == []


@pytest.mark.asyncio
async def test_password_set_of_a_missing_account_is_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _vault(monkeypatch, {"password": _NEW_PASSWORD})
    connector = _FakeConnector(answers={("PATCH", _ROOT): _http_error(404, "PATCH", _ROOT)})
    with pytest.raises(ConnectorResourceNotFoundError) as caught:
        await local_account_password_set_impl(
            connector,  # type: ignore[arg-type]
            _operator(),
            _Target(),
            {"username": "root", "password_secret_ref": _REF},
        )
    assert _NEW_PASSWORD not in str(caught.value)


@pytest.mark.asyncio
async def test_password_set_401_stays_a_401_without_the_vendor_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _vault(monkeypatch, {"password": _NEW_PASSWORD})
    echo = {"error_type": "UNAUTHENTICATED", "message": f"bad password {_NEW_PASSWORD}"}
    original = _http_error(401, "PATCH", _ROOT, echo)
    connector = _FakeConnector(answers={("PATCH", _ROOT): original})
    with pytest.raises(httpx.HTTPStatusError) as caught:
        await local_account_password_set_impl(
            connector,  # type: ignore[arg-type]
            _operator(),
            _Target(),
            {"username": "root", "password_secret_ref": _REF},
        )
    error = caught.value
    assert error is not original
    assert error.response.status_code == 401
    assert error.response.content == b""  # vendor text held the password: all of it dropped
    assert error.request.content == b""
    assert error.__context__ is None
    assert _NEW_PASSWORD not in str(error)


def test_sanitized_401_drops_the_session_header_the_body_and_the_vendor_text() -> None:
    request = httpx.Request(
        "PATCH",
        f"https://vc.test.invalid{_ROOT}",
        headers={"vmware-api-session-id": "session-token-canary"},
        json={"password": _NEW_PASSWORD},
    )
    response = httpx.Response(
        401,
        json={"error_type": "UNAUTHENTICATED", "message": f"bad {_NEW_PASSWORD}"},
        request=request,
    )
    original = httpx.HTTPStatusError("HTTP 401", request=request, response=response)

    error = sanitized_status_error(original, secrets=(_NEW_PASSWORD,))

    assert type(error) is httpx.HTTPStatusError
    assert error.response.status_code == 401
    assert error.request.method == "PATCH"
    assert error.request.url == request.url
    assert error.request.content == b""
    assert "vmware-api-session-id" not in error.request.headers
    assert error.response.content == b""
    assert _NEW_PASSWORD not in str(error)


def test_sanitized_401_without_a_secret_keeps_only_the_vapi_error() -> None:
    body = {
        "error_type": "UNAUTHENTICATED",
        "messages": [{"default_message": "The session expired."}],
        "message": "raw vendor text",
    }
    error = sanitized_status_error(_http_error(401, "PATCH", _ROOT, body))
    assert error.response.json() == {
        "error_type": "UNAUTHENTICATED",
        "messages": [{"default_message": "The session expired."}],
    }


@pytest.mark.asyncio
async def test_password_set_refusal_never_carries_the_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _vault(monkeypatch, {"password": _NEW_PASSWORD})
    echo = {
        "error_type": "INVALID_ARGUMENT",
        "messages": [{"default_message": f"bad password {_NEW_PASSWORD}", "args": []}],
    }
    connector = _FakeConnector(answers={("PATCH", _ROOT): _http_error(400, "PATCH", _ROOT, echo)})
    with pytest.raises(ConnectorUpstreamError) as caught:
        await local_account_password_set_impl(
            connector,  # type: ignore[arg-type]
            _operator(),
            _Target(),
            {"username": "root", "password_secret_ref": _REF},
        )
    error = caught.value
    assert error.http_status == 400
    assert error.upstream_message is None
    assert error.__cause__ is None
    assert error.__context__ is None
    for text in (str(error), repr(error.args)):
        assert _NEW_PASSWORD not in text
        assert json.dumps(_NEW_PASSWORD)[1:-1] not in text


@pytest.mark.asyncio
async def test_password_set_read_after_failure_still_reports_the_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _vault(monkeypatch, {"password": _NEW_PASSWORD})
    failure = _http_error(500, "GET", _ROOT, {"error_type": f"ERROR {_NEW_PASSWORD}"})
    connector = _FakeConnector(answers={("GET", _ROOT): failure})
    result = await local_account_password_set_impl(
        connector,  # type: ignore[arg-type]
        _operator(),
        _Target(),
        {"username": "root", "password_secret_ref": _REF},
    )
    assert result["password_set"] is True
    assert result["password_expires_at"] is None
    assert result["read_note"] == (
        "the change is done, but the read after it failed: HTTP 500. "
        "Read the account with vmware.appliance.local_account.get."
    )


@pytest.mark.parametrize(
    "echo_form",
    [_NEW_PASSWORD, json.dumps(_NEW_PASSWORD)[1:-1], "Neu-Pa%22ss%2Fw0rd%2B%26%3Cx%3E"],
)
def test_change_error_drops_every_vendor_text_that_holds_the_password(echo_form: str) -> None:
    body = {"error_type": "INVALID_ARGUMENT", "messages": [{"default_message": f"x {echo_form}"}]}
    error = _password_change_error(_http_error(400, "PATCH", _ROOT, body))
    assert isinstance(error, ConnectorUpstreamError)
    assert error.upstream_message is None
    assert echo_form not in str(error)


def test_change_error_keeps_the_vendor_reason() -> None:
    body = {
        "error_type": "INVALID_ARGUMENT",
        "messages": [{"default_message": "The password does not meet the password rules."}],
    }
    error = _password_change_error(_http_error(400, "PATCH", _ROOT, body))
    assert isinstance(error, ConnectorUpstreamError)
    assert error.http_status == 400
    assert error.upstream_message == (
        "INVALID_ARGUMENT; The password does not meet the password rules."
    )
    assert "'root'" in str(error)


def test_change_error_403_names_the_sso_group() -> None:
    body = {"error_type": "UNAUTHORIZED", "messages": []}
    error = _password_change_error(_http_error(403, "PATCH", _ROOT, body))
    assert isinstance(error, ConnectorUpstreamError)
    assert "SystemConfiguration.BashShellAdministrators" in str(error)
    assert error.upstream_message == "UNAUTHORIZED"


def test_change_error_reads_the_legacy_rest_error_shape() -> None:
    body = {
        "type": "com.vmware.vapi.std.errors.invalid_argument",
        "value": {"messages": [{"default_message": "Password was used before."}]},
    }
    error = _password_change_error(_http_error(400, "PATCH", _ROOT, body))
    assert isinstance(error, ConnectorUpstreamError)
    assert error.upstream_message == (
        "com.vmware.vapi.std.errors.invalid_argument; Password was used before."
    )


# ---------------------------------------------------------------------------
# Approval preview and flight recorder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_approver_sees_the_account_and_the_vault_path_only() -> None:
    assert _PREVIEW_BUILDERS[PASSWORD_SET_OP_ID] is password_set_preview
    ctx = PreviewContext(
        descriptor=SimpleNamespace(op_id=PASSWORD_SET_OP_ID),  # type: ignore[arg-type]
        connector_instance=None,
        operator=_operator(),
        target=_Target(),
        params={"username": "root", "password_secret_ref": _REF},
    )
    preview = await password_set_preview(ctx)
    assert preview == {
        "change": "set a new password for one local account of the vCenter appliance",
        "username": "root",
        "password_source": "vault",
        "password_secret_ref": _REF,
        "password_secret_mount": "secret",
        "password_secret_key": "password",
    }


def test_the_flight_recorder_keeps_no_body_of_the_password_change() -> None:
    span = redact_span(
        op_id=PASSWORD_SET_OP_ID,
        connector_id="vmware-rest-9.0",
        method="PATCH",
        tags=_op(PASSWORD_SET_OP_ID).tags,
        request_body=json.dumps({"password": _NEW_PASSWORD}),
        request_content_type="application/json",
        response_body=json.dumps({"error_type": "INVALID_ARGUMENT", "x": _NEW_PASSWORD}),
        response_content_type="application/json",
    )
    assert span.body_recorded is False
    assert span.request_body == SECRET_FAMILY_OMITTED_MARKER
    assert _NEW_PASSWORD not in json.dumps(span.model_dump(), default=str)
