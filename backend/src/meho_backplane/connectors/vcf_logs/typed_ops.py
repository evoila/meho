# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed (bound-method) read op(s) for :class:`VcfLogsConnector` (#2295).

vRLI shipped its read surface as a **generic-ingested** catalog: the
``endpoint_descriptor`` rows land via ``meho connector ingest`` against
``vcf-logs-9.0/openapi.yaml`` and
:func:`~meho_backplane.connectors.vcf_logs.core_ops.apply_vrli_core_curation`
flips a curated subset to ``is_enabled=True``. That model makes op
correctness depend on mutable per-deploy catalog state — the #2247 failure
class Initiative #2266 retires for the VCF family.

``vrli.event.query`` is vRLI's first **typed** op (``source_kind="typed"``):
a bound method on :class:`VcfLogsConnector` that issues the events query
directly on the connector's own authenticated session — no ingested
descriptor, no ``dispatch_child`` — so it works on a fresh boot with zero
catalog ingest. It is the one op the adopter's real operations exercise
(the hourly sentry cron + incident log pulls run exactly the events query),
which is why it is converted first; the other six curated ops stay ingested
(declined from typed conversion on #2295: unused in real operations, and the
ingested canonical spec covers the browse case).

The sibling precedent is
:mod:`meho_backplane.connectors.vmware_rest.typed_ops` (``vmware.host.usage``,
#2257) and :mod:`meho_backplane.connectors.argocd.ops`: a metadata dataclass
here, a thin bound-method handler on the connector, and a module-level
registrar queued onto
:func:`~meho_backplane.operations.typed_register.register_typed_op_registrar`.

Why route through ``_get_json_with_session_retry``
--------------------------------------------------

The events query is a session-token call, and vRLI idle-times out its
in-memory session (``trait.authenticated.440`` — the #1909 / #1135 soak
scenario). Dispatching the query through
:meth:`VcfLogsConnector._get_json_with_session_retry` gives the typed op the
same one-shot invalidate → re-login → retry-once recovery the connector's
fingerprint / probe already ride, keyed on the profile's ``{401, 440}``
``expiry_statuses``. The generic-ingested dispatch path's own #2067 recovery
seam (:meth:`VcfLogsConnector.invalidate_session`) does not apply here — a
typed handler owns its transport call, so the retry has to live in the
handler.

Reserved-expansion constraint path (#2003 / #2066)
--------------------------------------------------

vRLI encodes the query constraint set as a slash-delimited sub-path
(``text/CONTAINS error/hostname/CONTAINS vcsa``) after ``/api/v2/events/``.
Those structural slashes must reach the appliance literal — a ``%2F``-mangled
URL 400s. :func:`build_event_query_path` renders the constraint with the same
RFC6570 reserved-expansion safe-set the ingested dispatcher's
``_substitute_path`` uses for ``{+constraints}``, so structural chars pass
through while genuinely-unsafe chars (space, control) still percent-encode.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote

import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vcf_logs.session import VcfLogsTargetLike

if TYPE_CHECKING:
    from meho_backplane.connectors.vcf_logs.connector import VcfLogsConnector
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "VRLI_EVENT_QUERY_OP",
    "VRLI_TYPED_OPS",
    "VRLI_TYPED_WHEN_TO_USE_BY_GROUP",
    "VrliTypedOp",
    "build_event_query_path",
    "event_query_impl",
    "register_vrli_typed_operations",
]

_log = structlog.get_logger(__name__)

# The events read surface. The constraint chain is appended as a
# reserved-expansion sub-path (see module docstring); the base path with an
# empty constraint reaches ``/api/v2/events/`` (all events, bounded by limit).
_EVENTS_PATH_PREFIX = "/api/v2/events/"

# RFC6570 §3.2.3 reserved-expansion safe-set — the gen-delims + sub-delims
# that stay literal so vRLI's slash-delimited constraint chain reaches the
# appliance intact. Mirrors the ingested dispatcher's ``_RFC6570_RESERVED_SAFE``
# (``meho_backplane.operations._branches``); the shared behaviour is pinned by
# ``test_event_query_path_keeps_reserved_constraint_slashes_literal`` so the
# typed path and the ingested ``{+constraints}`` path cannot drift (#2003).
_CONSTRAINTS_RESERVED_SAFE = ":/?#[]@!$&'()*+,;="

# vRLI caps an unbounded events query at the appliance default; the op exposes
# ``limit`` so the agent can bound the pull explicitly (the adopter's incident
# pulls do). Sent as a query param, not part of the constraint sub-path.
_LIMIT_QUERY_KEY = "limit"

_EVENTS_GROUP_KEY = "vrli-events"


@dataclass(frozen=True)
class VrliTypedOp:
    """Metadata for one vRLI typed op registered at lifespan startup.

    Fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts so :func:`register_vrli_typed_operations` can splat the dataclass
    into the helper without per-op boilerplate. ``handler_attr`` is the
    attribute name on
    :class:`~meho_backplane.connectors.vcf_logs.connector.VcfLogsConnector`
    exposing the async handler; the registrar resolves the bound method against
    the class so the dispatcher's
    :func:`~meho_backplane.operations._handler_resolve.import_handler` walk
    recovers the callable from the persisted ``module.ClassName.method`` path.
    Mirrors :class:`~meho_backplane.connectors.vmware_rest.typed_ops.VmwareTypedOp`.
    """

    op_id: str
    handler_attr: str
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any] | None
    group_key: str | None
    tags: tuple[str, ...]
    safety_level: Literal["safe", "caution", "dangerous"]
    requires_approval: bool
    llm_instructions: dict[str, Any] | None


