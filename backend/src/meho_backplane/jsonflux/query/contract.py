# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bounded, validated query contract for the ``result_query`` read-back surface.

This module turns a small, typed argument grammar
(:class:`ResultQuerySpec`) into **exactly one parameterized, read-only
``SELECT``** over the single Arrow-registered handle table
(:data:`RESULT_TABLE`) inside the hardened JSONFlux
:class:`~meho_backplane.jsonflux.query.engine.QueryEngine`.

Why compile, not sanitize (#3366)
=================================

The alternative — accept a caller SQL string and try to prove it safe —
was rejected against the locked ``duckdb==1.5.5``: a ``:memory:`` database
cannot be opened ``read_only`` (raises ``CatalogException``); with
``enable_external_access=false`` an in-memory ``CREATE TABLE ... AS SELECT``
still succeeds; and ``execute("A; B")`` silently runs both statements. A
raw-SQL contract would therefore force MEHO to build and maintain a SQL
parser to close holes that compiling from a fixed template avoids by
construction. Here the unsafe forms are simply **not expressible**: there
is no ``sql=`` field, every referenced field is checked against the
handle's known column set (unknown field → rejected, Kubernetes
field-selector style), operators and aggregate functions are fixed
allow-lists, and every caller value is bound as a DuckDB prepared-statement
parameter — never string-interpolated. The only value ever interpolated
into the SQL text is the ``LIMIT`` (a validated, bounded integer) and the
column identifiers (each an exact match of a real column, double-quoted
with embedded quotes escaped).

Bounds
======

Filter predicates ≤ 10, ``group_by`` ≤ 4, ``order_by`` ≤ 4, ``select``
projection ≤ 64 columns, and each ``IN`` value list ≤ 1000 elements (all
rejected at model construction). The ``select`` and ``IN`` caps bound the
compile-time expansion — one quoted identifier per projected column, one
bound placeholder per ``IN`` element — so a caller cannot force an
arbitrarily large (though still single, still parameterized) ``SELECT``.
Operator allow-list ``=, !=, <, <=, >, >=, IN, IS NULL``; aggregate
allow-list ``COUNT, SUM, MIN, MAX, AVG``. The output-row
ceiling (``MAX_LIMIT``) is single-sourced in
:mod:`meho_backplane.operations.result_query` and passed to
:func:`compile_query` as ``max_limit`` — this module never imports it, to
keep the vendored ``jsonflux`` package free of an ``operations`` back-edge.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "RESULT_TABLE",
    "Aggregate",
    "CompiledQuery",
    "FilterPredicate",
    "OrderBy",
    "QueryContractError",
    "ResultQuerySpec",
    "compile_query",
]

#: The in-memory table the handle's rows are registered under. Must match
#: the reducer's ``_TABLE`` (``meho_backplane.operations.jsonflux_reducer``)
#: — a fixed wire constant, kept as a literal here so the vendored
#: ``jsonflux`` package needs no ``operations`` import.
RESULT_TABLE = "result"

#: Filter operators the compiler will emit. A fixed allow-list: a value
#: outside it is rejected at model construction (``Literal``), so an
#: injection attempt via the operator slot is not expressible.
FilterOperator = Literal["=", "!=", "<", "<=", ">", ">=", "IN", "IS NULL"]

#: Aggregate functions the compiler will emit. Fixed allow-list, same
#: reasoning as :data:`FilterOperator`.
AggregateFunc = Literal["COUNT", "SUM", "MIN", "MAX", "AVG"]

#: Sort directions. Compiled to the literal ``ASC`` / ``DESC`` keyword, so
#: this slot can never carry arbitrary text either.
SortDirection = Literal["asc", "desc"]

#: Operators that take no value (the value slot must be omitted).
_VALUELESS_OPERATORS: frozenset[str] = frozenset({"IS NULL"})

#: Operators whose value must be a non-empty list (expanded to one bound
#: placeholder per element).
_LIST_OPERATORS: frozenset[str] = frozenset({"IN"})

#: Cap on an ``IN`` value list. Each element compiles to one bound
#: placeholder (``_build_where``), so an unbounded list would expand the
#: single ``SELECT`` to as many placeholders as the caller supplies — the
#: statement stays one parameterized SELECT, but the compile-time expansion
#: is unbounded. Capped at construction, mirroring the ``filter`` /
#: ``group_by`` / ``order_by`` list caps.
_MAX_IN_VALUES = 1000

