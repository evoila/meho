# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the secret broker — G0.22-T1 (#1577).

Covers the mechanism this task establishes:

* The ``SecretMaterial`` wrapper redacts its value in ``repr``/``str``
  and exposes only ``value_sha256`` / ``length``.
* ``parse_secret_ref`` splits ``"<kind>:<ref>"`` (first-colon split) and
  rejects malformed intents.
* The synthetic ``secret-broker-1.x`` connector_id round-trips through
  the dispatcher's parser back to ``("secret", "1.x", "secret-broker")``
  — the descriptor is reachable.
* ``secret.move`` is registered with the exact synthetic identity +
  change-class posture (``safety_level="dangerous"``,
  ``requires_approval=True``).
* End-to-end vault-kv → vault-kv move through ``dispatch(...,
  _approved=True)``: the sink receives the value, the response carries
  ONLY ``status`` + ``value_sha256`` + ``length``, and the secret
  substring is absent from the response, the params, and the persisted
  audit row's ``payload`` AND ``raw_payload``.
* The existing approval gate parks an unapproved move at
  ``awaiting_approval`` and the sink write never runs.

Test isolation mirrors ``test_connectors_vault.py``: the operator-scoped
Vault client is built through the single ``_build_client`` seam, which
``install_fake_client`` monkeypatches to a controllable
``_FakeKVv2`` — no real HTTP, no Vault container. The autouse
``_default_database_url`` conftest fixture migrates the SQLite DB to head
so the ``endpoint_descriptor`` / ``operation_group`` / ``audit_log``
tables exist before the registrar runs.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.connectors.secret.endpoints import (
    SECRET_ENDPOINT_REGISTRY,
    SecretEndpoint,
    SecretMaterial,
    parse_secret_ref,
    register_secret_endpoint,
)
from meho_backplane.connectors.secret.ops import (
    register_secret_broker_operations,
    secret_move,
)
from meho_backplane.connectors.secret.vault_endpoint import VaultKvSecretEndpoint
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._lookup import parse_connector_id
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

_SECRET_VALUE = "hunter2"
_SECRET_SHA256 = hashlib.sha256(_SECRET_VALUE.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Settings env + dispatcher isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin env vars Settings / the operator-scoped Vault client need."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    reset_dispatcher_caches()
    yield
    get_settings.cache_clear()
    reset_dispatcher_caches()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub so registration doesn't pull ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def _registered_secret_broker_op(
    stub_embedding_service: AsyncMock,
) -> AsyncIterator[None]:
    """Upsert the ``secret.move`` descriptor row for dispatch-driving tests."""
    await register_secret_broker_operations(embedding_service=stub_embedding_service)
    yield


def _make_operator(jwt: str = "fake.jwt.value") -> Operator:
    """A request-scoped operator carrying the JWT the vault adapter forwards."""
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt=jwt,
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


async def _dispatch_move(
    params: dict[str, Any],
    *,
    jwt: str = "fake.jwt.value",
    approved: bool = True,
) -> OperationResult:
    """Dispatch ``secret.move`` through the real operator-aware path.

    ``target`` is ``None`` (the synthetic product has no connector
    instance); the handler is module-level, so the dispatcher resolves
    it with ``connector_instance=None``. ``approved`` threads the
    ``_approved`` resume flag — the op registers ``requires_approval=True``
    so an unapproved dispatch parks at ``awaiting_approval``.
    """
    return await dispatch(
        operator=_make_operator(jwt),
        connector_id="secret-broker-1.x",
        op_id="secret.move",
        target=None,
        params=params,
        _approved=approved,
    )


async def _fetch_audit_rows() -> list[AuditLog]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# SecretMaterial — value never leaks through repr/str; hash + length exposed
# ---------------------------------------------------------------------------


def test_secret_material_repr_and_str_redact_the_value() -> None:
    material = SecretMaterial(_SECRET_VALUE)
    assert _SECRET_VALUE not in repr(material)
    assert _SECRET_VALUE not in str(material)
    # The redacted form carries the provenance signal an auditor wants.
    assert _SECRET_SHA256 in repr(material)
    assert f"len={len(_SECRET_VALUE.encode())}" in repr(material)


def test_secret_material_exposes_sha256_and_length() -> None:
    material = SecretMaterial(_SECRET_VALUE)
    assert material.value_sha256 == _SECRET_SHA256
    assert material.length == len(_SECRET_VALUE.encode())
    # bytes and str of the same content hash identically.
    assert SecretMaterial(_SECRET_VALUE.encode()).value_sha256 == _SECRET_SHA256


