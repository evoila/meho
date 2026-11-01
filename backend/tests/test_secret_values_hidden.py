# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Answers hide secret values, only people may reveal them, and the audit stores fingerprints.

* ``vault.kv.read`` and ``vmware.composite.vm.guest.env.read`` return the
  names without the values by default, whatever the key names are.
* Every other operation, built in or from a connector package, hides the
  value of a secret-named field (``password``, ``api_token``,
  ``client_secret``) in its answer and in the rows stored behind a result
  handle. An operation declares the fields that hold no secret although
  their name says so (``register_plain_fields``); they stay.
* ``reveal_secret: true`` returns the raw values to a person, a PEM private
  key included. An agent, or a hosted agent run, is refused with an audit row.
* An operation that makes a new credential still returns it to the caller.
* ``audit_log.raw_payload`` stores a keyed fingerprint in place of every
  hidden value; the same value gives the same fingerprint in two rows.
* The free-text rules catch ``DB_PASSWORD=...`` style text and PEM private
  key blocks.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select

import meho_backplane.operations._audit as audit_module
from meho_backplane import connector_packages
from meho_backplane.audit_query.policy_replay import _comparable_recorded_manifest
from meho_backplane.auth.delegation import actor_delegation
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast.events import scrub_secret_named_values
from meho_backplane.connector_packages import record_installed_connector_packages
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.operations import (
    PassThroughReducer,
    dispatch,
    register_plain_fields,
    register_typed_operation,
    reset_dispatcher_caches,
    set_default_reducer,
)
from meho_backplane.operations._secret_values import (
    SECRET_VALUE_PLACEHOLDER,
    fingerprint_hidden_values,
    fingerprinted_paths,
    hide_secret_values,
    plain_fields_of,
    reveal_hidden_values,
)
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.redaction import redact
from meho_backplane.redaction.resolver import get_default_policy
from meho_backplane.settings import get_settings
from tests._result_store_helpers import MemoryResultStore

_HIDDEN = SECRET_VALUE_PLACEHOLDER
_HIDDEN_FIELD = "[REDACTED:param_name]"
_FINGERPRINT = "[FINGERPRINT:hmac-sha256:"

# Assembled from fragments so secret scanners do not flag the test source.
_PASSWORD = "planted" + "-password-1"
_TOKEN = "planted" + "-token-value-2"
_PIN = 12345678
_PEM_HEAD = "-----BEGIN " + "RSA PRIVATE KEY-----"
_PEM_TAIL = "-----END " + "RSA PRIVATE KEY-----"
_PEM_BODY = "MIIEplanted" + "keybodyline"
_PRIVATE_KEY = f"{_PEM_HEAD}\n{_PEM_BODY}\n{_PEM_TAIL}\n"

#: Key names that the name check alone does not see as secret.
_PLAIN_NAMES = (
    "key",
    "ssh_key",
    "value",
    "pass",
    "pin",
    "cert",
    "token_value",
    "secret_key",
    "privkey",
    "pem",
    "tls_key",
    "psk",
    "pat",
    "otp",
    "seed",
    "passcode",
    "pw",
    "community",
)


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
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


def _operator(principal_kind: PrincipalKind = PrincipalKind.USER) -> Operator:
    return Operator(
        sub="op-secret-values-test",
        name="Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a5ec"),
        tenant_role=TenantRole.OPERATOR,
        principal_kind=principal_kind,
    )


class _NoOpVaultConnector(Connector):
    product = "vault"
    version = "1.x"
    impl_id = "vault"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise NotImplementedError


async def _planted_secret_handler(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Module-level ``vault.kv.read`` stand-in with planted secrets."""
    data: dict[str, Any] = {name: f"planted-{name}-value" for name in _PLAIN_NAMES}
    data.update(
        {"password": _PASSWORD, "private_key": _PRIVATE_KEY, "token": _TOKEN, "number": _PIN}
    )
    return {"data": data, "version": 7}


async def _guest_env_handler(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Module-level guest environment read stand-in."""
    return {
        "vm": "vm-1",
        "process_manager_moid": "guestOperationsProcessManager",
        "variables": ["PATH=/usr/bin", f"DB_PASSWORD={_PASSWORD}", f"API_PAT={_TOKEN}"],
        "count": 3,
    }


async def _register(op_id: str, handler: Any, embedding_service: AsyncMock) -> None:
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=op_id,
        handler=handler,
        summary="Read a secret.",
        description="Read a secret.",
        parameter_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        safety_level="safe",
        when_to_use=None,
        embedding_service=embedding_service,
    )


