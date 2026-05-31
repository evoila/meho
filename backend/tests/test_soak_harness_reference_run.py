# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Reference soak run for the dual-run harness (G11.7-T2 #1402, AC4).

AC4 of the Task asks the harness be "applied to at least the first
dispatchable k8s write slice (#1398) as the reference run." #1398 (the
k8s write ops) is not yet merged, so there is no shipped ``k8s.scale`` /
``k8s.apply`` to drive. This test is the **reference run against live
backplane primitives** that does not depend on #1398 landing: it
registers a stand-in ``requires_approval=True`` write op (the same
pattern ``test_examples_r2_approval_gate.py`` uses for the R2 recipe),
drives the full *human* queued → approve → resume cycle that #1401 just
shipped, captures the **real** ``approval.request`` / ``approval.decision``
audit rows and the **real** broadcast events the dispatcher emits, and
feeds them into the harness's stage-4 verifier
(:func:`assert_approval_completeness`).

It proves the harness asserts the #817 governance invariant against the
actual substrate — not a hand-built fixture. When #1398 merges, the
consumer's ``scripts/parity-check-kubernetes.sh`` produces the same
evidence shape against the real ``k8s.scale`` op and the
``scripts/soak/soak-harness.sh`` driver runs it; the procedure is in
``docs/cross-repo/dual-run-soak-harness.md``.

This is the *human-principal* analogue of the R2 example's
agent-principal cycle — the principal kind that #1401 newly routes to
the queue instead of hard-denying, which is the population the soak
harness graduates write ops for.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from scripts.soak_harness import assert_approval_completeness, op_is_redacted, scorecard_cell
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast import reset_broadcast_client_for_testing
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, Tenant
from meho_backplane.settings import get_settings

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-00000000500a")
_REQUESTER_SUB = "operator:soak-requester"
_APPROVER_SUB = "operator:soak-approver"
#: Stand-in for the first dispatchable k8s write slice (#1398's k8s.scale).
#: Identical gate shape: requires_approval=True, dangerous-class write.
_OP_ID = "examples.soak.scale"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BROADCAST_REDIS_URL", "redis://broadcast.test:6379")
    get_settings.cache_clear()
    reset_broadcast_client_for_testing()
    yield
    get_settings.cache_clear()
    reset_broadcast_client_for_testing()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


