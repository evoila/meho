# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the satellite-runner work-item executor (#2497).

Covers the four executor contracts: a real ``safety_level="safe"`` op
executes and returns a structured result; a non-``safe`` op is refused
without invocation; a ``handler_ref`` outside the connector tree is
refused fail-closed; and a handler that raises becomes a structured
error result rather than a raised tick error.
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from meho_backplane.auth.operator import TenantRole
from meho_backplane.runner.executor import _verify_remote_write_signature, execute_work_item
from meho_backplane.runner.wire import (
    ResolvedTargetDescriptor,
    RunnerPrincipal,
    RunnerWorkItem,
)
from meho_backplane.runner.work_item_signing import (
    TARGETLESS_SCOPE,
    params_digest,
    sign_remote_write_item,
)
from meho_backplane.settings import get_settings

_ALLOWLIST_ENV = "MEHO_NETDIAG_PROBE_ALLOWLIST"
_NET_TCP_CHECK_REF = "meho_backplane.connectors.net.ops.net_tcp_check"


def _principal() -> RunnerPrincipal:
    return RunnerPrincipal(
        sub="runner-svc",
        tenant_id=uuid.uuid4(),
        tenant_role=TenantRole.READ_ONLY,
    )


def _tcp_check_item(*, host: str, port: int, **overrides: object) -> RunnerWorkItem:
    item = RunnerWorkItem(
        check_ref="chk-1",
        op_id="net.tcp_check",
        product="net",
        version="1.x",
        impl_id="net-probe",
        handler_ref=_NET_TCP_CHECK_REF,
        params={"host": host, "port": port},
        safety_level="safe",
        principal=_principal(),
    )
    return item.model_copy(update=overrides) if overrides else item


async def test_safe_op_executes_and_returns_structured_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_ALLOWLIST_ENV, "127.0.0.1")
    server = await asyncio.start_server(lambda _r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        item = _tcp_check_item(host="127.0.0.1", port=port)
        result = await execute_work_item(item)
    finally:
        server.close()
        await server.wait_closed()

    assert result.status == "ok"
    assert result.op_id == "net.tcp_check"
    assert result.check_ref == "chk-1"
    assert result.error is None
    assert result.result is not None
    # Structured reachability payload from the net.tcp_check handler.
    assert result.result["connected"] is True
    assert result.result["host"] == "127.0.0.1"
    # Runner-generated dedup id.
    assert len(result.result_uid) == 32


async def test_remote_write_op_is_refused_without_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An allowlisted host would let the probe succeed *if* it ran — proving
    # the refusal short-circuits before the handler is ever invoked. A
    # `caution` op is the `remote-write` tier: it fails closed at the edge
    # (mechanism 2's edge re-check) until the runner is authorised (#3188).
    monkeypatch.setenv(_ALLOWLIST_ENV, "127.0.0.1")
    item = _tcp_check_item(host="127.0.0.1", port=9, safety_level="caution", check_ref="chk-2")

    result = await execute_work_item(item)

    assert result.status == "refused"
    assert result.result is None
    assert "remote-write" in (result.error or "")


@pytest.mark.parametrize("level", ["dangerous", "destructive"])
async def test_excluded_op_is_refused_without_invocation(
    monkeypatch: pytest.MonkeyPatch, level: str
) -> None:
    # `dangerous` / `destructive` are the EXCLUDED tier — never dispatched to
    # a runner, refused at the edge as defence in depth (#3188).
    monkeypatch.setenv(_ALLOWLIST_ENV, "127.0.0.1")
    item = _tcp_check_item(host="127.0.0.1", port=9, safety_level=level, check_ref="chk-x")

    result = await execute_work_item(item)

    assert result.status == "refused"
    assert result.result is None
    assert level in (result.error or "")
    assert "never dispatched" in (result.error or "")


async def test_out_of_tree_handler_ref_is_refused_fail_closed() -> None:
    item = _tcp_check_item(host="127.0.0.1", port=9, handler_ref="os.system", check_ref="chk-3")

    result = await execute_work_item(item)

    assert result.status == "refused"
    assert result.result is None
    assert "os.system" in (result.error or "")


async def test_handler_exception_becomes_structured_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_ALLOWLIST_ENV, "127.0.0.1")
    # Omit the required ``port`` param so net_tcp_check raises KeyError.
    item = _tcp_check_item(host="127.0.0.1", port=9, check_ref="chk-4")
    item = item.model_copy(update={"params": {"host": "127.0.0.1"}})

    result = await execute_work_item(item)

    assert result.status == "error"
    assert result.result is None
    assert "KeyError" in (result.error or "")


# ---------------------------------------------------------------------------
# Remote-write signature verification at the edge (#3189, mechanism 1)
# ---------------------------------------------------------------------------


def _provision_verify_key(monkeypatch: pytest.MonkeyPatch) -> Ed25519PrivateKey:
    """Generate a keypair, provision the runner's verify key, return the signer."""
    key = Ed25519PrivateKey.generate()
    verify_b64 = base64.b64encode(key.public_key().public_bytes_raw()).decode("ascii")
    monkeypatch.setenv("SATELLITE_WRITE_VERIFY_KEY", verify_b64)
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    return key