async def _read(operator: Operator, *, reveal: bool, op_id: str = "vault.kv.read") -> Any:
    params: dict[str, Any] = {"path": "app/db"}
    if reveal:
        params["reveal_secret"] = True
    return await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id=op_id,
        target=None,
        params=params,
    )


async def _audit_rows(op_id: str) -> list[AuditLog]:
    async with get_sessionmaker()() as fresh:
        result = await fresh.execute(
            select(AuditLog).where(AuditLog.path == op_id).order_by(AuditLog.occurred_at)
        )
        return list(result.scalars().all())


def _planted_values() -> list[str]:
    return [_PASSWORD, _TOKEN, _PEM_BODY, str(_PIN)]


# ---------------------------------------------------------------------------
# Default view: names without values
# ---------------------------------------------------------------------------


def test_kv_read_hides_every_value_whatever_the_key_name() -> None:
    answer = {"data": dict.fromkeys(_PLAIN_NAMES, "x") | {"password": 1234}, "version": 2}
    shown = hide_secret_values("vault.kv.read", answer)
    assert shown["data"] == dict.fromkeys([*_PLAIN_NAMES, "password"], _HIDDEN)
    assert shown["version"] == 2


def test_kv_read_of_another_shape_is_hidden_whole() -> None:
    shown = hide_secret_values("vault.kv.read", {"secret": {"a": "x"}, "list": ["y", 3]})
    assert shown == {"secret": {"a": _HIDDEN}, "list": [_HIDDEN, _HIDDEN]}


def test_guest_env_read_keeps_names_and_hides_values() -> None:
    shown = hide_secret_values(
        "vmware.composite.vm.guest.env.read",
        {"vm": "vm-1", "variables": ["PATH=/usr/bin", "NOEQUALS"], "count": 2},
    )
    assert shown == {
        "vm": "vm-1",
        "variables": [f"PATH={_HIDDEN}", _HIDDEN],
        "count": 2,
    }


def test_guest_env_read_takes_the_names_from_the_raw_answer() -> None:
    """A free-text rule took a whole ``NAME=value`` entry apart; the raw entry has the name."""
    raw = {"vm": "vm-1", "variables": ["PATH=/usr/bin", f"DB_PASSWORD={_PASSWORD}"]}
    redacted = {"vm": "vm-1", "variables": ["PATH=/usr/bin", "[REDACTED:api_key]"]}
    shown = hide_secret_values("vmware.composite.vm.guest.env.read", redacted, raw=raw)
    assert shown["variables"] == [f"PATH={_HIDDEN}", f"DB_PASSWORD={_HIDDEN}"]
    # When the entries do not line up, the redacted entries are used.
    short = {"vm": "vm-1", "variables": ["PATH=/usr/bin"]}
    shown = hide_secret_values("vmware.composite.vm.guest.env.read", redacted, raw=short)
    assert shown["variables"] == [f"PATH={_HIDDEN}", _HIDDEN]


def test_reveal_puts_back_the_hidden_fields_and_keeps_the_other_redaction() -> None:
    raw = {"data": {"private_key": _PRIVATE_KEY, "pin": _PIN}, "note": f"Bearer {_TOKEN}"}
    redacted = {
        "data": {"private_key": "[REDACTED:pem_private_key]\n", "pin": _PIN},
        "note": "Bearer [REDACTED:bearer_token]",
    }
    shown = hide_secret_values("vault.kv.read", redacted, raw=raw)
    revealed = reveal_hidden_values(raw, redacted, shown)
    assert revealed == {
        "data": {"private_key": _PRIVATE_KEY, "pin": _PIN},
        "note": "Bearer [REDACTED:bearer_token]",
    }
    # A hidden subtree comes back whole; a key the raw answer lacks stays as redacted.
    nested = {"a": {"k": _PASSWORD}}
    assert reveal_hidden_values(nested, {"a": {"k": "[REDACTED:x]"}}, {"a": _HIDDEN}) == nested
    assert reveal_hidden_values({}, {"a": "b"}, {"a": "x"}) == {"a": "b"}