def _human(sub: str) -> Operator:
    return Operator(
        sub=sub,
        name=sub,
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


async def _soak_scale_handler(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Deterministic stand-in for k8s.scale; never touches a real cluster."""
    return {"resource": params.get("resource"), "replicas": params.get("replicas"), "scaled": True}


async def test_reference_soak_run_human_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive the full human queued→approve→resume cycle and graduate it.

    Steps map to the harness stages:

    * Stages 1/3 are connector-effect comparisons the consumer's
      per-op hooks produce; this in-process reference run focuses on
      **stage 4** (the governance invariant) which is the part that
      must hold against the live substrate and is connector-agnostic.
    * The captured ``approval.request`` + ``approval.decision`` audit
      rows and the single write-effect broadcast event are fed into
      :func:`assert_approval_completeness`.
    * :func:`scorecard_cell` then derives the supported cell: stages
      clean + no live soak yet → 🟡 SHADOW (the op is ready to enter
      stage 5, not yet ✅).
    """
    import meho_backplane.operations._audit as audit_module
    from meho_backplane.connectors.base import Connector
    from meho_backplane.connectors.registry import clear_registry, register_connector_v2
    from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
    from meho_backplane.operations import (
        dispatch,
        register_typed_operation,
        reset_dispatcher_caches,
    )
    from meho_backplane.operations.approval_queue import approve_request

    captured_events: list[Any] = []

    async def _capture(event: Any) -> None:
        captured_events.append(event)

    monkeypatch.setattr(audit_module, "publish_event", _capture)
    reset_dispatcher_caches()
    clear_registry()

    async with get_sessionmaker()() as s:
        s.add(Tenant(id=_TENANT_ID, slug="soak-ref", name="Soak Reference Tenant"))
        await s.commit()

    class _SoakConnector(Connector):
        product = "examples"
        version = "1.x"
        impl_id = "examples"
        priority = 10

        async def fingerprint(self, host: str, port: int | None) -> FingerprintResult:
            return FingerprintResult(
                probe=ProbeResult(reachable=True, probe_method="none"),
                product="examples",
                version="1.x",
            )

        async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
            raise NotImplementedError

        async def execute(  # type: ignore[override]
            self, target: Any, op_id: str, params: dict[str, Any]
        ) -> Any:
            raise NotImplementedError

    register_connector_v2(product="examples", version="", impl_id="", cls=_SoakConnector)

    from unittest.mock import AsyncMock

    stub_emb = AsyncMock()
    stub_emb.encode_one.return_value = [0.1] * 384
    stub_emb.encode.return_value = [[0.1] * 384]
    stub_emb.dimension = 384

    await register_typed_operation(
        product="examples",
        version="1.x",
        impl_id="examples",
        op_id=_OP_ID,
        handler=_soak_scale_handler,
        summary="Stand-in scale op for the soak-harness reference run.",
        description="Mirrors k8s.scale: requires_approval=True dangerous write.",
        parameter_schema={
            "type": "object",
            "properties": {"resource": {"type": "string"}, "replicas": {"type": "integer"}},
            "required": ["resource", "replicas"],
        },
        requires_approval=True,
        when_to_use=None,
        embedding_service=stub_emb,
    )

    params = {"resource": "deploy/web", "replicas": 5}

    # ---- A human operator dispatches → queued (the #1401 behaviour) ----
    requester = _human(_REQUESTER_SUB)
    first = await dispatch(
        operator=requester,
        connector_id="examples-1.x",
        op_id=_OP_ID,
        target=None,
        params=params,
    )
    assert first.status == "awaiting_approval", (
        f"human USER principal on a requires_approval op must queue (not deny) "
        f"post-#1401; got {first.status!r}: {first.error!r}"
    )
    request_id = uuid.UUID(first.extras["approval_request_id"])

    # ---- A DIFFERENT operator approves (requester != approver, #1401 guard) ----
    async with get_sessionmaker()() as s:
        row = await approve_request(s, request_id, operator=_human(_APPROVER_SUB), params=None)
        await s.commit()
    assert row.status == "approved"

    # ---- Resume re-dispatches with _approved=True → the op executes ----
    resumed = await dispatch(
        operator=requester,
        connector_id="examples-1.x",
        op_id=_OP_ID,
        target=None,
        params=params,
        _approved=True,
    )
    assert resumed.status == "ok", (
        f"resume should execute; got {resumed.status!r}: {resumed.error!r}"
    )
    assert resumed.result["scaled"] is True

    # ---- Capture the real audit rows the cycle wrote ----
    # Include the dispatcher's own path==op_id dispatch row alongside the
    # two approval.* rows so stage 4 can assert the full #817 invariant
    # (the durable write-record clause, B1).
    async with get_sessionmaker()() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(AuditLog).where(
                        AuditLog.path.in_(["approval.request", "approval.decision", _OP_ID])
                    )
                )
            )
            .scalars()
            .all()
        )
    audit_rows = [{"path": r.path, "operator_sub": r.operator_sub} for r in rows]

    # ---- Capture the real broadcast events (BroadcastEvent → dict) ----
    broadcast_events = [
        {"op_id": e.op_id, "op_class": e.op_class, "payload": e.payload} for e in captured_events
    ]

    # ---- Feed the LIVE evidence into the harness stage-4 verifier ----
    stage4 = assert_approval_completeness(
        _OP_ID,
        audit_rows=audit_rows,
        broadcast_events=broadcast_events,
        # The resume only returned after approve_request committed the
        # decision row (we awaited the commit above before re-dispatching).
        returned_after_decision=True,
    )
    assert stage4.passed, (
        f"stage-4 governance invariant failed on live substrate: {stage4.findings}"
    )

    # exactly one approval.request + one approval.decision row landed
    assert sum(r["path"] == "approval.request" for r in audit_rows) == 1
    assert sum(r["path"] == "approval.decision" for r in audit_rows) == 1
    # exactly one dispatch row for the executed write (path == op_id) — the
    # durable write-record clause stage 4 now asserts (B1).
    assert sum(r["path"] == _OP_ID for r in audit_rows) == 1
    # the request row is attributed to the requester, the decision to the approver
    req_row = next(r for r in audit_rows if r["path"] == "approval.request")
    dec_row = next(r for r in audit_rows if r["path"] == "approval.decision")
    assert req_row["operator_sub"] == _REQUESTER_SUB
    assert dec_row["operator_sub"] == _APPROVER_SUB

    # ---- scorecard cell: stages clean, no live soak yet → SHADOW (🟡) ----
    from scripts.soak_harness import SoakReport

    report = SoakReport(op_id=_OP_ID, connector_id="examples-1.x", stages=[stage4])
    assert scorecard_cell(report, soak_clean=False).value == "shadow"
    assert scorecard_cell(report, soak_clean=True).value == "ready"

    # ---- a plain write op is not in the redacted set; a secret op is ----
    assert not op_is_redacted(_OP_ID)
    assert op_is_redacted("k8s.secret.create")

    reset_dispatcher_caches()
    clear_registry()
