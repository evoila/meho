# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ArgoCD curated read core (G3.12-T2 #1391).

Coverage matrix (per Task #1391 acceptance criteria):

* **All six ops dispatch live** through :func:`~meho_backplane.operations.dispatch`
  against an ``argocd-server`` target: ``argocd.app.list`` / ``argocd.app.get``
  / ``argocd.app.diff`` / ``argocd.app.resource_tree`` /
  ``argocd.appproject.list`` / ``argocd.repo.list`` each hit the correct
  ArgoCD path with a bearer token and return ``status="ok"`` with the payload
  in ``OperationResult.result``. respx mocks the wire; the in-process Vault
  fake exercises the real default credential loader.
* **`argocd.app.diff` returns the managed-resources delta** — the same
  desired-vs-live drift ``argocd app diff <app>`` renders (each item carries
  ``liveState`` / ``targetState``).
* **Query-param plumbing** — ``app.list`` forwards ``projects`` (repeated) +
  ``selector``; the per-app ops URL-encode ``name`` into the path and forward
  the optional ``project`` scoping query.
* **Visibility to `search_operations`** — after registration the ops are
  retrievable by their connector_id.
* **`ARGOCD_OPS` registration shape** — all six carry ``safety_level="safe"``,
  ``requires_approval=False``, a ``read-only`` tag, ``additionalProperties=False``
  on the parameter schema, and non-empty ``llm_instructions``. No write op is
  registered.

Mirrors :mod:`tests.test_connectors_argocd_credread` (G3.12-T1 #1390) for the
dispatch lifecycle + Vault fake, and :mod:`tests.test_connectors_bind9_reads`
(#588) for the metadata-table invariants.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.argocd import ARGOCD_OPS, ArgoCdConnector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import reset_handler_cache
from meho_backplane.operations.meta_tools import search_operations
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

_CANARY_TOKEN = "argocd-bearer-canary-must-not-leak-reads"

_PRODUCT = "argocd"
_VERSION = "3.x"
_IMPL_ID = "argocd-api"
_CONNECTOR_ID = "argocd-api-3.x"

_ARGOCD_HOST = "argocd-reads.test.invalid"
_ARGOCD_BASE_URL = f"https://{_ARGOCD_HOST}"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis env vars Settings reads (Vault client + dispatcher)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher/handler caches + connector registry around every test."""
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        cls=ArgoCdConnector,
    )
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


@pytest.fixture
def _stub_embedding(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Deterministic embedding stub so registration/search don't pull ONNX.

    Patched at every site the typed-op pipeline and the search path resolve
    an embedding service, so neither registration (``encode_endpoint_text``)
    nor ``search_operations`` lazy-loads the fastembed model.
    """
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384

    def _fake_get_embedding_service() -> AsyncMock:
        return service

    # Registration computes the descriptor embedding via
    # ``encode_endpoint_text`` (imported into the typed_register namespace);
    # the search path resolves the query embedding via
    # ``_search.get_embedding_service``. Stub both so neither lazy-loads ONNX.
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    monkeypatch.setattr(
        "meho_backplane.operations._search.get_embedding_service",
        _fake_get_embedding_service,
    )
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """AsyncSession against the autouse-migrated per-worker SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


class _ReadTarget:
    """Target satisfying both ``ArgoCdTargetLike`` and the resolver shape."""

    def __init__(self) -> None:
        self.product = _PRODUCT
        self.fingerprint = type("_FP", (), {"version": "3.3.9"})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.name = "argocd-reads"
        self.host = _ARGOCD_HOST
        self.port = 443
        self.secret_ref = "targets/op-reads/argocd-reads"
        self.auth_model = "shared_service_account"


def _make_operator() -> Operator:
    """Operator carrying a non-empty raw_jwt (the fail-closed gate passes)."""
    return Operator(
        sub="op-reads-argocd",
        name="ArgoCD Reads Operator",
        email=None,
        raw_jwt="op.reads.argocd.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a4"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _register_ops(_stub_embedding: AsyncMock) -> None:
    """Walk ARGOCD_OPS through register_typed_operation against the test DB."""
    await ArgoCdConnector.register_operations()


# ---------------------------------------------------------------------------
# Live dispatch — each op hits the right path with a bearer token
# ---------------------------------------------------------------------------

#: Canned ArgoCD payloads (shaped to the real wire envelopes).
_APP_LIST_RESPONSE: dict[str, Any] = {
    "metadata": {"resourceVersion": "100"},
    "items": [
        {
            "metadata": {"name": "guestbook"},
            "status": {
                "sync": {"status": "OutOfSync"},
                "health": {"status": "Degraded"},
            },
        }
    ],
}
_APP_GET_RESPONSE: dict[str, Any] = {
    "metadata": {"name": "guestbook"},
    "spec": {"project": "default"},
    "status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"}},
}
_APP_DIFF_RESPONSE: dict[str, Any] = {
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
_RESOURCE_TREE_RESPONSE: dict[str, Any] = {
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
_APPPROJECT_LIST_RESPONSE: dict[str, Any] = {
    "metadata": {"resourceVersion": "5"},
    "items": [
        {
            "metadata": {"name": "default"},
            "spec": {"sourceRepos": ["*"], "destinations": [{"server": "*", "namespace": "*"}]},
        }
    ],
}
_REPO_LIST_RESPONSE: dict[str, Any] = {
    "metadata": {},
    "items": [
        {
            "repo": "https://github.com/example/gitops",
            "type": "git",
            "connectionState": {"status": "Successful", "message": ""},
        }
    ],
}


@pytest.mark.parametrize(
    ("op_id", "params", "method", "path", "payload"),
    [
        ("argocd.app.list", {}, "GET", "/api/v1/applications", _APP_LIST_RESPONSE),
        (
            "argocd.app.get",
            {"name": "guestbook"},
            "GET",
            "/api/v1/applications/guestbook",
            _APP_GET_RESPONSE,
        ),
        (
            "argocd.app.diff",
            {"name": "guestbook"},
            "GET",
            "/api/v1/applications/guestbook/managed-resources",
            _APP_DIFF_RESPONSE,
        ),
        (
            "argocd.app.resource_tree",
            {"name": "guestbook"},
            "GET",
            "/api/v1/applications/guestbook/resource-tree",
            _RESOURCE_TREE_RESPONSE,
        ),
        ("argocd.appproject.list", {}, "GET", "/api/v1/projects", _APPPROJECT_LIST_RESPONSE),
        ("argocd.repo.list", {}, "GET", "/api/v1/repositories", _REPO_LIST_RESPONSE),
    ],
)
@pytest.mark.asyncio
async def test_each_read_op_dispatches_live_and_returns_payload(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    params: dict[str, Any],
    method: str,
    path: str,
    payload: dict[str, Any],
) -> None:
    """Every curated read op dispatches end to end and returns the ArgoCD payload."""
    await _register_ops(_stub_embedding)
    install_fake_client(monkeypatch, secret={"token": _CANARY_TOKEN})

    target = _ReadTarget()
    operator = _make_operator()

    async with respx.mock(base_url=_ARGOCD_BASE_URL, assert_all_called=False) as mock:
        route = mock.request(method, path).respond(200, json=payload)
        result = await dispatch(
            operator=operator,
            connector_id=_CONNECTOR_ID,
            op_id=op_id,
            target=target,
            params=params,
        )

    assert result.status == "ok", result.error
    assert result.result == payload
    assert route.called and route.call_count == 1
    sent_auth = route.calls[0].request.headers.get("authorization")
    assert sent_auth == f"Bearer {_CANARY_TOKEN}"


@pytest.mark.asyncio
async def test_app_diff_returns_managed_resources_drift(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC: argocd.app.diff returns the desired-vs-live delta argocd-app-diff shows."""
    await _register_ops(_stub_embedding)
    install_fake_client(monkeypatch, secret={"token": _CANARY_TOKEN})

    async with respx.mock(base_url=_ARGOCD_BASE_URL, assert_all_called=False) as mock:
        mock.get("/api/v1/applications/guestbook/managed-resources").respond(
            200, json=_APP_DIFF_RESPONSE
        )
        result = await dispatch(
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id="argocd.app.diff",
            target=_ReadTarget(),
            params={"name": "guestbook"},
        )

    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    items = result.result["items"]
    assert len(items) == 1
    drift = items[0]
    # The drift carries both halves of the comparison the CLI renders.
    assert drift["liveState"] != drift["targetState"]
    assert drift["modified"] is True


@pytest.mark.asyncio
async def test_app_list_forwards_projects_and_selector_query(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """app.list forwards repeated ``projects`` + a ``selector`` query param."""
    await _register_ops(_stub_embedding)
    install_fake_client(monkeypatch, secret={"token": _CANARY_TOKEN})

    async with respx.mock(base_url=_ARGOCD_BASE_URL, assert_all_called=False) as mock:
        route = mock.get("/api/v1/applications").respond(200, json=_APP_LIST_RESPONSE)
        result = await dispatch(
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id="argocd.app.list",
            target=_ReadTarget(),
            params={"projects": ["team-a", "team-b"], "selector": "env=prod"},
        )

    assert result.status == "ok", result.error
    sent_url = route.calls[0].request.url
    assert sent_url.params.get_list("projects") == ["team-a", "team-b"]
    assert sent_url.params.get("selector") == "env=prod"


@pytest.mark.asyncio
async def test_app_get_url_encodes_name_and_forwards_project_query(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """app.get URL-encodes the name into the path and forwards ``project``."""
    await _register_ops(_stub_embedding)
    install_fake_client(monkeypatch, secret={"token": _CANARY_TOKEN})

    # A name with a slash must be percent-encoded so it stays one path segment.
    async with respx.mock(base_url=_ARGOCD_BASE_URL, assert_all_called=False) as mock:
        route = mock.get("/api/v1/applications/team%2Fapp").respond(200, json=_APP_GET_RESPONSE)
        result = await dispatch(
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id="argocd.app.get",
            target=_ReadTarget(),
            params={"name": "team/app", "project": "default"},
        )

    assert result.status == "ok", result.error
    assert route.called
    assert route.calls[0].request.url.params.get("project") == "default"


# ---------------------------------------------------------------------------
# search_operations visibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registered_ops_are_visible_to_search_operations(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """AC: the registered read ops are retrievable via search_operations."""
    await _register_ops(_stub_embedding)

    result = await search_operations(
        _make_operator(),
        {"connector_id": _CONNECTOR_ID, "query": "argocd application sync status", "limit": 25},
    )
    found = {hit["op_id"] for hit in result["hits"]}
    expected = {op.op_id for op in ARGOCD_OPS}
    assert expected <= found, f"missing from search: {expected - found}"


# ---------------------------------------------------------------------------
# ARGOCD_OPS registration-shape invariants
# ---------------------------------------------------------------------------

_EXPECTED_OP_IDS = {
    "argocd.app.list",
    "argocd.app.get",
    "argocd.app.diff",
    "argocd.app.resource_tree",
    "argocd.appproject.list",
    "argocd.repo.list",
}


def test_argocd_ops_table_is_exactly_the_six_read_ops() -> None:
    assert {op.op_id for op in ARGOCD_OPS} == _EXPECTED_OP_IDS


@pytest.mark.parametrize("op_id", sorted(_EXPECTED_OP_IDS))
def test_each_op_is_safe_no_approval_and_read_only(op_id: str) -> None:
    op = next(o for o in ARGOCD_OPS if o.op_id == op_id)
    assert op.safety_level == "safe"
    assert op.requires_approval is False
    assert "read-only" in op.tags


@pytest.mark.parametrize("op_id", sorted(_EXPECTED_OP_IDS))
def test_each_op_parameter_schema_disallows_additional_properties(op_id: str) -> None:
    op = next(o for o in ARGOCD_OPS if o.op_id == op_id)
    assert op.parameter_schema.get("additionalProperties") is False


@pytest.mark.parametrize("op_id", sorted(_EXPECTED_OP_IDS))
def test_each_op_has_llm_instructions_with_when_to_use_and_output_shape(op_id: str) -> None:
    op = next(o for o in ARGOCD_OPS if o.op_id == op_id)
    assert op.llm_instructions is not None
    assert op.llm_instructions.get("when_to_use", "").strip() != ""
    assert "output_shape" in op.llm_instructions


def test_no_write_or_mutating_op_is_registered() -> None:
    """AC: this Task ships read-only — no sync/rollback/set/write op."""
    for op in ARGOCD_OPS:
        assert op.safety_level == "safe"
        assert not any(
            token in op.op_id for token in (".sync", ".rollback", ".set", ".delete", ".create")
        )
        assert "write" not in op.tags


def test_per_app_ops_require_name_param() -> None:
    """app.get / app.diff / app.resource_tree must require the ``name`` param."""
    for op_id in ("argocd.app.get", "argocd.app.diff", "argocd.app.resource_tree"):
        op = next(o for o in ARGOCD_OPS if o.op_id == op_id)
        assert op.parameter_schema.get("required") == ["name"]