def test_name_scrub_hides_numbers_only_when_asked() -> None:
    answer = {"password": 12345678, "pin_set": True, "count": 3}
    unchanged, found = scrub_secret_named_values(answer)
    assert unchanged == answer
    assert found is False
    hidden, found = scrub_secret_named_values(answer, hide_numbers=True)
    assert hidden == {"password": "[REDACTED:param_name]", "pin_set": True, "count": 3}
    assert found is True


def test_other_ops_hide_secret_named_values_including_numbers() -> None:
    shown = hide_secret_values("demo.thing.list", {"password": 1234, "name": "a"})
    assert shown == {"password": "[REDACTED:param_name]", "name": "a"}


# ---------------------------------------------------------------------------
# Fingerprints for the audit copy
# ---------------------------------------------------------------------------


def test_fingerprint_replaces_hidden_values_and_keeps_the_rest() -> None:
    raw = {"data": {"a": "same", "b": "same", "c": 5}, "version": 1, "items": ["x", "y"]}
    shown = {"data": {"a": _HIDDEN, "b": _HIDDEN, "c": _HIDDEN}, "version": 1, "items": ["x"]}
    stored = fingerprint_hidden_values(raw, shown)
    assert stored["version"] == 1
    assert stored["data"]["a"].startswith(_FINGERPRINT)
    # The same value gives the same fingerprint; a different value does not.
    assert stored["data"]["a"] == stored["data"]["b"]
    assert stored["data"]["c"] != stored["data"]["a"]
    # A list of another length is stored as one fingerprint.
    assert stored["items"].startswith(_FINGERPRINT)
    assert fingerprinted_paths(stored) == {"data.a", "data.b", "data.c", "items"}


def test_policy_replay_leaves_out_entries_inside_a_fingerprinted_subtree() -> None:
    def _entry(path: str) -> dict[str, Any]:
        return {
            "rule": "api_key",
            "pattern": "x",
            "action": "redact",
            "count": 1,
            "span": [0, 1],
            "reason": "r",
            "path": path,
        }

    row = AuditLog(
        raw_payload={"items": _FINGERPRINT + "abc]", "itemsx": "plain", "data": {"a": "x"}},
        redaction_manifest=[_entry("items"), _entry("items.0.token"), _entry("itemsx")],
    )
    kept = _comparable_recorded_manifest(row)
    assert [entry.path for entry in kept] == ["itemsx"]


def test_fingerprint_of_a_key_the_view_does_not_have() -> None:
    stored = fingerprint_hidden_values({"a": "x", "b": "y"}, {"a": "x"})
    assert stored["a"] == "x"
    assert stored["b"].startswith(_FINGERPRINT)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


async def test_kv_read_default_answer_has_names_and_no_values(
    stub_embedding_service: AsyncMock,
) -> None:
    await _register("vault.kv.read", _planted_secret_handler, stub_embedding_service)

    result = await _read(_operator(PrincipalKind.AGENT), reveal=False)

    assert result.status == "ok", result.error
    assert set(result.result["data"].values()) == {_HIDDEN}
    assert set(result.result["data"]) >= {*_PLAIN_NAMES, "password", "private_key", "number"}
    dumped = result.model_dump_json()
    assert [value for value in _planted_values() if value in dumped] == []


@pytest.mark.parametrize("principal_kind", [PrincipalKind.AGENT, PrincipalKind.USER])
async def test_reveal_by_an_agent_is_refused(
    principal_kind: PrincipalKind, stub_embedding_service: AsyncMock
) -> None:
    """An agent principal, and a person's hosted agent run, get no raw value."""
    await _register("vault.kv.read", _planted_secret_handler, stub_embedding_service)
    operator = _operator(principal_kind)

    if principal_kind is PrincipalKind.AGENT:
        result = await _read(operator, reveal=True)
    else:
        with actor_delegation("agent:planner"):
            result = await _read(operator, reveal=True)

    assert result.status == "denied"
    assert result.result is None
    assert "reveal_secret is for people only" in (result.error or "")
    assert [value for value in _planted_values() if value in result.model_dump_json()] == []
    (row,) = await _audit_rows("vault.kv.read")
    assert row.payload["result_status"] == "denied"
    assert row.payload["reveal_secret"] is True


