# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G3.12-T3 ArgoCD recorded-fixture E2E test (#1392).

Drives every curated argocd read op through the full ``call_operation``
meta-tool dispatch stack — the same narrow-waist entry point the MCP
server and the ``meho operation call`` CLI verb reach — against a
respx-mocked ``argocd-server`` REST API. No running ArgoCD, no live
Vault: the bearer-token loader is injected (stub), ``respx`` replays
pre-recorded ArgoCD API fixtures, and the connector instance is
preseeded into the dispatcher's instance cache so dispatch uses the
stub-loaded connector rather than a plain one that would attempt a live
Vault read.

This is the operator/agent-surface counterpart to
:mod:`tests.test_connectors_argocd_reads`, which exercises the lower-level
:func:`~meho_backplane.operations.dispatch` entry point with a fabricated
target object. Here the target is a real ``targets`` row resolved through
:func:`~meho_backplane.targets.resolver.resolve_target`, so the test
proves the same path an operator hits via
``meho operation call argocd-api-3.x argocd.app.list --target …`` and an
agent hits via the MCP ``call_operation`` / ``search_operations``
meta-tools.

Acceptance criteria verified (Issue #1392)
==========================================

* All 6 read ops dispatch through ``call_operation`` against a registered
  ``argocd`` target and return ``status="ok"`` with the recorded ArgoCD
  payload (the CLI-verb-equivalent invocation surface).
* MCP ``search_operations(connector_id="argocd-api-3.x", …)`` surfaces all
  6 ops; ``call_operation`` dispatches them.
* Every dispatched op carries the Vault-sourced bearer token
  (``Authorization: Bearer <token>``) and the loader's secret never leaks
  into the returned envelope.

Fixtures reproduce realistic-but-minimal ArgoCD 3.x ``argocd-server`` REST
output (``ApplicationList`` / ``Application`` / managed-resources delta /
resource tree / ``AppProjectList`` / ``RepositoryList``).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import respx

import meho_backplane.connectors.argocd  # noqa: F401 -- import for registry side-effects
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.argocd import ArgoCdConnector
from meho_backplane.connectors.argocd.ops import ARGOCD_OPS
from meho_backplane.connectors.argocd.session import ArgoCdTargetLike
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.meta_tools import call_operation, search_operations
from meho_backplane.operations.reducer import PassThroughReducer

_CONNECTOR_ID = "argocd-api-3.x"
_TARGET_NAME = "rdc-argocd-e2e"
_ARGOCD_HOST = "argocd-e2e.test.invalid"
_ARGOCD_BASE_URL = f"https://{_ARGOCD_HOST}"
_BEARER_TOKEN = "argocd-bearer-token-e2e-DO-NOT-LEAK"

_OPERATOR_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a9")
_OPERATOR = Operator(
    sub="argocd-e2e-test",
    name="ArgoCD E2E Test Operator",
    email=None,
    raw_jwt="<argocd-e2e-raw-jwt>",
    tenant_id=_OPERATOR_TENANT_ID,
    tenant_role=TenantRole.OPERATOR,
)

EXPECTED_OP_IDS: tuple[str, ...] = (
    "argocd.app.list",
    "argocd.app.get",
    "argocd.app.diff",
    "argocd.app.resource_tree",
    "argocd.appproject.list",
    "argocd.repo.list",
)

_APP_NAME = "guestbook"


# ---------------------------------------------------------------------------
# Recorded argocd-server REST fixtures (minimal ArgoCD 3.x shapes)
# ---------------------------------------------------------------------------

_FIXTURE_APP_LIST: dict[str, Any] = {
    "metadata": {"resourceVersion": "12345"},
    "items": [
        {
            "metadata": {"name": _APP_NAME, "namespace": "argocd"},
            "spec": {
                "project": "default",
                "source": {
                    "repoURL": "https://github.com/example/gitops",
                    "path": "guestbook",
                    "targetRevision": "HEAD",
                },
                "destination": {
                    "server": "https://kubernetes.default.svc",
                    "namespace": "guestbook",
                },
            },
            "status": {
                "sync": {"status": "OutOfSync"},
                "health": {"status": "Degraded"},
            },
        }
    ],
}

_FIXTURE_APP_GET: dict[str, Any] = {
    "metadata": {"name": _APP_NAME, "namespace": "argocd"},
    "spec": {
        "project": "default",
        "source": {"repoURL": "https://github.com/example/gitops", "path": "guestbook"},
        "destination": {"server": "https://kubernetes.default.svc", "namespace": "guestbook"},
        "syncPolicy": {"automated": {"prune": True, "selfHeal": True}},
    },
    "status": {
        "sync": {"status": "Synced", "revision": "abc1234"},
        "health": {"status": "Healthy"},
        "conditions": [],
    },
}

_FIXTURE_APP_DIFF: dict[str, Any] = {
    "items": [
        {
            "group": "apps",
            "kind": "Deployment",
            "namespace": "guestbook",
            "name": "guestbook-ui",
            "liveState": '{"spec":{"replicas":1}}',
            "targetState": '{"spec":{"replicas":3}}',
            "normalizedLiveState": '{"spec":{"replicas":1}}',
            "predictedLiveState": '{"spec":{"replicas":3}}',
            "modified": True,
        }
    ]
}

_FIXTURE_RESOURCE_TREE: dict[str, Any] = {
    "nodes": [
        {
            "group": "apps",
            "kind": "Deployment",
            "namespace": "guestbook",
            "name": "guestbook-ui",
            "health": {"status": "Degraded"},
        }
    ],
    "orphanedNodes": [],
    "hosts": [],
    "shardsCount": "0",
}

_FIXTURE_APPPROJECT_LIST: dict[str, Any] = {
    "metadata": {"resourceVersion": "42"},
    "items": [
        {
            "metadata": {"name": "default"},
            "spec": {
                "sourceRepos": ["*"],
                "destinations": [{"server": "*", "namespace": "*"}],
            },
        }
    ],
}

_FIXTURE_REPO_LIST: dict[str, Any] = {
    "metadata": {},
    "items": [
        {
            "repo": "https://github.com/example/gitops",
            "type": "git",
            "connectionState": {"status": "Successful", "message": "", "attemptedAt": None},
        }
    ],
}


def _mount_argocd_routes(mock: respx.MockRouter) -> None:
    """Register the 6 read-op fixture routes on *mock*."""
    mock.get("/api/v1/applications").respond(200, json=_FIXTURE_APP_LIST)
    mock.get(f"/api/v1/applications/{_APP_NAME}").respond(200, json=_FIXTURE_APP_GET)
    mock.get(f"/api/v1/applications/{_APP_NAME}/managed-resources").respond(
        200, json=_FIXTURE_APP_DIFF
    )
    mock.get(f"/api/v1/applications/{_APP_NAME}/resource-tree").respond(
        200, json=_FIXTURE_RESOURCE_TREE
    )
    mock.get("/api/v1/projects").respond(200, json=_FIXTURE_APPPROJECT_LIST)
    mock.get("/api/v1/repositories").respond(200, json=_FIXTURE_REPO_LIST)


# ---------------------------------------------------------------------------
# Stub credential loader + seeded connector instance
# ---------------------------------------------------------------------------


def _stub_loader(
    _target: ArgoCdTargetLike, _operator: Operator
) -> Any:  # pragma: no cover - trivial
    """Return a fixed bearer token (no live Vault read)."""

    async def _load() -> dict[str, str]:
        return {"token": _BEARER_TOKEN}

    return _load()


async def _seed_argocd_target() -> TargetORM:
    """Insert the E2E target row (product=argocd, version=3.x) and return it."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        target = TargetORM(
            tenant_id=_OPERATOR_TENANT_ID,
            name=_TARGET_NAME,
            aliases=[],
            product="argocd",
            host=_ARGOCD_HOST,
            port=443,
            fqdn=None,
            secret_ref="rdc-hetzner-dc/argocd/api-token",
            auth_model="shared_service_account",
            vpn_required=False,
            extras={},
            fingerprint={"version": "3.x"},
            preferred_impl_id="argocd-api",
            notes="seeded by test_connectors_argocd_e2e",
        )
        session.add(target)
        await session.commit()
        await session.refresh(target)
        session.expunge(target)
        return target


def _wire_seeded_connector() -> ArgoCdConnector:
    """Preseed a stub-loader connector into the dispatcher's instance cache."""
    instance = ArgoCdConnector(credentials_loader=_stub_loader)
    from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE

    _CONNECTOR_INSTANCE_CACHE[ArgoCdConnector] = instance
    return instance


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin env vars :class:`Settings` requires for this module."""
    from meho_backplane.settings import get_settings

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher caches around every test."""
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()


@pytest.fixture
async def argocd_e2e() -> AsyncIterator[ArgoCdConnector]:
    """Register the read ops, seed the target, and preseed the connector."""
    set_default_reducer(PassThroughReducer())
    await ArgoCdConnector.register_operations()
    await _seed_argocd_target()
    connector = _wire_seeded_connector()
    yield connector
    await connector.aclose()


# ---------------------------------------------------------------------------
# Op-table invariant (no write op shipped)
# ---------------------------------------------------------------------------


def test_argocd_e2e_op_set_is_exactly_the_six_read_ops() -> None:
    """ARGOCD_OPS carries exactly the 6 curated read ops and no write op."""
    assert {op.op_id for op in ARGOCD_OPS} == set(EXPECTED_OP_IDS)
    assert len(ARGOCD_OPS) == 6


# ---------------------------------------------------------------------------
# Full call_operation dispatch path (one test per op)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_argocd_e2e_app_list(argocd_e2e: ArgoCdConnector) -> None:
    """argocd.app.list returns the ApplicationList with sync/health status."""
    with respx.mock(base_url=_ARGOCD_BASE_URL, assert_all_called=False) as mock:
        _mount_argocd_routes(mock)
        result = await call_operation(
            _OPERATOR,
            {
                "connector_id": _CONNECTOR_ID,
                "op_id": "argocd.app.list",
                "target": {"name": _TARGET_NAME},
                "params": {},
            },
        )
    assert result["status"] == "ok", f"app.list failed: {result.get('error')}"
    items = result["result"]["items"]
    assert items[0]["metadata"]["name"] == _APP_NAME
    assert items[0]["status"]["sync"]["status"] == "OutOfSync"
    assert items[0]["status"]["health"]["status"] == "Degraded"


@pytest.mark.asyncio
async def test_argocd_e2e_app_get(argocd_e2e: ArgoCdConnector) -> None:
    """argocd.app.get returns one app's full spec + status (bare-string target)."""
    with respx.mock(base_url=_ARGOCD_BASE_URL, assert_all_called=False) as mock:
        _mount_argocd_routes(mock)
        result = await call_operation(
            _OPERATOR,
            {
                "connector_id": _CONNECTOR_ID,
                "op_id": "argocd.app.get",
                # Bare-string target — the other shape call_operation normalises.
                "target": _TARGET_NAME,
                "params": {"name": _APP_NAME},
            },
        )
    assert result["status"] == "ok", f"app.get failed: {result.get('error')}"
    app = result["result"]
    assert app["metadata"]["name"] == _APP_NAME
    assert app["spec"]["syncPolicy"]["automated"]["selfHeal"] is True
    assert app["status"]["sync"]["status"] == "Synced"


@pytest.mark.asyncio
async def test_argocd_e2e_app_diff(argocd_e2e: ArgoCdConnector) -> None:
    """argocd.app.diff returns the managed-resources desired-vs-live delta."""
    with respx.mock(base_url=_ARGOCD_BASE_URL, assert_all_called=False) as mock:
        _mount_argocd_routes(mock)
        result = await call_operation(
            _OPERATOR,
            {
                "connector_id": _CONNECTOR_ID,
                "op_id": "argocd.app.diff",
                "target": {"name": _TARGET_NAME},
                "params": {"name": _APP_NAME},
            },
        )
    assert result["status"] == "ok", f"app.diff failed: {result.get('error')}"
    drift = result["result"]["items"][0]
    assert drift["modified"] is True
    assert drift["liveState"] != drift["targetState"]


@pytest.mark.asyncio
async def test_argocd_e2e_app_resource_tree(argocd_e2e: ArgoCdConnector) -> None:
    """argocd.app.resource_tree returns the reconciled tree with per-node health."""
    with respx.mock(base_url=_ARGOCD_BASE_URL, assert_all_called=False) as mock:
        _mount_argocd_routes(mock)
        result = await call_operation(
            _OPERATOR,
            {
                "connector_id": _CONNECTOR_ID,
                "op_id": "argocd.app.resource_tree",
                "target": {"name": _TARGET_NAME},
                "params": {"name": _APP_NAME},
            },
        )
    assert result["status"] == "ok", f"app.resource_tree failed: {result.get('error')}"
    nodes = result["result"]["nodes"]
    assert nodes[0]["kind"] == "Deployment"
    assert nodes[0]["health"]["status"] == "Degraded"


@pytest.mark.asyncio
async def test_argocd_e2e_appproject_list(argocd_e2e: ArgoCdConnector) -> None:
    """argocd.appproject.list returns AppProjects with their allow-lists."""
    with respx.mock(base_url=_ARGOCD_BASE_URL, assert_all_called=False) as mock:
        _mount_argocd_routes(mock)
        result = await call_operation(
            _OPERATOR,
            {
                "connector_id": _CONNECTOR_ID,
                "op_id": "argocd.appproject.list",
                "target": {"name": _TARGET_NAME},
                "params": {},
            },
        )
    assert result["status"] == "ok", f"appproject.list failed: {result.get('error')}"
    proj = result["result"]["items"][0]
    assert proj["metadata"]["name"] == "default"
    assert proj["spec"]["sourceRepos"] == ["*"]


@pytest.mark.asyncio
async def test_argocd_e2e_repo_list(argocd_e2e: ArgoCdConnector) -> None:
    """argocd.repo.list returns configured repos with their connection state."""
    with respx.mock(base_url=_ARGOCD_BASE_URL, assert_all_called=False) as mock:
        _mount_argocd_routes(mock)
        result = await call_operation(
            _OPERATOR,
            {
                "connector_id": _CONNECTOR_ID,
                "op_id": "argocd.repo.list",
                "target": {"name": _TARGET_NAME},
                "params": {},
            },
        )
    assert result["status"] == "ok", f"repo.list failed: {result.get('error')}"
    repo = result["result"]["items"][0]
    assert repo["type"] == "git"
    assert repo["connectionState"]["status"] == "Successful"


# ---------------------------------------------------------------------------
# Bearer-token + secret-leak guarantees across the full op set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_argocd_e2e_all_ops_carry_bearer_token_and_never_leak_secret(
    argocd_e2e: ArgoCdConnector,
) -> None:
    """Every dispatched op carries the Vault-sourced Bearer; the token never leaks."""
    with respx.mock(base_url=_ARGOCD_BASE_URL, assert_all_called=False) as mock:
        _mount_argocd_routes(mock)
        for op_id, params in (
            ("argocd.app.list", {}),
            ("argocd.app.get", {"name": _APP_NAME}),
            ("argocd.app.diff", {"name": _APP_NAME}),
            ("argocd.app.resource_tree", {"name": _APP_NAME}),
            ("argocd.appproject.list", {}),
            ("argocd.repo.list", {}),
        ):
            result = await call_operation(
                _OPERATOR,
                {
                    "connector_id": _CONNECTOR_ID,
                    "op_id": op_id,
                    "target": {"name": _TARGET_NAME},
                    "params": params,
                },
            )
            assert result["status"] == "ok", f"{op_id} failed: {result.get('error')}"
            # The Vault-sourced bearer token never rides back in the envelope.
            assert _BEARER_TOKEN not in str(result), f"{op_id} leaked the bearer token"

        # Every argocd-server GET carried the Vault-sourced bearer; the
        # operator JWT is never forwarded to ArgoCD.
        for call in mock.calls:
            req = call.request
            assert req.headers.get("authorization") == f"Bearer {_BEARER_TOKEN}"
            assert _OPERATOR.raw_jwt not in (req.headers.get("authorization") or "")


# ---------------------------------------------------------------------------
# search_operations visibility (MCP review surface)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_argocd_e2e_ops_visible_to_search_operations(
    argocd_e2e: ArgoCdConnector,
) -> None:
    """All 6 read ops are discoverable via the MCP search_operations meta-tool."""
    result = await search_operations(
        _OPERATOR,
        {
            "connector_id": _CONNECTOR_ID,
            "query": "argocd application sync health diff project repository",
            "limit": 50,
        },
    )
    surfaced = {hit["op_id"] for hit in result["hits"]}
    missing = set(EXPECTED_OP_IDS) - surfaced
    assert not missing, f"search_operations did not surface: {missing} (got {surfaced})"
