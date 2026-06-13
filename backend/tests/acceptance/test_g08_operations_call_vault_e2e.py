# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G0.8-T8 acceptance — real federated POST /api/v1/operations/call (vault).

Initiative #634 (G0.8) Definition-of-Done line: *the v0.2 acceptance
smoke is extended to exercise one real (non-dry-run, federated) write
so this class of green-but-hollow cannot recur*. This module closes
that gap and is the merge gate for tagging v0.2.1.

Why this test is not redundant with the existing suites
=======================================================

Every existing acceptance / canary test bypasses the HTTP +
auth-middleware layer by constructing an :class:`Operator` directly
and calling the agent-surface functions in-process
(``test_vmware_rest_agent_flow_e2e.py`` calls ``call_operation`` with
a hand-built operator; ``_canary_fixtures.py`` builds
``Operator(raw_jwt=...)`` directly). The real Vault round-trip is only
proven at the integration layer
(``tests/integration/test_connectors_vault_dev_e2e.py``), which also
dispatches through the dispatcher directly, not over HTTP.

Consequently *nothing* exercised the path

    ``POST /api/v1/operations/call``
      → :func:`~meho_backplane.api.v1.operations.post_call`
      → :func:`~meho_backplane.middleware.verify_jwt_and_bind`
      → :func:`~meho_backplane.operations.dispatch`
      → vault connector
      → JSONFlux success envelope

end to end. That middleware is exactly where #628's
:func:`~meho_backplane.tenancy.ensure_tenant` just-in-time tenant
seed and #629's operator-JWT-from-request-context live — the two
fixes whose absence made v0.2.0 "green but hollow". This test drives a
real authenticated request all the way through, against a real Vault
testcontainer and a Postgres whose ``tenant`` table holds **no row**
for the calling operator's tenant at request time.

The Vault client seam (identical to the integration harness)
============================================================

``vault.kv.read`` reaches Vault through
:func:`meho_backplane.auth.vault.vault_client_for_operator` — an
OIDC ``jwt_login`` async context manager that reads
``operator.raw_jwt`` (the request-scoped operator threaded by the
dispatcher, **G0.8-T3 #629** — *not* a ``target.raw_jwt``, which was
the pre-#629 ``'Target' object has no attribute 'raw_jwt'`` failure
signature this test explicitly asserts against). Dev mode has no OIDC
auth method wired, so this harness monkeypatches
``vault_client_for_operator`` to yield a root-token
:class:`hvac.Client` bound to the container — the exact single-seam
approach ``test_connectors_vault_dev_e2e.py`` documents. Only
credential acquisition is swapped; the full handler → hvac → unwrap →
dispatch → audit → broadcast path runs unchanged, and crucially the
operator the seam receives is the one
:func:`~meho_backplane.middleware.verify_jwt_and_bind` produced from
the real Bearer token.

Docker-socket-absent sandbox skips cleanly via the
``DOCKER_AVAILABLE`` / ``SKIP_REASON`` gate the acceptance conftest
already re-exports; CI runs it in the existing acceptance lane (the
``python-lint-test`` job collects ``tests/acceptance/`` and already
sets ``MEHO_TEST_VAULT_IMAGE`` the same way it does for
``test_connectors_vault_dev_e2e.py``).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import func, select

