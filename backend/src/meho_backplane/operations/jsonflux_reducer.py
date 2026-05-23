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

from meho_backplane.connectors.schemas import ResultHandle
from meho_backplane.jsonflux.query.engine import QueryEngine

__all__ = ["JsonFluxReducer"]

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
        contract. ``schema`` and ``context`` are accepted to satisfy the
        Protocol; the reducer infers the materialized schema from the
        DuckDB-registered table rather than the descriptor schema.
        """
        del schema, context  # schema is inferred from the registered table

        envelope_key, rows = _detect_collection(payload)
        if rows is None:
            # Not a set-shaped payload (scalar, dict-of-scalars, None) —
            # nothing to reduce.
            return payload, None

        if not self._over_threshold(rows, payload):
            return payload, None

        return self._materialize(payload, envelope_key, rows)

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
    ) -> tuple[dict[str, Any], ResultHandle]:
        """Register *rows* in DuckDB and mint a handle + inline summary.

        A fresh :class:`QueryEngine` is created per call (in-memory,
        per-payload isolation) and closed before returning so no DuckDB
        connection leaks across dispatches.
        """
        table_rows = _normalize_rows(rows)
        engine = QueryEngine()
        try:
            engine.register(_TABLE, table_rows, unwrap="auto")
            total_rows = engine.tables[_TABLE]["row_count"]
            schema_ = _build_json_schema(engine)
            summary_md = engine.describe_tables(samples=self._sample_size)
            sample_rows = _query_sample(engine, self._sample_size)
        finally:
            engine.close()

        handle = ResultHandle(
            handle_id=uuid.uuid4(),
            summary_md=f"{total_rows} rows materialized as a JSONFlux handle.\n\n{summary_md}",
            schema_=schema_,
            total_rows=total_rows,
            sample_rows=tuple(sample_rows) if sample_rows else None,
            ttl_seconds=self._ttl_seconds,
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


def _query_sample(engine: QueryEngine, sample_size: int) -> list[dict[str, Any]]:
    """Return the first *sample_size* rows of the table as plain dicts."""
    if sample_size <= 0:
        return []
    # Safe (sqlalchemy-execute-raw-query): DuckDB in-memory SELECT; the
    # table name is the fixed module constant and the limit is an int.
    return engine.query(f"SELECT * FROM {_TABLE} LIMIT {int(sample_size)}")


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