def build_event_query_path(constraints: str) -> str:
    """Render the ``/api/v2/events/<constraints>`` request path.

    *constraints* is vRLI's slash-delimited constraint chain (e.g.
    ``text/CONTAINS error/hostname/CONTAINS vcsa``); an empty string reaches
    the base ``/api/v2/events/`` (all events). The value is percent-encoded
    with the RFC6570 reserved-expansion safe-set so the structural slashes stay
    literal on the wire while spaces / control chars still encode — identical to
    how the ingested dispatcher renders a ``{+constraints}`` template (#2003 /
    #2066).
    """
    return _EVENTS_PATH_PREFIX + quote(constraints, safe=_CONSTRAINTS_RESERVED_SAFE)


def _coerce_events(payload: Any) -> list[Any]:
    """Return the ``events`` list from a vRLI events response, or ``[]``.

    vRLI wraps the raw event rows under a top-level ``events`` key. A response
    that is not a dict, or one missing / mis-typing ``events``, yields an empty
    list rather than raising — the appliance returning an unexpected shape
    surfaces as "no events", not a dispatch crash.
    """
    if not isinstance(payload, dict):
        return []
    events = payload.get("events")
    return events if isinstance(events, list) else []


async def event_query_impl(
    connector: VcfLogsConnector,
    operator: Operator,
    target: VcfLogsTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vrli.event.query`` — constraint-filtered event search.

    Issues ``GET /api/v2/events/<constraints>`` (optional ``limit`` query
    param) directly on the connector's authenticated session via
    :meth:`VcfLogsConnector._get_json_with_session_retry`, which recovers a
    440 / 401 session expiry with one re-login + retry (the #1909 / #1135
    scenario). No ingested descriptor, no ``dispatch_child`` — works on a fresh
    boot with zero catalog ingest.

    Parameters (from *params*):

    * ``constraints`` — the URL-path constraint chain (field/value pairs, time
      range) composed from field names an agent obtains via the ingested
      ``vrli.field.list`` browse op. Empty / absent → all events.
    * ``limit`` — optional cap on the number of event rows returned.

    Returns ``{"events": [...], "complete": bool}``. Large result sets are
    summarised into a JSONFlux :class:`~meho_backplane.connectors.schemas.ResultHandle`
    by the dispatcher's connector-agnostic reducer (``events`` is the reduced
    collection); the handler itself returns the plain envelope.
    """
    constraints = params.get("constraints") or ""
    if not isinstance(constraints, str):
        raise ValueError(
            f"vrli.event.query: 'constraints' must be a string; got {type(constraints).__name__}"
        )
    path = build_event_query_path(constraints)

    query: dict[str, Any] | None = None
    limit = params.get("limit")
    if limit is not None:
        query = {_LIMIT_QUERY_KEY: limit}

    payload = await connector._get_json_with_session_retry(
        target, path, operator=operator, params=query
    )
    events = _coerce_events(payload)
    complete = bool(payload.get("complete", True)) if isinstance(payload, dict) else True
    _log.info(
        "vrli_event_query_read",
        target=target.name,
        constraint_len=len(constraints),
        event_count=len(events),
        complete=complete,
    )
    return {"events": events, "complete": complete}


# ---------------------------------------------------------------------------
# Op metadata + registrar
# ---------------------------------------------------------------------------

#: Curated ``when_to_use`` blurb per typed-op group.
#: :func:`register_typed_operation` requires a non-empty string whenever
#: ``group_key`` is set (typed_register ``_validate_when_to_use_pairing``).
#: The ``vrli-events`` group is shared with the ingested ``aggregated-events``
#: browse op; the blurb covers the headline event-query surface either lands.
VRLI_TYPED_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    _EVENTS_GROUP_KEY: (
        "Use this group to query vRLI log events — the headline read surface of "
        "vRLI. vrli.event.query returns constraint-filtered, time-range-bounded "
        "raw log lines directly from the appliance session (works with zero "
        "catalog ingest). Compose the constraint chain from field names an agent "
        "confirms via the field catalog, and cap the pull with limit. Large "
        "result sets come back as a JSONFlux handle (bounded inline sample plus "
        "a fetch_more envelope); re-run with a narrower constraint / time range "
        "to act on more than the sample."
    ),
}

VRLI_EVENT_QUERY_OP = VrliTypedOp(
    op_id="vrli.event.query",
    handler_attr="event_query",
    summary="Query raw vRLI log events by constraint chain, with an optional limit.",
    description=(
        "Queries raw vRLI (VCF Operations for Logs) events via "
        "GET /api/v2/events/<constraints> issued directly on the connector's "
        "session — no catalog ingest required. The constraints path segment "
        "carries the URL-encoded, slash-delimited constraint chain (field/value "
        "pairs, time range) vRLI's printQuery renders; compose it from field "
        "names confirmed via the field catalog. Optional limit caps the number "
        "of event rows. Returns {events: [...], complete: bool}; event rows carry "
        "timestamp, text, hostname, source, and any extracted fields. The "
        "headline read surface of vRLI — use for 'show me events where source "
        "contains nsx and severity is error in the last 1h'. Large result sets "
        "return a JSONFlux ResultHandle (bounded inline sample plus a fetch_more "
        "envelope); re-run with a narrower constraint / time range to act on more "
        "than the sample. safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "constraints": {
                "type": "string",
                "description": (
                    "URL-path constraint chain (slash-delimited field/OP value "
                    "pairs, e.g. 'text/CONTAINS error/hostname/CONTAINS vcsa'). "
                    "Omit or pass an empty string for all events (bounded by "
                    "limit). Structural slashes reach the appliance literal."
                ),
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "Optional cap on the number of event rows returned. Omit for "
                    "the appliance default."
                ),
            },
        },
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "events": {"type": "array"},
            "complete": {"type": "boolean"},
        },
        "additionalProperties": True,
    },
    group_key=_EVENTS_GROUP_KEY,
    tags=("read-only", "vrli", "vcf-logs", "events", "log-query"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to query raw vRLI log events. Compose the constraints chain "
            "from field names obtained from the field catalog; set limit to bound "
            "the pull. The headline read surface of vRLI — use when answering "
            "'show me events where source contains nsx and severity is error in "
            "the last 1h'."
        ),
        "parameter_hints": {
            "constraints": ("Slash-delimited field/OP value chain; omit for all events."),
            "limit": "Max event rows to return; omit for the appliance default.",
        },
        "output_shape": (
            "{events: [{timestamp, text, hostname, source, ...}, ...], "
            "complete: bool}. Large result sets return a JSONFlux ResultHandle "
            "with a bounded inline sample plus a fetch_more envelope; drill via "
            "result_describe / result_query rather than expecting the full "
            "payload inline. complete=false means the limit / index truncated the "
            "set — re-run with a narrower constraint or time range."
        ),
        "next_step": (
            "If complete=false, surface the truncation and re-compose a tighter "
            "constraint (or lower the time range). For group-by counts, switch to "
            "the aggregated-events browse op."
        ),
    },
)

#: The typed ops :class:`VcfLogsConnector` registers at lifespan startup. Today
#: just ``vrli.event.query`` (#2295); the tuple shape lets a future typed vRLI
#: read join without touching the registrar.
VRLI_TYPED_OPS: tuple[VrliTypedOp, ...] = (VRLI_EVENT_QUERY_OP,)


async def register_vrli_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert every op in :data:`VRLI_TYPED_OPS` into ``endpoint_descriptor``.

    Queued onto the lifespan-driven registrar list via
    :func:`~meho_backplane.operations.typed_register.register_typed_op_registrar`
    in this package's ``__init__``; the runner
    (:func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`)
    invokes it after
    :func:`~meho_backplane.connectors.registry._eager_import_connectors` has
    walked every connector subpackage, so the descriptor row lands before the
    first dispatch. Idempotent across pod restarts (the helper skips the
    embedding recompute on unchanged summary / description / tags). Mirrors
    :func:`~meho_backplane.connectors.vmware_rest.typed_ops.register_vmware_typed_operations`
    and the argocd typed-op registrar.

    The ``embedding_service`` keyword-only parameter is the runner contract:
    :func:`run_typed_op_registrars` passes the process-wide
    :class:`EmbeddingService` (or a chassis-test stub) to every registrar, so
    each registrar must accept the kwarg. It is forwarded to
    :func:`register_typed_operation` (which falls back to the process-wide
    singleton when ``None``).
    """
    # Lazy import: the operations package pulls in the embedding pipeline
    # (ONNX runtime + model), which pure connector/handler unit tests should
    # not pay. Lifespan callers have it warmed by the time this runs.
    from meho_backplane.connectors.vcf_logs.connector import VcfLogsConnector
    from meho_backplane.operations.typed_register import register_typed_operation

    for op in VRLI_TYPED_OPS:
        handler = getattr(VcfLogsConnector, op.handler_attr, None)
        if handler is None:
            raise AttributeError(
                f"VcfLogsConnector typed op {op.op_id!r} declares "
                f"handler_attr={op.handler_attr!r} but the class has no such attribute"
            )
        when_to_use = (
            None if op.group_key is None else VRLI_TYPED_WHEN_TO_USE_BY_GROUP.get(op.group_key)
        )
        if op.group_key is not None and when_to_use is None:
            raise ValueError(
                f"VcfLogsConnector typed op {op.op_id!r} declares "
                f"group_key={op.group_key!r} but no curated when_to_use exists for "
                f"that key. Add an entry to VRLI_TYPED_WHEN_TO_USE_BY_GROUP."
            )
        await register_typed_operation(
            product=VcfLogsConnector.product,
            version=VcfLogsConnector.version,
            impl_id=VcfLogsConnector.impl_id,
            op_id=op.op_id,
            handler=handler,
            summary=op.summary,
            description=op.description,
            parameter_schema=op.parameter_schema,
            response_schema=op.response_schema,
            group_key=op.group_key,
            when_to_use=when_to_use,
            tags=list(op.tags),
            safety_level=op.safety_level,
            requires_approval=op.requires_approval,
            llm_instructions=op.llm_instructions,
            embedding_service=embedding_service,
        )
    _log.info(
        "vrli_typed_operations_registered",
        count=len(VRLI_TYPED_OPS),
        product=VcfLogsConnector.product,
        version=VcfLogsConnector.version,
        impl_id=VcfLogsConnector.impl_id,
    )