#: Cap on the ``select`` projection. Each entry compiles to one quoted
#: identifier; same unbounded-expansion reasoning as :data:`_MAX_IN_VALUES`.
#: Omitting ``select`` still projects the handle's full (non-caller-shaped)
#: schema via ``SELECT *``, so this bounds only caller-driven expansion.
_MAX_SELECT_COLUMNS = 64


class QueryContractError(ValueError):
    """A structured query cannot be compiled against this handle's schema.

    Raised by :func:`compile_query` for violations that need the handle's
    actual column set to detect — an unknown field, an aggregate over a
    missing column, an order-by column not covered by ``group_by`` in a
    grouped query, a malformed predicate value. Model-shape violations
    (caps, operator/aggregate allow-lists, ``extra="forbid"``) are caught
    earlier, at :class:`ResultQuerySpec` construction, as a pydantic
    ``ValidationError``. Each transport maps this to its own recoverable
    error (MCP ``-32602``; REST ``422``) so the caller can narrow and retry.
    """


class FilterPredicate(BaseModel):
    """One ``WHERE`` clause term: ``field op value``.

    ``value`` is omitted for ``IS NULL``, a list for ``IN`` (each element
    bound as its own placeholder), and a scalar otherwise. The value is
    always bound as a DuckDB prepared-statement parameter — never
    interpolated — so it cannot alter the statement's structure.
    """

    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1, description="Column to test; must exist on the handle.")
    op: FilterOperator = Field(description="Comparison operator (fixed allow-list).")
    value: Any = Field(
        default=None,
        description=(
            "The comparison value, bound as a parameter. Omit for `IS NULL`; "
            "a list for `IN`; a scalar for the ordering/equality operators."
        ),
    )

    @field_validator("value")
    @classmethod
    def _cap_in_values(cls, v: Any) -> Any:
        """Reject an over-long ``IN`` value list at construction.

        ``value`` is typed ``Any``, so ``max_length`` on the field cannot see
        the list length — the cap lives here. A list value is only meaningful
        for ``IN`` (any other operator rejects a list in ``_build_where``), so
        capping any list value is equivalent to capping the ``IN`` expansion.
        """
        if isinstance(v, list) and len(v) > _MAX_IN_VALUES:
            raise ValueError(f"IN list too long: {len(v)} values (max {_MAX_IN_VALUES}).")
        return v


class Aggregate(BaseModel):
    """One aggregate output column: ``func(field)`` (``COUNT`` may omit field).

    ``COUNT`` with no ``field`` compiles to ``COUNT(*)``; every other
    function requires a field. The output alias is derived deterministically
    (``count`` / ``sum_<field>`` / ...) and quoted, so callers never inject
    an alias identifier.
    """

    model_config = ConfigDict(extra="forbid")

    func: AggregateFunc = Field(description="Aggregate function (fixed allow-list).")
    field: str | None = Field(
        default=None,
        description="Column to aggregate; omit only for COUNT (compiles to COUNT(*)).",
    )


class OrderBy(BaseModel):
    """One ``ORDER BY`` term: a known column plus a direction."""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1, description="Column to sort by; must exist on the handle.")
    direction: SortDirection = Field(default="asc", description="Sort direction.")


