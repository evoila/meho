# How MEHO Handles Data

> Last verified: v2.0

Most AI-powered tools send raw API responses directly to the LLM. This works fine for small responses, but infrastructure APIs routinely return megabytes of JSON -- a Kubernetes cluster with 200 pods, a Prometheus query spanning 24 hours, a VMware inventory with nested host and VM hierarchies. Sending all of that to the LLM wastes context window, increases cost, and leads to hallucination when the model loses track of details buried in thousands of lines.

MEHO takes a different approach. The **JSONFlux data pipeline** sits between connectors and the reasoning engine, transforming raw API responses into a queryable format that lets the LLM work with precise, relevant data instead of drowning in raw JSON.

## Pipeline Overview

```
Connector API Call
        |
        v
  [1] Raw JSON Response
        |
        v
  [2] JSONFlux Shape Detection
        |  (single object, list-of-dicts,
        |   wrapped collection, nested)
        v
  [3] Arrow Table Conversion
        |  (smart column typing,
        |   conflict resolution)
        v
  [4] Parquet Cache (Redis)
        |  (persistent across
        |   conversation turns)
        v
  [5] DuckDB SQL Reduction
        |  (LLM writes SQL to get
        |   exactly what it needs)
        v
  [6] Reduced Data to LLM
```

## Stage 1: Connector Returns Raw JSON

Every connector operation returns the raw API response as JSON. A Kubernetes `list_pods` call might return 200 pod objects with dozens of fields each. A Prometheus query returns time series data with thousands of data points. MEHO doesn't filter or truncate at the connector level -- the full response is preserved.

## Stage 2: JSONFlux Shape Detection

The JSONFlux `Analyzer` examines the response structure using streaming analysis. It classifies every response into one of four shapes:

| Shape | Example | Detection Logic |
|-------|---------|----------------|
| **Single object** | `{"name": "pod-1", "status": "Running", ...}` | Root is a dict with no list-of-dicts values |
| **List of dicts** | `[{"name": "pod-1"}, {"name": "pod-2"}, ...]` | Root is a list where items are dicts |
| **Wrapped collection** | `{"items": [{"name": "pod-1"}, ...]}` | Root is a dict; first key with a list-of-dicts value is the data path |
| **Nested structure** | `{"cluster": {"nodes": [...], "config": {...}}}` | Objects and arrays at multiple levels |

Shape detection is fully dynamic -- no hardcoded key names. The analyzer walks the JSON tree using reservoir sampling to build a `Summary` that captures field types, nesting depth, and array element kinds without materializing the entire structure in memory.

For each field, JSONFlux tracks which primitive types appear (`str`, `int`, `float`, `bool`, `null`) and resolves conflicts deterministically:

- Mixed `int` + `float` promotes to `float64`
- Mixed numeric + `str` promotes to `string`
- Mixed `object` + `array` serializes to `string` (JSON dump)

## Stage 3: Arrow Table Conversion

Using the schema summary from Stage 2, JSONFlux converts the JSON data into Apache Arrow tables. This is where the heavy lifting happens:

1. **Schema generation** -- `summary_to_schema()` converts the JSONFlux `Summary` into a PyArrow `Schema`, mapping each field to the correct Arrow type (`pa.string()`, `pa.int64()`, `pa.float64()`, `pa.struct()`, `pa.list_()`)
2. **Data normalization** -- `normalize_data()` recursively walks each record and coerces values to match the Arrow schema (casting ints to floats where needed, serializing nested objects to JSON strings when the schema expects `string`)
3. **Table creation** -- normalized records are assembled into a `pa.Table` with strict typing

Arrow tables are columnar, which means operations like filtering by a specific column or computing aggregates are significantly faster than scanning row-oriented JSON.

## Stage 4: Parquet Caching

Arrow tables are serialized to Parquet format and stored in Redis with a session-scoped key. This provides:

- **Persistence across conversation turns** -- data from a query in turn 1 is still available in turn 10
- **Efficient storage** -- Parquet compression typically achieves 5-10x reduction over raw JSON
- **Fast deserialization** -- Arrow tables load from Parquet with zero-copy reads
- **Session isolation** -- each conversation has its own cache namespace

The `CachedData` structure tracks metadata alongside each cached table: table name (for SQL), source operation, connector ID, column names, row count, estimated token cost, entity type hints, and identifier/display-name field mappings.

## Stage 5: DuckDB SQL Reduction

This is where context management happens. When the LLM needs data, it doesn't get the full Arrow table. Instead, MEHO's `ReduceDataTool` gives the LLM the ability to write SQL queries against cached tables.

The `QueryEngine` registers all cached Arrow tables as DuckDB virtual tables and executes SQL queries directly:

```sql
-- LLM writes queries like:
SELECT name, status, restart_count
FROM pods
WHERE namespace = 'production' AND status != 'Running'

-- Or aggregations:
SELECT namespace, COUNT(*) as pod_count, SUM(restart_count) as total_restarts
FROM pods
GROUP BY namespace
ORDER BY total_restarts DESC
```

The LLM gets back only the rows and columns it asked for -- not 200 full pod definitions with 40 fields each. This is the core of MEHO's context management strategy.

### Token-Aware Tiering

Before deciding how to present data, MEHO estimates the token cost of the full response:

| Tier | Threshold | Behavior |
|------|-----------|----------|
| **INLINE** | < 2,000 tokens (~8KB JSON) | Full data returned directly to the LLM |
| **CACHED** | >= 2,000 tokens | Metadata only -- LLM must use `reduce_data` SQL to access the data |

This binary approach prevents hallucination by design: the LLM either has all the data (small responses) or must explicitly query for what it needs (large responses). There is no middle ground of "partial data" that could mislead the model.

When a response is cached (not inlined), the LLM receives a structured signal:

```json
{
  "data_available": false,
  "action_required": "reduce_data",
  "table": "pods",
  "row_count": 200,
  "columns": ["name", "namespace", "status", "node", "restart_count", ...],
  "next_step": {
    "tool": "reduce_data",
    "example_sql": "SELECT name FROM pods"
  }
}
```

The `action_required` and `data_available: false` fields make it unambiguous: the LLM knows it doesn't have the data and must query for it.

## Stage 6: Reduced Data to LLM

The reasoning engine receives precisely the data it needs for the current investigation step. A typical multi-turn investigation looks like:

1. **Turn 1:** "What pods are failing in production?" -- LLM calls `list_pods`, gets 200 pods cached, queries `SELECT name, status, restart_count FROM pods WHERE namespace='production' AND status != 'Running'` -- gets back 3 failing pods
2. **Turn 2:** "Check the node resources for those pods" -- LLM calls `get_node_metrics`, gets node data cached, queries for the specific nodes hosting failing pods
3. **Turn 3:** "Are those nodes on overcommitted VMs?" -- LLM uses topology to resolve nodes to VMs, calls VMware connector, queries for VM resource allocation

At each step, the LLM works with tens of rows instead of hundreds. Context stays focused. Reasoning stays accurate.

## Why This Matters

| Without JSONFlux | With JSONFlux |
|-----------------|---------------|
| Raw JSON responses sent to LLM | Structured Arrow tables with typed columns |
| Context window fills up after 2-3 queries | Data cached server-side, queried on demand |
| LLM hallucinates details from truncated data | LLM either has all data or queries for specific data |
| Can't handle APIs returning 1000+ records | DuckDB handles millions of rows efficiently |
| Each conversation turn starts fresh | Parquet cache persists across turns |
| No SQL -- LLM must parse raw JSON | Full SQL support (aggregations, joins, filtering) |

The result: MEHO can investigate infrastructure with hundreds of pods, thousands of metrics, and complex nested API responses -- all in a single conversation, with the reasoning engine staying focused on what matters.