import meho_backplane.auth.vault as _auth_vault
from meho_backplane.api.v1.operations import router as operations_router
from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vault import (
    VaultConnector,
    register_vault_typed_operations,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, Tenant
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.settings import get_settings
from tests._oidc_jwt_helpers import (
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from tests.integration.conftest import build_integration_app

from .conftest import DOCKER_AVAILABLE, SKIP_REASON

# ``pg_engine`` is the acceptance-local all-tenant-tables-TRUNCATE
# fixture re-exported by the acceptance conftest. It re-seeds
# ``tenant-a`` / ``tenant-b`` after the truncate but **not** the fresh
# UUID below, so the calling operator's tenant row count is 0
# pre-request (AC 3) while the conftest's superset-TRUNCATE keeps the
# head schema's FK graph satisfied.

_skip_no_docker = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)

#: ``vault-1.x`` is the ``<impl_id>-<version>`` connector id the
#: ``/api/v1/operations/*`` surface expects (the vault connector
#: self-registers as ``product=vault version=1.x impl_id=vault``).
_CONNECTOR_ID: str = "vault-1.x"

#: A fresh tenant UUID with no row in the (truncated, partially
#: re-seeded) ``tenant`` table — the clean-room deploy condition.
#: Deliberately distinct from the ``tenant-a`` /
#: ``11111111-…`` / ``22222222-…`` rows the acceptance ``pg_engine``
#: re-seeds, so a regression that accidentally pre-seeds this tenant
#: would not mask the JIT-seed assertion (AC 3).
_FRESH_TENANT_ID: str = "7e2d1c40-9a55-4b6e-83c1-2fd9a6b41e07"

#: Stable operator subject — asserted on the audit row (AC 4).
_OPERATOR_SUB: str = "op-g08-t8-vault-e2e"

#: KV-v2 mount dev mode provides out of the box (``-dev`` mounts
#: ``secret/`` as v2). The connector's ``_DEFAULT_KV_MOUNT`` is
#: ``"secret"`` so the op resolves here without an explicit ``mount``.
_KV_MOUNT: str = "secret"

#: Path + payload seeded into the dev Vault and read back through the
#: full HTTP → middleware → dispatcher → connector chain.
_SECRET_PATH: str = "g08-t8/federation"
_SECRET_DATA: dict[str, str] = {"db_url": "postgres://example", "flag": "on"}

#: Dev-mode root token. Generated *into* the container via
#: ``VAULT_DEV_ROOT_TOKEN_ID``; a throwaway value scoped to a
#: per-test-run in-memory Vault that never persists and is never
#: reachable off the runner. Never logged or echoed into an assertion.
_DEV_ROOT_TOKEN: str = "meho-dev-root-668"


@pytest.fixture(scope="module")
def vault_dev_addr() -> Iterator[str]:
    """Boot ``hashicorp/vault:1.18 -dev``, seed one KV secret, yield addr.

    Module scope amortises the container boot. Image overridable via
    ``MEHO_TEST_VAULT_IMAGE`` so the CI runner pulls through the
    in-cluster Harbor proxy — the same env-knob shape
    ``test_connectors_vault_dev_e2e.py`` uses and ``ci.yml`` already
    sets for the acceptance lane. Any boot failure → clean skip, not a
    red suite (matching every other testcontainer fixture in the repo).
    """
    if not DOCKER_AVAILABLE:
        pytest.skip(SKIP_REASON)

    # Local import: testcontainers transitively imports the docker SDK
    # which probes the socket on import. Keeping it inside the fixture
    # lets the module collect on a no-Docker sandbox and skip cleanly.
    from testcontainers.core.container import DockerContainer

    from tests._strategies import wait_for_log_message

    image = os.environ.get("MEHO_TEST_VAULT_IMAGE", "hashicorp/vault:1.18")
    container = (
        DockerContainer(image)
        .with_env("VAULT_DEV_ROOT_TOKEN_ID", _DEV_ROOT_TOKEN)
        .with_env("VAULT_DEV_LISTEN_ADDRESS", "0.0.0.0:8200")
        .with_exposed_ports(8200)
        .with_kwargs(cap_add=["IPC_LOCK"])
    )
    # No broad ``except`` around ``start()``. The no-Docker condition is
    # already a clean skip via the ``DOCKER_AVAILABLE`` gate above; any
    # *other* failure here (image-pull error, Vault boot regression,
    # daemon refusal) must fail this test, not silently skip it — this
    # acceptance test is the merge gate for tagging v0.2.1, and a
    # green-but-skipped gate is exactly the green-but-hollow failure
    # mode Initiative #634 exists to close.
    container.start()

    try:
        wait_for_log_message(container, "Vault server started!", timeout=60)
        host = container.get_container_host_ip()
        port = container.get_exposed_port(8200)
        addr = f"http://{host}:{port}"
        _root_client(addr).secrets.kv.v2.create_or_update_secret(
            path=_SECRET_PATH,
            secret=_SECRET_DATA,
            mount_point=_KV_MOUNT,
        )
        yield addr
    finally:
        container.stop()


def _root_client(addr: str) -> Any:
    """Construct a root-token hvac client bound to the dev container."""
    import hvac

    return hvac.Client(url=addr, token=_DEV_ROOT_TOKEN)


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub so registration doesn't load ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def vault_operations_app(
    vault_dev_addr: str,
    pg_engine: None,
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
) -> AsyncIterator[FastAPI]:
    """Production-middleware app + operations router, wired at dev Vault.

    * ``pg_engine`` (acceptance conftest) points the module-level
      engine cache at a migrated Postgres testcontainer and TRUNCATEs
      every tenant-scoped table; it re-seeds ``tenant-a`` / ``tenant-b``
      but **not** :data:`_FRESH_TENANT_ID`, so the calling operator's
      tenant row count is 0 pre-request.
    * The vault connector self-registered at package-import time;
      :func:`clear_registry` then an explicit re-register lands the
      typed ops against the known-empty PG ``endpoint_descriptor``
      table — same discipline ``test_connectors_vault_dev_e2e.py``
      uses.
    * ``vault_client_for_operator`` is replaced with a context manager
      yielding a root-token client bound to the dev container. The
      operator it receives is the one
      :func:`~meho_backplane.middleware.verify_jwt_and_bind` produced
      from the real Bearer token — proving #629.
    * The app is the production middleware stack
      (``RequestContextMiddleware`` + ``AuditMiddleware``) from
      ``build_integration_app`` plus the real ``operations`` router.
      No ``app.dependency_overrides`` on the auth seam — the request
      flows through the real ``verify_jwt`` → ``verify_jwt_and_bind``
      → ``require_role`` chain (AC 5).
    """
    reset_dispatcher_caches()
    set_default_reducer(PassThroughReducer())

    clear_registry()
    register_connector_v2(
        product="vault",
        version="1.x",
        impl_id="vault",
        cls=VaultConnector,
    )
    await register_vault_typed_operations(embedding_service=stub_embedding_service)

    # Capture the request-scoped operator the seam actually receives so
    # the test can assert it is the JWT-derived operator
    # ``verify_jwt_and_bind`` produced — making the #629 raw_jwt-from-
    # request-context contract a load-bearing assertion rather than an
    # incidental side effect of the 200 / no-``raw_jwt``-AttributeError
    # outcome.
    seen_operator: dict[str, Any] = {}

    @asynccontextmanager
    async def _root_client_cm(operator: Any) -> AsyncIterator[Any]:
        # ``operator`` is the request-scoped Operator the dispatcher
        # threads. Production would OIDC-login with ``operator.raw_jwt``;
        # dev mode has no OIDC method wired, so only the credential
        # acquisition is swapped (exactly as the integration harness
        # does) — but we record the operator's identity first so the
        # #629 threading contract is verified.
        seen_operator["sub"] = operator.sub
        seen_operator["tenant_id"] = str(operator.tenant_id)
        seen_operator["raw_jwt"] = operator.raw_jwt
        yield _root_client(vault_dev_addr)

    monkeypatch.setattr(_auth_vault, "vault_client_for_operator", _root_client_cm)

    # The default-on tenant-scope guard (#1725) pins KV calls under
    # ``secret/tenants/{tenant_id}/``. This e2e exercises the
    # JWT-bind → Vault-read path against a fixed legacy seed path
    # (``g08-t8/federation`` on the default ``secret`` mount), not the
    # per-tenant layout, so the guard would deny it with
    # VaultTenantScopeError. Disable the guard explicitly for this test
    # (the scope under test is the federated read path, not tenant
    # scoping — covered by ``test_connectors_vault_tenant_scope.py``).
    monkeypatch.setenv("VAULT_KV_TENANT_SCOPE_PREFIX", "")
    get_settings.cache_clear()

    app = build_integration_app()
    app.include_router(operations_router)
    app.state.seen_vault_operator = seen_operator
    try:
        yield app
    finally:
        reset_dispatcher_caches()
        clear_registry()


