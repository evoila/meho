# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approval-gating refinement for ``secret.move`` — G0.22-T3 (#1579).

#1577 established the *mechanism* (the synthetic ``secret-broker-1.x``
op, ``requires_approval=True`` + ``safety_level="dangerous"``, the
``SecretEndpoint`` adapter protocol) and proved the basic change-class
gate: an unapproved move parks at ``awaiting_approval`` and the handler
never runs. This module proves the *refinement* this task adds:

* **Ref-only ``proposed_effect``** — the parked
  :class:`~meho_backplane.db.models.ApprovalRequest` carries a
  human-readable summary naming the source / sink as parsed
  ``{kind, ref}`` references, with **no secret substring** anywhere on
  the row (``proposed_effect`` *or* ``params``). The preview builder
  (:func:`~meho_backplane.connectors.secret.move_preview.build_secret_move_preview`)
  is what populates it.
* **No execution on the park path** — the source ``read_secret`` and the
  sink ``write_secret`` are never called for an unapproved move (spied
  call counts stay 0).
* **No secret in the parking broadcast** — the ``approval.pending`` event
  the park publishes carries no value substring.
* **Time-boxed scope** — an AGENT principal holding an
  ``op_pattern="secret.*"`` / ``verdict="auto-execute"`` grant whose
  ``expires_at`` is in the past is refused (the move never executes); a
  *live* grant only lifts the verdict off the ``dangerous`` deny baseline
  to ``needs-approval`` (the ceiling for a ``dangerous`` op caps an agent
  at park — it can never auto-execute a credential move). A parked row
  swept to ``EXPIRED`` by ``expire_stale_requests`` is no longer decidable
  (``/decide`` → HTTP 409) and the stores stay un-touched.
* **Exactly-once on approval** — a four-eyes ``/decide`` re-dispatches
  the parked move, ``write_secret`` fires exactly once, and a second
  ``/decide`` on the same id returns HTTP 409 without a second write.

The grant / expiry / re-dispatch machinery is reused verbatim from the
existing approval-queue substrate — this task adds no parallel mechanism.

Test isolation mirrors ``test_connectors_secret_broker.py``: the
operator-scoped Vault client is built through the single ``_build_client``
seam that ``install_fake_client`` monkeypatches to a controllable
``_FakeKVv2`` (no real HTTP), and the autouse ``_default_database_url``
conftest fixture migrates the SQLite DB to head. The two HTTP-level
``/decide`` tests drive the real FastAPI app via ``TestClient`` with a
minted operator JWT (mirroring ``test_api_v1_approvals.py``).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

import meho_backplane.broadcast.publisher as _publisher_module
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast.events import BroadcastEvent
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.connectors.secret.endpoints import (
    SecretEndpoint,
    SecretMaterial,
)
from meho_backplane.connectors.secret.move_preview import build_secret_move_preview
from meho_backplane.connectors.secret.ops import register_secret_broker_operations
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentPermission,
    ApprovalRequest,
    ApprovalRequestStatus,
    PermissionVerdict,
)
from meho_backplane.main import app
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._preview import PreviewContext
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import (
    create_pending_request,
    expire_stale_requests,
)
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks
from ._vault_fakes import install_fake_client

_SECRET_VALUE = "hunter2-correct-horse"
_SECRET_SHA256 = hashlib.sha256(_SECRET_VALUE.encode()).hexdigest()

_TENANT_ID = uuid.UUID(int=0)
_FROM_REF = "vault:secret/db/prod#password"
_TO_REF = "vault:secret/db/replica#password"

_MOVE_PARAMS: dict[str, Any] = {
    "from": _FROM_REF,
    "to": _TO_REF,
    "reason": "promote replica credential",
}


# ---------------------------------------------------------------------------
# Settings env + dispatcher isolation (mirrors test_connectors_secret_broker)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin env vars Settings / the operator-scoped Vault client need."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_dispatcher_caches()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_dispatcher_caches()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub so registration doesn't pull ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def _registered_secret_broker_op(
    stub_embedding_service: AsyncMock,
) -> AsyncIterator[None]:
    """Upsert the ``secret.move`` descriptor row for dispatch-driving tests."""
    await register_secret_broker_operations(embedding_service=stub_embedding_service)
    yield


@pytest.fixture
def captured_broadcasts(monkeypatch: pytest.MonkeyPatch) -> list[BroadcastEvent]:
    """Record every :func:`publish_event` call.

    ``publish_approval_event`` imports ``publish_event`` from
    :mod:`meho_backplane.broadcast.publisher` at call time, so patching the
    source symbol on that module captures the parking ``approval.pending``
    broadcast.
    """
    events: list[BroadcastEvent] = []

    async def _capture(event: BroadcastEvent) -> None:
        events.append(event)

    monkeypatch.setattr(_publisher_module, "publish_event", _capture)
    return events