async def test_reveal_by_a_person_returns_values_and_audit_stores_fingerprints(
    stub_embedding_service: AsyncMock,
) -> None:
    await _register("vault.kv.read", _planted_secret_handler, stub_embedding_service)
    operator = _operator(PrincipalKind.USER)

    first = await _read(operator, reveal=True)
    second = await _read(operator, reveal=True)

    # The caller gets the secret fields as the handler gave them, the PEM
    # private key included.
    for result in (first, second):
        assert result.status == "ok", result.error
        assert result.result["data"]["password"] == _PASSWORD
        assert result.result["data"]["token"] == _TOKEN
        assert result.result["data"]["number"] == _PIN
        assert result.result["data"]["private_key"] == _PRIVATE_KEY
        assert result.result["version"] == 7

    rows = await _audit_rows("vault.kv.read")
    assert len(rows) == 2
    for row in rows:
        stored = json.dumps(row.raw_payload)
        assert [value for value in _planted_values() if value in stored] == []
        assert row.raw_payload["version"] == 7
        assert set(fingerprinted_paths(row.raw_payload)) == {
            f"data.{name}" for name in row.raw_payload["data"]
        }
        assert row.payload["reveal_secret"] is True
    # The same value gives the same fingerprint in two rows.
    assert rows[0].raw_payload["data"] == rows[1].raw_payload["data"]


async def test_reveal_returns_no_value_when_the_audit_row_cannot_commit(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _register("vault.kv.read", _planted_secret_handler, stub_embedding_service)

    def _raise_audit_commit(*_args: Any, **_kwargs: Any) -> Any:
        async def _boom() -> None:
            raise RuntimeError("audit DB unavailable")

        return _boom()

    monkeypatch.setattr(audit_module, "write_audit_row", _raise_audit_commit)

    result = await _read(_operator(PrincipalKind.USER), reveal=True)

    assert result.status == "error"
    assert (result.error or "").startswith("audit_commit_failed:")
    assert result.result is None
    assert [value for value in _planted_values() if value in result.model_dump_json()] == []


async def test_guest_env_read_hides_values_by_default(stub_embedding_service: AsyncMock) -> None:
    op_id = "vmware.composite.vm.guest.env.read"
    await _register(op_id, _guest_env_handler, stub_embedding_service)

    result = await _read(_operator(PrincipalKind.USER), reveal=False, op_id=op_id)

    assert result.status == "ok", result.error
    # The free-text rule took the second entry apart; its name still shows.
    assert result.result["variables"] == [
        f"PATH={_HIDDEN}",
        f"DB_PASSWORD={_HIDDEN}",
        f"API_PAT={_HIDDEN}",
    ]
    assert result.result["vm"] == "vm-1"
    (row,) = await _audit_rows(op_id)
    assert row.payload["reveal_secret"] is False
    assert [value for value in (_PASSWORD, _TOKEN) if value in json.dumps(row.raw_payload)] == []
    assert set(fingerprinted_paths(row.raw_payload)) == {
        "variables.0",
        "variables.1",
        "variables.2",
    }


# ---------------------------------------------------------------------------
# Free-text rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        f"DB_PASSWORD={_PASSWORD}",
        f"export GITHUB_TOKEN={_TOKEN}",
        f"x-api-key: {_TOKEN}",
    ],
)
def test_label_after_an_underscore_or_dash_is_redacted(text: str) -> None:
    redacted = redact(text, get_default_policy()).redacted
    assert _PASSWORD not in redacted
    assert _TOKEN not in redacted
    assert "[REDACTED:api_key]" in redacted


def test_label_glued_to_letters_is_not_redacted() -> None:
    text = f"mypassword={_PASSWORD}"
    assert redact(text, get_default_policy()).redacted == text


@pytest.mark.parametrize("kind", ["", "RSA ", "EC ", "ENCRYPTED ", "OPENSSH "])
def test_pem_private_key_block_is_redacted_whole(kind: str) -> None:
    head = "-----BEGIN " + kind + "PRIVATE KEY-----"
    tail = "-----END " + kind + "PRIVATE KEY-----"
    text = f"before\n{head}\n{_PEM_BODY}\nmore{_PEM_BODY}\n{tail}\nafter"
    redacted = redact(text, get_default_policy()).redacted
    assert redacted == "before\n[REDACTED:pem_private_key]\nafter"