def _target_descriptor(target_id: uuid.UUID) -> ResolvedTargetDescriptor:
    return ResolvedTargetDescriptor(
        id=target_id,
        tenant_id=uuid.uuid4(),
        name="vc-a",
        product="vmware",
        host="vc-a.example",
    )


def _signed_rw_item(
    signing_key: Ed25519PrivateKey,
    *,
    expires_at: datetime,
    target_descriptor: ResolvedTargetDescriptor | None = None,
    op_id: str = "vmware.vm.tag_set",
    params: dict[str, object] | None = None,
) -> RunnerWorkItem:
    params = params if params is not None else {"tag": "prod"}
    target_scope = str(target_descriptor.id) if target_descriptor is not None else TARGETLESS_SCOPE
    signature = sign_remote_write_item(
        signing_key,
        op_id=op_id,
        params_hash=params_digest(params),
        target_scope=target_scope,
        expires_at=expires_at,
    )
    return RunnerWorkItem(
        check_ref="chk-rw",
        op_id=op_id,
        product="vmware",
        version="9.0",
        impl_id="rest",
        handler_ref="meho_backplane.connectors.vmware.ops.vm_tag_set",
        params=params,
        safety_level="caution",
        principal=_principal(),
        target_descriptor=target_descriptor,
        signature=signature,
        expires_at=expires_at,
    )


def test_verify_passes_for_a_validly_signed_fresh_item(monkeypatch: pytest.MonkeyPatch) -> None:
    signing_key = _provision_verify_key(monkeypatch)
    item = _signed_rw_item(signing_key, expires_at=datetime.now(UTC) + timedelta(minutes=5))
    # The signature half passes; the remaining edge refusal (the still-fail-closed
    # allowlist gate, #3190) is a *different* mechanism, asserted separately below.
    assert _verify_remote_write_signature(item) is None


def test_unsigned_remote_write_item_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _provision_verify_key(monkeypatch)
    item = _tcp_check_item(host="127.0.0.1", port=9, safety_level="caution", check_ref="chk-rw")
    refusal = _verify_remote_write_signature(item)
    assert refusal is not None and "unsigned" in refusal and "remote-write" in refusal


def test_missing_freshness_bound_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    signing_key = _provision_verify_key(monkeypatch)
    item = _signed_rw_item(signing_key, expires_at=datetime.now(UTC) + timedelta(minutes=5))
    item = item.model_copy(update={"expires_at": None})
    refusal = _verify_remote_write_signature(item)
    assert refusal is not None and "expires_at" in refusal


def test_tampered_params_break_the_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    signing_key = _provision_verify_key(monkeypatch)
    item = _signed_rw_item(signing_key, expires_at=datetime.now(UTC) + timedelta(minutes=5))
    item = item.model_copy(update={"params": {"tag": "attacker"}})
    refusal = _verify_remote_write_signature(item)
    assert refusal is not None and "signature verification failed" in refusal


def test_out_of_scope_target_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    signing_key = _provision_verify_key(monkeypatch)
    signed_for = _target_descriptor(uuid.uuid4())
    item = _signed_rw_item(
        signing_key,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        target_descriptor=signed_for,
    )
    # Re-point the delivered item at a *different* target than the one signed.
    item = item.model_copy(update={"target_descriptor": _target_descriptor(uuid.uuid4())})
    refusal = _verify_remote_write_signature(item)
    assert refusal is not None and "signature verification failed" in refusal


def test_expired_but_validly_signed_item_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    signing_key = _provision_verify_key(monkeypatch)
    # A cryptographically valid signature over a past deadline — the separate
    # freshness check must still refuse it.
    item = _signed_rw_item(signing_key, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    refusal = _verify_remote_write_signature(item)
    assert refusal is not None and "expired" in refusal


def test_refused_when_no_verify_key_provisioned(monkeypatch: pytest.MonkeyPatch) -> None:
    signing_key = _provision_verify_key(monkeypatch)
    item = _signed_rw_item(signing_key, expires_at=datetime.now(UTC) + timedelta(minutes=5))
    monkeypatch.setenv("SATELLITE_WRITE_VERIFY_KEY", "")
    get_settings.cache_clear()
    refusal = _verify_remote_write_signature(item)
    assert refusal is not None and "remote-write" in refusal


async def test_tampered_signed_item_is_refused_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    # Through the real execute_work_item path: a tampered signed caution item is
    # refused before the handler is ever resolved.
    signing_key = _provision_verify_key(monkeypatch)
    item = _signed_rw_item(signing_key, expires_at=datetime.now(UTC) + timedelta(minutes=5))
    item = item.model_copy(update={"params": {"tag": "attacker"}})

    result = await execute_work_item(item)

    assert result.status == "refused"
    assert result.result is None
    assert "remote-write" in (result.error or "")
