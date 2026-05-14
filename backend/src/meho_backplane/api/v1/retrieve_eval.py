# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``POST /api/v1/retrieve/eval`` — corpus-driven retrieval-quality eval.

G4.3-T2 (#441) of Initiative #373. The route is the HTTP face of the
:mod:`meho_backplane.retrieval.eval.runner` engine — operators and the
``meho retrieval eval`` Go CLI hit this surface to run the
checked-in eval corpus against the retrieval substrate and read back
precision@5 / MRR / coverage with a green/yellow/red verdict.

RBAC
----

``operator`` role minimum (mirrors :mod:`meho_backplane.api.v1.retrieve`
+ :mod:`meho_backplane.api.v1.retrieve_usage`). The eval reads from the
caller's tenant — no surface accepts a different tenant id from the
body / query string.

Audit + broadcast contract
--------------------------

Mirrors the T5 (#444) usage telemetry posture: the route binds two
audit-override contextvars *before* the runner kicks off so a handler
exception still produces an audit row with the partial payload, and
so the broadcast publisher emits an ``audit_query``-class aggregate-
only event (the eval queries themselves can be operator-sensitive —
"is the operator searching for the disk-failure runbook?" leaks
intent).

* ``audit_op_id = "meho.retrieval.eval"`` — canonical op_id for every
  audit row this route writes. Operators querying ``audit_log`` for
  "everyone who triggered an eval" filter on
  ``payload->>'op_id' = 'meho.retrieval.eval'``.
* ``audit_op_class = "audit_query"`` — flips the broadcast event into
  aggregate-only mode (``{op_class, result_status, row_count}``), the
  same posture the G8 audit-query API ships under (#334) and that T5's
  retrieve_usage adopted.

Two further enrichment fields land in the audit_log row's ``payload``:

* ``audit_eval_surface`` — which surface(s) the operator requested.
* ``audit_eval_baseline`` — whether the grep baseline was requested
  (``"grep"`` or ``""``).

Both are aggregation parameters, not raw queries — no redaction
required.

``audit_row_count`` is bound after the runner returns so the
broadcast event's ``row_count`` field reflects ``query_count`` summed
across surfaces (the cardinality of "queries actually evaluated"),
not the per-surface row count.

Out of scope
------------

* **Server-side baseline evaluation.** v0.2 has no checked-in
  corpus snapshot on the server; an explicit ``baseline=grep`` in
  the request body is rejected with 501 Not Implemented (the field
  is kept on the schema so the rejection is honest rather than
  silent). The CLI runs the baseline locally against the operator's
  checked-out ``kb/`` snapshot. T7 / v0.2.next will wire a
  server-side corpus path and unblock this surface.
* **Save / compare baseline workflow.** The CLI handles disk
  serialisation; the API route returns the raw EvalResult and lets
  the caller persist it as they choose. (A future API surface for
  server-side baseline storage is filed as v0.2.next polish if
  operators ask for it.)
* **Per-surface threshold customisation in the request body.** Locked
  to :data:`~meho_backplane.retrieval.eval.metrics.GREEN_DEFAULTS` for
  v0.2 — operators tuning thresholds use the in-process runner
  directly via a custom script.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.retrieval.eval import (
    EvalRequestSurface,
    EvalResult,
    eval_all,
)

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/retrieve", tags=["retrieval"])

#: Module-level ``Depends`` closure for the route's RBAC gate. Built
#: once at import time to satisfy ruff's B008 rule (no function-call
#: defaults), matching the pattern :mod:`~meho_backplane.api.v1.retrieve`
#: and :mod:`~meho_backplane.api.v1.retrieve_usage` established.
_require_operator = Depends(require_role(TenantRole.OPERATOR))


class EvalRequest(BaseModel):
    """POST body for ``/api/v1/retrieve/eval``.

    Frozen + extra=forbid so a typo (``surfaces`` instead of
    ``surface``) fails 422 at the framework boundary rather than
    silently running the default surface and giving the caller a
    confusing "I asked for memory, why did kb run?" response.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    surface: EvalRequestSurface = Field(default="all")
    # Optional baseline kind. v0.2 server has no checked-in corpus
    # snapshot to evaluate against, so any non-empty value is rejected
    # at the handler with 501 Not Implemented (see
    # :func:`eval_endpoint`). Operators run the baseline locally via
    # the CLI verb instead. The field is kept on the schema so the
    # rejection surface is honest (`baseline=grep` → 501 with a
    # message pointing operators at the CLI) rather than silently
    # dropping the value. T7 / v0.2.next wires server-side corpus
    # storage and removes the rejection.
    baseline: str | None = Field(default=None, max_length=16)


def _bind_request_audit_context(
    *,
    surface: str,
    baseline: str | None,
) -> None:
    """Bind the audit overrides + enrichment fields for this request.

    Called **before** the runner kicks off so a handler exception
    still produces an audit row with partial payload (incident-
    postmortem hook). The two override keys (``audit_op_id`` /
    ``audit_op_class``) are honoured by the chassis broadcast
    publisher per :func:`meho_backplane.audit._publish_broadcast_event`.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id="meho.retrieval.eval",
        audit_op_class="audit_query",
        audit_eval_surface=surface,
        audit_eval_baseline=baseline or "",
    )


@router.post(
    "/eval",
    response_model=EvalResult,
    responses={
        501: {
            "description": (
                "Server-side baseline evaluation is not implemented; "
                "the request body's ``baseline`` field is non-empty "
                "and v0.2 has no checked-in corpus snapshot. Run the "
                "baseline locally via ``meho retrieval eval --baseline grep``."
            ),
        },
    },
)
async def eval_endpoint(
    body: EvalRequest,
    operator: Operator = _require_operator,
) -> EvalResult:
    """Run the eval corpus against the retrieving tenant's substrate.

    Tenant-scoped to ``operator.tenant_id`` — no surface accepts a
    different tenant id from the body. ``read_only`` operators get
    403 via :func:`require_role` before reaching this handler.

    The request's ``surface`` value flows through to
    :func:`~meho_backplane.retrieval.eval.runner.eval_all`; ``"all"``
    runs every shipped corpus, single-surface values scope the run.
    The ``baseline="grep"`` flag is honoured for kb only (v0.2 ships
    no operations / memory baseline); other surfaces silently ignore
    it.

    Audit overrides + enrichment contextvars are bound **before** the
    runner kicks off so a handler exception still produces an audit
    row with partial payload. The runner itself is in-process —
    no HTTP round-trip back to ``/api/v1/retrieve``.
    """
    _bind_request_audit_context(surface=body.surface, baseline=body.baseline)

    # The grep baseline isn't wired to a server-side corpus root in
    # v0.2 — operators run the baseline locally via the CLI verb
    # against a checked-in snapshot. Reject explicit baseline values
    # with 501 Not Implemented rather than silently dropping the
    # field: silent-drop produces a 200 with ``baseline_kind=null``
    # per surface, which a caller may not notice — and the CI signal
    # for "MEHO ≥ baseline" (retire-criterion #4 of Initiative #373)
    # would silently never fire on the API path. The audit context
    # is bound *before* this gate so the rejection still records the
    # operator's intent for follow-up review. T7 / v0.2.next will
    # wire a server-side corpus snapshot and unblock this path.
    if body.baseline:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=(
                "server-side baseline evaluation is not implemented; "
                "run `meho retrieval eval --baseline grep` from a "
                "machine that has the kb/ snapshot checked out"
            ),
        )

    # Resolve the surface filter for the runner. ``"all"`` -> None,
    # which the runner interprets as "every shipped surface".
    surfaces = None if body.surface == "all" else [body.surface]

    result = await eval_all(
        tenant_id=operator.tenant_id,
        surfaces=surfaces,
    )

    # Total queries actually evaluated across surfaces; feeds the
    # broadcast event's ``row_count`` field per the audit_query
    # convention.
    total_queries = sum(s.query_count for s in result.surfaces)
    structlog.contextvars.bind_contextvars(audit_row_count=total_queries)
    return result
