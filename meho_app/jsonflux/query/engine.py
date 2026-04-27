# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
from __future__ import annotations

import contextlib
import random
from pathlib import Path
from typing import Any

import duckdb
import msgspec
import pyarrow as pa

from ..core.analyzer import Analyzer, render_schema
from ..core.converter import normalize_data, summary_to_schema
from ..utils.sampling import ReservoirSampler

SQL_CODE_FENCE = "```sql"

# --------------------------
# Query result models
# --------------------------


class QueryResult(msgspec.Struct, gc=False):
    """Structured result from query execution."""

    success: bool
    """True if the query executed without error."""

    sql: str
    """The SQL query that was executed."""

    markdown: str
    """Full result formatted as a markdown table (empty string on error)."""

    error: str | None = None
    """Error message if success is False, None otherwise."""

    row_count: int = 0
    """Total number of rows in the result."""

    preview: str | None = None
    """First N rows as markdown table when split is requested, None otherwise."""


def _get_root_obj(summary: Any) -> dict[Any, Any] | None:
    """Unwrap a root array to get the object field map, or return obj directly."""
    obj: dict[Any, Any] | None = summary.obj
    if obj is None and summary.arr:
        ks = summary.arr.kind_summaries
        if "object" in ks and ks["object"].obj:
            obj = ks["object"].obj
    return obj


def _analyze_summary_fields(  # NOSONAR (cognitive complexity)
    summary: Any,
) -> tuple[list[tuple[str, str]], list[str], list[tuple[str, str]]]:
    """
    Analyze a table summary to categorize fields.

    Returns:
        (flat_fields, nested_fields, array_fields) where:
        - flat_fields: list of (name, type_str) for primitive fields
        - nested_fields: list of field names that are nested objects
        - array_fields: list of (name, element_kind) for array fields
    """
    flat: list[tuple[str, str]] = []
    nested: list[str] = []
    arrays: list[tuple[str, str]] = []

    obj = _get_root_obj(summary)
    if obj is None:
        return flat, nested, arrays

    for key, child in obj.items():
        has_prim = bool(child.primitives)
        has_obj = child.obj is not None and bool(child.obj)
        has_arr = child.arr is not None

        if has_arr:
            # Figure out element kind
            elem = "object"
            if child.arr.kind_summaries:
                kinds = list(child.arr.kind_summaries.keys())
                if kinds:
                    elem = kinds[0]
            elif child.arr.kind_minmax:
                for k, (_, mx) in child.arr.kind_minmax.items():
                    if mx > 0 and k != "object":
                        elem = k
                        break
            arrays.append((key, elem))
        elif has_obj:
            nested.append(key)
        elif has_prim:
            # Pick first primitive type
            for t in ("str", "int", "float", "bool"):
                if t in child.primitives:
                    flat.append((key, t))
                    break

    return flat, nested, arrays


def _collect_nested_id_fields(  # NOSONAR (cognitive complexity)
    summary: Any,
) -> list[tuple[str, str]]:
    """
    Scan one level of nested objects for primitive fields likely to be
    join keys (ending in ``_id`` or named ``id``).

    Returns a list of ``("parent.child", type_str)`` tuples using
    dot-notation paths that are valid in DuckDB SQL.
    """
    result: list[tuple[str, str]] = []
    obj = _get_root_obj(summary)
    if obj is None:
        return result

    for key, child in obj.items():
        if child.obj is None or not child.obj:
            continue
        # child is a nested object — scan its children
        for sub_key, sub_child in child.obj.items():
            sub_lower = sub_key.lower()
            if not (sub_lower.endswith("_id") or sub_lower == "id"):
                continue
            if sub_child.primitives:
                for t in ("str", "int", "float", "bool"):
                    if t in sub_child.primitives:
                        result.append((f"{key}.{sub_key}", t))
                        break
    return result