def test_pem_private_key_without_end_line_is_redacted_to_the_end() -> None:
    redacted = redact(f"key: {_PEM_HEAD}\n{_PEM_BODY}", get_default_policy()).redacted
    assert _PEM_BODY not in redacted


def test_pem_public_key_and_certificate_are_kept() -> None:
    text = "-----BEGIN " + "PUBLIC KEY-----\nabc\n-----END PUBLIC KEY-----"
    cert = "-----BEGIN " + "CERTIFICATE-----\nabc\n-----END CERTIFICATE-----"
    assert redact(text, get_default_policy()).redacted == text
    assert redact(cert, get_default_policy()).redacted == cert


# ---------------------------------------------------------------------------
# Every other operation: secret-named fields are hidden, declared plain fields stay
# ---------------------------------------------------------------------------

_API_TOKEN = "planted" + "-api-token-3"
_CLIENT_SECRET = "planted" + "-client-secret-4"
_EXPIRES_AT = "2027-03-01T00:00:00Z"
_PACKAGE = "example-connectors"
_ROWS = 120


def _device_rows() -> list[dict[str, Any]]:
    return [
        {
            "name": f"device-{index}",
            "password": _PASSWORD,
            "api_token": _API_TOKEN,
            "client_secret": _CLIENT_SECRET,
            "password_expires_at": _EXPIRES_AT,
        }
        for index in range(_ROWS)
    ]


