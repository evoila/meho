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
handle's result field catalog (unknown field → rejected, Kubernetes
field-selector style), operators and aggregate functions are fixed
allow-lists, and every caller value is bound as a DuckDB prepared-statement
parameter — never string-interpolated. The only value ever interpolated
into the SQL text is the ``LIMIT`` (a validated, bounded integer) and the
column identifiers (each a catalog-generated internal name or a
compiler-generated alias, double-quoted with embedded quotes escaped).

Output plan (#3635)
===================

Caller field names never reach the SQL text. Each exact, case-sensitive
field name resolves through the :class:`ResultFieldCatalog` to its
generated internal column, so ``Foo`` and ``foo`` stay distinct. Every
output column gets a generated alias, and :class:`OutputPlan` maps cursor
rows to public names by position. A flat (non-aggregate, non-grouped)
query selects only the hidden row ordinal (:data:`ROW_ORDINAL`); the
output is then rebuilt from the raw stored rows, so values come back
exactly as captured, not as the relational projection typed them.

Bounds
======

Filter predicates ≤ 10, aggregate outputs ≤ 8, ``group_by`` ≤ 4,
``order_by`` ≤ 4, ``select`` projection ≤ 64 columns, and each ``IN`` value
list ≤ 1000 elements (all
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

import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from meho_backplane.jsonflux.query.result_catalog import ResultField, ResultFieldCatalog

__all__ = [
    "RESULT_TABLE",
    "ROW_ORDINAL",
    "Aggregate",
    "CompiledQuery",
    "FilterPredicate",
    "OrderBy",
    "OutputPlan",
    "QueryContractError",
    "ResultQuerySpec",
    "compile_query",
]

#: The in-memory table the handle's rows are registered under. Must match
#: the reducer's ``_TABLE`` (``meho_backplane.operations.jsonflux_reducer``)
#: — a fixed wire constant, kept as a literal here so the vendored
#: ``jsonflux`` package needs no ``operations`` import.
RESULT_TABLE = "result"

#: Hidden column holding each row's position in the stored row list. A flat
#: query selects only this column and the output is rebuilt from raw rows.
ROW_ORDINAL = "_rr_ord"

#: Prefix for generated output aliases (``_rr_o_0``, ``_rr_o_1``, ...).
_OUTPUT_ALIAS_PREFIX = "_rr_o_"

#: The DuckDB type the explicit projection uses for each catalog scalar
#: family. Filter literals are checked against it before binding.
_FAMILY_COLUMN_TYPES: dict[str, str] = {
    "boolean": "BOOLEAN",
    "integer": "BIGINT",
    "number": "DOUBLE",
    "string": "VARCHAR",
}

#: Plain-language text for each catalog reason a field cannot be queried.
_NOT_QUERYABLE_REASONS: dict[str, str] = {
    "null_only": "it holds only null values",
    "mixed_non_null_kinds": "its values have more than one JSON kind",
    "container_value": "its values are objects or lists",
    "integer_out_of_int64_range": "it has integers outside the 64-bit range",
    "non_finite_number": "it has non-finite numbers",
}

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

#: Cap on aggregate outputs. Each aggregate adds an expression and generated
#: alias to the caller-shaped SELECT, so bound the construction work in the
#: same way as the other query lists.
_MAX_AGGREGATES = 8

#: Maximum number of known columns included in an unknown-field remediation.
#: A result handle can be very wide; the error remains agent-readable.
_MAX_KNOWN_SUGGESTIONS = 20


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
    The list caps (``filter`` ≤ 10, ``aggregate`` ≤ 8, ``group_by`` ≤ 4,
    ``order_by`` ≤ 4, ``select`` ≤ 64, and each ``IN`` value list ≤ 1000)
    and the operator/aggregate allow-lists are enforced here, at construction;
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
        max_length=_MAX_AGGREGATES,
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


class OutputPlan(BaseModel):
    """How to turn fetched cursor rows into the public output rows.

    Grouped and aggregate queries map each cursor position to its public
    name in ``columns`` (names are unique; the compiler rejects duplicates).
    Flat queries (``ordinal=True``) fetch only :data:`ROW_ORDINAL` and copy
    the raw stored row: the whole row when ``select`` is ``None``, else the
    requested keys that the row actually has (absent stays absent).
    """

    model_config = ConfigDict(frozen=True)

    columns: tuple[str, ...] = ()
    ordinal: bool = False
    select: tuple[str, ...] | None = None

    def rehydrate(
        self,
        fetched: Sequence[Sequence[Any]],
        raw_rows: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """Build the public output rows from *fetched* cursor rows."""
        if not self.ordinal:
            return [{name: record[i] for i, name in enumerate(self.columns)} for record in fetched]
        output: list[dict[str, Any]] = []
        for record in fetched:
            raw = raw_rows[record[0]]
            if self.select is None:
                output.append(dict(raw))
            else:
                output.append({key: raw[key] for key in self.select if key in raw})
        return output


class CompiledQuery(BaseModel):
    """The output of :func:`compile_query`: one SELECT + its bound params.

    ``effective_limit`` is the output-row ceiling actually applied (after
    clamping ``spec.limit`` to ``max_limit``); the core caps the returned
    rows to it and reports ``truncated`` when the underlying result had more.
    ``output_plan`` maps the fetched rows to public output rows.
    """

    model_config = ConfigDict(frozen=True)

    sql: str
    params: list[Any]
    effective_limit: int
    output_plan: OutputPlan


def _quote_ident(name: str) -> str:
    """Double-quote a SQL identifier, escaping embedded double quotes.

    The name is always an exact match of a real column (validated before
    this is called) or a compiler-generated alias, so quoting here is
    belt-and-suspenders that also handles pathological column names
    (spaces, dots, embedded quotes) correctly.
    """
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def _require_known(field: str, known: Mapping[str, ResultField]) -> None:
    """Reject a field that is not a column on the handle's schema."""
    if field not in known:
        suggestions = sorted(known)[:_MAX_KNOWN_SUGGESTIONS]
        omitted = len(known) - len(suggestions)
        suffix = f" (and {omitted} more)" if omitted else ""
        raise QueryContractError(
            f"unknown field {field!r}: not a column on this result handle. "
            f"Known columns: {suggestions}{suffix}."
        )


def _require_queryable(field: str, known: Mapping[str, ResultField], role: str) -> str:
    """Return the quoted internal column for a field used in SQL.

    A field the catalog marks non-queryable is rejected before any SQL runs.
    It can still be returned with ``select``, which reads the raw rows.
    """
    _require_known(field, known)
    entry = known[field]
    if not entry.queryable:
        reason = entry.reason or "unknown"
        text = _NOT_QUERYABLE_REASONS.get(reason, reason)
        raise QueryContractError(
            f"field {field!r} cannot be used in {role}: {text} (reason: {reason}). "
            "Return it with `select`, or read the raw rows by paging (`offset` / `limit`)."
        )
    return _quote_ident(entry.column)


def _validate_filter_literal(field: str, column_type: str, value: Any) -> None:
    """Reject a value whose Python kind cannot represent a scalar column type.

    *column_type* is the DuckDB type the projection uses for the field's
    catalog scalar family; only those canonical scalar types are recognized.
    """
    normalized = column_type.upper()
    if normalized == "BOOLEAN":
        matches = isinstance(value, bool)
    elif normalized in {
        "TINYINT",
        "SMALLINT",
        "INTEGER",
        "BIGINT",
        "HUGEINT",
        "UTINYINT",
        "USMALLINT",
        "UINTEGER",
        "UBIGINT",
        "UHUGEINT",
        "FLOAT",
        "REAL",
        "DOUBLE",
    } or re.fullmatch(r"DECIMAL\(\d+,\s*\d+\)", normalized):
        matches = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif normalized == "VARCHAR":
        matches = isinstance(value, str)
    else:
        return
    if not matches:
        raise QueryContractError(
            f"filter value for field {field!r} with column type {column_type!r} "
            f"must be a compatible scalar, got {type(value).__name__}."
        )


def _build_where(
    predicates: Sequence[FilterPredicate],
    known: Mapping[str, ResultField],
    params: list[Any],
) -> str:
    """Compile the predicate list into a parameterized ``WHERE`` body."""
    terms: list[str] = []
    for pred in predicates:
        ident = _require_queryable(pred.field, known, "`filter`")
        family = known[pred.field].scalar_family
        column_type = _FAMILY_COLUMN_TYPES.get(family) if family is not None else None
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
            if column_type is not None:
                for value in pred.value:
                    _validate_filter_literal(pred.field, column_type, value)
            placeholders = ", ".join("?" for _ in pred.value)
            terms.append(f"{ident} IN ({placeholders})")
            params.extend(pred.value)
        else:
            if pred.value is None or isinstance(pred.value, (list, dict)):
                raise QueryContractError(
                    f"operator {pred.op!r} on field {pred.field!r} needs a scalar value."
                )
            if column_type is not None:
                _validate_filter_literal(pred.field, column_type, pred.value)
            terms.append(f"{ident} {pred.op} ?")
            params.append(pred.value)
    return " AND ".join(terms)


def _aggregate_alias(agg: Aggregate) -> str:
    """Deterministic output name for an aggregate (``count`` / ``sum_field``)."""
    if agg.field is None:
        return agg.func.lower()
    return f"{agg.func.lower()}_{agg.field}"


def _output_alias(position: int) -> str:
    """Generated SQL alias for the output column at *position*."""
    return _quote_ident(f"{_OUTPUT_ALIAS_PREFIX}{position}")


def _build_grouped_projection(
    spec: ResultQuerySpec, known: Mapping[str, ResultField]
) -> tuple[str, str, OutputPlan]:
    """Compile the SELECT list + GROUP BY body for a grouped/aggregate query.

    Output columns are the ``group_by`` keys followed by the aggregate
    expressions; ``select`` is not allowed alongside ``aggregate`` (the
    output shape is fully determined by the keys + aggregates). Each output
    column gets a generated alias; the public names go into the output plan
    in the same order, and two equal public names are rejected.
    """
    if spec.aggregate and spec.select:
        raise QueryContractError(
            "`select` is not allowed with `aggregate`: the output columns are the "
            "`group_by` keys plus the aggregates."
        )
    select_parts: list[str] = []
    group_parts: list[str] = []
    public_names: list[str] = []
    for col in spec.group_by:
        ident = _require_queryable(col, known, "`group_by`")
        if col in public_names:
            raise QueryContractError(
                f"duplicate output column {col!r}; each output column must be distinct."
            )
        group_parts.append(ident)
        select_parts.append(f"{ident} AS {_output_alias(len(public_names))}")
        public_names.append(col)
    for agg in spec.aggregate:
        if agg.func == "COUNT" and agg.field is None:
            expr = "COUNT(*)"
        else:
            if agg.field is None:
                raise QueryContractError(
                    f"aggregate {agg.func} requires a `field` (only COUNT may omit it)."
                )
            expr = f"{agg.func}({_require_queryable(agg.field, known, '`aggregate`')})"
        alias = _aggregate_alias(agg)
        if alias in public_names:
            raise QueryContractError(
                f"duplicate aggregate output {alias!r}; each aggregate must be distinct."
            )
        select_parts.append(f"{expr} AS {_output_alias(len(public_names))}")
        public_names.append(alias)
    plan = OutputPlan(columns=tuple(public_names))
    return ", ".join(select_parts), ", ".join(group_parts), plan


def _build_projection(
    spec: ResultQuerySpec, known: Mapping[str, ResultField]
) -> tuple[str, str, OutputPlan]:
    """Compile the SELECT list, GROUP BY body, and output plan for any spec.

    Grouped and aggregate queries project the keys and aggregates. A flat
    query projects only the hidden row ordinal; the output plan rebuilds
    each row from the raw stored rows (``select`` keys, or the whole row).
    A ``select`` field only needs to exist: it is read raw, so a field that
    cannot be queried can still be returned.
    """
    if spec.aggregate or spec.group_by:
        return _build_grouped_projection(spec, known)
    for col in spec.select:
        _require_known(col, known)
    plan = OutputPlan(ordinal=True, select=tuple(spec.select) if spec.select else None)
    return _quote_ident(ROW_ORDINAL), "", plan


def _build_order(spec: ResultQuerySpec, known: Mapping[str, ResultField]) -> list[str]:
    """Compile the ``ORDER BY`` terms, validating each against the schema.

    In a grouped/aggregated query, an order-by column must be one of the
    ``group_by`` keys (ordering by a non-grouped raw column is not valid
    SQL); the check is done here so the caller gets a clear contract error
    rather than an opaque DuckDB binder error.
    """
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
        ident = _require_queryable(term.field, known, "`order_by`")
        parts.append(f"{ident} {'DESC' if term.direction == 'desc' else 'ASC'}")
    return parts


def compile_query(
    spec: ResultQuerySpec,
    catalog: ResultFieldCatalog,
    *,
    max_limit: int,
) -> CompiledQuery:
    """Compile *spec* into one parameterized, read-only ``SELECT``.

    *catalog* is the handle's result field catalog, built from every stored
    row. Every referenced field must be an exact, case-sensitive catalog
    name; a field used in ``filter`` / ``order_by`` / ``group_by`` /
    ``aggregate`` must also be queryable, and resolves to its generated
    internal column. *max_limit* is the output-row ceiling (single-sourced in
    :mod:`meho_backplane.operations.result_query`); ``spec.limit`` is clamped
    to it. The statement selects one extra row over the ceiling so the core
    can set ``truncated`` when the underlying result had more rows than fit.

    Raises :class:`QueryContractError` on any field-vs-catalog or
    value-shape violation. Filter literals are checked against the field's
    scalar family without coercion. The returned SQL is always a single
    ``SELECT`` over :data:`RESULT_TABLE` with no trailing statement separator.
    """
    known = {entry.name: entry for entry in catalog.fields}
    params: list[Any] = []

    where_sql = _build_where(spec.filter, known, params)
    select_sql, group_sql, output_plan = _build_projection(spec, known)
    order_parts = _build_order(spec, known)
    if output_plan.ordinal:
        # Stored order breaks ties, so a flat result is deterministic.
        order_parts.append(_quote_ident(ROW_ORDINAL))

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
    if order_parts:
        sql += f" ORDER BY {', '.join(order_parts)}"
    sql += f" LIMIT {fetch_limit}"

    return CompiledQuery(
        sql=sql,
        params=params,
        effective_limit=effective_limit,
        output_plan=output_plan,
    )