class QueryEngine:
    """
    SQL query engine for JSON files using DuckDB.

    Supports:
    - Multiple JSON files as tables
    - Full SQL: SELECT, JOIN, WHERE, GROUP BY, ORDER BY, etc.
    - Nested JSON access with dot notation
    - Array flattening with UNNEST

    Example:
        engine = QueryEngine()
        engine.register("products", products_data, path="$.catalog.products")
        engine.register("orders", orders_data)

        result = engine.query('''
            SELECT p.name, COUNT(*) as cnt
            FROM orders o
            JOIN products p ON o.product_id = p.product_id
            GROUP BY p.name
            ORDER BY cnt DESC
            LIMIT 10
        ''')
    """

    __slots__ = ("_closed", "conn", "tables")

    def __init__(self) -> None:
        self.conn = duckdb.connect(":memory:")
        self.tables: dict[str, dict[str, Any]] = {}
        self._closed = False

    def register(  # NOSONAR (cognitive complexity)
        self,
        name: str,
        source: str | Path | dict | list,
        path: str | None = None,
        description: str | None = None,
        unwrap: bool | str = "auto",
        append: bool = False,
    ) -> QueryEngine:
        """
        Register a JSON source as a queryable table using Arrow for type safety.

        Supports smart response shape detection for dict sources:

        - Flat array ``[{...}, ...]`` -> multi-row table (unchanged)
        - Wrapped collection ``{"results": [...], "total": N}`` -> unwrap
          list as main table, scalars/nested objects as ``{name}_meta``
        - Single flat object ``{"k": "v"}`` -> 1-row table with
          ``tier_hint="inline"``
        - Multi-collection ``{"pods": [...], "svcs": [...]}`` -> split into
          ``{name}_pods``, ``{name}_svcs`` tables

        Args:
            name: Table name to use in SQL queries
            source: File path, JSON string, dict, or list
            path: JSON path to extract (e.g., "$.catalog.products")
            description: Optional human-readable description of the table.
                Included in ``describe_tables()`` and ``generate_prompt()``
                output so LLMs understand the domain context.
            unwrap: Controls dict unwrapping behavior:
                - ``'auto'`` (default): heuristic shape detection
                - ``True``: force unwrap (find first list-of-dicts value)
                - ``False``: force 1-row table
            append: When ``True`` and a table with the same name already
                exists, concatenate new rows with existing data instead of
                replacing.  Metadata companion tables are always *replaced*
                (latest page wins).

        Returns:
            self (for chaining)
        """
        # Determine source type and load data
        if isinstance(source, (dict, list)):
            data = source
            source_desc = "memory"
        elif isinstance(source, (str, Path)):
            source_str = str(source)
            if source_str.strip().startswith("{") or source_str.strip().startswith("["):
                data = msgspec.json.decode(source_str)
                source_desc = "json_string"
            else:
                with open(source_str, "rb") as f:
                    data = msgspec.json.decode(f.read())
                source_desc = source_str
        else:
            raise TypeError(f"Unsupported source type: {type(source)}")

        if path:
            data = self._extract_path(data, path)

        if isinstance(data, dict):
            if unwrap == "auto":
                tables = self._detect_and_split(name, data, description)
                for tname, tdata, tdesc, tier_hint in tables:
                    # Only the main table (same name as requested) gets
                    # appended; companion tables (_meta, _tags, etc.) are
                    # always replaced so the latest metadata wins.
                    should_append = append and tname == name
                    self._register_table(
                        tname,
                        tdata,
                        tdesc,
                        append=should_append,
                        tier_hint=tier_hint,
                        source_desc=source_desc,
                        path=path,
                    )
                return self
            elif unwrap is True:
                # Force unwrap: find first list-of-dicts value
                for value in data.values():
                    if isinstance(value, list) and value and isinstance(value[0], dict):
                        data = value
                        break
                else:
                    data = [data]
            else:  # unwrap is False
                data = [data]

        if not isinstance(data, list):
            raise ValueError(f"Data at path must be array or object, got {type(data)}")

        self._register_table(
            name,
            data,
            description,
            append=append,
            tier_hint=None,
            source_desc=source_desc,
            path=path,
        )
        return self

    # ------------------------------------------------------------------
    # Shape detection helpers
    # ------------------------------------------------------------------

    def _detect_and_split(  # NOSONAR (cognitive complexity)
        self,
        name: str,
        data: dict,
        description: str | None,
    ) -> list[tuple[str, list[dict], str | None, str | None]]:
        """Split a dict response into appropriate table registrations.

        Classifies response shape and returns a list of
        ``(table_name, row_list, description, tier_hint)`` tuples.
        """
        list_of_dicts_keys: list[str] = []
        scalar_keys: dict[str, Any] = {}
        scalar_list_keys: dict[str, list] = {}
        nested_obj_keys: dict[str, dict] = {}

        for key, value in data.items():
            if isinstance(value, list):
                if not value:
                    # Empty list -- treat as collection (produces 0-row table)
                    list_of_dicts_keys.append(key)
                elif isinstance(value[0], dict):
                    list_of_dicts_keys.append(key)
                else:
                    scalar_list_keys[key] = value
            elif isinstance(value, dict):
                nested_obj_keys[key] = value
            else:
                scalar_keys[key] = value

        tables: list[tuple[str, list[dict], str | None, str | None]] = []

        if len(list_of_dicts_keys) == 0:
            # Single flat object -> 1-row table with inline tier hint
            tables.append((name, [data], description, "inline"))

        elif len(list_of_dicts_keys) == 1:
            # Wrapped collection -> unwrap main + metadata companion
            main_key = list_of_dicts_keys[0]
            tables.append((name, data[main_key], description, None))

            # Scalar lists get their own tables
            for sk, sv in scalar_list_keys.items():
                tables.append(
                    (
                        f"{name}_{sk}",
                        [{sk: item} for item in sv],
                        f"Scalar list from {name}.{sk}",
                        None,
                    )
                )

            # Build metadata companion from scalars + nested objects
            meta: dict[str, Any] = dict(scalar_keys)
            for nk, nv in nested_obj_keys.items():
                meta.update(self._flatten_metadata(nv, prefix=nk))
            if meta:
                tables.append((f"{name}_meta", [meta], f"Metadata for {name}", None))

        else:
            # Multiple collections -> split into {name}_{key} tables
            for key in list_of_dicts_keys:
                tables.append(
                    (
                        f"{name}_{key}",
                        data[key],
                        f"{key} from {name}",
                        None,
                    )
                )

            # Scalar lists get their own tables
            for sk, sv in scalar_list_keys.items():
                tables.append(
                    (
                        f"{name}_{sk}",
                        [{sk: item} for item in sv],
                        f"Scalar list from {name}.{sk}",
                        None,
                    )
                )

            # Remaining scalars + nested objects -> metadata
            meta = dict(scalar_keys)
            for nk2, nv2 in nested_obj_keys.items():
                meta.update(self._flatten_metadata(nv2, prefix=nk2))
            if meta:
                tables.append((f"{name}_meta", [meta], f"Metadata for {name}", None))

        return tables

    def _flatten_metadata(
        self, data: dict, prefix: str = ""
    ) -> dict[str, Any]:  # NOSONAR (cognitive complexity)
        """Flatten nested dicts to dot-notation keys.

        Skips list-of-dicts values (those are collections, not metadata).
        Scalar lists are kept as-is.
        """
        flat: dict[str, Any] = {}
        for key, value in data.items():
            full_key = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                flat.update(self._flatten_metadata(value, full_key))
            elif isinstance(value, list) and value and isinstance(value[0], dict):
                pass  # Skip list-of-dicts (collections, not metadata)
            else:
                flat[full_key] = value
        return flat

    def _register_table(
        self,
        name: str,
        data: list,
        description: str | None,
        *,
        append: bool = False,
        tier_hint: str | None = None,
        source_desc: str = "memory",
        path: str | None = None,
    ) -> None:
        """Register a list of dicts as a DuckDB table via Arrow.

        Handles schema analysis, normalization, Arrow table creation,
        DuckDB registration, and sample collection.  When *append* is
        ``True`` and the table already exists, new rows are concatenated
        with the existing table.
        """
        from ..core.models import Summary as SummaryModel

        # --- Arrow Content Integration ---
        analyzer = Analyzer()
        summary = analyzer.summarize(data, analyzer.max_depth)

        # Convert Summary to Arrow Schema
        if summary.arr:
            item_summary = summary.arr.kind_summaries.get("object") or SummaryModel()
            try:
                schema = summary_to_schema(item_summary)
            except ValueError:
                # Empty array with no schema info -> zero-column table
                schema = pa.schema([])
        elif summary.obj:
            schema = summary_to_schema(summary)
        else:
            # Completely empty / no structure -> zero-column table
            schema = pa.schema([])

        # Normalize data to match schema
        if schema and len(schema) > 0:
            norm_data = [normalize_data(row, schema) for row in data]
            new_table = pa.Table.from_pylist(norm_data, schema=schema)
        else:
            # DuckDB requires at least one column.  Create a minimal
            # placeholder so the table is queryable (0 rows).
            new_table = pa.table({"_empty": pa.array([], type=pa.int8())})

        if append and name in self.tables:
            # Append mode: concatenate with existing table
            # Safe (sqlalchemy-execute-raw-query): DuckDB in-memory query, table name from server-generated operation ID (not user input)
            result = self.conn.execute(f'SELECT * FROM "{name}"').arrow()
            # DuckDB may return RecordBatchReader; ensure we have a Table
            existing = result.read_all() if hasattr(result, "read_all") else result
            self.conn.unregister(name)
            combined = pa.concat_tables(
                [existing, new_table],
                promote_options="default",
            )
            self.conn.register(name, combined)
            row_count = len(combined)
        else:
            # Replace mode (default)
            if name in self.tables:
                self.conn.unregister(name)
            self.conn.register(name, new_table)
            row_count = len(new_table)

        # Eagerly collect samples so we can discard raw data
        sample_store: dict[tuple[str, ...], ReservoirSampler] = {}
        rng = random.Random(12345)  # noqa: S311 -- non-cryptographic context, random OK
        sample_analyzer = Analyzer()
        sample_analyzer.collect_samples(data, sample_store, k=3, rng=rng)

        table_info: dict[str, Any] = {
            "source": source_desc,
            "path": path,
            "row_count": row_count,
            "summary": summary,
            "sample_store": sample_store,
            "description": description,
        }
        if tier_hint is not None:
            table_info["tier_hint"] = tier_hint

        self.tables[name] = table_info

    def register_many(
        self,
        tables: dict[str, Any | tuple[Any, str | None]],
    ) -> QueryEngine:
        """
        Register multiple tables at once.

        Args:
            tables: Dict mapping table names to sources.
                - {"name": data} - direct data (dict or list)
                - {"name": "file.json"} - file path
                - {"name": ("file.json", "$.path")} - file with JSON path

        Returns:
            self (for chaining)
        """
        for name, source in tables.items():
            if isinstance(source, tuple):
                file_path, json_path = source
                self.register(name, file_path, path=json_path)
            else:
                self.register(name, source)
        return self

    def _extract_path(self, data: Any, path: str) -> Any:
        """Extract data at JSON path (simple $ notation)."""
        # Remove leading $ if present
        if path.startswith("$."):
            path = path[2:]
        elif path.startswith("$"):
            path = path[1:]

        if not path:
            return data

        # Navigate path
        parts = path.split(".")
        current = data
        for part in parts:
            # Handle array notation like items[]
            if part.endswith("[]"):
                part = part[:-2]
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list) and part.isdigit():
                current = current[int(part)]
            else:
                raise ValueError(f"Cannot navigate to '{part}' in path '{path}'")
            if current is None:
                raise ValueError(f"Path '{path}' not found in data")
        return current

    def query(self, sql: str) -> list[dict[str, Any]]:
        """Execute SQL query and return results as list of dicts."""
        result = self.conn.execute(sql)
        columns = [desc[0] for desc in result.description]
        return [dict(zip(columns, row, strict=False)) for row in result.fetchall()]

    def query_arrow(self, sql: str) -> Any:
        """Execute SQL and return as PyArrow Table."""
        return self.conn.execute(sql).arrow()

    def execute(self, sql: str) -> Any:
        """Execute SQL and return raw DuckDB result."""
        return self.conn.execute(sql)

    def explain(self, sql: str) -> str:
        """Show query execution plan."""
        # Safe (sqlalchemy-execute-raw-query): DuckDB in-memory EXPLAIN, sql from LLM-generated query (not direct user input)
        result = self.conn.execute(f"EXPLAIN {sql}").fetchall()
        return "\n".join(row[0] for row in result)

    def tables_info(self) -> str:
        """Show registered tables and their info."""
        lines = ["Registered tables:"]
        for name, info in self.tables.items():
            lines.append(f"  {name}:")
            lines.append(f"    source: {info.get('source', 'unknown')}")
            if info.get("path"):
                lines.append(f"    path: {info['path']}")
            lines.append(f"    rows: {info['row_count']:,}")
        return "\n".join(lines)

    def schema(self, table: str) -> str:
        """Show schema of a table."""
        # Safe (sqlalchemy-execute-raw-query): DuckDB in-memory DESCRIBE, table name from registered tables (not user input)
        result = self.conn.execute(f"DESCRIBE {table}").fetchall()
        lines = [f"Schema of '{table}':"]
        for row in result:
            lines.append(f"  {row[0]}: {row[1]}")
        return "\n".join(lines)

    def print_tables(self) -> None:
        """Print registered tables info."""
        print(self.tables_info())

    def print_schema(self, table: str) -> None:
        """Print table schema."""
        print(self.schema(table))

    def _get_sample_store(
        self,
        info: dict[str, Any],
        samples: int,
    ) -> dict[tuple[str, ...], ReservoirSampler] | None:
        """Return pre-collected sample store if samples are requested."""
        if samples <= 0:
            return None
        return info.get("sample_store")

    def describe_tables(
        self,
        samples: int | None = 3,
        max_sample_len: int = 60,
        max_schema_depth: int | None = 8,
    ) -> str:
        """
        Generate table schemas section for LLM consumption.
        Uses compact TypeScript-like schema format for maximum token efficiency.

        Args:
            samples: Number of sample values per field (0 or None = none)
            max_sample_len: Max length for sample value strings
            max_schema_depth: Maximum nesting depth to render in schemas.
                Structures deeper than this are collapsed to ``{...}`` /
                ``[...]`` to save tokens.  ``None`` means unlimited.

        Returns:
            Markdown-formatted string describing all registered tables.
        """
        samples_k = max(samples, 0) if samples is not None else 0

        lines: list[str] = []
        lines.append("## Available Tables\n")

        for table_name, info in self.tables.items():
            row_count = info.get("row_count", 0)
            description = info.get("description")
            if description:
                lines.append(f"### `{table_name}` ({row_count:,} rows) — {description}\n")
            else:
                lines.append(f"### `{table_name}` ({row_count:,} rows)\n")

            summary = info.get("summary")
            if summary:
                sample_store = self._get_sample_store(info, samples_k)
                schema_str = render_schema(
                    summary,
                    sample_store=sample_store,
                    max_sample_len=max_sample_len,
                    samples_k=samples_k,
                    max_depth=max_schema_depth,
                )
                lines.append("```typescript")
                lines.append(schema_str)
                lines.append("```")
            else:
                # Safe (sqlalchemy-execute-raw-query): DuckDB in-memory DESCRIBE, table_name from registered tables dict
                schema_raw = self.conn.execute(f"DESCRIBE {table_name}").fetchall()
                lines.append("| Column | Type |")
                lines.append("|--------|------|")
                for row in schema_raw:
                    lines.append(f"| {row[0]} | {row[1]} |")

            lines.append("")

        return "\n".join(lines)

    def _build_prompt(self, table_descriptions: str) -> str:  # NOSONAR (cognitive complexity)
        """
        Build the full system prompt with contextual examples derived from
        the actual registered table names and field names.
        """
        # Analyze all registered tables
        table_names = list(self.tables.keys())
        all_flat: dict[str, list[tuple[str, str]]] = {}
        all_nested: dict[str, list[str]] = {}
        all_arrays: dict[str, list[tuple[str, str]]] = {}

        for tname, info in self.tables.items():
            summary = info.get("summary")
            if summary:
                flat, nested, arrays = _analyze_summary_fields(summary)
                all_flat[tname] = flat
                all_nested[tname] = nested
                all_arrays[tname] = arrays

        has_nested = any(bool(v) for v in all_nested.values())
        has_arrays = any(bool(v) for v in all_arrays.values())
        has_multi = len(table_names) >= 2

        # Build enriched field set for join detection: flat fields +
        # one-level nested _id fields (e.g. orders.customer.customer_id)
        joinable: dict[str, list[tuple[str, str]]] = {}
        for tname, info in self.tables.items():
            summary = info.get("summary")
            fields = list(all_flat.get(tname, []))
            if summary:
                fields.extend(_collect_nested_id_fields(summary))
            joinable[tname] = fields

        lines: list[str] = []

        # -- Role & output rules --
        lines.append(
            "You are a DuckDB SQL query generator. "
            "Convert natural language data requests into SQL queries."
        )
        lines.append("")
        lines.append("RULES:")
        lines.append("- Return ONLY the raw SQL query text.")
        lines.append("- No explanations, no markdown fences, no code blocks, no comments.")
        lines.append("- If the request is ambiguous, make a reasonable choice and return the SQL.")
        lines.append("")

        # -- Schema notation (compact) --
        lines.append("## Schema Notation\n")
        lines.append("Schemas below use TypeScript-like notation:")
        lines.append("- `field: str` = string, `int` = integer, `float` = number, `bool` = boolean")
        lines.append("- `field: str?` = nullable (may be null)")
        lines.append("- `[{...}]` = array of objects, `[str]` = array of strings")
        if has_nested:
            # Use a real nested field for the dot notation example
            for tn, nl in all_nested.items():
                if nl:
                    summary = self.tables[tn].get("summary")
                    subs = self._get_nested_subfields(summary, nl[0])
                    if subs:
                        lines.append(f"- Nested objects use dot notation: `{tn}.{nl[0]}.{subs[0]}`")
                    else:
                        lines.append("- Nested objects use dot notation: `table_name.parent.child`")
                    break
        else:
            lines.append("- Nested objects use dot notation: `table_name.parent.child`")
        lines.append("")

        # -- Query patterns using REAL table/field names --
        lines.append("## Query Patterns\n")

        # Pattern: Basic SELECT
        # Pick first table with flat fields
        for tname, flat in all_flat.items():
            if len(flat) >= 2:
                f1, f2 = flat[0], flat[1]
                lines.append("### Basic Query")
                lines.append(SQL_CODE_FENCE)
                lines.append(f"SELECT {f1[0]}, {f2[0]} FROM {tname} ORDER BY {f1[0]} LIMIT 10")  # noqa: S608 -- static SQL query, no user input
                lines.append("```\n")
                break

        # Pattern: Nested objects (dot notation)
        if has_nested:
            for tname, nested_list in all_nested.items():
                if nested_list:
                    nfield = nested_list[0]
                    # Try to find sub-fields
                    summary = self.tables[tname].get("summary")
                    sub_fields = self._get_nested_subfields(summary, nfield)
                    if sub_fields:
                        sf1 = sub_fields[0]
                        lines.append("### Nested Objects (Dot Notation)")
                        lines.append("Access nested fields with dots:")
                        lines.append(SQL_CODE_FENCE)
                        lines.append(f"SELECT {nfield}.{sf1} FROM {tname}")  # noqa: S608 -- static SQL query, no user input
                        lines.append("```\n")
                    break

        # Pattern: UNNEST for arrays (CRITICAL)
        if has_arrays:
            for tname, arr_list in all_arrays.items():
                if arr_list:
                    arr_name, elem_kind = arr_list[0]
                    lines.append("### Arrays — CRITICAL")
                    lines.append(
                        "Array fields MUST be flattened with UNNEST "
                        "before grouping or aggregation.\n"
                    )
                    if elem_kind == "object":
                        # Get sub-fields of the array element
                        summary = self.tables[tname].get("summary")
                        arr_subfields = self._get_array_subfields(summary, arr_name)
                        if arr_subfields:
                            sf = arr_subfields[0]
                            lines.append(SQL_CODE_FENCE)
                            lines.append(f"SELECT item.{sf}, COUNT(*) as cnt")
                            lines.append(f"FROM (SELECT unnest({arr_name}) as item FROM {tname})")  # noqa: S608 -- static SQL query, no user input
                            lines.append(f"GROUP BY item.{sf}")
                            lines.append("```\n")
                        else:
                            lines.append(SQL_CODE_FENCE)
                            lines.append(f"SELECT unnest({arr_name}) as item FROM {tname}")  # noqa: S608 -- static SQL query, no user input
                            lines.append("```\n")
                    else:
                        lines.append(SQL_CODE_FENCE)
                        lines.append(f"SELECT unnest({arr_name}) as val FROM {tname}")  # noqa: S608 -- static SQL query, no user input
                        lines.append("```\n")

                    # Wrong/right example
                    lines.append(
                        "Always unnest first, then aggregate. "
                        "Never aggregate an array column directly."
                    )
                    lines.append("")
                    break

        # Pattern: JOINs (only if 2+ tables)
        if has_multi:
            lines.append("### JOINs")
            joined = ", ".join(f"`{t}`" for t in table_names)
            lines.append(f"Tables available for JOIN: {joined}")

            # Detect real FK/PK relationships (includes nested _id fields)
            join_hints = self._detect_join_keys(joinable)
            if join_hints:
                # Parse the first hint to generate a concrete example
                # Hints are "t1.col1 ↔ t2.col2 (both type)"
                hint = join_hints[0]
                left, rest = hint.split(" ↔ ", 1)
                right = rest.split(" (")[0]
                lt, lc = left.split(".", 1)
                rt, rc = right.split(".", 1)
                lines.append(SQL_CODE_FENCE)
                lines.append(f"SELECT a.*, b.* FROM {lt} a JOIN {rt} b ON a.{lc} = b.{rc}")  # noqa: S608 -- static SQL query, no user input
                lines.append("```")
                if len(join_hints) > 1:
                    lines.append("\nDetected join keys:")
                    for h in join_hints:
                        lines.append(f"- {h}")
                lines.append("")
            else:
                t1, t2 = table_names[0], table_names[1]
                lines.append("Check the schemas above for matching columns to join on.")
                lines.append(SQL_CODE_FENCE)
                lines.append(f"SELECT a.*, b.* FROM {t1} a JOIN {t2} b ON a.<key> = b.<key>")  # noqa: S608 -- static SQL query, no user input
                lines.append("```\n")

        # Pattern: UNNEST + JOIN (when array elements reference another table)
        if has_arrays and has_multi:
            unnest_join_done = False
            for tname, arr_list in all_arrays.items():
                if unnest_join_done:
                    break
                for arr_name, elem_kind in arr_list:
                    if elem_kind != "object":
                        continue
                    summary = self.tables[tname].get("summary")
                    arr_subs = self._get_array_subfields(summary, arr_name)
                    if not arr_subs:
                        continue
                    # Check if any array sub-field matches a column in
                    # another table (e.g. items.product_id ↔ products.product_id)
                    for other_t, other_cols in joinable.items():
                        if other_t == tname:
                            continue
                        for other_col, _ in other_cols:
                            other_leaf = other_col.rsplit(".", 1)[-1]
                            if other_leaf in arr_subs:
                                # Found: arr_subs field matches other table
                                # Pick a display field from the other table
                                # Prefer str fields like name/category over bools
                                disp = None
                                str_fields = [
                                    fc
                                    for fc, ft in all_flat.get(other_t, [])
                                    if ft == "str" and not fc.endswith("_id") and fc != "id"
                                ]
                                if str_fields:
                                    disp = str_fields[0]
                                else:
                                    for fc, _ in all_flat.get(other_t, []):
                                        if not fc.endswith("_id") and fc != "id":
                                            disp = fc
                                            break
                                disp = disp or other_leaf
                                lines.append("### UNNEST + JOIN")
                                lines.append(
                                    "When array elements reference another "
                                    "table, unnest first, then join:\n"
                                )
                                lines.append(SQL_CODE_FENCE)
                                lines.append(f"SELECT b.{disp}, COUNT(*) as cnt")
                                lines.append(
                                    f"FROM (SELECT unnest({arr_name}) as item FROM {tname}) sub"  # noqa: S608 -- static SQL query, no user input
                                )
                                lines.append(
                                    f"JOIN {other_t} b ON sub.item.{other_leaf} = b.{other_col}"
                                )
                                lines.append(f"GROUP BY b.{disp}")
                                lines.append("```\n")
                                unnest_join_done = True
                                break
                        if unnest_join_done:
                            break
                    if unnest_join_done:
                        break

        # -- DuckDB functions --
        lines.append("## DuckDB Functions\n")
        lines.append("- Aggregation: `SUM()`, `AVG()`, `COUNT()`, `MIN()`, `MAX()`")
        lines.append("- Strings: `LOWER()`, `UPPER()`, `CONTAINS()`, `LENGTH()`")
        lines.append("- Arrays: `UNNEST()`, `list_contains()`, `len()`, `array_agg()`")
        lines.append("- Math: `ROUND()`, `ABS()`, `CEIL()`, `FLOOR()`")
        lines.append("- Dates: `DATE_TRUNC()`, `EXTRACT()`, `CURRENT_DATE`")
        lines.append("- Window: `ROW_NUMBER()`, `RANK()`, `LAG()`, `LEAD()`")
        lines.append("- Conditional: `CASE WHEN ... THEN ... ELSE ... END`, `COALESCE()`")
        lines.append("")

        # -- Common mistakes (use real names when arrays exist) --
        lines.append("## Common Mistakes\n")
        if has_arrays:
            # Pick a real array field for concrete examples
            ex_tname = ""
            ex_arr = ""
            for tn, al in all_arrays.items():
                if al:
                    ex_tname, ex_arr = tn, al[0][0]
                    break
            lines.append(
                f"1. Forgetting UNNEST — array fields like `{ex_arr}` in "  # noqa: S608 -- static SQL query, no user input
                f"`{ex_tname}` MUST use "
                f"`SELECT unnest({ex_arr}) as item FROM {ex_tname}`"
            )
            lines.append("2. Missing alias after UNNEST — always write `unnest(col) as item`")
            lines.append("3. Using array indexing like `[0]` — use UNNEST instead")
        else:
            lines.append("1. If the data had array fields, they would require UNNEST")
        lines.append(
            f"{'4' if has_arrays else '2'}. Wrong dot notation path — check the schema carefully"
        )
        lines.append("")

        # -- Table descriptions --
        lines.append("---\n")
        lines.append("# YOUR DATA\n")
        lines.append(table_descriptions)

        return "\n".join(lines)

    def _get_nested_subfields(self, summary: Any, field_name: str) -> list[str]:
        """Get sub-field names from a nested object field."""
        obj = summary.obj
        if obj is None and summary.arr:
            ks = summary.arr.kind_summaries
            if "object" in ks and ks["object"].obj:
                obj = ks["object"].obj
        if obj is None or field_name not in obj:
            return []
        child = obj[field_name]
        if child.obj:
            return list(child.obj.keys())
        return []

    def _get_array_subfields(self, summary: Any, field_name: str) -> list[str]:
        """Get sub-field names from array-of-objects elements."""
        obj = summary.obj
        if obj is None and summary.arr:
            ks = summary.arr.kind_summaries
            if "object" in ks and ks["object"].obj:
                obj = ks["object"].obj
        if obj is None or field_name not in obj:
            return []
        child = obj[field_name]
        if child.arr and child.arr.kind_summaries:
            elem_sum = child.arr.kind_summaries.get("object")
            if elem_sum and elem_sum.obj:
                return list(elem_sum.obj.keys())
        return []

    def generate_prompt(
        self,
        samples: int | None = 3,
        max_sample_len: int = 60,
        max_schema_depth: int | None = 8,
    ) -> str:
        """
        Generate a complete system prompt for LLM SQL generation.

        The prompt is dynamically built from the registered tables: all SQL
        examples reference real table names and field names so the LLM never
        sees irrelevant placeholder tables.

        Args:
            samples: Number of sample values per field (0 or None = none)
            max_sample_len: Max length for sample value strings
            max_schema_depth: Maximum nesting depth for schemas (default 8).
                Deeply nested structures beyond this limit are collapsed to
                ``{...}`` / ``[...]`` to save tokens.

        Returns:
            Complete system prompt string ready to pass to an LLM.

        Example:
            ```python
            prompt = engine.generate_prompt(samples=3)
            # Use as system prompt for any LLM
            ```
        """
        table_desc = self.describe_tables(
            samples=samples,
            max_sample_len=max_sample_len,
            max_schema_depth=max_schema_depth,
        )
        return self._build_prompt(table_desc)

    def execute_query(
        self,
        sql: str,
        split: int | None = None,
        max_colwidth: int | None = 50,
    ) -> QueryResult:
        """
        Execute a SQL query and return a structured result.

        Designed for LLM tool workflows: always returns a result object
        with status, the SQL that was run, and markdown-formatted output.

        Args:
            sql: SQL query string to execute
            split: If set, ``preview`` will contain the first N rows as
                   markdown while ``markdown`` contains the full table.
                   None or <= 0 means no preview.
            max_colwidth: Max column width for markdown rendering
                          (None = unlimited)

        Returns:
            QueryResult with success/error status, SQL, and markdown tables.
        """
        from tabulate import tabulate

        try:
            result = self.conn.execute(sql)
            columns = [desc[0] for desc in result.description]
            rows = result.fetchall()
        except Exception as e:
            return QueryResult(
                success=False,
                sql=sql,
                markdown="",
                error=str(e),
            )

        row_count = len(rows)

        def _to_markdown(
            row_data: list[tuple[Any, ...]],
            cols: list[str],
        ) -> str:
            if max_colwidth is not None:

                def truncate(val: Any) -> Any:
                    if isinstance(val, str) and len(val) > max_colwidth:
                        return val[: max_colwidth - 1] + "…"
                    return val

                row_data = [tuple(truncate(v) for v in r) for r in row_data]
            return str(tabulate(row_data, headers=cols, tablefmt="github"))

        full_md = _to_markdown(rows, columns)

        preview_md: str | None = None
        if split is not None and split > 0 and row_count > 0:
            preview_md = _to_markdown(rows[:split], columns)

        return QueryResult(
            success=True,
            sql=sql,
            markdown=full_md,
            row_count=row_count,
            preview=preview_md,
        )

    def _detect_join_keys(
        self, schemas: dict[str, list[tuple[str, str]]]
    ) -> list[str]:  # NOSONAR (cognitive complexity)
        """Detect potential join keys between tables based on column names."""
        hints: list[str] = []
        table_names = list(schemas.keys())

        for i, t1 in enumerate(table_names):
            for t2 in table_names[i + 1 :]:
                cols1 = dict(schemas[t1])
                cols2 = dict(schemas[t2])

                # Look for join patterns
                for col1, dtype1 in cols1.items():
                    for col2, dtype2 in cols2.items():
                        if self._columns_might_join(col1, col2, dtype1, dtype2, t1, t2):
                            hints.append(f"{t1}.{col1} ↔ {t2}.{col2} (both {dtype1})")

        return hints

    def _columns_might_join(
        self, col1: str, col2: str, dtype1: str, dtype2: str, t1: str, t2: str
    ) -> bool:
        """
        Heuristic to detect if two columns might be joinable.

        Columns may be dot-notation paths (e.g. ``customer.customer_id``).
        Comparisons use the *leaf* name (part after the last dot).

        Detects:
        - Same leaf name ending in ``_id`` (e.g. both have ``customer_id``)
        - FK → PK patterns (e.g. ``products.id`` ↔ ``orders.product_id``)
        """
        # Must have compatible types
        if dtype1 != dtype2:
            return False

        # Use leaf name for comparison (e.g. "customer.customer_id" -> "customer_id")
        leaf1 = col1.rsplit(".", 1)[-1].lower()
        leaf2 = col2.rsplit(".", 1)[-1].lower()
        t1_lower = t1.lower().rstrip("s")  # "products" -> "product"
        t2_lower = t2.lower().rstrip("s")

        # Same leaf name ending in _id — very likely a join key
        # e.g., products.product_id ↔ orders.customer.customer_id is NOT a match,
        # but orders.customer.customer_id ↔ customers.customer_id IS
        if leaf1 == leaf2 and leaf1.endswith("_id"):
            return True

        # Skip other same-leaf matches (e.g. both have "name")
        if leaf1 == leaf2:
            return False

        # Pattern: table1.id ↔ table2.table1_id
        if leaf1 == "id" and leaf2 == f"{t1_lower}_id":
            return True
        if leaf2 == "id" and leaf1 == f"{t2_lower}_id":
            return True

        # Pattern: table1.x_id ↔ table2.id (where x matches table2 name)
        if leaf1.endswith("_id"):
            base = leaf1[:-3]
            if leaf2 == "id" and base == t2_lower:
                return True

        if leaf2.endswith("_id"):
            base = leaf2[:-3]
            if leaf1 == "id" and base == t1_lower:
                return True

        return False

    def _format_rows(
        self,
        columns: list[str],
        rows: list[tuple[Any, ...]],
        format: str = "grid",
        max_rows: int | None = None,
        max_colwidth: int | None = 50,
    ) -> str:
        """
        Format pre-fetched rows into a string.

        This is the shared formatting logic used by both ``format_query``
        and ``query_print``.
        """
        from tabulate import tabulate

        if max_rows is not None:
            rows = rows[:max_rows]

        # Truncate long string values if max_colwidth is set
        if max_colwidth is not None:

            def truncate(val: Any) -> Any:
                if isinstance(val, str) and len(val) > max_colwidth:
                    return val[: max_colwidth - 1] + "…"
                return val

            rows = [tuple(truncate(v) for v in row) for row in rows]

        if format == "csv":
            import csv
            import io

            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(columns)
            writer.writerows(rows)
            return output.getvalue()
        elif format == "json":
            data = [dict(zip(columns, row, strict=False)) for row in rows]
            return msgspec.json.encode(data).decode("utf-8")
        elif format in ("simple", "grid", "pipe", "markdown", "github"):
            tablefmt = "github" if format == "markdown" else format
            return str(tabulate(rows, headers=columns, tablefmt=tablefmt))
        else:
            raise ValueError(
                f"Unknown format: {format}. Use: simple, grid, pipe, markdown, csv, json"
            )

    def format_query(
        self,
        sql: str,
        format: str = "grid",
        max_rows: int | None = None,
        max_colwidth: int | None = 50,
    ) -> str:
        """
        Execute SQL and return formatted string.

        Args:
            sql: SQL query string
            format: Output format - "simple", "grid", "pipe", "markdown", "csv", "json"
            max_rows: Limit rows shown (None = all)
            max_colwidth: Max column width (None = unlimited)

        Returns:
            Formatted string, or an error message prefixed with "ERROR: "
            if the query fails.
        """
        try:
            result = self.conn.execute(sql)
            columns = [desc[0] for desc in result.description]
            rows = result.fetchall()
        except Exception as e:
            return f"ERROR: {e}"

        return self._format_rows(
            columns, rows, format=format, max_rows=max_rows, max_colwidth=max_colwidth
        )

    def query_print(
        self,
        sql: str,
        format: str = "grid",
        max_rows: int | None = 20,
        max_colwidth: int | None = 50,
        title: str | None = None,
    ) -> None:
        """
        Execute SQL and print formatted output.

        Args:
            sql: SQL query string
            format: Output format (default: "grid")
            max_rows: Limit rows shown (default 20)
            max_colwidth: Max column width
            title: Optional title to print above results
        """
        if title:
            print(f"\n📊 {title}")
            print("-" * 60)

        try:
            result = self.conn.execute(sql)
            columns = [desc[0] for desc in result.description]
            all_rows = result.fetchall()
        except Exception as e:
            print(f"ERROR: {e}")
            return

        total_rows = len(all_rows)
        output = self._format_rows(
            columns,
            all_rows,
            format=format,
            max_rows=max_rows,
            max_colwidth=max_colwidth,
        )
        print(output)

        if max_rows is not None and total_rows > max_rows:
            print(f"\n... showing {max_rows} of {total_rows} rows")

    def close(self) -> None:
        """Close the DuckDB connection and release resources."""
        if not self._closed:
            self._closed = True
            self.tables.clear()
            with contextlib.suppress(Exception):
                self.conn.close()

    def __enter__(self) -> QueryEngine:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    def __repr__(self) -> str:
        tables_str = ", ".join(self.tables.keys()) if self.tables else "(none)"
        return f"QueryEngine(tables=[{tables_str}])"