class ResultQuerySpec(BaseModel):
    """The structured, validated query arguments for a single handle.

    Every field is optional: an empty spec compiles to ``SELECT * FROM
    result LIMIT <max>`` — a full read-back capped at the output ceiling.
    The list caps (``filter`` ≤ 10, ``group_by`` ≤ 4, ``order_by`` ≤ 4,
    ``select`` ≤ 64, and each ``IN`` value list ≤ 1000) and the
    operator/aggregate allow-lists are enforced here, at construction;
    field-vs-schema validation needs the handle's columns and happens in
    :func:`compile_query`.
    """

    model_config = ConfigDict(extra="forbid")

    filter: list[FilterPredicate] = Field(
        default_factory=list,
        max_length=10,
        description="Predicates AND-ed into the WHERE clause (max 10).",
    )
    select: list[str] = Field(
        default_factory=list,
        max_length=_MAX_SELECT_COLUMNS,
        description=(
            "Projection: columns to return (max 64). Omit for all columns. Not "
            "allowed together with `aggregate` (the output is then the group "
            "keys plus the aggregates)."
        ),
    )
    group_by: list[str] = Field(
        default_factory=list,
        max_length=4,
        description="Columns to group by (max 4).",
    )
    aggregate: list[Aggregate] = Field(
        default_factory=list,
        description="Aggregate output columns (COUNT/SUM/MIN/MAX/AVG).",
    )
    order_by: list[OrderBy] = Field(
        default_factory=list,
        max_length=4,
        description="Sort terms (max 4).",
    )
    limit: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Max output rows. Omitted or above the server ceiling clamps to "
            "the ceiling (500); the result flags `truncated` when more rows existed."
        ),
    )

    def is_empty(self) -> bool:
        """True when no query dimension is set (a pure full read-back)."""
        return not (self.filter or self.select or self.group_by or self.aggregate or self.order_by)


class CompiledQuery(BaseModel):
    """The output of :func:`compile_query`: one SELECT + its bound params.

    ``effective_limit`` is the output-row ceiling actually applied (after
    clamping ``spec.limit`` to ``max_limit``); the core caps the returned
    rows to it and reports ``truncated`` when the underlying result had more.
    """

    model_config = ConfigDict(frozen=True)

    sql: str
    params: list[Any]
    effective_limit: int


def _quote_ident(name: str) -> str:
    """Double-quote a SQL identifier, escaping embedded double quotes.

    The name is always an exact match of a real column (validated before
    this is called) or a compiler-generated alias, so quoting here is
    belt-and-suspenders that also handles pathological column names
    (spaces, dots, embedded quotes) correctly.
    """
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def _require_known(field: str, known: set[str]) -> None:
    """Reject a field that is not a column on the handle's schema."""
    if field not in known:
        raise QueryContractError(
            f"unknown field {field!r}: not a column on this result handle. "
            f"Known columns: {sorted(known)}."
        )


def _build_where(
    predicates: Sequence[FilterPredicate],
    known: set[str],
    params: list[Any],
) -> str:
    """Compile the predicate list into a parameterized ``WHERE`` body."""
    terms: list[str] = []
    for pred in predicates:
        _require_known(pred.field, known)
        ident = _quote_ident(pred.field)
        if pred.op in _VALUELESS_OPERATORS:
            if pred.value is not None:
                raise QueryContractError(
                    f"operator {pred.op!r} on field {pred.field!r} takes no value; omit `value`."
                )
            terms.append(f"{ident} IS NULL")
        elif pred.op in _LIST_OPERATORS:
            if not isinstance(pred.value, list) or not pred.value:
                raise QueryContractError(
                    f"operator 'IN' on field {pred.field!r} needs a non-empty list value."
                )
            placeholders = ", ".join("?" for _ in pred.value)
            terms.append(f"{ident} IN ({placeholders})")
            params.extend(pred.value)
        else:
            if pred.value is None or isinstance(pred.value, (list, dict)):
                raise QueryContractError(
                    f"operator {pred.op!r} on field {pred.field!r} needs a scalar value."
                )
            terms.append(f"{ident} {pred.op} ?")
            params.append(pred.value)
    return " AND ".join(terms)


def _aggregate_alias(agg: Aggregate) -> str:
    """Deterministic output name for an aggregate (``count`` / ``sum_field``)."""
    if agg.field is None:
        return agg.func.lower()
    return f"{agg.func.lower()}_{agg.field}"


