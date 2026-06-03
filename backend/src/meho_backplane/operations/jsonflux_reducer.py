# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Real JSONFlux reducer — the production default behind the dispatcher seam.

G0.6.1-T3 (#753) of Initiative #750. Bridges the vendored
:mod:`meho_backplane.jsonflux` package (T2 #752) into the dispatcher's
:class:`~meho_backplane.operations.reducer.Reducer` Protocol contract.

The reducer's job, per CLAUDE.md postulate 6 / v0.1-spec §4 L294-311:
small / scalar payloads pass through verbatim with a ``None`` handle; a
set-shaped payload above the threshold (50 rows OR 4 KB serialized by
default) is materialized into an in-memory DuckDB table, summarized as
markdown, frozen into a JSON Schema, and addressed by a
:class:`~meho_backplane.connectors.schemas.ResultHandle` carrying a
bounded sample so no agent ever sees the full set inline.

Why ``QueryEngine`` directly, not ``JsonFlux``
==============================================

The vendored :class:`~meho_backplane.jsonflux.JsonFlux` facade exposes
``analyze`` / ``tree`` / ``stats`` / ``query`` but **not** the smart
``register(unwrap=...)`` method — that lives on the lower-level
:class:`~meho_backplane.jsonflux.query.engine.QueryEngine` (the
list-of-dicts → DuckDB-table materializer the T2 vendoring preserved
verbatim from MEHO.X commit ``8f48c141``). The reducer therefore drives
``QueryEngine`` directly: it is the component that owns ``register`` +
``describe_tables`` + ``DESCRIBE`` + sample querying.

Why the reducer detects the collection itself
=============================================

``register(unwrap="auto")`` unwraps a *wrapped collection of objects*
(``{"results": [{...}, ...]}`` → one row per object) but classifies a
*list of scalars* (Vault's ``{"keys": ["a", "b", ...]}``) as metadata,
collapsing it to a single 1-row table. Vendor list ops emit both
shapes, so the reducer locates the primary collection first
(``value`` / ``results`` / ``elements`` / ``keys`` envelopes, a bare
top-level list, or the largest list value) and normalizes a
list-of-scalars to ``[{column: value}, ...]`` rows before registering.
That makes the row count — the threshold input — correct for every
vendor shape, not just the object-collection happy path.
"""

from __future__ import annotations

import uuid
from typing import Any

import msgspec
import structlog
from pydantic import ValidationError

from meho_backplane.connectors.schemas import (
    FetchMore,
    FetchMoreDrillIn,
    FetchMoreNativePagination,
    PaginationHint,
    ResultHandle,
)
from meho_backplane.jsonflux.query.engine import QueryEngine

__all__ = ["JsonFluxReducer"]

_log = structlog.get_logger(__name__)

#: Rationale strings the reducer surfaces verbatim on the
#: :attr:`FetchMore.drill_in` and :attr:`FetchMore.native_pagination`
#: branches. Module-level constants so the wording stays consistent
#: across every reducing dispatch (the agent / consumer sees the same
#: prose every time) and so the tests can assert against a stable
#: anchor.
_DRILL_IN_UNAVAILABLE_RATIONALE: str = (
    "No JsonFlux drill-in path is exposed in this meho version. To act "
    "on more than the sample, re-call the operation with narrower "
    "params (see ``native_pagination`` below) or wait for a future "
    "release that surfaces the handle via an MCP tool / resource URI / "
    "REST route."
)
_NATIVE_PAGINATION_UNAVAILABLE_RATIONALE: str = (
    "The underlying op did not register a ``pagination_hint`` in its "
    "metadata; native pagination params for this op are not documented "
    "here. The connector author may register one via the typed-op "
    "registration ``llm_instructions.pagination_hint`` slot to surface "
    "specific param names + an example next call."
)

#: In-memory DuckDB table name the reducer registers each payload under.
#: One :class:`QueryEngine` is created per :meth:`JsonFluxReducer.reduce`
#: call, so a fixed name is safe — there is never table contention
#: across concurrent dispatches.
_TABLE = "result"

#: Envelope keys vendor list ops wrap their collections under, in
#: priority order: vCenter REST (``value``), NSX policy/manager API
#: (``results``), SDDC Manager (``elements``), Vault KV (``keys``).
_ENVELOPE_KEYS = ("value", "results", "elements", "keys", "items", "data")

#: Column name used when a list-of-scalars is normalized into row dicts.
_SCALAR_COLUMN = "value"

#: ``context`` key carrying the op's result-ordering hint (a plain dict the
#: dispatcher lifts from ``llm_instructions``). Mirrors the
#: ``pagination_hint`` threading: connector authors declare it at op
#: registration, the dispatcher forwards it verbatim, the reducer reads it
#: here. Recognised value: ``{"sample": "tail"}`` -- the op's collection is
#: chronologically ordered oldest-first (k8s/kubectl log line order) and the
#: meaningful inline preview is the *most-recent* rows, not the oldest.
_ORDERING_CONTEXT_KEY = "result_ordering"

#: The one ``sample`` ordering that changes the sample query: take the rows
#: from the tail of the collection (the most-recent N) rather than the head.
#: Any other / absent value keeps the head-first default, which is correct
#: for order-agnostic sets (Vault key lists, topology rows) where neither
#: end is "more recent".
_SAMPLE_TAIL = "tail"

#: Positional row-ordinal column the tail-sample query assigns via
#: ``row_number() OVER ()``. DuckDB does not guarantee the order of a bare
#: ``SELECT``; numbering the registered (Arrow-backed, insertion-ordered)
#: scan gives a deterministic ordinal we can sort on to pick the tail. The
#: name is leading-underscored so it can't collide with a real payload
#: column and is excluded from the returned rows.
_ROWNUM_COLUMN = "_jsonflux_rownum"

#: Map a DuckDB type's leading token to a JSON Schema ``type``. DuckDB
#: reports composite types as ``BIGINT[]`` (array) and ``STRUCT(...)``
#: (object); the prefix match below covers those without enumerating
#: every width-suffixed variant.
_DUCKDB_TYPE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("BOOLEAN", "boolean"),
    ("TINYINT", "integer"),
    ("SMALLINT", "integer"),
    ("INTEGER", "integer"),
    ("BIGINT", "integer"),
    ("HUGEINT", "integer"),
    ("UINTEGER", "integer"),
    ("UBIGINT", "integer"),
    ("UTINYINT", "integer"),
    ("USMALLINT", "integer"),
    ("DOUBLE", "number"),
    ("FLOAT", "number"),
    ("DECIMAL", "number"),
    ("STRUCT", "object"),
    ("MAP", "object"),
    ("JSON", "object"),
)


def _json_schema_type(duckdb_type: str) -> str:
    """Map a DuckDB column type to a JSON Schema scalar ``type`` string.

    Array types (``BIGINT[]``) collapse to ``"array"``; everything not
    matched by :data:`_DUCKDB_TYPE_PREFIXES` (``VARCHAR``, ``UUID``,
    ``DATE``, ``TIMESTAMP``, ...) maps to ``"string"`` — the safe JSON
    representation DuckDB itself uses when serializing those columns.
    """
    upper = duckdb_type.upper()
    if upper.endswith("[]") or upper.startswith("LIST"):
        return "array"
    for prefix, json_type in _DUCKDB_TYPE_PREFIXES:
        if upper.startswith(prefix):
            return json_type
    return "string"


class JsonFluxReducer:
    """Materialize large set-shaped payloads; pass small ones through.

    Structurally satisfies the
    :class:`~meho_backplane.operations.reducer.Reducer` Protocol (the
    Protocol is :func:`~typing.runtime_checkable`, so no explicit
    inheritance is needed and ``isinstance(reducer, Reducer)`` accepts
    this class).

    Thresholds default to v0.1-spec §4: a payload materializes when its
    detected collection has **more than** ``row_threshold`` rows **or**
    serializes to more than ``byte_threshold`` bytes. ``row_threshold=0``
    forces materialization for every non-empty set (test / force mode).
    """

    def __init__(
        self,
        *,
        row_threshold: int = 50,
        byte_threshold: int = 4096,
        sample_size: int = 5,
        ttl_seconds: int = 3600,
    ) -> None:
        self._row_threshold = row_threshold
        self._byte_threshold = byte_threshold
        self._sample_size = sample_size
        self._ttl_seconds = ttl_seconds

    async def reduce(
        self,
        payload: Any,
        schema: dict[str, Any] | None,
        context: dict[str, Any] | None = None,
    ) -> tuple[Any, ResultHandle | None]:
        """Return ``(payload, None)`` or ``(summary, ResultHandle)``.

        See the class docstring for the threshold + collection-detection
        contract. ``schema`` is accepted to satisfy the Protocol; the
        reducer infers the materialized schema from the DuckDB-registered
        table rather than the descriptor schema. ``context`` carries the
        op's :class:`~meho_backplane.connectors.schemas.PaginationHint`
        (under the ``pagination_hint`` key) when the op registered one
        via its ``llm_instructions``; the reducer copies the hint
        verbatim into :attr:`ResultHandle.fetch_more.native_pagination`.
        """
        del schema  # schema is inferred from the registered table

        envelope_key, rows = _detect_collection(payload)
        if rows is None:
            # Not a set-shaped payload (scalar, dict-of-scalars, None) —
            # nothing to reduce.
            return payload, None

        if not self._over_threshold(rows, payload):
            return payload, None

        return self._materialize(payload, envelope_key, rows, context)

    def _over_threshold(self, rows: list[Any], payload: Any) -> bool:
        """True when *rows* exceeds the row OR byte threshold.

        ``row_threshold=0`` forces every non-empty collection over the
        bound (force mode). An empty collection never materializes — a
        0-row handle carries no information a pass-through doesn't.
        """
        if not rows:
            return False
        if len(rows) > self._row_threshold:
            return True
        return len(_serialize(payload)) > self._byte_threshold

    def _materialize(
        self,
        payload: Any,
        envelope_key: str | None,
        rows: list[Any],
        context: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], ResultHandle]:
        """Register *rows* in DuckDB and mint a handle + inline summary.

        A fresh :class:`QueryEngine` is created per call (in-memory,
        per-payload isolation) and closed before returning so no DuckDB
        connection leaks across dispatches.

        ``context`` carries the dispatcher's per-call extras --
        ``op_id``, ``operator_sub``, ``source_kind``, ``target_id``,
        ``pagination_hint`` (when the op registered one). The reducer
        reads ``pagination_hint`` to build
        :attr:`FetchMore.native_pagination`; the rest of the context is
        currently informational (future routing decisions can read it
        without breaking the contract).
        """
        table_rows = _normalize_rows(rows)
        sample_from_tail = _sample_from_tail(context)
        engine = QueryEngine()
        try:
            engine.register(_TABLE, table_rows, unwrap="auto")
            total_rows = engine.tables[_TABLE]["row_count"]
            schema_ = _build_json_schema(engine)
            summary_md = engine.describe_tables(samples=self._sample_size)
            sample_rows = _query_sample(engine, self._sample_size, from_tail=sample_from_tail)
        finally:
            engine.close()

        fetch_more = _build_fetch_more(context)
        handle_id = uuid.uuid4()
        sample_rows_tuple = tuple(sample_rows) if sample_rows else None
        handle = ResultHandle(
            handle_id=handle_id,
            summary_md=f"{total_rows} rows materialized as a JSONFlux handle.\n\n{summary_md}",
            schema_=schema_,
            total_rows=total_rows,
            sample_rows=sample_rows_tuple,
            ttl_seconds=self._ttl_seconds,
            fetch_more=fetch_more,
        )
        summary = {
            "row_count": total_rows,
            "total": total_rows,
            "sample": sample_rows,
        }
        if envelope_key is not None:
            summary["source_key"] = envelope_key
        return summary, handle


def _detect_collection(payload: Any) -> tuple[str | None, list[Any] | None]:
    """Locate the primary set-shaped collection in *payload*.

    Returns ``(envelope_key, rows)`` where ``envelope_key`` is the dict
    key the list was found under (``None`` for a bare top-level list)
    and ``rows`` is the list itself, or ``(None, None)`` when *payload*
    carries no list-shaped collection.

    Resolution order:

    1. A bare top-level ``list`` → ``(None, payload)``.
    2. A ``dict`` whose first matching :data:`_ENVELOPE_KEYS` value is a
       list → ``(key, value)``.
    3. A ``dict`` with no known envelope key → its largest list value,
       if any (covers vendors that wrap under a non-standard key).
    """
    if isinstance(payload, list):
        return None, payload
    if not isinstance(payload, dict):
        return None, None

    for key in _ENVELOPE_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return key, value

    largest_key: str | None = None
    largest: list[Any] | None = None
    for key, value in payload.items():
        if isinstance(value, list) and (largest is None or len(value) > len(largest)):
            largest_key, largest = key, value
    if largest is not None:
        return largest_key, largest
    return None, None


def _normalize_rows(rows: list[Any]) -> list[dict[str, Any]]:
    """Coerce *rows* into a list of dicts DuckDB can register.

    A list of dicts is returned unchanged. A list of scalars (Vault's
    ``keys``) is wrapped one-per-row under :data:`_SCALAR_COLUMN` so the
    smart ``register`` materializes one row per element rather than
    collapsing the list to metadata.
    """
    if rows and isinstance(rows[0], dict):
        return rows
    return [{_SCALAR_COLUMN: item} for item in rows]


def _build_json_schema(engine: QueryEngine) -> dict[str, Any]:
    """Build a JSON Schema (Draft 2020-12) for the registered table.

    Reads the DuckDB ``DESCRIBE`` for the main table and maps each
    column's type to a JSON Schema property. The shape is
    ``{"type": "array", "items": {"type": "object", "properties":
    {...}}}`` — the set-of-objects contract a future ``result_describe``
    reports.
    """
    described = engine.conn.execute(f"DESCRIBE {_TABLE}").fetchall()
    properties = {
        column_name: {"type": _json_schema_type(column_type)}
        for column_name, column_type, *_ in described
    }
    return {
        "type": "array",
        "items": {"type": "object", "properties": properties},
    }


def _query_sample(
    engine: QueryEngine, sample_size: int, *, from_tail: bool = False
) -> list[dict[str, Any]]:
    """Return *sample_size* rows of the table as plain dicts.

    Default (``from_tail=False``): the **head** -- the first ``sample_size``
    rows in registration order. Correct for order-agnostic sets (Vault key
    lists, topology rows) where neither end is more salient.

    ``from_tail=True``: the **tail** -- the *most-recent* ``sample_size``
    rows, returned in chronological (oldest-first) order so the inline
    preview reads like the bottom of a ``kubectl logs`` window. This is the
    fix for the v0.10.0 dogfood defect where a ``k8s.logs(tail=500)`` reduce
    surfaced the oldest 5 lines (health-probe noise) instead of the 5 most
    recent. A bare ``SELECT ... LIMIT`` has no ``ORDER BY`` and so returns
    an implementation-ordered subset (DuckDB docs: order is uncontrolled
    without ``ORDER BY``); numbering the scan with ``row_number() OVER ()``
    and selecting the tail makes the choice deterministic.
    """
    if sample_size <= 0:
        return []
    limit = int(sample_size)
    if not from_tail:
        # Safe (sqlalchemy-execute-raw-query): DuckDB in-memory SELECT; the
        # table name is the fixed module constant and the limit is an int.
        return engine.query(f"SELECT * FROM {_TABLE} LIMIT {limit}")
    # Tail: assign a positional ordinal over the registered scan, keep the
    # highest-ordinal (most-recent) ``limit`` rows, and re-sort ascending so
    # the returned slice stays chronological. ``EXCLUDE`` drops the helper
    # ordinal so callers never see it.
    # Safe (sqlalchemy-execute-raw-query): DuckDB in-memory SELECT; the table
    # name + ordinal column are fixed module constants and the limit is an int.
    sql = (
        f"SELECT * EXCLUDE ({_ROWNUM_COLUMN}) FROM ("
        f"SELECT *, row_number() OVER () AS {_ROWNUM_COLUMN} FROM {_TABLE} "
        f"ORDER BY {_ROWNUM_COLUMN} DESC LIMIT {limit}"
        f") ORDER BY {_ROWNUM_COLUMN} ASC"
    )
    return engine.query(sql)


def _sample_from_tail(context: dict[str, Any] | None) -> bool:
    """True when the op declared a tail/newest-last result ordering.

    The connector author registers ``llm_instructions["result_ordering"] =
    {"sample": "tail"}`` on a chronologically-ordered op (``k8s.logs``); the
    dispatcher forwards that dict verbatim under
    ``context[_ORDERING_CONTEXT_KEY]``. Absent / malformed / any other value
    keeps the head-first default -- the hint is purely additive, so an op
    without it behaves exactly as before. A non-dict or unexpected value is
    logged once (actionable for the connector author) and treated as "no
    tail ordering" rather than raised, matching the never-raise discipline
    the sibling pagination-hint path follows.
    """
    if not context:
        return False
    raw = context.get(_ORDERING_CONTEXT_KEY)
    if raw is None:
        return False
    if not isinstance(raw, dict):
        _log.warning(
            "jsonflux_result_ordering_invalid_shape",
            op_id=context.get("op_id"),
            received_type=type(raw).__name__,
        )
        return False
    return raw.get("sample") == _SAMPLE_TAIL


def _serialize(payload: Any) -> bytes:
    """Serialize *payload* to JSON bytes for the byte-threshold check.

    Uses msgspec (already a jsonflux dependency) for speed; falls back
    to ``str`` bytes for any payload msgspec can't encode so the byte
    check never raises.
    """
    try:
        return msgspec.json.encode(payload)
    except (TypeError, msgspec.EncodeError):
        return str(payload).encode("utf-8", "replace")


def _build_fetch_more(context: dict[str, Any] | None) -> FetchMore:
    """Build the :class:`FetchMore` envelope from the reducer's *context*.

    G0.15-T8 (#1219). The contract is **always shipped** on a
    reducing-response handle so the agent never has to guess whether
    the envelope teaches it how to fetch more rows. Two branches:

    * ``drill_in.available`` is always ``False`` in this meho version --
      no MCP tool / resource URI / REST route / CLI verb addresses the
      handle. The rationale string surfaces the workaround verbatim
      (re-call with narrower params via ``native_pagination``). When
      a future Task ships the drill-in surface, it flips
      ``available=True`` and populates ``mcp_resource_uri`` /
      ``mcp_tool`` / ``example_call`` / ``expires_at`` in the same
      envelope -- no consumer needs to re-parse.
    * ``native_pagination`` is populated from the op's
      :class:`PaginationHint` (registered under
      ``llm_instructions.pagination_hint`` and threaded through the
      dispatcher as ``context["pagination_hint"]``). When no hint
      exists -- or the hint cannot be validated -- the envelope still
      ships, with ``available=False`` and a curated rationale; the
      response shape is uniform regardless of source.

    Invalid pagination hints (a connector authored a malformed dict)
    do **not** raise here: the reducer logs a structured warning and
    falls back to the unavailable branch. A reduce-time exception
    would otherwise convert into a ``connector_error``
    :class:`~meho_backplane.connectors.OperationResult` via the
    dispatcher's :func:`~meho_backplane.operations.dispatcher._reduce_or_error`
    guard -- failing a real read because an operator-facing metadata
    field had a typo is the wrong fail mode. The validation surfaces
    instead at op-registration time (the connector author writes the
    hint as a :class:`PaginationHint` literal there, not as a free
    dict).
    """
    drill_in = FetchMoreDrillIn(
        available=False,
        rationale=_DRILL_IN_UNAVAILABLE_RATIONALE,
    )
    native = _native_pagination_from_context(context)
    return FetchMore(drill_in=drill_in, native_pagination=native)


def _native_pagination_from_context(
    context: dict[str, Any] | None,
) -> FetchMoreNativePagination:
    """Return the :class:`FetchMoreNativePagination` branch for *context*.

    Pull order:

    1. ``context["pagination_hint"]`` already-validated
       :class:`PaginationHint` instance -- the reducer copies its
       ``params`` + ``example_next_call`` verbatim.
    2. ``context["pagination_hint"]`` plain dict (the dispatcher reads
       the descriptor's ``llm_instructions`` JSON, which lives as
       primitive Python types) -- validated through
       :class:`PaginationHint` here; the dispatcher does not import
       Pydantic for this codepath.
    3. Absent / invalid -- ``available=False`` with the curated
       rationale.
    """
    hint = _resolve_pagination_hint(context)
    if hint is None:
        return FetchMoreNativePagination(
            available=False,
            rationale=_NATIVE_PAGINATION_UNAVAILABLE_RATIONALE,
        )
    return FetchMoreNativePagination(
        available=True,
        params=hint.params,
        example_next_call=hint.example_next_call,
    )


def _resolve_pagination_hint(context: dict[str, Any] | None) -> PaginationHint | None:
    """Coerce ``context['pagination_hint']`` into a :class:`PaginationHint` or ``None``.

    Tolerates three shapes per :func:`_native_pagination_from_context`'s
    pull order. A validation failure on the dict branch is logged at
    warning level and converted to ``None`` -- the reduce path stays
    on the happy path; the connector author sees the warning in
    structured logs and fixes the metadata. The op_id (when carried
    in *context*) names the offender so the warning is actionable.
    """
    if not context:
        return None
    raw = context.get("pagination_hint")
    if raw is None:
        return None
    if isinstance(raw, PaginationHint):
        return raw
    if not isinstance(raw, dict):
        _log.warning(
            "jsonflux_pagination_hint_invalid_shape",
            op_id=context.get("op_id"),
            received_type=type(raw).__name__,
        )
        return None
    try:
        return PaginationHint.model_validate(raw)
    except ValidationError as exc:
        _log.warning(
            "jsonflux_pagination_hint_validation_failed",
            op_id=context.get("op_id"),
            errors=exc.errors(),
        )
        return None
