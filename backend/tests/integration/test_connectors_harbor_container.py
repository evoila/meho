# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G3.5-T10 (#622) — Harbor real-container E2E for robot lifecycle + read ops.

Boots a real Harbor 2.x stack (harbor-db + redis + harbor-core) via
testcontainers, then dispatches typed robot ops (``harbor.robot.create``
and ``harbor.robot.delete``) and a pair of read-only ops
(``GET:/api/v2.0/systeminfo``, ``GET:/api/v2.0/robots``) through the
**real G0.6 dispatcher** with a live Postgres audit store.

What this harness proves (issue #622 DoD)
=========================================

* ``goharbor/harbor-core:v2.11.0`` container starts against a real
  ``goharbor/harbor-db:v2.11.0`` (Harbor's schema-seeded Postgres)
  and ``redis:7-alpine``; all three share a Docker network.
  Image tags are overridable via ``MEHO_TEST_HARBOR_DB_IMAGE``,
  ``MEHO_TEST_HARBOR_REDIS_IMAGE``, and ``MEHO_TEST_HARBOR_CORE_IMAGE``
  so the CI runner can pull through the in-cluster Harbor proxy.
* ``harbor.robot.create`` (typed op, ``safety_level="caution"``)
  dispatches against the live Harbor and returns a minted ``secret``
  in ``result``. A synchronous ``audit_log`` row commits
  (CLAUDE.md postulate 7).
* The broadcast event for ``harbor.robot.create`` is classified
  ``credential_mint`` — the secret never appears in the broadcast
  payload (aggregate-only collapse enforced by
  :func:`~meho_backplane.broadcast.events.classify_op`).
* ``harbor.robot.delete`` (typed op, ``safety_level="caution"``) removes
  the robot created in the step above; classified ``write``; audited.
* ``GET:/api/v2.0/systeminfo`` (ingested read op) returns the Harbor
  version string from the live container; classified ``read``.
* ``GET:/api/v2.0/robots`` (ingested read op, ``source_kind='ingested'``)
  returns the robot list **without** a ``secret`` field on any entry
  (Harbor's list-response-never-has-secret invariant holds in the
  real container, not just the acceptance mocks).

Container topology
==================

::

    Docker network "harbor-e2e-<random>"
    ├── harbor-db:5432   (goharbor/harbor-db:v2.11.0) — Harbor's seeded PG
    ├── redis:6379        (redis:7-alpine)
    └── harbor-core:8080  (goharbor/harbor-core:v2.11.0) — API server

The harbor-core container is the only surface this test talks to from
the host; its port 8080 is exposed and mapped to an ephemeral host port.
The DB and redis containers are accessible only on the shared network.

Harbor admin credentials
========================

``HARBOR_ADMIN_PASSWORD`` is set to a test-time value that is never
committed to files or echoed in logs. The constant is module-scoped
and only ever used inside the fixture; it is intentionally short and
obvious-looking (``Harbor12345``) because this is a throwaway
in-memory container that never persists and is not reachable off the
runner once the test session ends.

Skip conditions
===============

* Docker socket missing — same as the rest of ``tests/integration/``.
* Any container start failure (privileged not allowed, pull rate-limit,
  OOM kill) → ``pytest.skip``, not a red test run.
* Harbor core startup timeout (30s for DB-ready, 60s for core) →
  ``pytest.skip``.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

import meho_backplane.operations._audit as _audit_module
from meho_backplane.broadcast import BroadcastEvent
from meho_backplane.connectors.harbor import (
    HARBOR_CONNECTOR_ID,
    HARBOR_CORE_GROUPS,
    HARBOR_CORE_OPS,
    HARBOR_IMPL_ID,
    HARBOR_PRODUCT,
    HARBOR_VERSION,
    HarborConnector,
    HarborTargetLike,
)
from meho_backplane.connectors.harbor.ops import register_harbor_robot_operations
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor, OperationGroup
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance
from meho_backplane.operations.approval_queue import approve_request
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.tenancy import ensure_tenant
from tests.test_operations_dispatcher import _make_operator

# ---------------------------------------------------------------------------
# Docker-availability gate — identical to other integration suites.
# ---------------------------------------------------------------------------


def _docker_socket_present() -> bool:
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


DOCKER_AVAILABLE: bool = _docker_socket_present()
SKIP_REASON: str = (
    "Docker socket unavailable in this sandbox; runs in CI where containers are provisioned."
)

#: Default Harbor admin password for the throwaway container.
#: This is a well-known test value for an in-memory container that never
#: persists and is never reachable off the runner after the test session.
_HARBOR_ADMIN_PASSWORD: str = "Harbor12345"

#: Project seeded for robot lifecycle tests.
_TEST_PROJECT_NAME: str = "e2e-robot-test"

#: Container network alias names (resolvable within the Docker network).
_DB_ALIAS: str = "harbor-db"
_REDIS_ALIAS: str = "redis"
_CORE_ALIAS: str = "harbor-core"

#: Tenant every operator in this module belongs to. Pinned (not random)
#: because ``harbor.robot.create`` now parks (#147) and the parked
#: ``ApprovalRequest.tenant_id`` FKs to ``tenant(id)`` — a random tenant
#: would violate ``approval_request_tenant_id_fkey``. Matches the
#: ``_make_operator`` default so operators built without an explicit
#: ``tenant_id`` share it. The ``harbor_e2e`` fixture seeds this row via
#: :func:`~meho_backplane.tenancy.ensure_tenant` before any dispatch.
_HARBOR_E2E_TENANT_ID: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-00000000a0a0")

#: Stable id for the persisted Harbor ``Target`` the approve→resume path
#: re-hydrates by id (``resolve_target_by_id`` is tenant-scoped, so the
#: row's ``tenant_id`` must equal the operator's). The parked request
#: pins ``target_id``; resume fails closed if no live row matches.
_HARBOR_E2E_TARGET_ID: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-00000000ba02")


# ---------------------------------------------------------------------------
# Multi-container Harbor stack — module-scoped
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def harbor_core_addr() -> Iterator[str]:
    """Boot harbor-db + redis + harbor-core; yield the core's base URL.

    All three containers share a dedicated Docker network for
    inter-container communication. Only harbor-core exposes a host port.

    Yields a URL like ``http://127.0.0.1:<host-port>`` that the test
    connector instance and the seeding httpx client both target.
    """
    if not DOCKER_AVAILABLE:
        pytest.skip(SKIP_REASON)

    # Late import: transitively probes Docker socket on import.
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.network import Network

    from tests._strategies import wait_for_log_message

    db_image = os.environ.get("MEHO_TEST_HARBOR_DB_IMAGE", "goharbor/harbor-db:v2.11.0")
    redis_image = os.environ.get("MEHO_TEST_HARBOR_REDIS_IMAGE", "redis:7-alpine")
    core_image = os.environ.get("MEHO_TEST_HARBOR_CORE_IMAGE", "goharbor/harbor-core:v2.11.0")

    # Core and job secrets must be the same across the service group;
    # use stable test-run-scoped values.
    core_secret = "e2e-core-secret-622"
    job_secret = "e2e-job-secret-622"

    network = Network()
    network.create()

    db_container = (
        DockerContainer(db_image)
        .with_name(f"harbor-db-{network.id[:8]}")
        .with_network(network)
        .with_network_aliases(_DB_ALIAS)
    )
    redis_container = (
        DockerContainer(redis_image)
        .with_name(f"harbor-redis-{network.id[:8]}")
        .with_network(network)
        .with_network_aliases(_REDIS_ALIAS)
    )
    core_container = (
        DockerContainer(core_image)
        .with_name(f"harbor-core-{network.id[:8]}")
        .with_network(network)
        .with_network_aliases(_CORE_ALIAS)
        .with_env("DATABASE_TYPE", "postgresql")
        .with_env("POSTGRESQL_HOST", _DB_ALIAS)
        .with_env("POSTGRESQL_PORT", "5432")
        .with_env("POSTGRESQL_USERNAME", "postgres")
        .with_env("POSTGRESQL_PASSWORD", "root123")
        .with_env("POSTGRESQL_DATABASE", "registry")
        .with_env("_REDIS_URL_CORE", f"redis://{_REDIS_ALIAS}:6379/0")
        .with_env("HARBOR_ADMIN_PASSWORD", _HARBOR_ADMIN_PASSWORD)
        .with_env("CORE_SECRET", core_secret)
        .with_env("JOBSERVICE_SECRET", job_secret)
        # Self-referential token service URL for standalone core.
        .with_env("TOKEN_SERVICE_URL", f"http://{_CORE_ALIAS}:8080/service/token")
        # Disable registry + notary features not available in standalone mode.
        .with_env("REGISTRY_URL", "")
        .with_env("REGISTRY_CONTROLLER_URL", "")
        .with_exposed_ports(8080)
    )

    try:
        db_container.start()
        redis_container.start()
    except Exception as exc:
        db_container.stop()
        redis_container.stop()
        network.remove()
        pytest.skip(f"harbor-db/redis containers failed to start ({type(exc).__name__}): {exc}")

    try:
        # Give DB time to reach ready state before starting core.
        wait_for_log_message(db_container, "PostgreSQL init process complete", timeout=30)
    except Exception:
        # Some harbor-db images don't log this line; fall through and
        # let core fail-fast if the DB isn't ready.
        time.sleep(5)

    try:
        core_container.start()
    except Exception as exc:
        db_container.stop()
        redis_container.stop()
        network.remove()
        pytest.skip(f"harbor-core container failed to start ({type(exc).__name__}): {exc}")

    try:
        try:
            wait_for_log_message(core_container, "HTTP proxy is up", timeout=60)
        except Exception:
            # Fall back: poll the /api/v2.0/systeminfo endpoint directly.
            host = core_container.get_container_host_ip()
            port = core_container.get_exposed_port(8080)
            base = f"http://{host}:{port}"
            _wait_for_harbor_ready(base, timeout=60)

        host = core_container.get_container_host_ip()
        port = core_container.get_exposed_port(8080)
        base_url = f"http://{host}:{port}"

        _seed_harbor(base_url)

        yield base_url
    finally:
        core_container.stop()
        db_container.stop()
        redis_container.stop()
        network.remove()


def _wait_for_harbor_ready(base_url: str, timeout: int) -> None:
    """Poll GET /api/v2.0/systeminfo until Harbor responds 200."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{base_url}/api/v2.0/systeminfo", timeout=3)
            if r.status_code == 200:
                return
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ReadError):
            pass
        time.sleep(2)
    raise TimeoutError(f"Harbor core at {base_url} did not become ready within {timeout}s")


def _seed_harbor(base_url: str) -> None:
    """Create the test project needed for robot lifecycle tests."""
    auth = ("admin", _HARBOR_ADMIN_PASSWORD)
    with httpx.Client(base_url=base_url, auth=auth, timeout=10) as client:
        r = client.post(
            "/api/v2.0/projects",
            json={
                "project_name": _TEST_PROJECT_NAME,
                "public": False,
                "metadata": {"public": "false"},
            },
        )
        # 201 = created, 409 = already exists (idempotent seed).
        if r.status_code not in (201, 409):
            raise RuntimeError(f"Harbor project seed failed: {r.status_code} {r.text[:200]}")


# ---------------------------------------------------------------------------
# Connector wiring fixture — function-scoped
# ---------------------------------------------------------------------------


@dataclass
class _HarborTarget:
    """Minimal duck-typed Target for Harbor dispatch tests.

    ``id`` / ``tenant_id`` are pinned to the module constants (not random)
    so the parked ``ApprovalRequest`` — created when ``harbor.robot.create``
    parks (#147) — re-hydrates the persisted :class:`TargetORM` row on the
    approve→resume path (``resolve_target_by_id`` is tenant-scoped) and its
    ``tenant_id`` satisfies the ``tenant(id)`` FK the fixture seeds.
    """

    product: str = "harbor"
    name: str = "harbor-e2e"
    host: str = ""
    port: int = 8080
    auth_model: str = "shared_service_account"

    def __post_init__(self) -> None:
        self.id = _HARBOR_E2E_TARGET_ID
        self.tenant_id = _HARBOR_E2E_TENANT_ID
        self.preferred_impl_id: str | None = None

        class _FP:
            version = "v2.11.0"

        self.fingerprint = _FP()


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> list[BroadcastEvent]:
    """Capture every broadcast event the dispatcher emits (no Valkey needed)."""
    events: list[BroadcastEvent] = []

    async def _capture(event: BroadcastEvent) -> None:
        events.append(event)

    monkeypatch.setattr(_audit_module, "publish_event", _capture)
    return events


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub — avoids loading the ONNX model."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def harbor_e2e(
    harbor_core_addr: str,
    pg_engine: None,
    stub_embedding_service: AsyncMock,
) -> AsyncIterator[tuple[_HarborTarget, str]]:
    """Wire a HarborConnector against the live container + real PG audit store.

    * Inserts the 9 curated ``EndpointDescriptor`` rows for the ingested
      core ops so :func:`dispatch` can look them up.
    * Registers ``harbor.robot.create`` and ``harbor.robot.delete`` typed ops.
    * Patches ``HarborConnector._credentials_loader`` to return admin
      credentials for the throwaway container.
    * Sets up :class:`PassThroughReducer` so list results come back inline.
    """
    reset_dispatcher_caches()
    set_default_reducer(PassThroughReducer())

    clear_registry()
    register_connector_v2(
        product=HARBOR_PRODUCT,
        version=HARBOR_VERSION,
        impl_id=HARBOR_IMPL_ID,
        cls=HarborConnector,
    )

    await _insert_harbor_descriptors()
    await register_harbor_robot_operations(embedding_service=stub_embedding_service)

    # Parse host:port from the container address (strips http:// prefix).
    import urllib.parse

    parsed = urllib.parse.urlparse(harbor_core_addr)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8080

    # Seed the tenant row + a durable Target the approve→resume path needs.
    # ``harbor.robot.create`` now parks (#147); the parked ``ApprovalRequest``
    # FKs its ``tenant_id`` to ``tenant(id)`` and the resume re-hydrates the
    # target by id (tenant-scoped). ``pg_engine`` truncated ``tenant`` +
    # ``targets`` and re-seeded two *other* tenants, so this fixture seeds
    # the module tenant just-in-time and persists a matching Harbor target.
    await _seed_harbor_tenant_and_target(host, port)

    instance = get_or_create_connector_instance(HarborConnector)
    instance._credentials_loader = _make_credentials_loader(  # type: ignore[misc]
        "admin", _HARBOR_ADMIN_PASSWORD
    )
    # The test container serves plain HTTP; _base_url() hardcodes https for
    # production targets. Override on the instance so the client reaches
    # the container without an SSL handshake.
    instance._base_url = lambda _target: harbor_core_addr  # type: ignore[method-assign]

    target = _HarborTarget(host=host, port=port)
    try:
        yield target, harbor_core_addr
    finally:
        await instance.aclose()
        reset_dispatcher_caches()
        clear_registry()


def _make_credentials_loader(username: str, password: str):  # type: ignore[no-untyped-def]
    # Inner loader uses 2-arg signature (target, operator) per
    # G3.10-T1 #945's HarborCredentialsLoader contract; the operator
    # is unused here because the container test injects credentials
    # directly without touching Vault.
    async def _loader(_target: HarborTargetLike, _operator: object) -> dict[str, str]:
        return {"username": username, "password": password}

    return _loader


async def _seed_harbor_tenant_and_target(host: str, port: int) -> None:
    """Seed the module tenant + a durable Harbor ``Target`` for resume.

    Idempotent: ``ensure_tenant`` is ``INSERT ... ON CONFLICT DO NOTHING``
    and the target insert is guarded by an existence check so re-running
    across the module's tests (module-scoped PG container, function-scoped
    ``harbor_e2e``) does not raise a unique violation. The persisted target
    mirrors the ``_HarborTarget`` double's identity (``id`` / ``tenant_id``
    / ``product`` / ``name``) so the approve→resume re-hydration resolves
    it; ``version`` is the connector version, but connector resolution on
    resume keys on the stored ``connector_id``, so ``host`` / ``port`` here
    are only informational — the patched instance ``_base_url`` ignores
    them and always targets the container.
    """
    async with get_sessionmaker()() as session:
        await ensure_tenant(_HARBOR_E2E_TENANT_ID, session)
        existing = await session.get(TargetORM, _HARBOR_E2E_TARGET_ID)
        if existing is None:
            session.add(
                TargetORM(
                    id=_HARBOR_E2E_TARGET_ID,
                    tenant_id=_HARBOR_E2E_TENANT_ID,
                    name="harbor-e2e",
                    product=HARBOR_PRODUCT,
                    version=HARBOR_VERSION,
                    host=host,
                    port=port,
                    aliases=[],
                    secret_ref="harbor/harbor-e2e",
                    auth_model="shared_service_account",
                )
            )
        await session.commit()


async def _create_robot_via_approve_resume(
    *,
    target: _HarborTarget,
    requester_sub: str,
    approver_sub: str,
    name: str,
    project: str,
    duration: int,
) -> OperationResult:
    """Drive ``harbor.robot.create`` through the real four-eyes approve→resume.

    ``harbor.robot.create`` mints a credential and is registered
    ``requires_approval=True`` (#147), so a lone dispatch parks at
    ``awaiting_approval`` instead of executing. This helper reproduces the
    production flow the unit lane exercises over HTTP ``/decide``
    (:mod:`tests.test_broadcast_credential_mint_dispatch`): it commits the
    real approval via the shared service-layer function ``approve_request``
    (the same one ``/decide`` calls) and then re-dispatches with
    ``_approved=True`` against the live target — avoiding an OIDC/JWKS mock
    against a suite that must reach the live Harbor container.

    Steps: dispatch as *requester_sub* → assert ``awaiting_approval`` +
    ``approval_request_id`` → approve as a **distinct** *approver_sub*
    (the requester≠approver guard, ``approval_allow_self_approval=False``
    by default, enforces real four-eyes) → resume re-dispatch with
    ``_approved=True`` against the **live** ``target`` object. Returns the
    resumed :class:`~meho_backplane.connectors.schemas.OperationResult`
    (``status`` ``"ok"`` on success, carrying the minted ``secret``).

    The resume deliberately re-dispatches against the live ``_HarborTarget``
    fixture (mirroring the unit lane
    :mod:`tests.test_broadcast_credential_mint_dispatch`) rather than
    ``resume_dispatch_after_approval``. The latter re-hydrates the ``Target``
    from its persisted DB row and re-resolves the connector by product/version;
    the live container connector binding (base_url override + injected
    credentials) lives on the fixture instance, not on a plain DB row, so a
    DB-rehydrated resume resolves ``no_connector``. The committed approval is
    the authorization; ``_approved=True`` skips the gate and executes against
    the live Harbor container.
    """
    requester = _make_operator(sub=requester_sub, tenant_id=_HARBOR_E2E_TENANT_ID)
    parked = await dispatch(
        operator=requester,
        connector_id=HARBOR_CONNECTOR_ID,
        op_id="harbor.robot.create",
        target=target,
        params={"name": name, "project": project, "duration": duration},
    )
    assert parked.status == "awaiting_approval", parked.error
    request_id = uuid.UUID(parked.extras["approval_request_id"])

    approver = _make_operator(sub=approver_sub, tenant_id=_HARBOR_E2E_TENANT_ID)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await approve_request(session, request_id, operator=approver)
        await session.commit()

    return await dispatch(
        operator=requester,
        connector_id=HARBOR_CONNECTOR_ID,
        op_id="harbor.robot.create",
        target=target,
        params={"name": name, "project": project, "duration": duration},
        _approved=True,
    )


_PATH_VAR_RE = re.compile(r"\{([^{}]+)\}")


async def _insert_harbor_descriptors() -> None:
    """Seed the 9 curated Harbor core ops as enabled EndpointDescriptor rows.

    Idempotent: skips rows that already exist. The ``pg_engine`` fixture is
    module-scoped so the same PG container is shared across every test in the
    module; naively inserting on every ``harbor_e2e`` setup call would raise a
    ``UniqueViolationError`` on the second test's setup because the first
    test's commit is still visible.
    """
    sessionmaker = get_sessionmaker()
    group_ids: dict[str, uuid.UUID] = {}
    async with sessionmaker() as session:
        for group in HARBOR_CORE_GROUPS:
            result = await session.execute(
                select(OperationGroup).where(
                    OperationGroup.tenant_id.is_(None),
                    OperationGroup.product == HARBOR_PRODUCT,
                    OperationGroup.version == HARBOR_VERSION,
                    OperationGroup.impl_id == HARBOR_IMPL_ID,
                    OperationGroup.group_key == group.group_key,
                )
            )
            existing_group = result.scalar_one_or_none()
            if existing_group is not None:
                group_ids[group.group_key] = existing_group.id
                continue
            group_row = OperationGroup(
                tenant_id=None,
                product=HARBOR_PRODUCT,
                version=HARBOR_VERSION,
                impl_id=HARBOR_IMPL_ID,
                group_key=group.group_key,
                name=group.name,
                when_to_use=group.when_to_use,
                review_status="enabled",
            )
            session.add(group_row)
            await session.flush()
            group_ids[group.group_key] = group_row.id

        for op in HARBOR_CORE_OPS:
            existing_desc = (
                await session.execute(
                    select(EndpointDescriptor).where(
                        EndpointDescriptor.tenant_id.is_(None),
                        EndpointDescriptor.product == HARBOR_PRODUCT,
                        EndpointDescriptor.version == HARBOR_VERSION,
                        EndpointDescriptor.impl_id == HARBOR_IMPL_ID,
                        EndpointDescriptor.op_id == op.op_id,
                    )
                )
            ).scalar_one_or_none()
            if existing_desc is not None:
                continue

            method, path = op.op_id.split(":", 1)
            placeholders = _PATH_VAR_RE.findall(path)
            param_schema: dict[str, object]
            if placeholders:
                param_schema = {
                    "type": "object",
                    "properties": {
                        name: {"type": "string", "x-meho-param-loc": "path"}
                        for name in placeholders
                    },
                    "required": list(placeholders),
                }
            else:
                param_schema = {"type": "object", "properties": {}}

            descriptor = EndpointDescriptor(
                tenant_id=None,
                product=HARBOR_PRODUCT,
                version=HARBOR_VERSION,
                impl_id=HARBOR_IMPL_ID,
                op_id=op.op_id,
                source_kind="ingested",
                method=method,
                path=path,
                handler_ref=None,
                group_id=group_ids[op.group_key],
                summary=f"Harbor core op {op.op_id}.",
                description=f"Harbor core op {op.op_id}.",
                parameter_schema=param_schema,
                response_schema={"type": "object"},
                llm_instructions=op.llm_instructions,
                safety_level="safe",
                requires_approval=False,
                is_enabled=True,
                tags=["spec:harbor-2.x/swagger.yaml"],
            )
            session.add(descriptor)
        await session.commit()


# ---------------------------------------------------------------------------
# Audit assertion helper
# ---------------------------------------------------------------------------


async def _assert_audited(
    op_id: str,
    *,
    operator_sub: str,
    expected_op_class: str,
    expected_source_kind: str,
    events: list[BroadcastEvent],
) -> None:
    """Assert one ``audit_log`` row + one broadcast event for *op_id*."""
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
        f"expected exactly one audit row for {op_id!r} / {operator_sub!r}, got {len(rows)}"
    )
    row = rows[0]
    assert row.method == "DISPATCH"
    assert row.status_code == 200
    assert row.payload["op_id"] == op_id
    assert row.payload["source_kind"] == expected_source_kind
    assert row.payload["result_status"] == "ok"

    matching = [e for e in events if e.op_id == op_id]
    assert len(matching) == 1, f"expected one broadcast for {op_id!r}, got {len(matching)}"
    event = matching[0]
    assert event.op_class == expected_op_class, (
        f"{op_id}: expected op_class={expected_op_class!r}, got {event.op_class!r}"
    )
    assert event.result_status == "ok"
    assert event.audit_id == row.id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_systeminfo_returns_harbor_version(
    harbor_e2e: tuple[_HarborTarget, str],
    captured_events: list[BroadcastEvent],
) -> None:
    """``GET:/api/v2.0/systeminfo`` returns harbor_version from the live container."""
    target, _ = harbor_e2e
    operator = _make_operator(sub="e2e-systeminfo")
    result = await dispatch(
        operator=operator,
        connector_id=HARBOR_CONNECTOR_ID,
        op_id="GET:/api/v2.0/systeminfo",
        target=target,
        params={},
    )
    assert result.status == "ok", result.error
    assert "harbor_version" in result.result, (
        f"systeminfo result missing harbor_version: {result.result!r}"
    )
    assert result.result["harbor_version"].startswith("v2."), (
        f"unexpected harbor_version: {result.result['harbor_version']!r}"
    )
    await _assert_audited(
        "GET:/api/v2.0/systeminfo",
        operator_sub="e2e-systeminfo",
        expected_op_class="read",
        expected_source_kind="ingested",
        events=captured_events,
    )


@pytest.mark.asyncio
async def test_robot_list_never_returns_secret(
    harbor_e2e: tuple[_HarborTarget, str],
    captured_events: list[BroadcastEvent],
) -> None:
    """``GET:/api/v2.0/robots`` list response contains no ``secret`` field."""
    target, _ = harbor_e2e
    operator = _make_operator(sub="e2e-robot-list")
    result = await dispatch(
        operator=operator,
        connector_id=HARBOR_CONNECTOR_ID,
        op_id="GET:/api/v2.0/robots",
        target=target,
        params={},
    )
    assert result.status == "ok", result.error
    assert isinstance(result.result, list), (
        f"GET:/api/v2.0/robots must return a list, got "
        f"{type(result.result).__name__}: {result.result!r}"
    )
    robots: list[dict[str, object]] = result.result
    for robot in robots:
        assert isinstance(robot, dict), f"unexpected robot entry shape: {robot!r}"
        assert "secret" not in robot, (
            f"robot list entry {robot.get('name')!r} must not expose 'secret'"
        )
    await _assert_audited(
        "GET:/api/v2.0/robots",
        operator_sub="e2e-robot-list",
        expected_op_class="read",
        expected_source_kind="ingested",
        events=captured_events,
    )


@pytest.mark.asyncio
async def test_robot_create_credential_mint_classification(
    harbor_e2e: tuple[_HarborTarget, str],
    captured_events: list[BroadcastEvent],
) -> None:
    """``harbor.robot.create`` mints a secret; broadcast is credential_mint (#147).

    ``harbor.robot.create`` now parks (``requires_approval=True``, #147), so a
    lone dispatch never executes. The minted-secret + aggregate-only broadcast
    contract is therefore proven through the real four-eyes approve→resume
    flow: a lone operator parks the mint, a **second** operator approves, and
    the committed approval drives the re-dispatch that mints against the live
    Harbor container. The executed op's result carries the secret while its
    broadcast collapses to aggregate-only.
    """
    target, _ = harbor_e2e
    result = await _create_robot_via_approve_resume(
        target=target,
        requester_sub="e2e-robot-create",
        approver_sub="e2e-robot-create-approver",
        name="e2e-ci-robot",
        project=_TEST_PROJECT_NAME,
        duration=7,
    )
    assert result.status == "ok", result.error

    # The minted secret must be present in the result payload.
    assert "secret" in result.result, (
        f"harbor.robot.create result must include 'secret'; got: {result.result!r}"
    )
    assert result.result["secret"], "minted secret must be non-empty"
    assert "id" in result.result, "harbor.robot.create result must include 'id'"
    assert "name" in result.result, "harbor.robot.create result must include 'name'"

    # The executed (post-approval) op audits under the approver identity —
    # the park writes an ``APPROVAL`` / ``approval.request`` row (not keyed
    # on the op-id), so filtering on ``path == "harbor.robot.create"`` +
    # the approver sub selects exactly the one executed dispatch row.
    await _assert_audited(
        "harbor.robot.create",
        operator_sub="e2e-robot-create-approver",
        expected_op_class="credential_mint",
        expected_source_kind="typed",
        events=captured_events,
    )

    # The broadcast event must NOT carry the secret — credential_mint
    # classification collapses the event to aggregate-only, so the
    # result payload in the broadcast is null/omitted (no secret egress).
    event = next(e for e in captured_events if e.op_id == "harbor.robot.create")
    # The event.payload (always the redacted view) must not contain the secret key.
    if event.payload is not None:
        assert "secret" not in event.payload, (
            "broadcast event for credential_mint must not expose robot secret"
        )


@pytest.mark.asyncio
async def test_robot_delete_classified_write(
    harbor_e2e: tuple[_HarborTarget, str],
    captured_events: list[BroadcastEvent],
) -> None:
    """``harbor.robot.delete`` removes the robot; broadcast is classified write."""
    target, _ = harbor_e2e

    # Create a robot to delete. ``harbor.robot.create`` parks (#147), so the
    # setup drives the same four-eyes approve→resume flow to mint it; only
    # then is there a robot id to delete.
    create_result = await _create_robot_via_approve_resume(
        target=target,
        requester_sub="e2e-robot-del-setup",
        approver_sub="e2e-robot-del-setup-approver",
        name="e2e-delete-me",
        project=_TEST_PROJECT_NAME,
        duration=7,
    )
    assert create_result.status == "ok", create_result.error
    robot_id = create_result.result["id"]

    # Now delete it.
    operator = _make_operator(sub="e2e-robot-delete")
    result = await dispatch(
        operator=operator,
        connector_id=HARBOR_CONNECTOR_ID,
        op_id="harbor.robot.delete",
        target=target,
        params={
            "project": _TEST_PROJECT_NAME,
            "id": robot_id,
        },
    )
    assert result.status == "ok", result.error
    assert result.result.get("deleted") is True, (
        f"harbor.robot.delete must return deleted=true; got {result.result!r}"
    )

    await _assert_audited(
        "harbor.robot.delete",
        operator_sub="e2e-robot-delete",
        expected_op_class="write",
        expected_source_kind="typed",
        events=captured_events,
    )