def _make_operator(
    sub: str = "test-operator",
    *,
    principal_kind: PrincipalKind = PrincipalKind.USER,
    jwt: str = "fake.jwt.value",
) -> Operator:
    """A request-scoped operator carrying the JWT the vault adapter forwards."""
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt=jwt,
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=principal_kind,
    )


async def _dispatch_move(
    operator: Operator,
    params: dict[str, Any] | None = None,
) -> OperationResult:
    """Dispatch ``secret.move`` through the real gate (no resume flag).

    No ``_approved`` flag: ``requires_approval=True`` routes the call to
    the approval queue (park) unless a standing ``auto-execute`` grant
    pre-authorizes it.
    """
    return await dispatch(
        operator=operator,
        connector_id="secret-broker-1.x",
        op_id="secret.move",
        target=None,
        params=_MOVE_PARAMS if params is None else params,
    )


async def _fetch_pending_rows() -> list[ApprovalRequest]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(ApprovalRequest).order_by(ApprovalRequest.created_at))
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Ref-only proposed_effect — the preview builder, in isolation
# ---------------------------------------------------------------------------


def _make_preview_ctx(params: dict[str, Any]) -> PreviewContext:
    """A minimal :class:`PreviewContext` — the builder reads only params."""
    return PreviewContext(
        descriptor=object(),  # type: ignore[arg-type]  # builder ignores it
        connector_instance=None,
        operator=_make_operator(),
        target=None,
        params=params,
    )


async def test_preview_builder_emits_ref_only_summary() -> None:
    """The builder names source + sink as ``{kind, ref}`` and echoes reason.

    No store I/O, so the value cannot enter the summary — and the params
    it reads carry references only.
    """
    preview = await build_secret_move_preview(_make_preview_ctx(_MOVE_PARAMS))
    assert preview == {
        "action": "secret.move",
        "source": {"kind": "vault", "ref": "secret/db/prod#password"},
        "sink": {"kind": "vault", "ref": "secret/db/replica#password"},
        "reason": "promote replica credential",
    }


async def test_preview_builder_declines_on_malformed_ref() -> None:
    """A malformed ref declines (→ identifier-only default), never raises."""
    bad_kind = await build_secret_move_preview(
        _make_preview_ctx({"from": "no-colon", "to": _TO_REF})
    )
    missing_to = await build_secret_move_preview(_make_preview_ctx({"from": _FROM_REF}))
    assert bad_kind is None
    assert missing_to is None


# ---------------------------------------------------------------------------
# (Criterion 2) No-grant move parks PENDING with a ref-only, value-free row
# ---------------------------------------------------------------------------


async def test_move_with_no_grant_parks_pending(
    monkeypatch: pytest.MonkeyPatch,
    _registered_secret_broker_op: None,
) -> None:
    """A USER move with no standing grant parks exactly one PENDING row.

    The row's ``proposed_effect`` names both refs (so the reviewer reads
    the move structurally) and carries no secret substring — neither in
    ``proposed_effect`` nor in the stored ``params``.
    """
    install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})

    result = await _dispatch_move(_make_operator())

    assert result.status == "awaiting_approval", result.error

    rows = await _fetch_pending_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.status == ApprovalRequestStatus.PENDING.value

    effect = row.proposed_effect
    # build_proposed_effect wraps the builder dict as {op_class, preview}.
    assert effect["op_class"] == "other"
    preview = effect["preview"]
    assert preview["source"] == {"kind": "vault", "ref": "secret/db/prod#password"}
    assert preview["sink"] == {"kind": "vault", "ref": "secret/db/replica#password"}
    # Both refs are present in the serialised proposed_effect.
    effect_json = json.dumps(effect)
    assert "secret/db/prod#password" in effect_json
    assert "secret/db/replica#password" in effect_json

    # The crux: no secret substring anywhere on the durable row.
    assert _SECRET_VALUE not in effect_json
    assert _SECRET_VALUE not in json.dumps(row.params)


# ---------------------------------------------------------------------------
# (Criterion 3) The park path never reads the source / writes the sink
# ---------------------------------------------------------------------------


async def test_move_refuses_execution_without_approval(
    monkeypatch: pytest.MonkeyPatch,
    _registered_secret_broker_op: None,
) -> None:
    """Neither ``read_secret`` nor ``write_secret`` runs on the park path."""
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})

    result = await _dispatch_move(_make_operator())

    assert result.status == "awaiting_approval", result.error
    # The connector-resolution / read / write never happen for a park.
    assert fake.secrets.kv.v2.read_calls == []
    assert fake.secrets.kv.v2.put_calls == []