# ---------------------------------------------------------------------------
# parse_secret_ref — first-colon split + malformed rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,kind,ref",
    [
        ("vault:secret/db/prod#password", "vault", "secret/db/prod#password"),
        # First-colon split: the ref may itself contain colons.
        ("vault:secret/a:b#field", "vault", "secret/a:b#field"),
        ("keycloak:realm/clients/web#secret", "keycloak", "realm/clients/web#secret"),
    ],
    ids=["vault", "ref-with-colon", "keycloak-kind"],
)
def test_parse_secret_ref_splits_kind_and_ref(raw: str, kind: str, ref: str) -> None:
    parsed = parse_secret_ref(raw)
    assert parsed.kind == kind
    assert parsed.ref == ref


@pytest.mark.parametrize(
    "raw",
    ["no-colon", ":ref-only", "kind-only:", ""],
    ids=["missing-colon", "empty-kind", "empty-ref", "empty"],
)
def test_parse_secret_ref_rejects_malformed(raw: str) -> None:
    with pytest.raises(ValueError, match="malformed secret ref"):
        parse_secret_ref(raw)


# ---------------------------------------------------------------------------
# Reachability + registration identity
# ---------------------------------------------------------------------------


def test_secret_broker_connector_id_round_trips() -> None:
    """The wire connector_id resolves to the registered natural key.

    Guards the unreachable-identity trap: a non-digit-led version or a
    colon form would silently never match the descriptor.
    """
    assert parse_connector_id("secret-broker-1.x") == ("secret", "1.x", "secret-broker")


def test_vault_endpoint_registered_under_kind_vault() -> None:
    assert SECRET_ENDPOINT_REGISTRY.get("vault") is VaultKvSecretEndpoint


def test_vault_endpoint_satisfies_secret_endpoint_protocol() -> None:
    endpoint = VaultKvSecretEndpoint("secret/db/prod#password")
    assert isinstance(endpoint, SecretEndpoint)


def test_register_secret_endpoint_rejects_duplicate_kind() -> None:
    with pytest.raises(ValueError, match="already registered"):
        register_secret_endpoint("vault", VaultKvSecretEndpoint)


async def test_secret_move_registered_with_synthetic_identity(
    _registered_secret_broker_op: None,
) -> None:
    """The descriptor row carries the exact synthetic identity + posture."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.product == "secret",
                EndpointDescriptor.version == "1.x",
                EndpointDescriptor.impl_id == "secret-broker",
                EndpointDescriptor.op_id == "secret.move",
            )
        )
        row = result.scalar_one()
    assert row.source_kind == "typed"
    assert row.safety_level == "dangerous"
    assert row.requires_approval is True


# ---------------------------------------------------------------------------
# End-to-end move — value reaches the sink, never the response/params/audit
# ---------------------------------------------------------------------------


async def test_secret_move_vault_to_vault_round_trips_value_server_side(
    monkeypatch: pytest.MonkeyPatch,
    _registered_secret_broker_op: None,
) -> None:
    """A vault-kv → vault-kv move writes the value to the sink path.

    The single fake Vault client serves both endpoints (same operator,
    same Vault): the source read returns the seeded secret; the sink
    write records to ``put_calls``. The handler returns only status +
    hash + length.
    """
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})

    result = await _dispatch_move(
        {
            "from": "vault:secret/db/prod#password",
            "to": "vault:secret/db/replica#password",
            "reason": "promote replica credential",
        }
    )

    assert result.status == "ok", result.error
    # (a) the sink received the value at the 'to' path.
    put_calls = fake.secrets.kv.v2.put_calls
    assert put_calls == [
        {
            "path": "secret/db/replica",
            "secret": {"password": _SECRET_VALUE},
            "cas": None,
            "mount_point": "secret",
        }
    ]


async def test_secret_move_response_carries_only_status_hash_length(
    monkeypatch: pytest.MonkeyPatch,
    _registered_secret_broker_op: None,
) -> None:
    """(b) The result payload is exactly {status, value_sha256, length}.

    And the secret substring is absent from the JSON-serialised result.
    """
    install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})

    result = await _dispatch_move(
        {
            "from": "vault:secret/db/prod#password",
            "to": "vault:secret/db/replica#password",
        }
    )

    assert result.status == "ok", result.error
    assert result.result == {
        "status": "moved",
        "value_sha256": _SECRET_SHA256,
        "length": len(_SECRET_VALUE.encode()),
    }
    # The value never appears anywhere in the serialised OperationResult.
    assert _SECRET_VALUE not in json.dumps(result.result)
    assert _SECRET_VALUE not in result.model_dump_json()


async def test_secret_move_audit_row_omits_value(
    monkeypatch: pytest.MonkeyPatch,
    _registered_secret_broker_op: None,
) -> None:
    """(c) The persisted audit row's payload AND raw_payload omit the value.

    The dispatcher stores ``params_hash`` (not the params) in ``payload``
    and the handler's return dict (hash + length only) in ``raw_payload``,
    so the value is absent from both by construction.
    """
    install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})

    result = await _dispatch_move(
        {
            "from": "vault:secret/db/prod#password",
            "to": "vault:secret/db/replica#password",
        }
    )
    assert result.status == "ok", result.error

    rows = await _fetch_audit_rows()
    move_rows = [r for r in rows if r.path == "secret.move"]
    assert len(move_rows) == 1
    row = move_rows[0]
    assert row.status_code == 200
    assert _SECRET_VALUE not in json.dumps(row.payload)
    assert _SECRET_VALUE not in json.dumps(row.raw_payload)
    # The raw payload is the handler's return dict — hash + length only.
    assert row.raw_payload == {
        "status": "moved",
        "value_sha256": _SECRET_SHA256,
        "length": len(_SECRET_VALUE.encode()),
    }


def test_secret_move_params_never_carry_the_value() -> None:
    """The dispatched params dict carries only the '<kind>:<ref>' strings."""
    params = {
        "from": "vault:secret/db/prod#password",
        "to": "vault:secret/db/replica#password",
        "reason": "promote replica credential",
    }
    assert _SECRET_VALUE not in json.dumps(params)


async def test_secret_move_handler_does_not_read_value_from_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handler resolves the value from the source store, not params.

    A params dict carrying a (would-be-malicious) value field is rejected
    by the schema's ``additionalProperties: false``; calling the handler
    directly with only refs proves the value comes from the source read.
    """
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    operator = _make_operator()

    result = await secret_move(
        operator,
        None,
        {
            "from": "vault:secret/db/prod#password",
            "to": "vault:secret/db/replica#password",
        },
    )

    assert result == {
        "status": "moved",
        "value_sha256": _SECRET_SHA256,
        "length": len(_SECRET_VALUE.encode()),
    }
    # The value the sink wrote came from the source read, not params.
    assert fake.secrets.kv.v2.put_calls[0]["secret"] == {"password": _SECRET_VALUE}


