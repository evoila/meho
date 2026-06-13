# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G11.7-T1 (#1401) ``credential_write`` broadcast-redaction dispatch test.

Proves the request-side secret-redaction guarantee for the new Phase-C
write ops whose secret rides in the *request params* (Vault
``userpass.write`` / ``update_password``, ``vault.kv.put``, k8s
``secret.create``): the broadcast feed must emit **aggregate-only** — the
fact that a credential was written, never the secret material — while the
:class:`~meho_backplane.connectors.schemas.OperationResult` returned to the
caller is unaffected.

Mechanism mirrors :mod:`tests.test_broadcast_credential_mint_dispatch`
(``credential_mint``, response-side secret) but for the request-side
class:

* :func:`~meho_backplane.broadcast.events.classify_op` returns
  ``"credential_write"`` for the listed ops.
* :func:`~meho_backplane.broadcast.events.redact_payload` collapses
  ``credential_write`` to ``{op_class, result_status}``.
* The broadcast publisher is swapped for a recording stub so the emitted
  :class:`~meho_backplane.broadcast.events.BroadcastEvent` is inspectable
  without a Valkey container — so this test runs **unconditionally** (no
  ``BROADCAST_REDIS_URL`` skip guard), closing the AC4 leak-assertion gap
  deterministically in the sandbox.

The assertion is by-exclusion on the *whole serialised event*, not just
``payload``: a regression that placed the secret in a new top-level field
would pass a ``payload``-only check.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest

import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast import BroadcastEvent
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
from meho_backplane.settings import get_settings

#: Distinctive secret — any appearance in a serialised event is a leak.
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
def captured_events(monkeypatch: pytest.MonkeyPatch) -> list[BroadcastEvent]:
    """Replace :func:`publish_event` with an in-memory recording stub."""
    events: list[BroadcastEvent] = []

    async def _capture(event: BroadcastEvent) -> None:
        events.append(event)

    monkeypatch.setattr(audit_module, "publish_event", _capture)
    return events


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
async def test_credential_write_broadcast_is_aggregate_only(
    op_id: str,
    captured_events: list[BroadcastEvent],
    stub_embedding_service: AsyncMock,
) -> None:
    """A request-secret write broadcasts only ``{op_class, result_status}``.

    AC4 (#1401): the written secret in ``params`` never appears in the
    serialised BroadcastEvent. Driven through the real dispatcher +
    broadcast path so the redaction is proven where it ships, not just
    in a unit call to :func:`redact_payload`.
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

    # The secret rides in params — exactly what the broadcast must not leak.
    result = await dispatch(
        operator=operator,
        connector_id="credwrite-1.x",
        op_id=op_id,
        target=_Target(),
        params={"path": "secret/db", "data": {"password": _SENTINEL_SECRET}},
    )
    assert result.status == "ok", result.error

    assert len(captured_events) == 1
    event = captured_events[0]
    assert event.op_id == op_id
    assert event.op_class == "credential_write"
    assert event.payload == {"op_class": "credential_write", "result_status": "ok"}

    serialised = event.model_dump_json()
    assert _SENTINEL_SECRET not in serialised, (
        f"credential_write leak: {_SENTINEL_SECRET!r} reached the broadcast "
        f"event for {op_id} — serialised event: {serialised}"
    )
