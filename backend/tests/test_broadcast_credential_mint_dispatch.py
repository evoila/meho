# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G6 ``credential_mint`` classifier integration test (G3.5-T9 / #621).

Proves the load-bearing PII guarantee for ``harbor.robot.create``:
the G6 broadcast feed must emit **aggregate-only** — the fact that a
robot account was minted, never the minted secret credential — while
the :class:`~meho_backplane.connectors.schemas.OperationResult`
returned to the caller still contains the secret.

This mirrors :mod:`tests.test_broadcast_credential_read_dispatch` for
the ``credential_read`` class (decision #3 precedent). The mechanism
is identical:

* :func:`~meho_backplane.broadcast.events.classify_op` returns
  ``"credential_mint"`` for ``harbor.robot.create``.
* :func:`~meho_backplane.broadcast.events.redact_payload` collapses
  ``credential_mint`` to ``{op_class, result_status}`` — the same
  aggregate branch used for ``credential_read``.
* The broadcast publisher is swapped for a recording stub so the
  emitted :class:`~meho_backplane.broadcast.events.BroadcastEvent`
  can be inspected without a Valkey container.
* The Harbor HTTP client is intercepted by respx so the real handler
  runs against a mock endpoint; the test does not need a live Harbor
  instance.
* The connector's Vault-backed credentials loader is replaced with
  a stub so no Vault address is reached.

Why assert by exclusion on the *serialised* event: the redacted
``payload`` collapses to ``{op_class, result_status}``, but the
enclosing :class:`BroadcastEvent` always carries ``op_id`` and
``target_name``. The leak surface is the whole serialised event, not
just ``payload`` — a regression that placed the secret in a new
top-level field would pass a ``payload``-only assertion.

Skip-clean: the test skips (does not fail) when
``BROADCAST_REDIS_URL`` is unset, mirroring the ``credential_read``
test's operator-intent guard. The publisher swap means no real Valkey
socket is opened regardless.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import respx

import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import BroadcastEvent
from meho_backplane.connectors.harbor import HarborConnector
from meho_backplane.connectors.harbor.ops import register_harbor_robot_operations
from meho_backplane.connectors.harbor.session import HarborTargetLike
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance
from meho_backplane.settings import get_settings

# ---------------------------------------------------------------------------
# Skip-clean guard
# ---------------------------------------------------------------------------

_BROADCAST_DISABLED = not (os.environ.get("BROADCAST_REDIS_URL") or "").strip()

pytestmark = pytest.mark.skipif(
    _BROADCAST_DISABLED,
    reason="broadcast feed disabled (BROADCAST_REDIS_URL unset) — G6 "
    "credential_mint classifier integration test skipped cleanly per #621 DoD",
)

# ---------------------------------------------------------------------------
# Sentinel secret material
# ---------------------------------------------------------------------------

#: Distinctive robot-credential token — unusual enough that any appearance
#: in a serialised BroadcastEvent is an unambiguous secret-material leak.
_SENTINEL_SECRET = "sntlrobotcred0zzz"

#: Every substring that MUST NOT appear anywhere in a ``credential_mint``
#: broadcast event. The secret is the only forbidden sentinel here —
#: the broadcast is *permitted* to carry ``op_id``, ``target_name``,
#: and ``result_status`` (the allowed aggregate per decision #3).
_FORBIDDEN_SUBSTRINGS: tuple[str, ...] = (_SENTINEL_SECRET,)

# ---------------------------------------------------------------------------
# Stub credentials loader
# ---------------------------------------------------------------------------


async def _stub_credentials_loader(_target: HarborTargetLike) -> dict[str, str]:
    """Return hard-coded admin credentials — no Vault call."""
    return {"username": "admin", "password": "test-password"}


# ---------------------------------------------------------------------------
# Stub dispatch target
# ---------------------------------------------------------------------------


class _FakeFingerprint:
    """Duck-typed fingerprint — the resolver reads only ``version``."""

    def __init__(self, version: str) -> None:
        self.version = version


class _HarborDispatchTarget:
    """Target stub satisfying the dispatcher's resolution + audit reads.

    The dispatcher reads ``product`` / ``fingerprint.version`` /
    ``preferred_impl_id`` (connector resolution) and ``id`` / ``name``
    (audit row + broadcast ``target_name``). The Harbor connector reads
    ``auth_model`` + ``name`` + ``host`` + ``port`` during auth and
    HTTP-client setup.

    ``name`` is a benign label; the broadcast event may carry it.
    It is deliberately distinct from every forbidden sentinel so the
    by-exclusion assertion cannot false-positive on it.
    """

    def __init__(self) -> None:
        self.product = "harbor"
        self.fingerprint = _FakeFingerprint(version="2.11.0")
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.name = "harbor-test-prod"
        self.host = "harbor.test.invalid"
        self.port: int | None = 443
        self.secret_ref = "kv/data/harbor/harbor-test-prod"
        self.auth_model: str | None = "shared_service_account"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher caches + registry; pre-seed the connector instance."""
    reset_dispatcher_caches()
    clear_registry()
    register_connector_v2(
        product=HarborConnector.product,
        version=HarborConnector.version,
        impl_id=HarborConnector.impl_id,
        cls=HarborConnector,
    )
    # Materialise the connector instance the dispatcher will use and patch
    # its credentials loader. The patched instance survives in
    # _CONNECTOR_INSTANCE_CACHE until reset_dispatcher_caches() teardown.
    instance = get_or_create_connector_instance(HarborConnector)
    instance._credentials_loader = _stub_credentials_loader  # type: ignore[attr-defined]
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub so ``register_typed_operation`` skips ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> list[BroadcastEvent]:
    """Replace :func:`publish_event` with an in-memory recording stub."""
    events: list[BroadcastEvent] = []

    async def _capture(event: BroadcastEvent) -> None:
        events.append(event)

    monkeypatch.setattr(audit_module, "publish_event", _capture)
    return events


def _make_operator() -> Operator:
    return Operator(
        sub="op-credential-mint-test",
        name="Credential Mint Test Operator",
        email=None,
        raw_jwt="header.payload.signature",
        tenant_id=UUID("00000000-0000-0000-0000-0000000000c9"),
        tenant_role=TenantRole.OPERATOR,
    )


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _assert_credential_mint_aggregate(event: BroadcastEvent, op_id: str) -> None:
    """Assert *event* is the aggregate shape for a ``credential_mint`` op.

    The redacted ``payload`` is exactly ``{op_class, result_status}``.
    The full serialised event must contain none of the forbidden
    secret-shaped sentinels.
    """
    assert event.op_id == op_id
    assert event.op_class == "credential_mint"
    assert event.result_status == "ok"
    assert event.payload == {
        "op_class": "credential_mint",
        "result_status": "ok",
    }

    serialised = event.model_dump_json()
    assert json.loads(serialised)["op_class"] == "credential_mint"

    for forbidden in _FORBIDDEN_SUBSTRINGS:
        assert forbidden not in serialised, (
            f"credential_mint leak: {forbidden!r} reached the broadcast "
            f"event for {op_id} — serialised event: {serialised}"
        )


# ---------------------------------------------------------------------------
# credential_mint aggregate-only contract — harbor.robot.create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_robot_create_broadcast_is_aggregate_only(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """``harbor.robot.create`` broadcasts only ``{op_id, target, result_status}``.

    DoD (AC3 of #621) — the dispatch produces a broadcast event that is
    aggregate-only: the minted secret never appears in the serialised
    BroadcastEvent. The OperationResult returned to the caller still
    contains the secret (proves the exclusion is a real one, not vacuous).
    """
    await register_harbor_robot_operations(embedding_service=stub_embedding_service)

    operator = _make_operator()
    target = _HarborDispatchTarget()

    with respx.mock() as mock:
        mock.post("https://harbor.test.invalid/api/v2.0/projects/proj/robots").mock(
            return_value=respx.MockResponse(
                201,
                json={
                    "id": 7,
                    "name": "robot$proj+bot",
                    "secret": _SENTINEL_SECRET,
                    "expiration_time": -1,
                },
            )
        )
        result = await dispatch(
            operator=operator,
            connector_id="harbor-rest-2.x",
            op_id="harbor.robot.create",
            target=target,
            params={"name": "bot", "project": "proj", "duration": -1},
        )

    assert result.status == "ok", result.error
    # Caller gets the secret — proves the assertion below is a real exclusion.
    assert result.result is not None
    assert result.result["secret"] == _SENTINEL_SECRET  # type: ignore[index]

    assert len(captured_events) == 1
    _assert_credential_mint_aggregate(captured_events[0], "harbor.robot.create")


@pytest.mark.asyncio
async def test_robot_delete_broadcast_is_full_detail(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """``harbor.robot.delete`` broadcasts full detail (``write`` class).

    Contrast test: ``harbor.robot.delete`` classifies ``write``, not
    ``credential_mint``. Its broadcast carries the full ``params`` block.
    Same dispatch + broadcast path as the create test; the divergent
    behaviour is solely due to :func:`classify_op`.
    """
    await register_harbor_robot_operations(embedding_service=stub_embedding_service)

    operator = _make_operator()
    target = _HarborDispatchTarget()

    with respx.mock() as mock:
        mock.delete("https://harbor.test.invalid/api/v2.0/projects/proj/robots/7").mock(
            return_value=respx.MockResponse(200)
        )
        result = await dispatch(
            operator=operator,
            connector_id="harbor-rest-2.x",
            op_id="harbor.robot.delete",
            target=target,
            params={"project": "proj", "id": 7},
        )

    assert result.status == "ok", result.error

    assert len(captured_events) == 1
    event = captured_events[0]
    assert event.op_id == "harbor.robot.delete"
    assert event.op_class == "write"
    # Full-detail broadcast — params block is present.
    assert "params" in event.payload
    assert event.op_class != "credential_mint"


@pytest.mark.asyncio
async def test_credential_mint_and_write_diverge_on_same_dispatch_path(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """Create is aggregate-only; delete is full-detail on the same path.

    Dispatches both ops back-to-back through the identical infrastructure.
    The only variable is the op-id; divergent redaction proves
    :func:`classify_op` drives the behavior, not the handler or target.
    """
    await register_harbor_robot_operations(embedding_service=stub_embedding_service)

    operator = _make_operator()
    target = _HarborDispatchTarget()

    with respx.mock() as mock:
        mock.post("https://harbor.test.invalid/api/v2.0/projects/proj/robots").mock(
            return_value=respx.MockResponse(
                201,
                json={
                    "id": 7,
                    "name": "robot$proj+bot",
                    "secret": _SENTINEL_SECRET,
                    "expiration_time": -1,
                },
            )
        )
        mock.delete("https://harbor.test.invalid/api/v2.0/projects/proj/robots/7").mock(
            return_value=respx.MockResponse(200)
        )

        create_result = await dispatch(
            operator=operator,
            connector_id="harbor-rest-2.x",
            op_id="harbor.robot.create",
            target=target,
            params={"name": "bot", "project": "proj", "duration": -1},
        )
        delete_result = await dispatch(
            operator=operator,
            connector_id="harbor-rest-2.x",
            op_id="harbor.robot.delete",
            target=target,
            params={"project": "proj", "id": 7},
        )

    assert create_result.status == "ok", create_result.error
    assert delete_result.status == "ok", delete_result.error
    assert len(captured_events) == 2

    create_event = next(e for e in captured_events if e.op_id == "harbor.robot.create")
    delete_event = next(e for e in captured_events if e.op_id == "harbor.robot.delete")

    # Same path, opposite redaction — classifier-driven by construction.
    assert create_event.op_class == "credential_mint"
    assert delete_event.op_class == "write"
    _assert_credential_mint_aggregate(create_event, "harbor.robot.create")
    assert "params" in delete_event.payload
