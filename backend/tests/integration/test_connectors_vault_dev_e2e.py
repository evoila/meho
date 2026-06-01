# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G3.3-T7 — Vault dev-mode CI integration harness + kv/sys/auth e2e.

Boots a real ``hashicorp/vault:1.18`` server in dev mode via
testcontainers, seeds the surfaces every op in Initiative #366 touches
(KV-v2 ``secret/``, ``userpass``, ``approle``), then dispatches every
registered ``vault.kv.*`` / ``vault.sys.*`` / ``vault.auth.*`` op
through the **real G0.6 dispatcher** against the live Vault and a real
Postgres audit store. The unit suites (``test_connectors_vault*.py``)
mock hvac via a fake client; this is the integration layer that proves
the connector code actually round-trips against a running Vault and
that the dispatcher's synchronous audit write and op-class
classification hold end to end.

What this harness proves (issue #551 DoD)
=========================================

* ``hashicorp/vault:1.18`` dev-mode container, image overridable via
  ``MEHO_TEST_VAULT_IMAGE`` so the CI runner pulls through the
  in-cluster Harbor proxy (same env-knob shape as
  ``MEHO_TEST_PGVECTOR_IMAGE`` / ``MEHO_TEST_VALKEY_IMAGE``;
  ``ci.yml`` sets it). Docker-socket-absent sandbox skips cleanly —
  the gate matches ``tests/integration/conftest.py``.
* Fixture seeds: KV-v2 mount (dev mode mounts ``secret/`` as v2 by
  default) with sample secrets plus one path holding **> 50 keys**
  (the JSONFlux fixture G3.3-T4's threshold test also consumes), a
  ``userpass`` user, and an ``approle`` role.
* Every registered Vault op dispatches through
  :func:`~meho_backplane.operations.dispatch`; each asserts the live
  response shape, that a synchronous ``audit_log`` row committed
  (CLAUDE.md postulate 7), and that the broadcast event's
  ``op_class`` is correct — ``credential_write`` for ``vault.kv.put``
  (its KV-v2 secret rides in the request params, so the broadcast
  collapses to aggregate-only per G11.7-T1 #1401), ``write`` for
  ``vault.kv.delete``, ``credential_read`` for ``vault.kv.read`` /
  ``vault.kv.list``, ``read`` for the KV-v2 / sys metadata reads and
  the ``.list`` auth ops, and ``other`` for the two ``.read``
  auth-config ops (``vault.auth.userpass.read`` /
  ``vault.auth.approle.read``). ``.read`` is deliberately absent from
  ``_READ_SUFFIXES`` so the suffix check never over-matches the
  ``credential_read``-allowlisted ``vault.kv.read``; the auth-config
  ``.read`` ops therefore classify ``other``, the safe
  over-broadcast direction for non-secret auth-method metadata
  (decision #3). The test assertions below encode this split.
* The dev-root token is generated into the container via
  ``VAULT_DEV_ROOT_TOKEN_ID`` and only ever held in the fixture's
  return value — it is never written to a workflow log, a committed
  file, or an assertion message (secrets-in-fixtures discipline).
* Hermetic: no external network (testcontainers-local Vault, RFC-safe
  image tag), deterministic (pinned image, fixed seed data).

The Vault client seam
=====================

The connector handlers reach Vault through
:func:`meho_backplane.auth.vault.vault_client_for_operator` (an OIDC
``jwt_login`` context manager) and ``vault.sys.health`` /
``vault.sys.seal_status`` additionally through
:func:`meho_backplane.auth.vault._build_client`. Dev mode has no OIDC
auth method wired, so this harness monkeypatches
``vault_client_for_operator`` to yield a root-token
:class:`hvac.Client` bound to the container, and pins ``VAULT_ADDR``
so the ``_build_client`` path resolves to the same container. This is
the documented single-seam test approach the unit suites already use
(``monkeypatch.setattr(vault_module, ...)``); the full connector code
path (handler → hvac call → structural unwrap → dispatcher reduce →
audit → broadcast) runs unchanged. Only the credential acquisition is
swapped, exactly as a production OIDC login would have produced an
authenticated client.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import hvac
import pytest
from sqlalchemy import select

import meho_backplane.auth.vault as _auth_vault
import meho_backplane.operations._audit as _audit_module
from meho_backplane.broadcast import BroadcastEvent
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vault import (
    VaultConnector,
    register_vault_sys_typed_operations,
    register_vault_typed_operations,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.reducer import PassThroughReducer
from tests.test_operations_dispatcher import _make_operator

# ---------------------------------------------------------------------------
# Docker-availability gate — identical heuristic to
# tests/integration/conftest.py so every testcontainers suite skips on
# the same signal.
# ---------------------------------------------------------------------------


def _docker_socket_present() -> bool:
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


DOCKER_AVAILABLE: bool = _docker_socket_present()
SKIP_REASON: str = (
    "Docker socket unavailable in this sandbox; runs in CI where containers are provisioned."
)

#: Dev-mode root token. Generated *into* the container via
#: ``VAULT_DEV_ROOT_TOKEN_ID``; this is a throwaway value scoped to a
#: per-test-run in-memory Vault that never persists and is never
#: reachable off the runner. Kept as a module constant (not derived
#: from a real secret) so the seed code reads cleanly; it is never
#: logged or echoed into an assertion message.
_DEV_ROOT_TOKEN: str = "meho-dev-root-551"

#: KV-v2 mount dev mode provides out of the box (``-dev`` mounts
#: ``secret/`` as v2). The connector's ``_DEFAULT_KV_MOUNT`` is
#: ``"secret"`` so the ops resolve here without an explicit ``mount``.
_KV_MOUNT: str = "secret"

#: Path seeded with > 50 keys for the JSONFlux set-shape fixture.
#: G3.3-T4 (#566) owns the threshold/handle assertion; this harness
#: only guarantees the fixture exists and the inline pass-through
#: default returns every key (v0.2 ships PassThroughReducer).
_BULK_PATH_PREFIX: str = "bulk"
_BULK_KEY_COUNT: int = 60


@dataclass
class _VaultTarget:
    """Minimal duck-typed target the dispatcher / resolver / audit read.

    The vault handlers read the operator JWT from the request-scoped
    :class:`~meho_backplane.auth.operator.Operator` the dispatcher
    threads (G0.8-T3 #629), **not** from the target — so this stub
    carries no ``raw_jwt``. ``id`` / ``name`` / ``product`` / ``host``
    / ``port`` / ``auth_model`` cover what the resolver and audit row
    read.
    """

    product: str = "vault"
    name: str = "vault-dev"
    host: str = "127.0.0.1"
    port: int = 8200
    auth_model: str = "shared_service_account"

    def __post_init__(self) -> None:
        import uuid

        self.id = uuid.uuid4()
        self.preferred_impl_id: str | None = None

        class _FP:
            version = "1.18.0"

        self.fingerprint = _FP()


# ---------------------------------------------------------------------------
# Vault dev-mode container — module-scoped (one boot, seeded once)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def vault_dev_addr() -> Iterator[str]:
    """Boot ``hashicorp/vault:1.18 -dev``, seed it, yield its address.

    Module scope amortises the container boot across every op test.
    The image's default entrypoint runs ``vault server -dev``;
    ``VAULT_DEV_ROOT_TOKEN_ID`` pins the generated root token and
    ``VAULT_DEV_LISTEN_ADDRESS`` binds ``0.0.0.0:8200`` so the
    host-mapped port is reachable. ``IPC_LOCK`` is granted because
    Vault mlocks memory (it logs a warning and continues without it,
    but granting it matches the documented run contract).
    """
    if not DOCKER_AVAILABLE:
        pytest.skip(SKIP_REASON)

    # Local import: testcontainers transitively imports the docker SDK
    # which probes the socket on import. Keeping it inside the fixture
    # lets the module collect on a no-Docker sandbox and skip cleanly.
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.waiting_utils import wait_for_logs

    image = os.environ.get("MEHO_TEST_VAULT_IMAGE", "hashicorp/vault:1.18")
    container = (
        DockerContainer(image)
        .with_env("VAULT_DEV_ROOT_TOKEN_ID", _DEV_ROOT_TOKEN)
        .with_env("VAULT_DEV_LISTEN_ADDRESS", "0.0.0.0:8200")
        .with_exposed_ports(8200)
        .with_kwargs(cap_add=["IPC_LOCK"])
    )
    try:
        container.start()
    except Exception as exc:
        # Any boot failure (privileged denied, image pull rate-limit,
        # cgroup refusal) → clean skip, not a red suite — same stance
        # the k3d / pgvector container fixtures take.
        pytest.skip(f"vault dev container failed to start ({type(exc).__name__}): {exc}")

    try:
        # Vault logs this line once the dev server is unsealed and
        # serving; testcontainers polls the log stream until it appears
        # or the timeout trips.
        wait_for_logs(container, "Vault server started!", timeout=60)
        host = container.get_container_host_ip()
        port = container.get_exposed_port(8200)
        addr = f"http://{host}:{port}"
        _seed_vault(addr)
        yield addr
    finally:
        container.stop()


def _root_client(addr: str) -> hvac.Client:
    """Construct a root-token hvac client bound to the dev container."""
    return hvac.Client(url=addr, token=_DEV_ROOT_TOKEN)


def _seed_vault(addr: str) -> None:
    """Seed every surface the Initiative's ops exercise.

    * KV-v2 ``secret/`` (mounted by dev mode) — one ordinary secret,
      one secret that gets a second version (so ``vault.kv.versions``
      sees ``current_version >= 2``), and a ``bulk/`` path holding
      ``_BULK_KEY_COUNT`` (> 50) child keys for the JSONFlux fixture.
    * ``userpass`` auth method enabled + one user seeded.
    * ``approle`` auth method enabled + one role seeded.
    """
    client = _root_client(addr)

    # --- KV-v2 sample secrets ---------------------------------------
    client.secrets.kv.v2.create_or_update_secret(
        path="app/config",
        secret={"db_url": "postgres://example", "feature_flag": "on"},
        mount_point=_KV_MOUNT,
    )
    # Two writes → current_version == 2 so the versions op has > 1.
    client.secrets.kv.v2.create_or_update_secret(
        path="app/rotating",
        secret={"token": "v1"},
        mount_point=_KV_MOUNT,
    )
    client.secrets.kv.v2.create_or_update_secret(
        path="app/rotating",
        secret={"token": "v2"},
        mount_point=_KV_MOUNT,
    )
    # > 50 sibling keys under one folder for the JSONFlux set-shape
    # fixture. KV-v2 list returns the child key names beneath the
    # folder; each is a distinct secret.
    for i in range(_BULK_KEY_COUNT):
        client.secrets.kv.v2.create_or_update_secret(
            path=f"{_BULK_PATH_PREFIX}/key-{i:03d}",
            secret={"i": str(i)},
            mount_point=_KV_MOUNT,
        )

    # --- userpass --------------------------------------------------
    client.sys.enable_auth_method(method_type="userpass", path="userpass")
    client.auth.userpass.create_or_update_user(
        username="ci-operator",
        # Throwaway dev-mode credential on an in-memory Vault that
        # never persists and is never reachable off the runner.
        password="ci-operator-pw",
        policies=["default"],
        mount_point="userpass",
    )

    # --- approle ---------------------------------------------------
    client.sys.enable_auth_method(method_type="approle", path="approle")
    client.auth.approle.create_or_update_approle(
        role_name="ci-role",
        token_policies=["default"],
        token_ttl="1h",
        mount_point="approle",
    )


# ---------------------------------------------------------------------------
# Per-test wiring: seam swap, broadcast capture, descriptor registration
# ---------------------------------------------------------------------------


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> list[BroadcastEvent]:
    """Record every :func:`publish_event` the dispatcher emits.

    Patches the imported reference inside
    :mod:`meho_backplane.operations._audit` — the same target the
    dispatcher unit suite uses — so no Valkey container is needed to
    assert on ``op_class``.
    """
    events: list[BroadcastEvent] = []

    async def _capture(event: BroadcastEvent) -> None:
        events.append(event)

    monkeypatch.setattr(_audit_module, "publish_event", _capture)
    return events


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub so registration doesn't load ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def vault_e2e(
    vault_dev_addr: str,
    pg_engine: None,
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
) -> AsyncIterator[tuple[_VaultTarget, str]]:
    """Wire the connector at the live dev Vault + real PG audit store.

    * ``pg_engine`` (from ``tests/integration/conftest.py``) points the
      module-level engine cache at a migrated Postgres testcontainer so
      ``register_typed_operation`` writes the descriptor rows and the
      dispatcher's synchronous audit write both land in real Postgres.
    * The v2 connector registry is cleared and the connector
      re-registered against a known-empty table — importing
      :mod:`meho_backplane.connectors.vault` already self-registers it
      at module-import time, so ``register_connector_v2`` would raise
      ``RuntimeError: connector already registered`` without the
      clear. Same ``clear_registry()`` discipline the dispatcher unit
      suite's ``_reset_module_state`` fixture uses. Every Vault op
      group's typed registrar then runs against the PG store.
    * ``vault_client_for_operator`` is replaced with a context manager
      yielding a **root-token** client bound to the dev container, and
      ``VAULT_ADDR`` is pinned so the ``_build_client`` path
      (``vault.sys.health`` / ``vault.sys.seal_status``) resolves to
      the same container.
    """
    reset_dispatcher_caches()
    set_default_reducer(PassThroughReducer())

    # Importing the vault package self-registered the connector at
    # module-import time; clear first so the explicit re-register lands
    # against a known-empty table rather than raising the
    # already-registered RuntimeError.
    clear_registry()
    register_connector_v2(
        product="vault",
        version="1.x",
        impl_id="vault",
        cls=VaultConnector,
    )
    await register_vault_typed_operations(embedding_service=stub_embedding_service)
    await register_vault_sys_typed_operations(embedding_service=stub_embedding_service)

    @asynccontextmanager
    async def _root_client_cm(_target: Any) -> AsyncIterator[hvac.Client]:
        # Mirrors the production context manager's contract: yield an
        # authenticated client, no revoke needed for a root token on a
        # throwaway in-memory dev Vault.
        yield _root_client(vault_dev_addr)

    monkeypatch.setattr(_auth_vault, "vault_client_for_operator", _root_client_cm)
    # _build_client reads VAULT_ADDR via Settings; pin it at the
    # container so the sys.health / sys.seal_status path (which does
    # not go through vault_client_for_operator) hits the dev Vault.
    monkeypatch.setenv("VAULT_ADDR", vault_dev_addr)
    from meho_backplane.settings import get_settings

    get_settings.cache_clear()

    target = _VaultTarget()
    try:
        yield target, vault_dev_addr
    finally:
        get_settings.cache_clear()
        reset_dispatcher_caches()
        clear_registry()


# ---------------------------------------------------------------------------
# Audit / broadcast assertion helper
# ---------------------------------------------------------------------------


async def _assert_audited(
    op_id: str,
    *,
    operator_sub: str,
    expected_op_class: str,
    events: list[BroadcastEvent],
) -> None:
    """Assert exactly one ``audit_log`` row + one broadcast for *op_id*.

    Reads the real Postgres audit row the dispatcher wrote
    synchronously (CLAUDE.md postulate 7: the op does not return
    success unless the row committed) and cross-checks the broadcast
    event's ``op_class``.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(AuditLog).where(
                        AuditLog.path == op_id,
                        AuditLog.operator_sub == operator_sub,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1, (
        f"expected exactly one audit row for {op_id} / operator {operator_sub}, got {len(rows)}"
    )
    row = rows[0]
    assert row.method == "DISPATCH"
    assert row.status_code == 200
    assert row.operator_sub == operator_sub
    assert row.payload["op_id"] == op_id
    assert row.payload["source_kind"] == "typed"
    assert row.payload["result_status"] == "ok"
    assert "params_hash" in row.payload

    matching = [e for e in events if e.op_id == op_id]
    assert len(matching) == 1, f"expected one broadcast for {op_id}, got {len(matching)}"
    event = matching[0]
    assert event.op_class == expected_op_class, (
        f"{op_id}: expected op_class={expected_op_class!r}, got {event.op_class!r}"
    )
    assert event.result_status == "ok"
    assert event.audit_id == row.id


# ---------------------------------------------------------------------------
# KV-v2 group — read / list / put / versions / delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kv_read_against_dev_vault(
    vault_e2e: tuple[_VaultTarget, str],
    captured_events: list[BroadcastEvent],
) -> None:
    """``vault.kv.read`` returns the seeded secret; audited; credential_read."""
    target, _ = vault_e2e
    operator = _make_operator(sub="op-kv-read")
    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.kv.read",
        target=target,
        params={"path": "app/config"},
    )
    assert result.status == "ok", result.error
    assert result.result == {
        "data": {"db_url": "postgres://example", "feature_flag": "on"},
        "version": 1,
    }
    await _assert_audited(
        "vault.kv.read",
        operator_sub="op-kv-read",
        expected_op_class="credential_read",
        events=captured_events,
    )


@pytest.mark.asyncio
async def test_kv_list_against_dev_vault(
    vault_e2e: tuple[_VaultTarget, str],
    captured_events: list[BroadcastEvent],
) -> None:
    """``vault.kv.list`` over the > 50-key fixture returns every key inline.

    v0.2 ships :class:`PassThroughReducer`, so the full set comes back
    on ``result`` with ``handle is None`` — G3.3-T4 (#566) owns the
    force-mode handle assertion. ``op_class`` is ``credential_read``
    (key names can leak structure — decision #3).
    """
    target, _ = vault_e2e
    operator = _make_operator(sub="op-kv-list")
    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.kv.list",
        target=target,
        params={"path": _BULK_PATH_PREFIX},
    )
    assert result.status == "ok", result.error
    assert result.handle is None  # v0.2 pass-through default
    assert isinstance(result.result, dict)
    keys = result.result["keys"]
    assert len(keys) == _BULK_KEY_COUNT > 50
    assert "key-000" in keys
    assert "key-059" in keys
    await _assert_audited(
        "vault.kv.list",
        operator_sub="op-kv-list",
        expected_op_class="credential_read",
        events=captured_events,
    )


@pytest.mark.asyncio
async def test_kv_put_against_dev_vault(
    vault_e2e: tuple[_VaultTarget, str],
    captured_events: list[BroadcastEvent],
) -> None:
    """``vault.kv.put`` writes a new version; audited; op_class=credential_write.

    G11.7-T1 (#1401) reclassified ``vault.kv.put`` from plain ``write`` to
    ``credential_write``: the KV-v2 secret rides in the *request params*, so
    a plain-``write`` classification broadcast the written secret in full to
    every operator on the feed. ``credential_write`` collapses the broadcast
    to aggregate-only (``{op_class, result_status}``) — the team-coordination
    signal "someone wrote a credential" without the secret material.

    This integration assertion strengthens the contract end-to-end: the
    written secret value is seeded as a distinctive sentinel and the test
    positively asserts it is **absent** from the *entire serialised
    BroadcastEvent* (not just ``payload`` — a regression placing the secret
    in a new top-level field would slip a payload-only check), mirroring
    :func:`tests.test_broadcast_credential_write_dispatch
    .test_credential_write_broadcast_is_aggregate_only`. The
    :class:`~meho_backplane.connectors.schemas.OperationResult` and the
    Vault read-back still carry the secret — only the broadcast is redacted.
    """
    target, addr = vault_e2e
    # Distinctive value so any appearance in the serialised broadcast event
    # is an unambiguous leak (a short/common value like "v" could collide
    # with field names or framework strings and make absence unprovable).
    sentinel = "kvput-secret-sentinel-1401"
    operator = _make_operator(sub="op-kv-put")
    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.kv.put",
        target=target,
        params={"path": "app/written", "data": {"k": sentinel}},
    )
    assert result.status == "ok", result.error
    assert result.result == {"version": 1}
    # Read back through a fresh root client to prove the write landed — the
    # secret is intact in Vault and in the caller's OperationResult; only the
    # broadcast view is redacted.
    readback = _root_client(addr).secrets.kv.v2.read_secret_version(
        path="app/written",
        mount_point=_KV_MOUNT,
        raise_on_deleted_version=False,
    )
    assert readback["data"]["data"] == {"k": sentinel}
    await _assert_audited(
        "vault.kv.put",
        operator_sub="op-kv-put",
        expected_op_class="credential_write",
        events=captured_events,
    )
    # Positive secret-absence assertion (AC4): the redacted broadcast carries
    # only the aggregate view and the sentinel never reaches the feed.
    put_event = next(e for e in captured_events if e.op_id == "vault.kv.put")
    assert put_event.payload == {
        "op_class": "credential_write",
        "result_status": "ok",
    }
    serialised = put_event.model_dump_json()
    assert sentinel not in serialised, (
        f"credential_write leak: {sentinel!r} reached the broadcast event for "
        f"vault.kv.put — serialised event: {serialised}"
    )


@pytest.mark.asyncio
async def test_kv_versions_against_dev_vault(
    vault_e2e: tuple[_VaultTarget, str],
    captured_events: list[BroadcastEvent],
) -> None:
    """``vault.kv.versions`` returns metadata only; audited; op_class=read."""
    target, _ = vault_e2e
    operator = _make_operator(sub="op-kv-versions")
    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.kv.versions",
        target=target,
        params={"path": "app/rotating"},
    )
    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    # app/rotating was written twice in the seed.
    assert result.result["current_version"] == 2
    assert set(result.result["versions"].keys()) == {"1", "2"}
    await _assert_audited(
        "vault.kv.versions",
        operator_sub="op-kv-versions",
        expected_op_class="read",
        events=captured_events,
    )


@pytest.mark.asyncio
async def test_kv_delete_against_dev_vault(
    vault_e2e: tuple[_VaultTarget, str],
    captured_events: list[BroadcastEvent],
) -> None:
    """``vault.kv.delete`` soft-deletes a version; audited; op_class=write."""
    target, addr = vault_e2e
    # Seed a dedicated secret so the delete doesn't perturb other tests.
    _root_client(addr).secrets.kv.v2.create_or_update_secret(
        path="app/to-delete",
        secret={"x": "1"},
        mount_point=_KV_MOUNT,
    )
    operator = _make_operator(sub="op-kv-delete")
    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.kv.delete",
        target=target,
        params={"path": "app/to-delete", "versions": [1]},
    )
    assert result.status == "ok", result.error
    assert result.result == {"deleted_versions": [1]}
    await _assert_audited(
        "vault.kv.delete",
        operator_sub="op-kv-delete",
        expected_op_class="write",
        events=captured_events,
    )


# ---------------------------------------------------------------------------
# sys group — health / seal_status / mounts.list / auth.list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sys_health_against_dev_vault(
    vault_e2e: tuple[_VaultTarget, str],
    captured_events: list[BroadcastEvent],
) -> None:
    """``vault.sys.health`` over the unsealed dev Vault; op_class=read."""
    target, _ = vault_e2e
    operator = _make_operator(sub="op-sys-health")
    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.sys.health",
        target=target,
        params={},
    )
    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result["ok"] is True
    assert result.result["sealed"] is False
    assert result.result["initialized"] is True
    assert result.result["version"] is not None
    await _assert_audited(
        "vault.sys.health",
        operator_sub="op-sys-health",
        expected_op_class="read",
        events=captured_events,
    )


@pytest.mark.asyncio
async def test_sys_seal_status_against_dev_vault(
    vault_e2e: tuple[_VaultTarget, str],
    captured_events: list[BroadcastEvent],
) -> None:
    """``vault.sys.seal_status`` — dev Vault is unsealed; op_class=read."""
    target, _ = vault_e2e
    operator = _make_operator(sub="op-sys-seal")
    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.sys.seal_status",
        target=target,
        params={},
    )
    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result["sealed"] is False
    assert result.result["initialized"] is True
    await _assert_audited(
        "vault.sys.seal_status",
        operator_sub="op-sys-seal",
        expected_op_class="read",
        events=captured_events,
    )


@pytest.mark.asyncio
async def test_sys_mounts_list_against_dev_vault(
    vault_e2e: tuple[_VaultTarget, str],
    captured_events: list[BroadcastEvent],
) -> None:
    """``vault.sys.mounts.list`` includes the dev KV-v2 ``secret/`` mount."""
    target, _ = vault_e2e
    operator = _make_operator(sub="op-sys-mounts")
    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.sys.mounts.list",
        target=target,
        params={},
    )
    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    mounts = result.result["mounts"]
    # Dev mode mounts KV-v2 at ``secret/``; the map is keyed by
    # trailing-slash mount path.
    assert "secret/" in mounts
    await _assert_audited(
        "vault.sys.mounts.list",
        operator_sub="op-sys-mounts",
        expected_op_class="read",
        events=captured_events,
    )


@pytest.mark.asyncio
async def test_sys_auth_list_against_dev_vault(
    vault_e2e: tuple[_VaultTarget, str],
    captured_events: list[BroadcastEvent],
) -> None:
    """``vault.sys.auth.list`` includes the seeded userpass + approle."""
    target, _ = vault_e2e
    operator = _make_operator(sub="op-sys-auth")
    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.sys.auth.list",
        target=target,
        params={},
    )
    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    auth_methods = result.result["auth_methods"]
    assert "userpass/" in auth_methods
    assert "approle/" in auth_methods
    await _assert_audited(
        "vault.sys.auth.list",
        operator_sub="op-sys-auth",
        expected_op_class="read",
        events=captured_events,
    )


# ---------------------------------------------------------------------------
# auth group — userpass + approle read-only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_userpass_list_against_dev_vault(
    vault_e2e: tuple[_VaultTarget, str],
    captured_events: list[BroadcastEvent],
) -> None:
    """``vault.auth.userpass.list`` returns the seeded user; op_class=read."""
    target, _ = vault_e2e
    operator = _make_operator(sub="op-up-list")
    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.auth.userpass.list",
        target=target,
        params={},
    )
    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert "ci-operator" in result.result["keys"]
    await _assert_audited(
        "vault.auth.userpass.list",
        operator_sub="op-up-list",
        expected_op_class="read",
        events=captured_events,
    )


@pytest.mark.asyncio
async def test_auth_userpass_read_against_dev_vault(
    vault_e2e: tuple[_VaultTarget, str],
    captured_events: list[BroadcastEvent],
) -> None:
    """``vault.auth.userpass.read`` returns the user's policies.

    ``op_class`` is ``other``, not ``read``: ``classify_op`` keys the
    read class off an explicit suffix allowlist (``.list`` / ``.get`` /
    ``.info`` / … / ``.versions``) and ``.read`` is deliberately absent
    so the generic suffix never over-matches ``vault.kv.read`` (which
    is in the ``credential_read`` allowlist instead). A userpass-config
    read returns policy/TTL metadata, not set-shaped data or secret
    values, so the ``other`` full-detail broadcast is the intended
    classification per decision #3.
    """
    target, _ = vault_e2e
    operator = _make_operator(sub="op-up-read")
    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.auth.userpass.read",
        target=target,
        params={"username": "ci-operator"},
    )
    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert "default" in result.result["token_policies"]
    await _assert_audited(
        "vault.auth.userpass.read",
        operator_sub="op-up-read",
        expected_op_class="other",
        events=captured_events,
    )


@pytest.mark.asyncio
async def test_auth_approle_list_against_dev_vault(
    vault_e2e: tuple[_VaultTarget, str],
    captured_events: list[BroadcastEvent],
) -> None:
    """``vault.auth.approle.list`` returns the seeded role; op_class=read."""
    target, _ = vault_e2e
    operator = _make_operator(sub="op-ar-list")
    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.auth.approle.list",
        target=target,
        params={},
    )
    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert "ci-role" in result.result["keys"]
    await _assert_audited(
        "vault.auth.approle.list",
        operator_sub="op-ar-list",
        expected_op_class="read",
        events=captured_events,
    )


@pytest.mark.asyncio
async def test_auth_approle_read_against_dev_vault(
    vault_e2e: tuple[_VaultTarget, str],
    captured_events: list[BroadcastEvent],
) -> None:
    """``vault.auth.approle.read`` returns the role config.

    ``op_class`` is ``other`` for the same reason as
    ``vault.auth.userpass.read``: ``.read`` is not in
    ``classify_op``'s read-suffix allowlist (kept narrow so it never
    over-matches the ``credential_read``-classified ``vault.kv.read``),
    and a role-config read carries no secret-id / set-shaped payload,
    so the full-detail ``other`` broadcast is intended.
    """
    target, _ = vault_e2e
    operator = _make_operator(sub="op-ar-read")
    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.auth.approle.read",
        target=target,
        params={"role_name": "ci-role"},
    )
    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert "default" in result.result["token_policies"]
    await _assert_audited(
        "vault.auth.approle.read",
        operator_sub="op-ar-read",
        expected_op_class="other",
        events=captured_events,
    )


# ---------------------------------------------------------------------------
# auth write group (G3.15-T3 #1411) — userpass + approle credential lifecycle
#
# Every write op registers ``requires_approval=True``; an ordinary dispatch
# would park it in the approval queue (G11.7-T1 #1401). These tests pass
# ``_approved=True`` (the approvals-API resume flag) to drive the
# authorized execution path — the same handler/audit/broadcast path that
# runs once a human approves — against the live Vault. The redaction
# assertions positively prove the password (request-side) and the minted
# SecretID (response-side) never reach the serialised broadcast event.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_userpass_write_redacts_password_against_dev_vault(
    vault_e2e: tuple[_VaultTarget, str],
    captured_events: list[BroadcastEvent],
) -> None:
    """``vault.auth.userpass.write`` creates a user; password absent from the broadcast.

    The password rides in the request params, so the op classifies
    ``credential_write`` (G11.7-T1 #1401) and the broadcast collapses to
    aggregate-only. A distinctive sentinel password is seeded and the
    test positively asserts it is absent from the *entire serialised
    BroadcastEvent*; the write still lands in Vault (the user logs in).
    """
    target, addr = vault_e2e
    sentinel = "userpass-write-pw-sentinel-1411"
    operator = _make_operator(sub="op-up-write")
    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.auth.userpass.write",
        target=target,
        params={"username": "minted-user", "password": sentinel, "token_policies": ["default"]},
        _approved=True,
    )
    assert result.status == "ok", result.error
    assert result.result["written"] is True
    assert sentinel not in str(result.result)
    # The write landed — the user can authenticate with the sentinel.
    login = _root_client(addr).auth.userpass.login(
        username="minted-user", password=sentinel, mount_point="userpass"
    )
    assert login["auth"]["client_token"]
    await _assert_audited(
        "vault.auth.userpass.write",
        operator_sub="op-up-write",
        expected_op_class="credential_write",
        events=captured_events,
    )
    event = next(e for e in captured_events if e.op_id == "vault.auth.userpass.write")
    assert event.payload == {"op_class": "credential_write", "result_status": "ok"}
    serialised = event.model_dump_json()
    assert sentinel not in serialised, (
        f"credential_write leak: {sentinel!r} reached the broadcast event"
    )


@pytest.mark.asyncio
async def test_auth_userpass_update_password_redacts_against_dev_vault(
    vault_e2e: tuple[_VaultTarget, str],
    captured_events: list[BroadcastEvent],
) -> None:
    """``vault.auth.userpass.update_password`` rotates a password; redacted from broadcast."""
    target, addr = vault_e2e
    # Seed a user to rotate so this test is independent of the create test.
    _root_client(addr).auth.userpass.create_or_update_user(
        username="rotate-user", password="initial-pw", policies=["default"], mount_point="userpass"
    )
    sentinel = "userpass-rotate-pw-sentinel-1411"
    operator = _make_operator(sub="op-up-rotate")
    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.auth.userpass.update_password",
        target=target,
        params={"username": "rotate-user", "password": sentinel},
        _approved=True,
    )
    assert result.status == "ok", result.error
    assert result.result == {
        "username": "rotate-user",
        "mount": "userpass",
        "password_updated": True,
    }
    login = _root_client(addr).auth.userpass.login(
        username="rotate-user", password=sentinel, mount_point="userpass"
    )
    assert login["auth"]["client_token"]
    await _assert_audited(
        "vault.auth.userpass.update_password",
        operator_sub="op-up-rotate",
        expected_op_class="credential_write",
        events=captured_events,
    )
    event = next(e for e in captured_events if e.op_id == "vault.auth.userpass.update_password")
    serialised = event.model_dump_json()
    assert sentinel not in serialised, "rotated password leaked into broadcast event"


@pytest.mark.asyncio
async def test_auth_userpass_delete_against_dev_vault(
    vault_e2e: tuple[_VaultTarget, str],
    captured_events: list[BroadcastEvent],
) -> None:
    """``vault.auth.userpass.delete`` removes a user; op_class=write (no secret)."""
    target, addr = vault_e2e
    _root_client(addr).auth.userpass.create_or_update_user(
        username="doomed-user", password="pw", policies=["default"], mount_point="userpass"
    )
    operator = _make_operator(sub="op-up-delete")
    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.auth.userpass.delete",
        target=target,
        params={"username": "doomed-user"},
        _approved=True,
    )
    assert result.status == "ok", result.error
    assert result.result == {"username": "doomed-user", "mount": "userpass", "deleted": True}
    # The user is gone — list no longer contains it.
    listing = _root_client(addr).auth.userpass.list_user(mount_point="userpass")
    assert "doomed-user" not in listing["data"]["keys"]
    await _assert_audited(
        "vault.auth.userpass.delete",
        operator_sub="op-up-delete",
        expected_op_class="write",
        events=captured_events,
    )


@pytest.mark.asyncio
async def test_auth_approle_write_against_dev_vault(
    vault_e2e: tuple[_VaultTarget, str],
    captured_events: list[BroadcastEvent],
) -> None:
    """``vault.auth.approle.write`` creates a role; op_class=write."""
    target, addr = vault_e2e
    operator = _make_operator(sub="op-ar-write")
    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.auth.approle.write",
        target=target,
        params={"role_name": "minted-role", "token_policies": ["default"], "token_ttl": 600},
        _approved=True,
    )
    assert result.status == "ok", result.error
    assert result.result == {"role_name": "minted-role", "mount": "approle", "written": True}
    role = _root_client(addr).auth.approle.read_role(role_name="minted-role", mount_point="approle")
    assert "default" in role["data"]["token_policies"]
    await _assert_audited(
        "vault.auth.approle.write",
        operator_sub="op-ar-write",
        expected_op_class="write",
        events=captured_events,
    )


@pytest.mark.asyncio
async def test_auth_approle_delete_against_dev_vault(
    vault_e2e: tuple[_VaultTarget, str],
    captured_events: list[BroadcastEvent],
) -> None:
    """``vault.auth.approle.delete`` removes a role; op_class=write."""
    target, addr = vault_e2e
    _root_client(addr).auth.approle.create_or_update_approle(
        role_name="doomed-role", token_policies=["default"], mount_point="approle"
    )
    operator = _make_operator(sub="op-ar-delete")
    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.auth.approle.delete",
        target=target,
        params={"role_name": "doomed-role"},
        _approved=True,
    )
    assert result.status == "ok", result.error
    assert result.result == {"role_name": "doomed-role", "mount": "approle", "deleted": True}
    listing = _root_client(addr).auth.approle.list_roles(mount_point="approle")
    assert "doomed-role" not in listing["data"]["keys"]
    await _assert_audited(
        "vault.auth.approle.delete",
        operator_sub="op-ar-delete",
        expected_op_class="write",
        events=captured_events,
    )


@pytest.mark.asyncio
async def test_auth_approle_generate_secret_id_redacts_against_dev_vault(
    vault_e2e: tuple[_VaultTarget, str],
    captured_events: list[BroadcastEvent],
) -> None:
    """``vault.auth.approle.generate_secret_id`` mints a SecretID; redacted from broadcast.

    The minted SecretID lands in the *response*, so the op classifies
    ``credential_mint`` and the broadcast collapses to aggregate-only. The
    caller's OperationResult carries the SecretID (the point of minting),
    but the test positively asserts that exact value is absent from the
    entire serialised BroadcastEvent. Non-idempotent: a second call mints
    a distinct SecretID.
    """
    target, addr = vault_e2e
    _root_client(addr).auth.approle.create_or_update_approle(
        role_name="sid-role", token_policies=["default"], mount_point="approle"
    )
    operator = _make_operator(sub="op-ar-sid")
    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.auth.approle.generate_secret_id",
        target=target,
        params={"role_name": "sid-role"},
        _approved=True,
    )
    assert result.status == "ok", result.error
    minted_secret_id = result.result["secret_id"]
    assert minted_secret_id, "the minted SecretID must reach the caller's OperationResult"
    assert result.result["role_name"] == "sid-role"
    await _assert_audited(
        "vault.auth.approle.generate_secret_id",
        operator_sub="op-ar-sid",
        expected_op_class="credential_mint",
        events=captured_events,
    )
    event = next(e for e in captured_events if e.op_id == "vault.auth.approle.generate_secret_id")
    assert event.payload == {"op_class": "credential_mint", "result_status": "ok"}
    serialised = event.model_dump_json()
    assert minted_secret_id not in serialised, (
        "credential_mint leak: the minted SecretID reached the broadcast event"
    )
    # Non-idempotent: a second mint yields a distinct SecretID.
    second = await dispatch(
        operator=_make_operator(sub="op-ar-sid-2"),
        connector_id="vault-1.x",
        op_id="vault.auth.approle.generate_secret_id",
        target=target,
        params={"role_name": "sid-role"},
        _approved=True,
    )
    assert second.status == "ok", second.error
    assert second.result["secret_id"] != minted_secret_id
