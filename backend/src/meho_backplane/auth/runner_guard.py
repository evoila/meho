# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Gateway guard primitives for satellite-runner principals (Initiative #2415).

Task #2502 builds the identity half of the remote-execution gateway. The
sibling gateway routes — the outbound long-poll command plane (#2498) and
the versioned assignment / result-ingest API (#2499) — are the consumers
of this module; they do not exist yet, so the guard ships with its own
unit tests against a stub route.

Two primitives, layered:

* :func:`require_runner` — a FastAPI dependency factory (moulded on
  :func:`~meho_backplane.auth.rbac.require_role`). It asserts the caller
  is a ``principal_kind=runner`` token carrying a non-``None``
  ``runner_id`` and returns the :class:`~meho_backplane.auth.operator.Operator`.
  This is a **kind** gate, not a **scope** gate: it proves *a* runner is
  calling, not *which* runner may act on the route's target.

* :func:`assert_runner_scope` — the scope gate. Given the runner operator
  and the runner **name** the route addresses (the gateway set keys
  runners by name: #2498's ``{runner}`` path segment, #2499's ``?runner=``
  query param), it resolves the tenant-scoped ``runner_principal`` row and
  requires ``row.id == operator.runner_id``. This is the single binding
  point between the route's name-addressed target and the token's
  unforgeable ``runner_id`` claim, so a runner token can only fetch its
  own assignment and submit its own results.

Relationship to the negative route cage
----------------------------------------
The cage in :func:`~meho_backplane.middleware.verify_jwt_and_bind` already
fail-closed 403s a runner token on every route outside
:data:`~meho_backplane.middleware.RUNNER_ALLOWED_PATH_PREFIXES`. The cage
is the coarse "runners live only on the gateway prefixes" boundary;
:func:`require_runner` + :func:`assert_runner_scope` are the fine-grained
"this runner, on this route, for its own identity" gates that the gateway
routes mount on top. All authorization stays central (Initiative #2415
design principle): the runner never self-authorizes.

Deliberately **not** a revocation check
---------------------------------------
:func:`assert_runner_scope` reads identity columns only; it does **not**
consult ``revoked``. Mould parity with agent principals: the kill switch
is Keycloak ``enabled=false`` (blocks new token grants) plus a short
access-token TTL, not a per-request DB check. Central staleness handling
for a dead/revoked runner is #2501's dead-man switch.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import structlog
from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, PrincipalKind
from meho_backplane.db.models import RunnerPrincipal
from meho_backplane.middleware import verify_jwt_and_bind

__all__ = ["assert_runner_scope", "require_runner"]

#: Detail token shared by every runner-authorization failure — the
#: negative cage (:func:`~meho_backplane.middleware.verify_jwt_and_bind`),
#: the MCP rejection (:func:`~meho_backplane.mcp.auth.verify_mcp_jwt_and_bind`),
#: and both gates here. A single code keeps the client-facing contract and
#: the on-call telemetry uniform; the structlog ``surface`` field
#: distinguishes where the violation fired.
_RUNNER_SCOPE_VIOLATION: str = "runner_scope_violation"


def require_runner() -> Callable[[Operator], Operator]:
    """Build a FastAPI dependency that admits only runner-kind principals.

    Returns a callable for ``Depends(...)`` whose body reads the validated
    :class:`Operator` produced by
    :func:`~meho_backplane.middleware.verify_jwt_and_bind` and requires
    ``principal_kind is PrincipalKind.RUNNER`` **and** a non-``None``
    ``runner_id``, returning the operator on success and raising HTTP 403
    ``runner_scope_violation`` otherwise.

    The ``runner_id is not None`` half is belt-and-suspenders: the JWT
    chain already rejects a runner-kind token without the claim at 401
    (``missing_runner_id_claim`` in
    :func:`~meho_backplane.auth.jwt._operator_from_claims`), so an Operator
    that reaches this dependency with ``principal_kind=runner`` always
    carries a ``runner_id``. Re-asserting it here keeps
    :func:`assert_runner_scope`'s ``row.id == operator.runner_id`` compare
    from ever running against ``None`` if that invariant later drifts.

    The factory shape mirrors
    :func:`~meho_backplane.auth.rbac.require_role` so gateway routes can
    declare ``Depends(require_runner())`` (and, when they also need the
    operator instance, bind it: ``operator: Operator =
    Depends(require_runner())``). Non-runner tokens (user / service /
    agent) that reach a gateway route are rejected here — the cage lets
    them through (it only gates runner-kind tokens), so this dependency is
    what keeps a human/agent token off a runner-only route.
    """

    def _checker(operator: Operator = Depends(verify_jwt_and_bind)) -> Operator:
        if operator.principal_kind is not PrincipalKind.RUNNER or operator.runner_id is None:
            structlog.get_logger(__name__).warning(
                _RUNNER_SCOPE_VIOLATION,
                operator_sub=operator.sub,
                principal_kind=operator.principal_kind.value,
                reason="not_a_runner_principal",
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=_RUNNER_SCOPE_VIOLATION,
            )
        return operator

    return _checker


async def assert_runner_scope(
    operator: Operator,
    *,
    runner_name: str,
    session: AsyncSession,
) -> RunnerPrincipal:
    """Bind a runner token's ``runner_id`` to the runner *named* by the route.

    Resolves the tenant-scoped ``runner_principal`` row for
    ``(operator.tenant_id, runner_name)`` — one indexed read on the unique
    ``(tenant_id, name)`` index — and requires ``row.id ==
    operator.runner_id``. Returns the row so callers can reuse it without a
    second query.

    Heartbeat side effect (#2501)
    -----------------------------
    On a successful scope match this guard stamps ``row.last_seen_at =
    now()`` on the **central clock** and commits it — the dead-man switch's
    mandatory heartbeat. This is the single choke-point every runner-plane
    request passes through (#2498's ``GET .../next`` + ``POST .../result``,
    #2499's ``GET /checks/assignment`` + ``POST /checks/results`` all call
    it, and nothing else does), so piggybacking the stamp here measures the
    runner's real ability to reach central and cannot be missed by a future
    runner route that forgets to stamp. The stamp is keyed by ``row.id``
    (the token's unforgeable ``runner_id`` claim) and reads no request
    field, so ``last_seen_at`` is never client-controlled — the exact
    discipline ``web_session.last_seen_at`` follows. There is deliberately
    **no** dedicated heartbeat endpoint: a healthy idle runner still issues
    at least one authenticated request per poll window, so the idle work
    cycle *is* the heartbeat (the #1501 zombie-heartbeat lesson).

    Fail-closed with **no existence oracle**: a name that resolves to no
    row *and* a name that resolves to a **different** runner both raise the
    same HTTP 403 ``runner_scope_violation``. A runner probing another
    runner's name therefore cannot distinguish "no such runner" from "not
    my runner" — both are the same 403, so the gateway leaks no runner
    inventory to a caged principal.

    Args:
        operator: The runner-kind operator (typically produced by
            :func:`require_runner`). ``operator.runner_id`` must be set;
            :func:`require_runner` guarantees this.
        runner_name: The runner name the route addresses (path/query param).
        session: An open :class:`AsyncSession` the caller owns. On a
            successful match this guard stamps the heartbeat and
            ``commit()``s it on this session (see "Heartbeat side effect");
            it does not close the session. Callers must therefore invoke
            ``assert_runner_scope`` **before** any other mutation on the
            session — its natural position as the auth/scope gate — so the
            commit flushes only the heartbeat.

    Returns:
        The matching :class:`~meho_backplane.db.models.RunnerPrincipal` row.

    Raises:
        HTTPException: 403 ``runner_scope_violation`` when no tenant-scoped
            runner named *runner_name* exists, or the row's id does not
            match the token's ``runner_id``.
    """
    result = await session.execute(
        select(RunnerPrincipal).where(
            RunnerPrincipal.tenant_id == operator.tenant_id,
            RunnerPrincipal.name == runner_name,
        )
    )
    row = result.scalar_one_or_none()
    if row is None or row.id != operator.runner_id:
        structlog.get_logger(__name__).warning(
            _RUNNER_SCOPE_VIOLATION,
            operator_sub=operator.sub,
            runner_name=runner_name,
            reason="unknown_runner" if row is None else "runner_id_mismatch",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=_RUNNER_SCOPE_VIOLATION,
        )
    # Heartbeat (#2501): stamp runner liveness on the central clock, keyed
    # by the token's ``runner_id`` claim (``row.id``), reading no request
    # field. Committed on the caller's own session (no nested session -- the
    # SQLite dev/test pool is a single-connection StaticPool). This guard is
    # always the first operation in each runner-plane handler, so the commit
    # flushes only the heartbeat and it persists even for the callers (the
    # long-poll GET and the assignment GET) that never otherwise commit.
    row.last_seen_at = datetime.now(UTC)
    await session.commit()
    return row
