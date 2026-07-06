# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G6 ``credential_mint`` classifier integration test (G3.5-T9 / #621, #147).

Proves the load-bearing PII guarantee for ``harbor.robot.create``: the
G6 broadcast feed must emit **aggregate-only** — the fact that a robot
account was minted, never the minted secret credential — while the
:class:`~meho_backplane.connectors.schemas.OperationResult` returned to
the caller still contains the secret.

Four-eyes gating (#147)
-----------------------

``harbor.robot.create`` mints a credential in its response, so it is now
registered ``requires_approval=True`` (#147): the non-agent policy gate
(:func:`~meho_backplane.operations._validate._non_agent_verdict`) keys the
verdict solely on ``requires_approval``, so a human ``tenant_admin``
dispatching it **parks** at ``awaiting_approval`` instead of executing —
closing the credential-mint arm of the four-eyes bypass #128. The
redaction contract therefore can no longer be proven by a single lone
dispatch (the op never executes). Every redaction assertion here now
drives the op through the real **approve → resume** flow:

1. dispatch as a lone operator → assert ``awaiting_approval`` +
   ``extras["approval_request_id"]``;
2. approve as a **second** operator via
   ``POST /api/v1/approvals/{id}/decide`` (the self-approval guard
   rejects the requester — real four-eyes);
3. the ``/decide`` route re-hydrates the stored target and re-dispatches
   with ``_approved=True`` (the committed approval is the authorization);
4. assert ``dispatch_status == "ok"`` **and** the redacted broadcast on
   the executed (post-approval) op.

Mirrors the approve→resume harness in
:mod:`tests.test_secret_move_approval` (HTTP ``/decide`` with a minted
operator JWT) and :mod:`tests.test_approval_queue` (real DB ``Target``
re-hydration).

Mechanism (unchanged from #621)
-------------------------------

* :func:`~meho_backplane.broadcast.events.classify_op` returns
  ``"credential_mint"`` for ``harbor.robot.create``.
* :func:`~meho_backplane.broadcast.events.redact_payload` collapses
  ``credential_mint`` to ``{op_class, result_status}`` — the same
  aggregate branch used for ``credential_read``.
* The broadcast publisher is swapped for a recording stub so the emitted
  :class:`~meho_backplane.broadcast.events.BroadcastEvent` can be
  inspected without a Valkey container.
* The Harbor HTTP client is intercepted by respx so the real handler runs
  against a mock endpoint; the test does not need a live Harbor instance.
* The connector's Vault-backed credentials loader is replaced with a stub
  so no Vault address is reached.

Why assert by exclusion on the *serialised* event: the redacted
``payload`` collapses to ``{op_class, result_status}``, but the enclosing
:class:`BroadcastEvent` always carries ``op_id`` and ``target_name``. The
leak surface is the whole serialised event, not just ``payload`` — a
regression that placed the secret in a new top-level field would pass a
``payload``-only assertion.

Runs unconditionally: the recording-stub publisher means no real Valkey
socket is opened, so — like :mod:`tests.test_broadcast_credential_write_dispatch`
— there is no ``BROADCAST_REDIS_URL`` skip guard. The prior guard (#621)
skipped the whole suite locally, so the redaction contract was only ever
exercised in CI; the approve→resume rework closes that gap by executing
the op deterministically in the sandbox (AC2 of #147).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast import BroadcastEvent
from meho_backplane.connectors.harbor import HarborConnector
from meho_backplane.connectors.harbor.ops import register_harbor_robot_operations
from meho_backplane.connectors.harbor.session import HarborTargetLike
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.main import app
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks

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

#: Tenant every principal in this module belongs to.
_TENANT_ID = UUID("00000000-0000-0000-0000-0000000000c9")

#: Stable id for the persisted Harbor target the resume path re-hydrates.
_TARGET_ID = UUID("00000000-0000-0000-0000-00000000ba01")
_TARGET_NAME = "harbor-test-prod"
_TARGET_HOST = "harbor.test.invalid"

#: Harbor v2 robot endpoints the handler calls, against ``_base_url`` =
#: ``https://<host>`` (port 443 omitted). ``robot_create`` POSTs to
#: ``/api/v2.0/robots``; ``robot_delete`` DELETEs ``/api/v2.0/robots/{id}``.
_ROBOTS_URL = f"https://{_TARGET_HOST}/api/v2.0/robots"
_ROBOT_7_URL = f"https://{_TARGET_HOST}/api/v2.0/robots/7"


# ---------------------------------------------------------------------------
# Stub credentials loader
# ---------------------------------------------------------------------------


async def _stub_credentials_loader(
    _target: HarborTargetLike, _operator: Operator
) -> dict[str, str]:
    """Return hard-coded admin credentials — no Vault call.

    The 2-arg signature matches the
    :class:`~meho_backplane.connectors.harbor.session.HarborCredentialsLoader`
    G3.10-T1 (#945) introduced.
    """
    return {"username": "admin", "password": "test-password"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher caches + patch the Harbor connector's credentials loader.

    The full connector registry is populated once per worker by conftest's
    session-scoped ``_force_import_connector_modules`` and snapshot/restored
    per-test by ``_isolate_global_registries`` — so this fixture does NOT
    clear + re-register a single connector. Clearing the registry down to
    Harbor would break the ``TestClient(app)`` lifespan: the app's
    connector-spec catalog validation fails when the other connector
    classes are absent from the registry.

    Patch the *cached* connector instance's credentials loader so no Vault
    address is reached, both for the direct requester dispatch and for the
    server-side re-dispatch the ``/decide`` route runs on the same cached
    instance. ``_isolate_global_registries`` restores the instance cache
    after the test, so the patch cannot bleed into another connector test.
    """
    reset_dispatcher_caches()
    instance = get_or_create_connector_instance(HarborConnector)
    instance._credentials_loader = _stub_credentials_loader  # type: ignore[attr-defined]
    yield
    reset_dispatcher_caches()


@pytest.fixture
async def _persisted_harbor_target() -> AsyncIterator[UUID]:
    """Persist a real Harbor ``Target`` the resume path re-hydrates by id.

    The ``/decide`` re-dispatch loads the target from the DB
    (:func:`~meho_backplane.targets.resolver.resolve_target_by_id`) — it
    does not carry the request-time object — so a durable row must exist
    or the resume fails closed. ``version`` resolves the connector;
    ``host`` / ``port`` / ``auth_model`` / ``name`` drive the HTTP client
    and audit ``target_name``.
    """
    async with get_sessionmaker()() as session:
        session.add(
            TargetORM(
                id=_TARGET_ID,
                tenant_id=_TENANT_ID,
                name=_TARGET_NAME,
                product="harbor",
                version="2.11.0",
                host=_TARGET_HOST,
                port=443,
                aliases=[],
                secret_ref=f"harbor/{_TARGET_NAME}",
                auth_model="shared_service_account",
            )
        )
        await session.commit()
    yield _TARGET_ID


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


class _HarborDispatchTarget:
    """Request-time target stub the *requester* dispatches against.

    The dispatcher reads ``product`` / ``fingerprint.version`` /
    ``preferred_impl_id`` (connector resolution), ``tenant_id`` / ``id``
    (pooled-HTTP-client cache key + audit row), and ``name`` (broadcast
    ``target_name``). ``tenant_id`` / ``id`` match the persisted row so
    the parked ``ApprovalRequest.target_id`` re-hydrates the real
    :class:`Target` on resume.
    """

    def __init__(self) -> None:
        self.product = "harbor"
        self.fingerprint = {"version": "2.11.0"}
        self.preferred_impl_id: str | None = None
        self.tenant_id: UUID = _TENANT_ID
        self.id: UUID = _TARGET_ID
        self.name = _TARGET_NAME
        self.host = _TARGET_HOST
        self.port: int | None = 443
        self.secret_ref = f"harbor/{_TARGET_NAME}"
        self.auth_model: str | None = "shared_service_account"


def _make_operator(sub: str = "op-credential-mint-requester") -> Operator:
    """A human (USER) operator — parks a ``requires_approval`` op, never denied."""
    return Operator(
        sub=sub,
        name="Credential Mint Test Operator",
        email=None,
        raw_jwt="header.payload.signature",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


# ---------------------------------------------------------------------------
# Helpers
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


def _mock_robot_create(mock: respx.MockRouter) -> None:
    """Route ``POST /api/v2.0/robots`` to a minted-secret response."""
    mock.post(_ROBOTS_URL).mock(
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


async def _park_robot_create(
    stub_embedding_service: AsyncMock,
    *,
    requester: Operator,
) -> UUID:
    """Register the ops, dispatch ``harbor.robot.create`` as *requester*, park.

    Returns the pending ``approval_request_id``. Asserts the dispatch
    parked (``awaiting_approval``) rather than executing — the #147 gate.
    """
    await register_harbor_robot_operations(embedding_service=stub_embedding_service)
    parked = await dispatch(
        operator=requester,
        connector_id="harbor-rest-2.x",
        op_id="harbor.robot.create",
        target=_HarborDispatchTarget(),
        params={"name": "bot", "project": "proj", "duration": -1},
    )
    assert parked.status == "awaiting_approval", parked.error
    assert parked.extras["approval_request_id"]
    return UUID(parked.extras["approval_request_id"])


def _approve_via_decide(
    request_id: UUID,
    mock: respx.MockRouter,
    *,
    approver_sub: str,
) -> dict[str, Any]:
    """Approve *request_id* over HTTP ``/decide`` as a distinct operator.

    Mocks OIDC discovery + JWKS on *mock* (the same respx router already
    intercepting the Harbor HTTP call), mints a token for *approver_sub*
    (≠ the requester — the self-approval guard rejects the requester), and
    posts the decision. The route re-hydrates the stored target and
    re-dispatches with ``_approved=True``; returns the decision-response
    body so callers assert ``dispatch_status``.
    """
    key = make_rsa_keypair("kid-decider")
    mock_discovery_and_jwks(mock, public_jwks(key))
    token = mint_token(
        key,
        sub=approver_sub,
        tenant_role=TenantRole.OPERATOR.value,
        tenant_id=str(_TENANT_ID),
    )
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/approvals/{request_id}/decide",
            headers={"Authorization": f"Bearer {token}"},
            json={"decision": "approved"},
        )
    assert response.status_code == 200, response.text
    return response.json()


async def _fetch_audit_rows(op_id: str) -> list[AuditLog]:
    """Return audit rows for *op_id*, oldest first.

    ``op_id`` / ``result_status`` live inside the JSON ``payload`` column
    (not as top-level ``AuditLog`` columns), so filter in Python after a
    tenant-scoped load — SQLite JSON-path predicates are not portable.
    """
    async with get_sessionmaker()() as session:
        result = await session.execute(
            select(AuditLog).where(AuditLog.tenant_id == _TENANT_ID).order_by(AuditLog.occurred_at)
        )
        rows = list(result.scalars().all())
    return [r for r in rows if isinstance(r.payload, dict) and r.payload.get("op_id") == op_id]


# ---------------------------------------------------------------------------
# (AC1) Human dispatch parks + audit row records the needs-approval verdict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_robot_create_parks_and_audits_needs_approval(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
    _persisted_harbor_target: UUID,
) -> None:
    """Dispatching ``harbor.robot.create`` as a human parks — not executes (#147).

    AC1: the op returns ``awaiting_approval`` (four-eyes gate) with an
    ``approval_request_id``, and the parked "request" audit row carries
    ``policy_decision = "needs-approval"`` (the column landed in #130 /
    migration 0051). No broadcast carries the (never-minted) secret.
    """
    requester = _make_operator()
    request_id = await _park_robot_create(stub_embedding_service, requester=requester)

    # The parked "request" audit row honestly records the gate verdict.
    rows = await _fetch_audit_rows("harbor.robot.create")
    request_rows = [r for r in rows if r.payload.get("result_status") == "request"]
    assert len(request_rows) == 1
    assert request_rows[0].policy_decision == "needs-approval"

    # The op never executed, so no secret can have reached any broadcast.
    for event in captured_events:
        assert _SENTINEL_SECRET not in event.model_dump_json()

    assert request_id  # a pending row exists to decide


# ---------------------------------------------------------------------------
# (AC2) credential_mint aggregate-only contract through approve → resume
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_robot_create_broadcast_is_aggregate_only(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
    _persisted_harbor_target: UUID,
) -> None:
    """``harbor.robot.create`` broadcasts only ``{op_class, result_status}``.

    DoD (AC3 of #621, AC2 of #147) — the redaction runs against the
    **executed** op. The requester parks the mint; a second operator
    approves via ``/decide``; the re-dispatch mints the credential and
    produces an aggregate-only broadcast: the minted secret never appears
    in the serialised BroadcastEvent, while the dispatch response still
    carries it (proves the exclusion is real, not vacuous).
    """
    requester = _make_operator(sub="op-requester")

    with respx.mock() as mock:
        _mock_robot_create(mock)
        request_id = await _park_robot_create(stub_embedding_service, requester=requester)
        # Parking must not have emitted the (never-minted) secret.
        assert not any(_SENTINEL_SECRET in e.model_dump_json() for e in captured_events)

        body = _approve_via_decide(request_id, mock, approver_sub="op-approver")

    assert body["dispatch_status"] == "ok", body
    # The re-dispatch response carries the minted secret to the caller —
    # proves the broadcast exclusion below is a real one.
    assert body["dispatch_result"]["secret"] == _SENTINEL_SECRET

    mint_events = [e for e in captured_events if e.op_id == "harbor.robot.create"]
    # Exactly one *executed* credential_mint broadcast (the resume run).
    executed = [e for e in mint_events if e.result_status == "ok"]
    assert len(executed) == 1
    _assert_credential_mint_aggregate(executed[0], "harbor.robot.create")


# ---------------------------------------------------------------------------
# (AC2) harbor.robot.delete stays full-detail (write) — no gate, executes direct
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_robot_delete_broadcast_is_full_detail(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
    _persisted_harbor_target: UUID,
) -> None:
    """``harbor.robot.delete`` broadcasts full detail (``write`` class).

    Contrast test: ``harbor.robot.delete`` classifies ``write``, not
    ``credential_mint``, and is left ungated (#147 — a ``caution`` write
    that revokes, not mints), so a human dispatches it directly (no
    approval park). Its broadcast carries the full ``params`` block. The
    divergent behaviour is solely due to :func:`classify_op`.
    """
    await register_harbor_robot_operations(embedding_service=stub_embedding_service)

    operator = _make_operator()

    with respx.mock() as mock:
        mock.delete(_ROBOT_7_URL).mock(return_value=respx.MockResponse(200))
        result = await dispatch(
            operator=operator,
            connector_id="harbor-rest-2.x",
            op_id="harbor.robot.delete",
            target=_HarborDispatchTarget(),
            params={"project": "proj", "id": 7},
        )

    assert result.status == "ok", result.error

    delete_events = [e for e in captured_events if e.op_id == "harbor.robot.delete"]
    assert len(delete_events) == 1
    event = delete_events[0]
    assert event.op_class == "write"
    # Full-detail broadcast — params block is present.
    assert "params" in event.payload
    assert event.op_class != "credential_mint"


@pytest.mark.asyncio
async def test_credential_mint_and_write_diverge_on_same_dispatch_path(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
    _persisted_harbor_target: UUID,
) -> None:
    """Create is aggregate-only (post-approval); delete is full-detail (direct).

    Both ops flow through the identical dispatch + broadcast infrastructure
    against the same target. The only variables are the op-id and the
    #147 gate: create parks then executes via approve→resume; delete
    executes directly. Divergent redaction proves :func:`classify_op`
    drives the behaviour, not the handler or target.
    """
    requester = _make_operator(sub="op-requester")
    # Register both ops up front (idempotent — _park_robot_create re-calls it)
    # so the direct delete dispatch resolves its descriptor.
    await register_harbor_robot_operations(embedding_service=stub_embedding_service)

    with respx.mock() as mock:
        _mock_robot_create(mock)
        mock.delete(_ROBOT_7_URL).mock(return_value=respx.MockResponse(200))

        # Ungated write executes directly.
        delete_result = await dispatch(
            operator=requester,
            connector_id="harbor-rest-2.x",
            op_id="harbor.robot.delete",
            target=_HarborDispatchTarget(),
            params={"project": "proj", "id": 7},
        )
        assert delete_result.status == "ok", delete_result.error

        # Gated credential-mint parks, then executes on second-operator approval.
        request_id = await _park_robot_create(stub_embedding_service, requester=requester)
        body = _approve_via_decide(request_id, mock, approver_sub="op-approver")

    assert body["dispatch_status"] == "ok", body

    create_event = next(
        e for e in captured_events if e.op_id == "harbor.robot.create" and e.result_status == "ok"
    )
    delete_event = next(e for e in captured_events if e.op_id == "harbor.robot.delete")

    # Same path, opposite redaction — classifier-driven by construction.
    assert create_event.op_class == "credential_mint"
    assert delete_event.op_class == "write"
    _assert_credential_mint_aggregate(create_event, "harbor.robot.create")
    assert "params" in delete_event.payload


# ---------------------------------------------------------------------------
# (AC3) No credential-mint op may silently ship requires_approval=False
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_harbor_credential_mint_op_bypasses_approval(
    stub_embedding_service: AsyncMock,
) -> None:
    """Every harbor-registered ``credential_mint`` op is four-eyes-gated (#147).

    The non-agent policy gate (``_non_agent_verdict``) keys only on
    ``requires_approval``, so a ``credential_mint`` op (privilege issuance)
    left ``requires_approval=False`` lets a human ``tenant_admin`` mint a
    credential with no four-eyes step — the bypass #147 closes. This is the
    harbor analogue of bind9's ``test_no_dangerous_op_bypasses_approval``
    (#129): it runs the real registrar and asserts the invariant against
    the persisted descriptors, so a future harbor mint op can't
    reintroduce the gap.
    """
    from meho_backplane.broadcast.events import classify_op
    from meho_backplane.db.models import EndpointDescriptor

    await register_harbor_robot_operations(embedding_service=stub_embedding_service)

    async with get_sessionmaker()() as session:
        result = await session.execute(
            select(EndpointDescriptor).where(EndpointDescriptor.product == "harbor")
        )
        descriptors = list(result.scalars().all())

    assert descriptors, "expected harbor descriptors to be registered"

    # harbor.robot.create classifies credential_mint and MUST be gated.
    mint_ops = [d for d in descriptors if classify_op(d.op_id) == "credential_mint"]
    assert any(d.op_id == "harbor.robot.create" for d in mint_ops), (
        "harbor.robot.create must classify credential_mint — the guard "
        "asserts on that class; a mis-classification would silently void it"
    )
    offenders = [d.op_id for d in mint_ops if not d.requires_approval]
    assert offenders == [], (
        f"credential_mint harbor ops shipped without four-eyes gating: {offenders}"
    )