def _build_aggregate_projection(spec: ResultQuerySpec, known: set[str]) -> tuple[str, str]:
    """Compile the SELECT list + GROUP BY body for an aggregate query.

    Output columns are the ``group_by`` keys followed by the aggregate
    expressions; ``select`` is not allowed alongside ``aggregate`` (the
    output shape is fully determined by the keys + aggregates).
    """
    if spec.select:
        raise QueryContractError(
            "`select` is not allowed with `aggregate`: the output columns are the "
            "`group_by` keys plus the aggregates."
        )
    select_parts: list[str] = []
    seen_aliases: set[str] = set()
    for col in spec.group_by:
        _require_known(col, known)
        select_parts.append(_quote_ident(col))
    for agg in spec.aggregate:
        if agg.func == "COUNT" and agg.field is None:
            expr = "COUNT(*)"
        else:
            if agg.field is None:
                raise QueryContractError(
                    f"aggregate {agg.func} requires a `field` (only COUNT may omit it)."
                )
            _require_known(agg.field, known)
            expr = f"{agg.func}({_quote_ident(agg.field)})"
        alias = _aggregate_alias(agg)
        if alias in seen_aliases:
            raise QueryContractError(
                f"duplicate aggregate output {alias!r}; each aggregate must be distinct."
            )
        seen_aliases.add(alias)
        select_parts.append(f"{expr} AS {_quote_ident(alias)}")
    group_sql = ", ".join(_quote_ident(col) for col in spec.group_by)
    return ", ".join(select_parts), group_sql


def _build_projection(spec: ResultQuerySpec, known: set[str]) -> tuple[str, str]:
    """Compile the SELECT list and GROUP BY body for any spec.

    Three shapes: aggregate (keys + aggregates), plain ``group_by`` (the
    distinct key combinations), or a flat projection (``select`` columns, or
    ``*`` when omitted).
    """
    if spec.aggregate:
        return _build_aggregate_projection(spec, known)
    if spec.group_by:
        for col in spec.group_by:
            _require_known(col, known)
        cols = ", ".join(_quote_ident(col) for col in spec.group_by)
        return cols, cols
    if spec.select:
        for col in spec.select:
            _require_known(col, known)
        return ", ".join(_quote_ident(col) for col in spec.select), ""
    return "*", ""


def _build_order(spec: ResultQuerySpec, known: set[str]) -> str:
    """Compile the ``ORDER BY`` body, validating each term against the schema.

    In a grouped/aggregated query, an order-by column must be one of the
    ``group_by`` keys (ordering by a non-grouped raw column is not valid
    SQL); the check is done here so the caller gets a clear contract error
    rather than an opaque DuckDB binder error.
    """
    if not spec.order_by:
        return ""
    grouped = bool(spec.group_by or spec.aggregate)
    group_cols = set(spec.group_by)
    parts: list[str] = []
    for term in spec.order_by:
        _require_known(term.field, known)
        if grouped and term.field not in group_cols:
            raise QueryContractError(
                f"cannot order by {term.field!r} in a grouped query: order only by a "
                "`group_by` key."
            )
        parts.append(f"{_quote_ident(term.field)} {'DESC' if term.direction == 'desc' else 'ASC'}")
    return ", ".join(parts)


def compile_query(
    spec: ResultQuerySpec,
    columns: Sequence[str],
    *,
    max_limit: int,
) -> CompiledQuery:
    """Compile *spec* into one parameterized, read-only ``SELECT``.

    *columns* is the handle's known column set (from the registered table's
    ``DESCRIBE``); every referenced field is checked against it. *max_limit*
    is the output-row ceiling (single-sourced in
    :mod:`meho_backplane.operations.result_query`); ``spec.limit`` is clamped
    to it. The statement selects one extra row over the ceiling so the core
    can set ``truncated`` when the underlying result had more rows than fit.

    Raises :class:`QueryContractError` on any field-vs-schema or
    value-shape violation. The returned SQL is always a single ``SELECT``
    over :data:`RESULT_TABLE` with no trailing statement separator.
    """
    known = set(columns)
    params: list[Any] = []

    where_sql = _build_where(spec.filter, known, params)
    select_sql, group_sql = _build_projection(spec, known)
    order_sql = _build_order(spec, known)

    effective_limit = max_limit if spec.limit is None else min(spec.limit, max_limit)
    # +1 so the core can detect "more rows existed than the ceiling admits"
    # and flag ``truncated`` without a second count query. The value is a
    # validated, bounded int — the only integer ever interpolated here.
    fetch_limit = effective_limit + 1

    sql = f"SELECT {select_sql} FROM {_quote_ident(RESULT_TABLE)}"
    if where_sql:
        sql += f" WHERE {where_sql}"
    if group_sql:
        sql += f" GROUP BY {group_sql}"
    if order_sql:
        sql += f" ORDER BY {order_sql}"
    sql += f" LIMIT {fetch_limit}"

    return CompiledQuery(sql=sql, params=params, effective_limit=effective_limit)
