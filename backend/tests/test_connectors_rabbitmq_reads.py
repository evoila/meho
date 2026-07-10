# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Dispatch-level + registration tests for RabbitMqConnector (#2233).

Coverage matrix (per Task #2233 acceptance criteria):

* **Registry resolution (AC #1)** — ``rabbitmq`` resolves via
  ``register_connector_v2`` (versioned + wildcard) and appears in
  ``all_connectors_v2()``.
* **Live dispatch (AC #5)** — representative ops dispatch end to end
  through :func:`~meho_backplane.operations.dispatch` against a RabbitMQ
  target, hitting the right path with an HTTP Basic header and returning
  the payload. The in-process Vault fake exercises the real default
  credential loader (``username`` + ``password``).
* **Redaction end to end (AC #3)** — the ``rabbitmq.shovels`` op returns a
  payload whose ``amqp://u:p@h`` userinfo + ``password`` values are blanked.
* **Vhost scoping** — a topology op forwards ``vhost`` into a
  percent-encoded path segment.
* **Registration shape (AC #1/#2)** — every op is ``safety_level="safe"`` +
  ``requires_approval=False``, carries a ``read-only`` tag and a RabbitMQ
  user tag, and its parameter schema disallows additional properties.
* **search_operations visibility (AC #5)** — the topology ops are
  retrievable by connector_id.

Mirrors :mod:`tests.test_connectors_argocd_reads` for the dispatch
lifecycle + Vault fake.
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
from meho_backplane.connectors.rabbitmq import (
    RABBITMQ_OPS,
    RABBITMQ_REDACTED_OP_IDS,
    RabbitMqConnector,
)
from meho_backplane.connectors.registry import (
    all_connectors_v2,
    clear_registry,
    register_connector_v2,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import reset_handler_cache
from meho_backplane.operations.meta_tools import search_operations
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

_PRODUCT = "rabbitmq"
_VERSION = "3.x"
_IMPL_ID = "rabbitmq-management"
_CONNECTOR_ID = "rabbitmq-management-3.x"

_RABBIT_HOST = "rabbitmq-reads.test.invalid"
_RABBIT_BASE_URL = f"https://{_RABBIT_HOST}:15672"

_USERNAME = "monitor"
_PASSWORD = "rabbit-reads-canary-must-not-leak"


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
        product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, cls=RabbitMqConnector
    )
    register_connector_v2(product=_PRODUCT, version="", impl_id="", cls=RabbitMqConnector)
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


class _ReadTarget:
    """Target satisfying both ``RabbitMqTargetLike`` and the resolver shape."""

    def __init__(self) -> None:
        self.product = _PRODUCT
        self.fingerprint = type("_FP", (), {"version": "3.13.7"})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
        self.name = "rabbitmq-reads"
        self.host = _RABBIT_HOST
        self.port = 15672
        self.secret_ref = "targets/op-reads/rabbitmq-reads"
        self.auth_model = "shared_service_account"


def _make_operator() -> Operator:
    return Operator(
        sub="op-reads-rabbitmq",
        name="RabbitMQ Reads Operator",
        email=None,
        raw_jwt="op.reads.rabbitmq.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-00000000b2b4"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _register_ops() -> None:
    await RabbitMqConnector.register_operations()


# ---------------------------------------------------------------------------
# Registry resolution (AC #1)
# ---------------------------------------------------------------------------


def test_rabbitmq_registers_versioned_and_wildcard() -> None:
    """AC: rabbitmq resolves via register_connector_v2 (versioned + wildcard)."""
    registry = all_connectors_v2()
    assert registry[(_PRODUCT, _VERSION, _IMPL_ID)] is RabbitMqConnector
    assert registry[(_PRODUCT, "", "")] is RabbitMqConnector


# ---------------------------------------------------------------------------
# Live dispatch (AC #5)
# ---------------------------------------------------------------------------

_OVERVIEW: dict[str, Any] = {"rabbitmq_version": "3.13.7", "cluster_name": "rabbit@primary"}
_QUEUES: list[dict[str, Any]] = [
    {"name": "events", "vhost": "/", "messages": 42, "consumers": 1, "state": "running"}
]


@pytest.mark.parametrize(
    ("op_id", "params", "method", "path", "payload"),
    [
        ("rabbitmq.overview", {}, "GET", "/api/overview", _OVERVIEW),
        ("rabbitmq.nodes", {}, "GET", "/api/nodes", [{"name": "rabbit@a", "running": True}]),
        ("rabbitmq.queues", {}, "GET", "/api/queues", _QUEUES),
        ("rabbitmq.vhosts", {}, "GET", "/api/vhosts", [{"name": "/"}]),
        ("rabbitmq.connections", {}, "GET", "/api/connections", []),
    ],
)
@pytest.mark.asyncio
async def test_each_read_op_dispatches_live_with_basic_auth(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    params: dict[str, Any],
    method: str,
    path: str,
    payload: Any,
) -> None:
    """AC: each read op dispatches end to end and returns the RabbitMQ payload."""
    await _register_ops()
    install_fake_client(monkeypatch, secret={"username": _USERNAME, "password": _PASSWORD})

    async with respx.mock(base_url=_RABBIT_BASE_URL, assert_all_called=False) as mock:
        route = mock.request(method, path).respond(200, json=payload)
        result = await dispatch(
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=op_id,
            target=_ReadTarget(),
            params=params,
        )

    assert result.status == "ok", result.error
    assert route.called and route.call_count == 1
    sent = route.calls[0].request.headers.get("authorization")
    assert sent is not None and sent.startswith("Basic ")


@pytest.mark.asyncio
async def test_queues_forwards_vhost_into_encoded_path(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vhost-scoped op appends a percent-encoded ``/{vhost}`` segment."""
    await _register_ops()
    install_fake_client(monkeypatch, secret={"username": _USERNAME, "password": _PASSWORD})

    async with respx.mock(base_url=_RABBIT_BASE_URL, assert_all_called=False) as mock:
        # The default vhost '/' percent-encodes to '%2F'.
        route = mock.get("/api/queues/%2F").respond(200, json=_QUEUES)
        result = await dispatch(
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id="rabbitmq.queues",
            target=_ReadTarget(),
            params={"vhost": "/"},
        )

    assert result.status == "ok", result.error
    assert route.called


@pytest.mark.asyncio
async def test_shovels_op_redacts_amqp_credentials_end_to_end(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC #3: rabbitmq.shovels returns a payload with amqp userinfo + password blanked."""
    await _register_ops()
    install_fake_client(monkeypatch, secret={"username": _USERNAME, "password": _PASSWORD})

    leaky = [
        {
            "name": "to-dr",
            "vhost": "/",
            "value": {"src-uri": "amqp://svc:leaked@primary", "password": "topsecret"},
        }
    ]
    async with respx.mock(base_url=_RABBIT_BASE_URL, assert_all_called=False) as mock:
        mock.get("/api/parameters/shovel").respond(200, json=leaky)
        result = await dispatch(
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id="rabbitmq.shovels",
            target=_ReadTarget(),
            params={},
        )

    assert result.status == "ok", result.error
    blob = repr(result.result)
    assert "leaked" not in blob
    assert "topsecret" not in blob


@pytest.mark.asyncio
async def test_credential_loader_chain_reads_vault_and_never_leaks(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC #5: the default loader reads Vault under the operator; no secret leaks."""
    await _register_ops()
    fake = install_fake_client(monkeypatch, secret={"username": _USERNAME, "password": _PASSWORD})

    target = _ReadTarget()
    async with respx.mock(base_url=_RABBIT_BASE_URL, assert_all_called=False) as mock:
        mock.get("/api/overview").respond(200, json=_OVERVIEW)
        result = await dispatch(
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id="rabbitmq.overview",
            target=target,
            params={},
        )

    assert result.status == "ok", result.error
    # The default loader read Vault under the operator's identity.
    assert fake.auth.jwt.login_calls[-1]["jwt"] == "op.reads.rabbitmq.jwt"
    assert fake.secrets.kv.v2.read_calls[-1]["path"] == target.secret_ref
    # The password never rides the OperationResult.
    assert _PASSWORD not in repr(result)


# ---------------------------------------------------------------------------
# search_operations visibility (AC #5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registered_ops_are_visible_to_search_operations(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """AC: the registered ops are retrievable via search_operations."""
    await _register_ops()
    result = await search_operations(
        _make_operator(),
        {"connector_id": _CONNECTOR_ID, "query": "rabbitmq queue depth shovel status", "limit": 50},
    )
    found = {hit["op_id"] for hit in result["hits"]}
    expected = {op.op_id for op in RABBITMQ_OPS}
    assert expected <= found, f"missing from search: {expected - found}"


# ---------------------------------------------------------------------------
# Registration-shape invariants (AC #1 / #2)
# ---------------------------------------------------------------------------

_EXPECTED_OP_IDS = {
    "rabbitmq.overview",
    "rabbitmq.nodes",
    "rabbitmq.exchanges",
    "rabbitmq.queues",
    "rabbitmq.bindings",
    "rabbitmq.vhosts",
    "rabbitmq.connections",
    "rabbitmq.channels",
    "rabbitmq.consumers",
    "rabbitmq.shovels",
    "rabbitmq.shovel_status",
    "rabbitmq.federation_links",
    "rabbitmq.parameters",
    "rabbitmq.policies",
    "rabbitmq.definitions",
    "rabbitmq.request",
}

_USER_TAGS = {"monitoring", "policymaker", "administrator"}


def test_ops_table_is_the_expected_set() -> None:
    assert {op.op_id for op in RABBITMQ_OPS} == _EXPECTED_OP_IDS


@pytest.mark.parametrize("op_id", sorted(_EXPECTED_OP_IDS))
def test_every_op_is_safe_read_only_and_tagged(op_id: str) -> None:
    op = next(o for o in RABBITMQ_OPS if o.op_id == op_id)
    assert op.safety_level == "safe"
    assert op.requires_approval is False
    assert "read-only" in op.tags
    # Exactly one RabbitMQ user tag documents the required broker tag.
    assert len(_USER_TAGS & set(op.tags)) == 1
    assert op.parameter_schema.get("additionalProperties") is False
    assert op.llm_instructions.get("when_to_use", "").strip() != ""
    assert "output_shape" in op.llm_instructions


def test_shovel_federation_param_ops_require_policymaker() -> None:
    """Shovel-definitions / parameters / policies are tagged policymaker."""
    for op_id in ("rabbitmq.shovels", "rabbitmq.parameters", "rabbitmq.policies"):
        op = next(o for o in RABBITMQ_OPS if o.op_id == op_id)
        assert "policymaker" in op.tags


def test_definitions_requires_administrator() -> None:
    op = next(o for o in RABBITMQ_OPS if o.op_id == "rabbitmq.definitions")
    assert "administrator" in op.tags


def test_redacted_op_ids_match_credential_bearing_surfaces() -> None:
    """The redacted-op set is exactly the credential-bearing surfaces + passthrough."""
    assert (
        frozenset(
            {
                "rabbitmq.shovels",
                "rabbitmq.shovel_status",
                "rabbitmq.federation_links",
                "rabbitmq.parameters",
                "rabbitmq.definitions",
                "rabbitmq.request",
            }
        )
        == RABBITMQ_REDACTED_OP_IDS
    )


def test_no_write_or_mutating_op_is_registered() -> None:
    """This connector ships read-only — no write/mutating op."""
    for op in RABBITMQ_OPS:
        assert op.safety_level == "safe"
        assert "write" not in op.tags
        assert not any(tok in op.op_id for tok in (".create", ".delete", ".set", ".update"))