# ---------------------------------------------------------------------------
# (Criterion 4) The parking broadcast carries no secret substring
# ---------------------------------------------------------------------------


async def test_no_secret_in_broadcast(
    monkeypatch: pytest.MonkeyPatch,
    captured_broadcasts: list[BroadcastEvent],
    _registered_secret_broker_op: None,
) -> None:
    """The ``approval.pending`` broadcast the park publishes is value-free.

    ``secret.move`` classifies ``"other"`` (full-detail broadcast on the
    publish-on-write path), so the value-free-params invariant is
    load-bearing. The park path itself publishes only the
    ``approval.pending`` lifecycle event (no params); this asserts no
    secret substring appears in any captured event.
    """
    install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})

    result = await _dispatch_move(_make_operator())
    assert result.status == "awaiting_approval", result.error

    assert captured_broadcasts, "expected an approval.pending broadcast"
    for event in captured_broadcasts:
        assert _SECRET_VALUE not in json.dumps(event.payload)
        assert _SECRET_VALUE not in event.model_dump_json()


# ---------------------------------------------------------------------------
# (Criterion 5) Time-boxed scope — an expired grant does not authorize
# ---------------------------------------------------------------------------


async def _insert_grant(
    *,
    principal_sub: str,
    expires_at: datetime | None,
    verdict: str = PermissionVerdict.AUTO_EXECUTE.value,
    op_pattern: str = "secret.*",
) -> None:
    """Insert an ``AgentPermission`` grant row for *principal_sub*."""
    async with get_sessionmaker()() as session:
        session.add(
            AgentPermission(
                tenant_id=_TENANT_ID,
                principal_sub=principal_sub,
                op_pattern=op_pattern,
                target_scope="*",
                verdict=verdict,
                created_by_sub="operator:granter",
                expires_at=expires_at,
            )
        )
        await session.commit()


async def test_expired_grant_not_honored(
    monkeypatch: pytest.MonkeyPatch,
    _registered_secret_broker_op: None,
) -> None:
    """An AGENT with an *expired* ``auto-execute`` grant does not move.

    The resolver excludes a grant whose ``expires_at`` is in the past, so
    the agent reverts to the ``dangerous`` baseline, which for an agent is
    ``deny`` (``_SAFETY_DEFAULT["dangerous"]`` in
    :mod:`meho_backplane.auth.permissions`). The point of the criterion —
    an expired grant grants nothing, and ``read_secret`` / ``write_secret``
    never run — holds: the move is refused outright. (An agent move parks
    only while a live grant raises the verdict off the deny baseline; see
    ``test_active_grant_parks_not_auto_executes``.)
    """
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    agent_sub = "agent:expired-grant"
    await _insert_grant(
        principal_sub=agent_sub,
        expires_at=datetime.now(UTC) - timedelta(hours=1),
    )

    result = await _dispatch_move(_make_operator(agent_sub, principal_kind=PrincipalKind.AGENT))

    # Refused — not parked, not executed. The dangerous baseline denies an
    # agent with no live grant; the expired grant authorizes nothing.
    assert result.status == "denied", result.error
    assert fake.secrets.kv.v2.read_calls == []
    assert fake.secrets.kv.v2.put_calls == []


