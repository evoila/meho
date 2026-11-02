# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Default key-name scrub for credential_read dispatch responses (#2467).

A ``credential_read``-classified op (``vault.kv.read`` et al.) returns the
raw secret in its handler payload -- that is the op's documented contract.
The connector-boundary redaction engine cannot mask it: it matches only
*labelled* secret shapes inside string leaves, so a ``{"password": "..."}``
dict value passes through verbatim and lands in the ``call_operation``
caller's response (and, for an agent caller, its model-API transcript).

These tests pin the contract:

* the caller-bound answer of ``vault.kv.read`` hides every value under
  ``data`` by default and keeps the key names and ``version``;
* ``params.reveal_secret=true`` from a person opts into the raw value AND
  stamps the choice on the audit row (queryable), while ``reveal_secret``
  never reaches the op's ``parameter_schema`` (it is stripped as a
  dispatch control) nor the ``params_hash``;
* the audit row's ``raw_payload`` stores a keyed fingerprint in place of
  each hidden value in BOTH cases;
* a non-credential_read op answer is untouched and carries no
  ``reveal_secret`` stamp.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast.events import scrub_secret_named_values
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations import (
    dispatch,
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.settings import get_settings

# Assembled from fragments so gitleaks' built-in rules don't false-positive
# on the test source.
_PASSWORD_SECRET = "hunter2" + "longenough"
_REDACTED = "[REDACTED:param_name]"
_HIDDEN = "[REDACTED:secret_value]"
_FINGERPRINT = "[FINGERPRINT:hmac-sha256:"


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


def _make_operator(*, principal_kind: PrincipalKind = PrincipalKind.USER) -> Operator:
    return Operator(
        sub="op-2467-test",
        name="Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-000000002467"),
        tenant_role=TenantRole.OPERATOR,
        principal_kind=principal_kind,
    )


class _NoOpVaultConnector(Connector):
    """Connector class satisfying the resolver lookup for the typed op."""

    product = "vault"
    version = "1.x"
    impl_id = "vault"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self,
        target: Any,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        raise NotImplementedError