def _make_async_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    )


async def _count_tenant_rows(tenant_id: UUID) -> int:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(func.count()).select_from(Tenant).where(Tenant.id == tenant_id),
        )
        return int(result.scalar_one())


@_skip_no_docker
async def test_operations_call_vault_kv_read_through_verify_jwt_and_bind(
    vault_operations_app: FastAPI,
) -> None:
    """Real federated ``POST /api/v1/operations/call`` (vault) e2e.

    Drives a non-dry-run ``vault.kv.read`` over HTTP through the
    production ``verify_jwt_and_bind`` middleware against a real Vault
    container and a Postgres whose ``tenant`` table holds no row for
    the operator's tenant. Asserts the full G0.8 contract:

    * AC 2 — HTTP 200 + JSONFlux success envelope (``status == "ok"``);
      explicitly **not** a 500 and the body does **not** contain the
      pre-#629 ``'Target' object has no attribute 'raw_jwt'`` signature.
    * AC 3 — the ``tenant`` table held 0 rows for the operator's
      ``tenant_id`` before the request and exactly the JIT-seeded row
      after (proves #628 ``ensure_tenant`` fired in
      ``verify_jwt_and_bind``, not a pre-seeded fixture).
    * AC 4 — a synchronous ``audit_log`` row committed
      (``method=POST``, ``path=/api/v1/operations/call``,
      ``status_code=200``, ``operator_sub`` = the minted token's sub).
    """
    fresh_uuid = UUID(_FRESH_TENANT_ID)
    key = make_rsa_keypair("kid-g08-t8")
    token = mint_token(
        key,
        sub=_OPERATOR_SUB,
        tenant_id=_FRESH_TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )

    # AC 3 precondition: the clean-room operator's tenant is absent.
    assert await _count_tenant_rows(fresh_uuid) == 0

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        async with _make_async_client(vault_operations_app) as client:
            resp = await client.post(
                "/api/v1/operations/call",
                json={
                    "connector_id": _CONNECTOR_ID,
                    "op_id": "vault.kv.read",
                    "params": {"path": _SECRET_PATH},
                },
                headers={"Authorization": f"Bearer {token}"},
            )

    # AC 2 — success path, and explicitly NOT the pre-#629 failure.
    assert resp.status_code == 200, resp.text
    assert resp.status_code != 500
    assert "'Target' object has no attribute 'raw_jwt'" not in resp.text
    envelope = resp.json()
    assert envelope["status"] == "ok", envelope
    assert envelope["result"] == {"data": _SECRET_DATA, "version": 1}

    # AC 5 / #629 — the operator threaded into vault_client_for_operator
    # is the one verify_jwt_and_bind produced from the real Bearer
    # token, not a target. Asserting sub / tenant_id / raw_jwt makes the
    # operator-JWT-from-request-context contract load-bearing rather
    # than only incidentally proven by the 200 above.
    seen = vault_operations_app.state.seen_vault_operator
    assert seen, "vault_client_for_operator was never invoked"
    assert seen["sub"] == _OPERATOR_SUB
    assert seen["tenant_id"] == _FRESH_TENANT_ID
    assert seen["raw_jwt"] == token

    # AC 3 — ensure_tenant seeded exactly one row just-in-time.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (
            (await session.execute(select(Tenant).where(Tenant.id == fresh_uuid))).scalars().all()
        )
    assert len(rows) == 1
    assert rows[0].slug == f"tenant-{fresh_uuid}"

    # AC 4 — synchronous audit_log row for the HTTP call (postulate 7).
    async with sessionmaker() as session:
        audit_rows = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.operator_sub == _OPERATOR_SUB,
                        AuditLog.path == "/api/v1/operations/call",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(audit_rows) == 1, audit_rows
    audit = audit_rows[0]
    assert audit.method == "POST"
    assert audit.status_code == 200
    assert audit.operator_sub == _OPERATOR_SUB
    assert audit.tenant_id == fresh_uuid