async def test_active_grant_parks_not_auto_executes(
    monkeypatch: pytest.MonkeyPatch,
    _registered_secret_broker_op: None,
) -> None:
    """A *live* ``auto-execute`` grant parks the move — it never auto-executes.

    The counterpart to the expired case, and the load-bearing safety
    property: ``secret.move`` is ``safety_level="dangerous"``, whose agent
    ceiling is ``needs-approval`` (``_SAFETY_CEILING["dangerous"]``), so a
    grant of ``verdict="auto-execute"`` is capped to ``needs-approval`` —
    an agent can NEVER auto-execute a credential move, even with a
    standing grant. The live grant's only effect is to raise the verdict
    off the deny baseline (deny → needs-approval), so the move parks
    instead of being denied. The stores stay un-touched on the park.
    """
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    agent_sub = "agent:active-grant"
    await _insert_grant(
        principal_sub=agent_sub,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    result = await _dispatch_move(_make_operator(agent_sub, principal_kind=PrincipalKind.AGENT))

    # Parked (the live grant lifted deny → needs-approval), never executed.
    assert result.status == "awaiting_approval", result.error
    assert fake.secrets.kv.v2.read_calls == []
    assert fake.secrets.kv.v2.put_calls == []


async def test_expired_request_not_decidable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parked row swept to EXPIRED is not decidable: ``/decide`` → 409.

    Parks a ``secret.move`` row with a past ``expires_at``, runs
    ``expire_stale_requests`` (flips it to ``EXPIRED``), then drives the
    real ``/decide`` route over HTTP. The already-decided guard maps the
    terminal status to 409 ``approval_request_already_expired`` and the
    stores stay un-touched (no re-dispatch fires for a non-PENDING row).
    """
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    operator = _make_operator()
    params = _MOVE_PARAMS

    async with get_sessionmaker()() as session:
        request = await create_pending_request(
            session,
            operator=operator,
            connector_id="secret-broker-1.x",
            op_id="secret.move",
            target=None,
            params=params,
            params_hash=compute_params_hash(params),
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        await session.commit()
        request_id = request.id

    async with get_sessionmaker()() as session:
        expired = await expire_stale_requests(session, operator=operator)
        await session.commit()
    assert [r.id for r in expired] == [request_id]

    key = make_rsa_keypair("kid-expired")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        token = mint_token(
            key,
            sub="operator:decider",
            tenant_role=TenantRole.OPERATOR.value,
            tenant_id=str(_TENANT_ID),
        )
        with TestClient(app) as client:
            response = client.post(
                f"/api/v1/approvals/{request_id}/decide",
                headers={"Authorization": f"Bearer {token}"},
                json={"decision": "approved"},
            )

    assert response.status_code == 409, response.text
    assert response.json() == {"detail": "approval_request_already_expired"}
    # No re-dispatch fired for the expired row.
    assert fake.secrets.kv.v2.read_calls == []
    assert fake.secrets.kv.v2.put_calls == []


# ---------------------------------------------------------------------------
# (Criterion 6) Approving re-dispatches the move exactly once
# ---------------------------------------------------------------------------


async def test_decide_redispatch_executes_move_once(
    monkeypatch: pytest.MonkeyPatch,
    _registered_secret_broker_op: None,
) -> None:
    """A four-eyes ``/decide`` executes the move once; a second decide → 409.

    The requester parks the move; a *different* operator approves via
    ``/decide`` (the self-approval guard rejects the original requester).
    The approval re-dispatches the stored params with ``_approved=True``,
    so ``write_secret`` fires exactly once. A second ``/decide`` on the
    same id hits the already-decided guard (409) and writes nothing more.
    """
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})

    # Requester parks the move (USER principal, no grant).
    requester = _make_operator(sub="operator:requester")
    parked = await _dispatch_move(requester)
    assert parked.status == "awaiting_approval", parked.error
    request_id = uuid.UUID(parked.extras["approval_request_id"])
    assert fake.secrets.kv.v2.put_calls == []

    key = make_rsa_keypair("kid-decider")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        token = mint_token(
            key,
            sub="operator:decider",  # different from the requester
            tenant_role=TenantRole.OPERATOR.value,
            tenant_id=str(_TENANT_ID),
        )
        with TestClient(app) as client:
            first = client.post(
                f"/api/v1/approvals/{request_id}/decide",
                headers={"Authorization": f"Bearer {token}"},
                json={"decision": "approved"},
            )
            second = client.post(
                f"/api/v1/approvals/{request_id}/decide",
                headers={"Authorization": f"Bearer {token}"},
                json={"decision": "approved"},
            )

    assert first.status_code == 200, first.text
    assert first.json()["dispatch_status"] == "ok"
    # The move executed exactly once on approval.
    assert len(fake.secrets.kv.v2.put_calls) == 1
    assert fake.secrets.kv.v2.put_calls[0]["secret"] == {"password": _SECRET_VALUE}

    # The second decide is refused; no second write.
    assert second.status_code == 409, second.text
    assert second.json() == {"detail": "approval_request_already_approved"}
    assert len(fake.secrets.kv.v2.put_calls) == 1


# ---------------------------------------------------------------------------
# SecretEndpoint spies stay un-called — protocol-shaped, not Vault-coupled
# ---------------------------------------------------------------------------


class _SpyEndpoint:
    """A :class:`SecretEndpoint` recording read/write calls (protocol check)."""

    def __init__(self) -> None:
        self.read_calls = 0
        self.write_calls = 0

    async def read_secret(self, operator: Operator) -> SecretMaterial:
        self.read_calls += 1
        return SecretMaterial(_SECRET_VALUE)

    async def write_secret(self, operator: Operator, material: SecretMaterial) -> None:
        self.write_calls += 1


def test_spy_endpoint_satisfies_secret_endpoint_protocol() -> None:
    """The spy is a structural ``SecretEndpoint`` — the protocol the broker uses."""
    assert isinstance(_SpyEndpoint(), SecretEndpoint)