async def _builtin_device_list(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    """Module-level stand-in for a built-in ``safe`` read with many rows."""
    return _device_rows()


async def _package_device_list(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    """Module-level stand-in for a read a connector package registers."""
    return {"devices": _device_rows()[:3], "count": 3}


async def _mint_handler(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    """Module-level stand-in for an operation that makes a new credential."""
    return {"secret_id": _TOKEN, "secret_id_accessor": "accessor-1", "secret_id_ttl": 600}


def _field_values() -> list[str]:
    return [_PASSWORD, _API_TOKEN, _CLIENT_SECRET]


@pytest.fixture
def _plain_fields_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("meho_backplane.operations._secret_values._PLAIN_FIELDS", {})


@pytest.fixture
def _package_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat this test module as a folder that the package installed."""
    monkeypatch.setattr(connector_packages, "_CONNECTORS_MODULE_PREFIX", "tests.")
    monkeypatch.setattr(connector_packages, "_installed_folders", {})
    record_installed_connector_packages({"test_secret_values_hidden": _PACKAGE})


async def _owner_package(op_id: str) -> str | None:
    async with get_sessionmaker()() as fresh:
        row = (
            await fresh.execute(select(EndpointDescriptor).where(EndpointDescriptor.op_id == op_id))
        ).scalar_one()
        return row.owner_package


def test_plain_fields_stay_in_the_default_view(_plain_fields_registry: None) -> None:
    register_plain_fields("made_up.read", ("password_expires_at", "token"))
    answer = {
        "password": "x",
        "password_expires_at": _EXPIRES_AT,
        "token": {"path": "/t", "mode": "0600", "token_file_secret": "kept whole"},
        "nested": {"token": {"mode": "0600"}, "api_token": "y"},
    }

    assert plain_fields_of("made_up.read") == {"password_expires_at", "token"}
    assert plain_fields_of("other.read") == frozenset()
    assert hide_secret_values("made_up.read", answer) == {
        "password": _HIDDEN_FIELD,
        "password_expires_at": _EXPIRES_AT,
        "token": {"path": "/t", "mode": "0600", "token_file_secret": "kept whole"},
        "nested": {"token": {"mode": "0600"}, "api_token": _HIDDEN_FIELD},
    }
    assert hide_secret_values("other.read", answer)["password_expires_at"] == _HIDDEN_FIELD


async def test_builtin_read_hides_secret_named_fields_in_the_answer_and_the_stored_rows(
    stub_embedding_service: AsyncMock,
) -> None:
    """A built-in ``safe`` read: no secret field reaches the caller or the stored rows."""
    op_id = "vault.device.list"
    await _register(op_id, _builtin_device_list, stub_embedding_service)
    operator = _operator(PrincipalKind.AGENT)
    store = MemoryResultStore()
    set_default_reducer(JsonFluxReducer(store=store))
    try:
        result = await _read(operator, reveal=False, op_id=op_id)
    finally:
        set_default_reducer(PassThroughReducer())

    assert result.status == "ok", result.error
    assert result.handle is not None
    assert [value for value in _field_values() if value in result.model_dump_json()] == []
    assert operator.tenant_id is not None
    stored = await store.fetch_rows(
        tenant_id=operator.tenant_id, operator_sub=operator.sub, handle_id=result.handle.handle_id
    )
    assert stored is not None
    assert len(stored.rows) == _ROWS
    for row in stored.rows:
        assert row["password"] == _HIDDEN_FIELD
        assert row["api_token"] == _HIDDEN_FIELD
        assert row["client_secret"] == _HIDDEN_FIELD
        assert row["name"].startswith("device-")
    (audit,) = await _audit_rows(op_id)
    assert [value for value in _field_values() if value in json.dumps(audit.raw_payload)] == []
    assert audit.raw_payload[0]["password"].startswith(_FINGERPRINT)
    assert "reveal_secret" not in audit.payload


async def test_package_read_hides_secret_named_fields_and_keeps_its_plain_fields(
    stub_embedding_service: AsyncMock, _package_installed: None, _plain_fields_registry: None
) -> None:
    """A read from a connector package is protected the same way, and can declare plain fields."""
    op_id = "vault.device.list_from_package"
    await _register(op_id, _package_device_list, stub_embedding_service)
    assert await _owner_package(op_id) == _PACKAGE
    # What the package's registrar does for a date that only looks like a secret.
    register_plain_fields(op_id, ("password_expires_at",))

    hidden = await _read(_operator(PrincipalKind.AGENT), reveal=False, op_id=op_id)

    assert hidden.status == "ok", hidden.error
    assert hidden.result["count"] == 3
    for device in hidden.result["devices"]:
        assert device["password"] == _HIDDEN_FIELD
        assert device["api_token"] == _HIDDEN_FIELD
        assert device["client_secret"] == _HIDDEN_FIELD
        assert device["password_expires_at"] == _EXPIRES_AT
    assert [value for value in _field_values() if value in hidden.model_dump_json()] == []

    revealed = await _read(_operator(PrincipalKind.USER), reveal=True, op_id=op_id)

    assert revealed.status == "ok", revealed.error
    assert revealed.result["devices"][0]["password"] == _PASSWORD
    assert revealed.result["devices"][0]["api_token"] == _API_TOKEN
    rows = await _audit_rows(op_id)
    assert [row.payload.get("reveal_secret") for row in rows] == [None, True]
    for row in rows:
        assert [value for value in _field_values() if value in json.dumps(row.raw_payload)] == []
        assert row.raw_payload["devices"][0]["password_expires_at"] == _EXPIRES_AT


async def test_reveal_by_an_agent_is_refused_on_a_plain_read(
    stub_embedding_service: AsyncMock,
) -> None:
    op_id = "vault.device.list"
    await _register(op_id, _builtin_device_list, stub_embedding_service)

    result = await _read(_operator(PrincipalKind.AGENT), reveal=True, op_id=op_id)

    assert result.status == "denied"
    assert result.result is None
    assert [value for value in _field_values() if value in result.model_dump_json()] == []
    (row,) = await _audit_rows(op_id)
    assert row.payload["result_status"] == "denied"
    assert row.payload["reveal_secret"] is True


async def test_an_operation_that_makes_a_credential_still_returns_it(
    stub_embedding_service: AsyncMock,
) -> None:
    """The caller gets the new SecretID; the audit row keeps only the other fields."""
    op_id = "vault.auth.approle.generate_secret_id"
    await _register(op_id, _mint_handler, stub_embedding_service)

    result = await _read(_operator(PrincipalKind.USER), reveal=False, op_id=op_id)

    assert result.status == "ok", result.error
    assert result.result["secret_id"] == _TOKEN
    assert result.result["secret_id_accessor"] == "accessor-1"
    (row,) = await _audit_rows(op_id)
    assert _TOKEN not in json.dumps(row.raw_payload)
    assert row.raw_payload["secret_id"] == _HIDDEN_FIELD
    assert "reveal_secret" not in row.payload