# ---------------------------------------------------------------------------
# Approval gate — an unapproved move is parked, the sink write never runs
# ---------------------------------------------------------------------------


async def test_secret_move_parks_without_approval(
    monkeypatch: pytest.MonkeyPatch,
    _registered_secret_broker_op: None,
) -> None:
    """``requires_approval=True`` parks the move; the handler never runs.

    Dispatching without the approval-resume flag routes the call to the
    approval queue (``awaiting_approval``) per G11.7-T1 (#1401) rather
    than reaching the source read / sink write — the sink ``put_calls``
    log stays empty.
    """
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})

    result = await _dispatch_move(
        {
            "from": "vault:secret/db/prod#password",
            "to": "vault:secret/db/replica#password",
        },
        approved=False,
    )

    assert result.status == "awaiting_approval", result.error
    assert fake.secrets.kv.v2.put_calls == []
    assert fake.secrets.kv.v2.read_calls == []


# ---------------------------------------------------------------------------
# Error mapping — unknown kind / missing field surface as connector_error
# ---------------------------------------------------------------------------


async def test_secret_move_unknown_kind_is_connector_error(
    monkeypatch: pytest.MonkeyPatch,
    _registered_secret_broker_op: None,
) -> None:
    install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})

    result = await _dispatch_move(
        {
            "from": "nope:whatever#field",
            "to": "vault:secret/db/replica#password",
        }
    )

    assert result.status == "error"
    assert result.extras.get("exception_class") == "UnknownSecretKindError"


async def test_secret_move_missing_field_is_connector_error(
    monkeypatch: pytest.MonkeyPatch,
    _registered_secret_broker_op: None,
) -> None:
    install_fake_client(monkeypatch, secret={"username": "demo"})

    result = await _dispatch_move(
        {
            "from": "vault:secret/db/prod#password",
            "to": "vault:secret/db/replica#password",
        }
    )

    assert result.status == "error"
    assert result.extras.get("exception_class") == "VaultSecretRefError"


@pytest.mark.parametrize(
    "params",
    [
        {"to": "vault:secret/db/replica#password"},
        {"from": "vault:secret/db/prod#password"},
        {"from": "no-colon", "to": "vault:secret/db/replica#password"},
    ],
    ids=["missing-from", "missing-to", "malformed-from"],
)
async def test_secret_move_invalid_params_returns_dispatcher_error(
    monkeypatch: pytest.MonkeyPatch,
    params: dict[str, Any],
    _registered_secret_broker_op: None,
) -> None:
    """The param schema rejects missing/malformed refs before the handler."""
    install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    result = await _dispatch_move(params)

    assert result.status == "error"
    assert result.extras.get("error_code") == "invalid_params"
