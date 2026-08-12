# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Harbor audited-read typed ops (#2856).

Coverage matrix (per Task #2856 acceptance criteria):

* **Zero-catalog typed dispatch (AC #1).** Each of the 9 reads dispatches
  through :func:`~meho_backplane.operations.dispatch` against a
  respx-mocked Harbor registry with **only** the typed registrar run (no
  ingested descriptor rows) and returns ``status="ok"``. The persisted
  descriptors carry ``source_kind="typed"``. Path templates interpolate
  their ``{project_name}`` / ``{repository_name}`` / ``{reference}``
  params, and optional filters (``public`` / ``page`` / ``page_size`` …)
  forward as query params.
* **Robot-secret invariant (AC #2).** ``harbor.robot.list`` returns each
  robot's numeric ``id`` and never a ``secret`` — the handler passes
  Harbor's list payload through verbatim, and Harbor never includes the
  secret in a list response (only the create-time POST does, #621).
* **Registration-shape invariants (AC #3).** All 9 reads carry
  ``safety_level="safe"``, ``requires_approval=False``, a ``read-only``
  tag, and a non-empty ``llm_instructions`` blob (moved verbatim from the
  retired ``HARBOR_CORE_OPS``); no write op is registered.

Harbor authenticates with HTTP Basic on every request (no token/session
establish), so the respx router mocks the read GETs directly. Mirrors
:mod:`tests.test_connectors_sddc_manager_typed_reads` for the
dispatch-lifecycle + embedding-stub plumbing.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors.harbor import (
    HARBOR_CONNECTOR_ID,
    HARBOR_IMPL_ID,
    HARBOR_PRODUCT,
    HARBOR_TYPED_OPS,
    HARBOR_VERSION,
    HarborConnector,
    register_harbor_typed_operations,
)
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations.meta_tools import search_operations
from meho_backplane.settings import get_settings

_HARBOR_HOST = "harbor-typed.test.invalid"
_HARBOR_BASE_URL = f"https://{_HARBOR_HOST}"

_EXPECTED_OP_IDS = {
    "harbor.about",
    "harbor.health",
    "harbor.project.list",
    "harbor.project.info",
    "harbor.repository.list",
    "harbor.repository.info",
    "harbor.artifact.list",
    "harbor.artifact.info",
    "harbor.robot.list",
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis env vars Settings reads (Vault client + dispatcher)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
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
        product=HARBOR_PRODUCT,
        version=HARBOR_VERSION,
        impl_id=HARBOR_IMPL_ID,
        cls=HarborConnector,
    )
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


@pytest.fixture
def _stub_embedding(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Deterministic embedding stub so registration/search don't pull ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384

    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    monkeypatch.setattr(
        "meho_backplane.operations._search.get_embedding_service",
        lambda: service,
    )
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """AsyncSession against the autouse-migrated per-worker SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


class _HarborReadTarget:
    """Target satisfying both ``HarborTargetLike`` and the resolver shape."""

    def __init__(self) -> None:
        self.product = HARBOR_PRODUCT
        # ``supported_version_range`` is ``">=2.0,<3.0"``; the resolver parses
        # fingerprint.version as PEP 440, so use a concrete dotted version (not
        # the wildcard "2.x" triple version, which isn't PEP-440 parseable).
        self.fingerprint = type("_FP", (), {"version": "2.11.0"})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = UUID("00000000-0000-0000-0000-0000000000d0")
        self.name = "harbor-typed"
        self.host = _HARBOR_HOST
        self.port = 443
        self.secret_ref = "targets/op-reads/harbor-typed"
        self.auth_model = "shared_service_account"


def _make_operator() -> Operator:
    """Operator carrying a non-empty raw_jwt (the fail-closed gate passes)."""
    return Operator(
        sub="op-reads-harbor",
        name="Harbor Reads Operator",
        email=None,
        raw_jwt="op.reads.harbor.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-0000000000d4"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _harbor_credentials_loader(_target: object, _operator: Operator) -> dict[str, str]:
    """Static credentials loader — bypasses the live operator-context Vault read."""
    return {"username": "harbor-typed-svc", "password": "harbor-typed-pw"}


async def _register_and_resolve(_stub_embedding: AsyncMock) -> HarborConnector:
    """Run the typed registrar (only) and return the credentials-stubbed instance."""
    await register_harbor_typed_operations()
    instance = get_or_create_connector_instance(HarborConnector)
    instance._credentials_loader = _harbor_credentials_loader  # type: ignore[attr-defined]
    return instance


# ---------------------------------------------------------------------------
# AC #1 — zero-catalog typed dispatch
# ---------------------------------------------------------------------------

_ROBOTS_PAYLOAD: list[dict[str, Any]] = [
    {"id": 7, "name": "robot$ci-pusher", "level": "system", "disable": False, "expires_at": -1},
]

_DISPATCH_CASES: list[tuple[str, str, dict[str, Any], Any]] = [
    ("harbor.about", "/api/v2.0/systeminfo", {}, {"harbor_version": "v2.11.0"}),
    ("harbor.health", "/api/v2.0/health", {}, {"status": "healthy"}),
    ("harbor.project.list", "/api/v2.0/projects", {}, [{"id": 1, "name": "library"}]),
    (
        "harbor.project.info",
        "/api/v2.0/projects/library",
        {"project_name": "library"},
        {"id": 1, "name": "library"},
    ),
    (
        "harbor.repository.list",
        "/api/v2.0/projects/library/repositories",
        {"project_name": "library"},
        [{"id": 1, "name": "library/ubuntu"}],
    ),
    (
        "harbor.repository.info",
        "/api/v2.0/projects/library/repositories/ubuntu",
        {"project_name": "library", "repository_name": "ubuntu"},
        {"id": 1, "name": "library/ubuntu"},
    ),
    (
        "harbor.artifact.list",
        "/api/v2.0/projects/library/repositories/ubuntu/artifacts",
        {"project_name": "library", "repository_name": "ubuntu"},
        [{"digest": "sha256:abc", "tags": [{"name": "latest"}]}],
    ),
    (
        "harbor.artifact.info",
        "/api/v2.0/projects/library/repositories/ubuntu/artifacts/latest",
        {"project_name": "library", "repository_name": "ubuntu", "reference": "latest"},
        {"digest": "sha256:abc", "tags": [{"name": "latest"}]},
    ),
    ("harbor.robot.list", "/api/v2.0/robots", {}, _ROBOTS_PAYLOAD),
]


@pytest.mark.parametrize(("op_id", "path", "params", "payload"), _DISPATCH_CASES, ids=lambda v: v)
@pytest.mark.asyncio
async def test_each_typed_op_dispatches_zero_catalog(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    op_id: str,
    path: str,
    params: dict[str, Any],
    payload: Any,
) -> None:
    """AC #1: each read dispatches typed (zero ingested catalog) with HTTP Basic auth."""
    await _register_and_resolve(_stub_embedding)

    async with respx.mock(base_url=_HARBOR_BASE_URL, assert_all_called=False) as mock:
        route = mock.get(path).respond(200, json=payload)
        result = await dispatch(
            operator=_make_operator(),
            connector_id=HARBOR_CONNECTOR_ID,
            op_id=op_id,
            target=_HarborReadTarget(),
            params=params,
        )

    assert result.status == "ok", result.error
    assert result.result == payload
    assert route.called and route.call_count == 1
    # Harbor is HTTP-Basic on every request — the read carries the header.
    sent_auth = route.calls[0].request.headers.get("authorization")
    assert sent_auth is not None and sent_auth.startswith("Basic ")


@pytest.mark.asyncio
async def test_project_info_builds_path_from_name(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """AC #1: harbor.project.info interpolates project_name into the path."""
    await _register_and_resolve(_stub_embedding)

    async with respx.mock(base_url=_HARBOR_BASE_URL, assert_all_called=False) as mock:
        route = mock.get("/api/v2.0/projects/my-proj").respond(200, json={"name": "my-proj"})
        result = await dispatch(
            operator=_make_operator(),
            connector_id=HARBOR_CONNECTOR_ID,
            op_id="harbor.project.info",
            target=_HarborReadTarget(),
            params={"project_name": "my-proj"},
        )

    assert result.status == "ok", result.error
    assert route.called and route.call_count == 1


@pytest.mark.asyncio
async def test_project_list_forwards_query_params(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """AC #1: project.list forwards public/page/page_size; a False bool is not dropped."""
    await _register_and_resolve(_stub_embedding)

    async with respx.mock(base_url=_HARBOR_BASE_URL, assert_all_called=False) as mock:
        route = mock.get("/api/v2.0/projects").respond(200, json=[])
        result = await dispatch(
            operator=_make_operator(),
            connector_id=HARBOR_CONNECTOR_ID,
            op_id="harbor.project.list",
            target=_HarborReadTarget(),
            params={"public": False, "page": 2, "page_size": 50},
        )

    assert result.status == "ok", result.error
    sent = route.calls[0].request.url
    # httpx renders a Python bool as lowercase; presence-based forwarding keeps False.
    assert sent.params.get("public") == "false"
    assert sent.params.get("page") == "2"
    assert sent.params.get("page_size") == "50"


@pytest.mark.asyncio
async def test_registered_ops_are_source_kind_typed(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """AC #1: the persisted descriptor rows carry ``source_kind='typed'``."""
    await register_harbor_typed_operations()

    rows = (
        (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == HARBOR_PRODUCT,
                    EndpointDescriptor.impl_id == HARBOR_IMPL_ID,
                    EndpointDescriptor.op_id.in_(list(_EXPECTED_OP_IDS)),
                )
            )
        )
        .scalars()
        .all()
    )
    assert {r.op_id for r in rows} == _EXPECTED_OP_IDS
    assert all(r.source_kind == "typed" for r in rows)
    assert all(r.handler_ref is not None for r in rows)


# ---------------------------------------------------------------------------
# AC #2 — robot-secret invariant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_robot_list_returns_id_and_never_secret(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """AC #2: robot.list exposes each robot's numeric id and never a secret.

    The handler returns Harbor's list payload verbatim; Harbor never includes
    ``secret`` in a list response, so the invariant holds by pass-through (the
    handler fabricates nothing).
    """
    await _register_and_resolve(_stub_embedding)
    payload = [
        {"id": 42, "name": "robot$ci-pusher", "disable": False, "expires_at": -1},
        {"id": 43, "name": "robot$cd-puller", "disable": True, "expires_at": 1893456000},
    ]

    async with respx.mock(base_url=_HARBOR_BASE_URL, assert_all_called=False) as mock:
        mock.get("/api/v2.0/robots").respond(200, json=payload)
        result = await dispatch(
            operator=_make_operator(),
            connector_id=HARBOR_CONNECTOR_ID,
            op_id="harbor.robot.list",
            target=_HarborReadTarget(),
            params={},
        )

    assert result.status == "ok", result.error
    assert isinstance(result.result, list)
    assert [r["id"] for r in result.result] == [42, 43]
    assert "secret" not in repr(result.result)


def test_all_reads_classify_as_read_op_class() -> None:
    """AC #2/#3: classify_op maps all 9 reads to 'read' (full-detail, non-sensitive)."""
    for op_id in _EXPECTED_OP_IDS:
        assert classify_op(op_id) == "read", op_id


# ---------------------------------------------------------------------------
# AC #3 — registration-shape invariants
# ---------------------------------------------------------------------------


def test_typed_ops_table_is_exactly_the_read_core() -> None:
    assert {op.op_id for op in HARBOR_TYPED_OPS} == _EXPECTED_OP_IDS


@pytest.mark.parametrize("op_id", sorted(_EXPECTED_OP_IDS))
def test_each_op_is_safe_no_approval_and_read_only(op_id: str) -> None:
    op = next(o for o in HARBOR_TYPED_OPS if o.op_id == op_id)
    assert op.safety_level == "safe"
    assert op.requires_approval is False
    assert "read-only" in op.tags


@pytest.mark.parametrize("op_id", sorted(_EXPECTED_OP_IDS))
def test_each_op_has_llm_instructions_with_canonical_keys(op_id: str) -> None:
    """The llm_instructions blob (moved verbatim from HARBOR_CORE_OPS) keeps its keys."""
    op = next(o for o in HARBOR_TYPED_OPS if o.op_id == op_id)
    assert op.llm_instructions is not None
    for key in ("when_to_call", "output_shape", "next_step"):
        value = op.llm_instructions.get(key)
        assert isinstance(value, str) and value.strip(), f"{op_id}: {key}"


@pytest.mark.parametrize("op_id", sorted(_EXPECTED_OP_IDS))
def test_each_op_parameter_schema_disallows_additional_properties(op_id: str) -> None:
    op = next(o for o in HARBOR_TYPED_OPS if o.op_id == op_id)
    assert op.parameter_schema.get("additionalProperties") is False


def test_no_write_or_mutating_op_is_registered() -> None:
    """Read-only Task: no create/write op ships here (robot writes live in harbor.ops)."""
    for op in HARBOR_TYPED_OPS:
        assert not any(
            token in op.op_id for token in (".create", ".delete", ".update", ".set", ".put")
        )
        assert "write" not in op.tags


@pytest.mark.asyncio
async def test_registered_ops_are_visible_to_search_operations(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """The registered typed reads are retrievable via search_operations."""
    await register_harbor_typed_operations()

    result = await search_operations(
        _make_operator(),
        {
            "connector_id": HARBOR_CONNECTOR_ID,
            "query": "harbor projects repositories artifacts robots health systeminfo",
            "limit": 25,
        },
    )
    found = {hit["op_id"] for hit in result["hits"]}
    assert found >= _EXPECTED_OP_IDS, f"missing from search: {_EXPECTED_OP_IDS - found}"