async def _module_vault_read_handler(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Module-level ``vault.kv.read`` stand-in returning a raw secret.

    Mirrors the real handler's ``{"data": <secret dict>, "version": N}``
    shape. ``reveal_secret`` must have been stripped by the dispatcher, so
    it is asserted absent from the handler-visible params.
    """
    assert "reveal_secret" not in params, "reveal_secret must be stripped before the handler"
    return {"data": {"password": _PASSWORD_SECRET, "username": "root"}, "version": 3}


async def _register_vault_read(embedding_service: AsyncMock) -> None:
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.kv.read",
        handler=_module_vault_read_handler,
        summary="Read a KV v2 secret.",
        description="Read a secret.",
        parameter_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        when_to_use=None,
        embedding_service=embedding_service,
    )


async def _audit_row(op_id: str) -> AuditLog:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (await fresh.execute(select(AuditLog).where(AuditLog.path == op_id))).scalars().all()
    assert len(rows) == 1, f"expected exactly one audit row for {op_id}, got {len(rows)}"
    return rows[0]


# ---------------------------------------------------------------------------
# Scrubbed by default
# ---------------------------------------------------------------------------


async def test_credential_read_response_scrubbed_by_default(
    stub_embedding_service: AsyncMock,
) -> None:
    """No ``reveal_secret`` -> every value under ``data`` is hidden; names stay."""
    await _register_vault_read(stub_embedding_service)

    result = await dispatch(
        operator=_make_operator(),
        connector_id="vault-1.x",
        op_id="vault.kv.read",
        target=None,
        params={"path": "app/db"},
    )
    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    # Every value under data is hidden; the names and the version stay.
    assert result.result["data"] == {"password": _HIDDEN, "username": _HIDDEN}
    assert result.result["version"] == 3
    # The raw secret never reaches the caller-bound envelope.
    assert _PASSWORD_SECRET not in result.model_dump_json()

    # The audit row stores fingerprints, not the values, and stamps the
    # scrubbed (reveal_secret=False) choice.
    row = await _audit_row("vault.kv.read")
    assert isinstance(row.raw_payload, dict)
    assert row.raw_payload["data"]["password"].startswith(_FINGERPRINT)
    assert row.raw_payload["data"]["username"].startswith(_FINGERPRINT)
    assert row.raw_payload["version"] == 3
    assert _PASSWORD_SECRET not in str(row.raw_payload)
    assert row.payload["reveal_secret"] is False


async def test_credential_read_scrub_is_principal_independent(
    stub_embedding_service: AsyncMock,
) -> None:
    """The default hiding keys on the op, not the caller kind.

    A ``USER`` principal (default-allow) reaches execution and gets the
    names without the values, like every other caller.
    """
    await _register_vault_read(stub_embedding_service)

    result = await dispatch(
        operator=_make_operator(principal_kind=PrincipalKind.USER),
        connector_id="vault-1.x",
        op_id="vault.kv.read",
        target=None,
        params={"path": "app/db"},
    )
    assert result.status == "ok", result.error
    assert result.result["data"]["password"] == _HIDDEN


# ---------------------------------------------------------------------------
# reveal_secret=true opt-in
# ---------------------------------------------------------------------------


async def test_reveal_secret_true_returns_raw_and_stamps_audit(
    stub_embedding_service: AsyncMock,
) -> None:
    """``reveal_secret=true`` -> raw value returned AND stamped on the audit row."""
    await _register_vault_read(stub_embedding_service)

    result = await dispatch(
        operator=_make_operator(),
        connector_id="vault-1.x",
        op_id="vault.kv.read",
        target=None,
        params={"path": "app/db", "reveal_secret": True},
    )
    assert result.status == "ok", result.error
    # The caller opted in: the raw value comes back.
    assert result.result["data"]["password"] == _PASSWORD_SECRET
    assert result.result["data"]["username"] == "root"

    row = await _audit_row("vault.kv.read")
    # The reveal is queryable on the audit row.
    assert row.payload["reveal_secret"] is True
    # raw_payload stores a fingerprint, also when the caller got the value.
    assert isinstance(row.raw_payload, dict)
    assert row.raw_payload["data"]["password"].startswith(_FINGERPRINT)
    assert _PASSWORD_SECRET not in str(row.raw_payload)


async def test_reveal_secret_does_not_reach_op_schema_or_hash(
    stub_embedding_service: AsyncMock,
) -> None:
    """``reveal_secret`` is a dispatch control: stripped before schema + hash.

    The op schema is ``additionalProperties: false``; if ``reveal_secret``
    were forwarded, dispatch would 422 with ``invalid_params``. And the
    scrubbed read and its reveal must share one ``params_hash`` (the reveal
    choice is recorded separately on the audit row, not in the hash).
    """
    await _register_vault_read(stub_embedding_service)
    operator = _make_operator()

    scrubbed = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.kv.read",
        target=None,
        params={"path": "app/db"},
    )
    revealed = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.kv.read",
        target=None,
        params={"path": "app/db", "reveal_secret": True},
    )
    # Neither dispatch 422'd on the strict schema.
    assert scrubbed.status == "ok", scrubbed.error
    assert revealed.status == "ok", revealed.error

    # Both audit rows share one params_hash despite the differing reveal.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (await fresh.execute(select(AuditLog).where(AuditLog.path == "vault.kv.read")))
            .scalars()
            .all()
        )
    assert len(rows) == 2
    hashes = {row.payload["params_hash"] for row in rows}
    assert len(hashes) == 1, "reveal choice must not perturb params_hash"


# ---------------------------------------------------------------------------
# Every other op hides its secret-named fields too
# ---------------------------------------------------------------------------


async def _module_metadata_handler(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """A plain ``read`` op that happens to echo a secret-named field."""
    return {"password": _PASSWORD_SECRET, "note": "not a credential_read op"}


async def _register_metadata_read(stub_embedding_service: AsyncMock) -> None:
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.kv.versions",
        handler=_module_metadata_handler,
        summary="List KV metadata versions.",
        description="Metadata only.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )


async def test_read_op_hides_secret_named_fields_and_carries_no_stamp(
    stub_embedding_service: AsyncMock,
) -> None:
    """A ``read``-class op hides a secret-named field, and its audit row has no reveal key.

    ``vault.kv.versions`` classifies ``read`` (metadata browse), not
    ``credential_read``. The answer still hides the ``password`` field: the
    name rule runs on every answer. The reveal stamp is for secret reads
    and reveals only, so the row of a plain read carries no key.
    """
    await _register_metadata_read(stub_embedding_service)

    result = await dispatch(
        operator=_make_operator(),
        connector_id="vault-1.x",
        op_id="vault.kv.versions",
        target=None,
        params={"path": "app/db"},
    )
    assert result.status == "ok", result.error
    assert result.result["password"] == "[REDACTED:param_name]"
    assert result.result["note"] == "not a credential_read op"
    assert _PASSWORD_SECRET not in result.model_dump_json()

    row = await _audit_row("vault.kv.versions")
    assert "reveal_secret" not in row.payload
    assert _PASSWORD_SECRET not in json.dumps(row.raw_payload)


async def test_read_op_reveal_by_a_person_returns_the_field_and_stamps_audit(
    stub_embedding_service: AsyncMock,
) -> None:
    """``reveal_secret: true`` on a plain read works as on a secret read."""
    await _register_metadata_read(stub_embedding_service)

    result = await dispatch(
        operator=_make_operator(),
        connector_id="vault-1.x",
        op_id="vault.kv.versions",
        target=None,
        params={"path": "app/db", "reveal_secret": True},
    )
    assert result.status == "ok", result.error
    assert result.result["password"] == _PASSWORD_SECRET

    row = await _audit_row("vault.kv.versions")
    assert row.payload["reveal_secret"] is True
    assert _PASSWORD_SECRET not in json.dumps(row.raw_payload)


# ---------------------------------------------------------------------------
# scrub_secret_named_values unit contract
# ---------------------------------------------------------------------------


def test_scrub_secret_named_values_replaces_nested_secret() -> None:
    scrubbed, found = scrub_secret_named_values(
        {"data": {"password": _PASSWORD_SECRET, "username": "root"}, "version": 3}
    )
    assert found is True
    assert scrubbed == {
        "data": {"password": _REDACTED, "username": "root"},
        "version": 3,
    }


def test_scrub_secret_named_values_passthrough_when_no_secret() -> None:
    payload = {"keys": ["app/db", "app/api"], "count": 2}
    scrubbed, found = scrub_secret_named_values(payload)
    assert found is False
    assert scrubbed == payload
