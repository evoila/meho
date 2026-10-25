# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G11.7-T1 (#1401) ``credential_write`` dispatch test.

Covers the write ops whose secret rides in the *request params* (Vault
``userpass.write`` / ``update_password``, ``vault.kv.put``, k8s
``secret.create``):

* :func:`~meho_backplane.broadcast.events.classify_op` returns
  ``"credential_write"`` for the listed ops, and the audit row records it.
* The written secret never reaches the stored audit row. The check looks
  at the *whole* row payload, so a regression that adds the secret under
  a new key is still caught.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
from meho_backplane.settings import get_settings

#: Distinctive secret — any appearance in the audit row is a leak.
_SENTINEL_SECRET = "wr1tesecret0sentinel"

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000007a1")


async def _write_handler(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Echo a benign ack — the secret stays in params, never the response."""
    return {"written": True}


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    # The default-on tenant-scope guard (#1725) pins KV calls under
    # ``secret/tenants/{tenant_id}/``. This test dispatches a
    # ``vault.kv.write`` against a sentinel mount under a real operator
    # tenant to exercise the credential-classifier redaction path, not
    # tenant isolation (covered by ``test_connectors_vault_tenant_scope.py``).
    # The sentinel path is not under ``secret/tenants/<id>/``, so the guard
    # would deny it with VaultTenantScopeError once Redis is present.
    # Disable the guard explicitly — matching the empty-prefix pin the
    # #1725 PR used for its e2e fixtures.
    monkeypatch.setenv("VAULT_KV_TENANT_SCOPE_PREFIX", "")
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


def _make_operator() -> Operator:
    return Operator(
        sub="op-credential-write-test",
        name="Credential Write Test Operator",
        email=None,
        raw_jwt="header.payload.signature",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.AGENT,
    )


class _WriteConnector(Connector):
    product = "credwrite"
    version = "1.x"
    impl_id = "credwrite"
    priority = 10

    async def fingerprint(self, host: str, port: int | None) -> FingerprintResult:
        return FingerprintResult(
            probe=ProbeResult(reachable=True, probe_method="none"),
            product="credwrite",
            version="1.x",
        )

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self, target: Any, op_id: str, params: dict[str, Any]
    ) -> Any:
        # Typed op: the registered handler does the work, not this.
        raise NotImplementedError


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "op_id",
    [
        "vault.auth.userpass.write",
        "vault.auth.userpass.update_password",
        "vault.kv.put",
        "k8s.secret.create",
    ],
)
async def test_credential_write_secret_not_in_audit_row(
    op_id: str,
    stub_embedding_service: AsyncMock,
) -> None:
    """A request-secret write is audited as ``credential_write`` without the secret.

    AC4 (#1401): the written secret in ``params`` never appears in the
    stored audit row. Driven through the real dispatcher + audit path.
    """
    register_connector_v2(product="credwrite", version="", impl_id="", cls=_WriteConnector)
    await register_typed_operation(
        product="credwrite",
        version="1.x",
        impl_id="credwrite",
        op_id=op_id,
        handler=_write_handler,
        summary="Secret-bearing write op.",
        description="Test.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    operator = _make_operator()

    class _Target:
        product = "credwrite"
        id = uuid.uuid4()
        name = "cred-write-target"

    # The secret rides in params — exactly what the audit row must not keep.
    result = await dispatch(
        operator=operator,
        connector_id="credwrite-1.x",
        op_id=op_id,
        target=_Target(),
        params={"path": "secret/db", "data": {"password": _SENTINEL_SECRET}},
    )
    assert result.status == "ok", result.error

    async with get_sessionmaker()() as fresh:
        rows = (await fresh.execute(select(AuditLog).where(AuditLog.path == op_id))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.payload["op_class"] == "credential_write"
    serialised = repr(row.payload)
    assert _SENTINEL_SECRET not in serialised, (
        f"credential_write leak: {_SENTINEL_SECRET!r} reached the audit row "
        f"for {op_id} — payload: {serialised}"
    )
