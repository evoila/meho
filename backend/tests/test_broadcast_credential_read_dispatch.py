# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G6.1 ``credential_read`` dispatch test (decision #3, #549).

Vault KV reads carry the shape of an environment (mount, path, key
names) and the secret itself. The dispatcher writes one audit row per
dispatch; the row's payload must carry the ``credential_read`` class
and none of that material.

The test drives the **real** :func:`meho_backplane.operations.dispatch`
path against the in-process Vault fake (``install_fake_vault``) using
distinctive sentinel mount / path / key / value strings, then reads the
stored audit row back and checks by exclusion. A dispatch publishes no
Broadcast event (#3758), so the audit row is the only record to check.

A contrast op (``vault.sys.health``, class ``read``) goes through the
same path so the classification is shown to be op-driven.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vault import VaultConnector
from meho_backplane.connectors.vault.ops import register_vault_typed_operations
from meho_backplane.connectors.vault.ops_sys import (
    register_vault_sys_typed_operations,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.settings import get_settings
from tests._vault_fakes import install_fake_vault

# ---------------------------------------------------------------------------
# Sentinel secret material — distinctive enough that any leak is obvious
# ---------------------------------------------------------------------------

#: Non-default mount so a hardcoded ``"secret"`` default can't mask a
#: leak. Deliberately unusual so a substring search is unambiguous and
#: cannot collide with the (allowed) target name.
_SENTINEL_MOUNT = "sntlmount0zzz"
#: Folder/secret path. Each segment is its own distinctive token so a
#: *partial* leak (just one segment reaching the row) is still
#: caught — exactly the environment-name-shaped structure decision #3
#: forbids recording. No segment is a generic English word, so the
#: by-exclusion search can't false-positive on the allowed
#: ``target_name`` (``rdc-vault-prod``).
_SENTINEL_PATH_SEGMENTS = ("sntlenv0zzz", "sntlsvc0zzz", "sntlleaf0zzz")
_SENTINEL_PATH = "/".join(_SENTINEL_PATH_SEGMENTS)
#: Secret key names returned by the list op.
_SENTINEL_KEYS = ["sntlkey0apikey0zzz", "sntlkey0token0zzz"]
#: Secret value returned by the read op.
_SENTINEL_SECRET = {"password": "sntlvalue0p4ssw0rd0zzz"}

#: Every substring that MUST NOT appear anywhere in a ``credential_read``
#: audit row payload. The mount, the full path, **each path segment** (a
#: partial-leak regression is still a leak), every key name, and the
#: secret value. None of these sentinels appear in the allowed fields
#: (``op_id``, ``op_class``, ``result_status``, hashes), so a hit is an
#: unambiguous secret-material leak.
_FORBIDDEN_SUBSTRINGS: tuple[str, ...] = (
    _SENTINEL_MOUNT,
    _SENTINEL_PATH,
    *_SENTINEL_PATH_SEGMENTS,
    *_SENTINEL_KEYS,
    *_SENTINEL_SECRET.values(),
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env vars :class:`Settings` requires for this module.

    Mirrors :mod:`tests.test_operations_dispatcher` — the dispatcher
    constructs :class:`Settings` (Vault address, Keycloak issuer) while
    resolving the typed op, so the cache is cleared around the test.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    # The default-on tenant-scope guard (#1725) pins KV calls under
    # ``secret/tenants/{tenant_id}/``. These tests dispatch against the
    # ``_SENTINEL_MOUNT`` sentinel mount under a real operator tenant to
    # exercise the credential-classifier path, not tenant isolation
    # (covered by ``test_connectors_vault_tenant_scope.py``). The sentinel
    # path is not under ``secret/tenants/<id>/``, so the guard would deny
    # it with VaultTenantScopeError once Redis is present. Disable the
    # guard explicitly — matching the empty-prefix pin the #1725 PR used
    # for its e2e fixtures.
    monkeypatch.setenv("VAULT_KV_TENANT_SCOPE_PREFIX", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher caches + connector registry around every test.

    Registers the v2 ``vault`` connector entry after the setup clear so
    the dispatcher's natural-key resolution finds an implementation for
    ``connector_id="vault-1.x"`` — the per-test
    ``register_vault_typed_operations`` call only upserts the typed-op
    *descriptor* rows, not the connector *class*, and connector
    resolution is a hard gate before the handler runs (``no_connector``
    otherwise). Mirrors the ``_registered_vault_substrate`` fixture in
    ``test_api_v1_health.py``.
    """
    reset_dispatcher_caches()
    clear_registry()
    register_connector_v2(product="vault", version="1.x", impl_id="vault", cls=VaultConnector)
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub so ``register_typed_operation`` skips ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


def _make_operator() -> Operator:
    """Construct an :class:`Operator` directly — no JWT round-trip.

    ``raw_jwt`` is read by
    :func:`~meho_backplane.auth.vault.vault_client_for_operator`, which
    the real Vault handlers call. The value is irrelevant — the fake
    Vault client (``install_fake_vault``) accepts any JWT.
    """
    return Operator(
        sub="op-credential-read-test",
        name="Credential Read Test Operator",
        email=None,
        raw_jwt="header.payload.signature",
        tenant_id=UUID("00000000-0000-0000-0000-0000000000c3"),
        tenant_role=TenantRole.OPERATOR,
    )


class _FakeFingerprint:
    """Duck-typed fingerprint — the resolver reads only ``version``."""

    def __init__(self, version: str | None = "1.x") -> None:
        self.version = version


class _VaultDispatchTarget:
    """Target that satisfies the dispatcher's resolution + audit reads.

    The dispatcher reads ``product`` / ``fingerprint.version`` /
    ``preferred_impl_id`` (connector resolution — tolerated-miss for
    typed ops) and ``id`` / ``name`` (audit row). The Vault handlers
    read the operator JWT from the request-scoped
    :class:`~meho_backplane.auth.operator.Operator` the dispatcher
    threads (G0.8-T3 #629), **not** from this target — so it carries
    no ``raw_jwt``.

    ``name`` is a benign, non-secret label. It is deliberately distinct
    from every forbidden sentinel so the by-exclusion assertion can't
    false-positive on it.
    """

    def __init__(self) -> None:
        self.product = "vault"
        self.fingerprint = _FakeFingerprint(version="1.x")
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.name = "rdc-vault-prod"


# ---------------------------------------------------------------------------
# credential_read audit-row contract — vault.kv.read / vault.kv.list
# ---------------------------------------------------------------------------


async def _audit_row_for(op_id: str) -> AuditLog:
    """Return the single dispatch audit row written for *op_id*."""
    async with get_sessionmaker()() as session:
        rows = (
            (await session.execute(select(AuditLog).where(AuditLog.path == op_id))).scalars().all()
        )
    assert len(rows) == 1, f"expected one audit row for {op_id}, got {len(rows)}"
    return rows[0]


def _assert_no_secret_in_row(row: AuditLog, op_id: str) -> None:
    """Assert the credential-read audit row for *op_id* carries no secret material."""
    assert row.payload["op_id"] == op_id
    assert row.payload["op_class"] == "credential_read"
    assert row.payload["result_status"] == "ok"

    serialised = json.dumps(row.payload, default=str)
    for forbidden in _FORBIDDEN_SUBSTRINGS:
        assert forbidden not in serialised, (
            f"credential_read leak: {forbidden!r} reached the audit row "
            f"for {op_id} — payload: {serialised}"
        )


@pytest.mark.asyncio
async def test_vault_kv_read_secret_not_in_audit_row(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
) -> None:
    """``vault.kv.read`` is audited as ``credential_read`` without the secret.

    DoD #1 — mount / path / key / value never reach the audit row
    (by-exclusion).
    """
    fake = install_fake_vault(monkeypatch)
    fake.secrets.kv.v2.secret = dict(_SENTINEL_SECRET)

    await register_vault_typed_operations(embedding_service=stub_embedding_service)

    operator = _make_operator()
    target = _VaultDispatchTarget()

    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.kv.read",
        target=target,
        params={"mount": _SENTINEL_MOUNT, "path": _SENTINEL_PATH},
    )

    assert result.status == "ok", result.error
    # The handler really ran against the fake: the read landed and the
    # result carries the data block. The response redactor masks the
    # password value before it reaches the caller, so the exclusion
    # below is checked on the audit row, not on the result.
    assert set(result.result) == {"data", "version"}
    assert result.result["version"] == fake.secrets.kv.v2.version
    assert fake.secrets.kv.v2.read_calls == [
        {"path": _SENTINEL_PATH, "mount_point": _SENTINEL_MOUNT}
    ]

    _assert_no_secret_in_row(await _audit_row_for("vault.kv.read"), "vault.kv.read")


@pytest.mark.asyncio
async def test_vault_kv_list_secret_not_in_audit_row(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
) -> None:
    """``vault.kv.list`` is audited as ``credential_read`` without the key names.

    DoD #2 — same by-exclusion assertion as the read op. Key names are
    structure too (decision #3) — the list op is ``credential_read``
    even though it returns no secret values.
    """
    fake = install_fake_vault(monkeypatch)
    fake.secrets.kv.v2.keys = list(_SENTINEL_KEYS)

    await register_vault_typed_operations(embedding_service=stub_embedding_service)

    operator = _make_operator()
    target = _VaultDispatchTarget()

    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.kv.list",
        target=target,
        params={"mount": _SENTINEL_MOUNT, "path": _SENTINEL_PATH},
    )

    assert result.status == "ok", result.error
    assert result.result == {"keys": _SENTINEL_KEYS}
    assert fake.secrets.kv.v2.list_calls == [
        {"path": _SENTINEL_PATH, "mount_point": _SENTINEL_MOUNT}
    ]

    _assert_no_secret_in_row(await _audit_row_for("vault.kv.list"), "vault.kv.list")


# ---------------------------------------------------------------------------
# Contrast — a non-credential_read op is audited with its own class
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_credential_read_op_is_audited_as_read(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
) -> None:
    """``vault.sys.health`` is audited with class ``read``.

    DoD #3 — proves the classification is op-driven, not blanket.
    ``vault.sys.health`` classifies ``read`` (the ``.health``
    suffix). Same dispatch + audit path, same fake Vault — only the
    op-id differs, so a divergence here is purely the classifier doing
    its job.
    """
    install_fake_vault(monkeypatch)
    await register_vault_sys_typed_operations(embedding_service=stub_embedding_service)

    operator = _make_operator()
    target = _VaultDispatchTarget()

    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.sys.health",
        target=target,
        params={},
    )

    assert result.status == "ok", result.error

    row = await _audit_row_for("vault.sys.health")
    assert row.payload["op_id"] == "vault.sys.health"
    # Classifier-driven: NOT credential_read.
    assert row.payload["op_class"] == "read"
    assert row.payload["result_status"] == "ok"


# ---------------------------------------------------------------------------
# Classifier-contrast on a single fixed input — same op-id surface,
# different class. Pins the "credential_read vs read" boundary that
# the whole PII guarantee rests on.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_credential_read_and_contrast_diverge_on_same_dispatch_path(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
) -> None:
    """One run, two ops: read is ``credential_read``, sys.health is ``read``.

    Dispatches a ``credential_read`` op and a ``read`` op back-to-back
    through the identical path with identical infrastructure. The only
    variable is the op-id; the divergent class is therefore
    attributable solely to :func:`classify_op`. This is the
    load-bearing assertion decision #3 exists for.
    """
    fake = install_fake_vault(monkeypatch)
    fake.secrets.kv.v2.secret = dict(_SENTINEL_SECRET)

    await register_vault_typed_operations(embedding_service=stub_embedding_service)
    await register_vault_sys_typed_operations(embedding_service=stub_embedding_service)

    operator = _make_operator()
    target = _VaultDispatchTarget()

    read_result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.kv.read",
        target=target,
        params={"mount": _SENTINEL_MOUNT, "path": _SENTINEL_PATH},
    )
    health_result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.sys.health",
        target=target,
        params={},
    )

    assert read_result.status == "ok", read_result.error
    assert health_result.status == "ok", health_result.error

    cred_row = await _audit_row_for("vault.kv.read")
    health_row = await _audit_row_for("vault.sys.health")

    # Same path, different class — classifier-driven by construction.
    assert cred_row.payload["op_class"] == "credential_read"
    assert health_row.payload["op_class"] == "read"
    _assert_no_secret_in_row(cred_row, "vault.kv.read")
