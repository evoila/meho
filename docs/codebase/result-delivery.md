# Result delivery: cache, worker pool, and size limits

How MEHO stores large results, reads them back, and keeps every response
inside a size limit. This doc covers three parts that were added
together (#3639, #3640, #3641) and the numbers that size them (#3642):

- a **dedicated result cache**: its own Valkey with `noeviction`, apart
  from the governance store;
- a **bounded worker pool** for result reads: a read is refused before it
  touches the store when the pool is full, and one deadline covers the
  whole read;
- **delivery budgets and states**: two wire ceilings measured on the real
  MCP encoder, and the `delivery` states `complete`, `partial`, and
  `unavailable`.

The spill and read-back walkthrough (how a large response becomes a
handle, paging, queries, `path` reads) is in
[`result-spill.md`](result-spill.md). The chart side is in
[`devops.md`](devops.md#result-cache-subchart-chartsresult-cache). This
doc is the operator view across all three parts, plus the capacity
numbers and the telemetry rules.

## Dedicated result cache

**What it is.** Result handles hold the full rows of a large result for
one TTL (default 3600 s). They now live in their own Valkey 9.x instance,
the chart subchart `result-cache` (Valkey 9.x is the store chosen in
ADR 0005; see [`devops.md`](devops.md) and [`broadcast.md`](broadcast.md)).
Its `valkey.conf` sets an explicit `maxmemory` (chart
`resultCache.maxmemory`, default `768mb`) and
`maxmemory-policy noeviction`.

**Why it is separate.** The broadcast Valkey also holds rate-limit
counters and approval waits. It has `allkeys-lru` and no byte ceiling, so
a burst of large handles could push it to its pod memory limit and take
governance down with it. `maxmemory-policy` is one setting for the whole
instance, so the result cache needs its own instance to get a different
policy.

**Why `noeviction`.** With `noeviction`, Valkey refuses a write that
needs more memory once `used_memory` is over `maxmemory`. It never drops
an older handle that an agent is still paging. The backplane treats a
refused write as "rows not stored": the call still returns its inline
sample, and the handle says the rows cannot be read
(`reason=result_store_unavailable`).

**Moving to it safely (reader-first).** Reads check the new `v2` key on
the result cache first, then the old key on the broadcast store. Writes
move only when `RESULT_CACHE_WRITER_ENABLED` is turned on, after every
backplane replica runs the reader code. The steps are in
[`result-spill.md`](result-spill.md#dedicated-result-cache-store-3639).

**Record size cap.** One stored record is capped at
`RESULT_HANDLE_MAX_RECORD_BYTES` (default 16 MiB) when it is written. A
read checks the same cap **before** it decodes anything
(`ResultHandleStore.fetch_record`). Older code capped rows only, so an
old record can be larger. Such a record is a miss: the caller gets the
typed "handle not readable" result (`handle_not_found`, "re-run the
operation"), and the backplane logs `result_handle_record_over_cap` with
the byte counts only. The record is never decoded and its rows are never
returned.

## Bounded executor for result reads

**What it is.** Every result read (paging, query, and `path` read) runs
on a dedicated thread pool, `RESULT_QUERY_EXECUTOR_WORKERS` threads
(default 4). The code is in `operations/result_read_admission.py` and
`operations/result_query.py`.

**Admission comes first.** A read must pass three checks before the
store is read. A refused read makes no Valkey `GET`.

1. Rate check per tenant and principal (`RESULT_QUERY_RATE_PER_MINUTE`,
   default `0` = off).
2. A free worker slot. There is no queue: when all slots are busy, the
   read is refused at once with `reason=capacity_exceeded`.
3. A tenant lease (`RESULT_QUERY_TENANT_CONCURRENCY`, default 4).

MCP answers a refused read with the recoverable rate-limited error and a
`retry_after_seconds`. REST answers `429` with `Retry-After`.

**One deadline for the whole read.** `RESULT_QUERY_TIMEOUT_SECONDS`
(default 10) covers the store `GET` and the whole worker job: decode,
shaping, and the wire check. When it passes, the caller gets a
recoverable timeout error at once, and a running DuckDB query is
interrupted.

**A running worker cannot be killed.** Python cannot stop a running
thread: `Future.cancel()` returns `False` once the work has started. So
the deadline is a deadline for the **caller**, not a hard CPU limit. The
worker keeps its slot and its tenant lease until it finishes; a keeper
task renews the lease while it runs. Plan capacity with this in mind: a
slow read holds a slot past its deadline. The worst-case decode cost is
in the benchmark table below.

## Delivery budgets and states

There are two kinds of size checks. Shaping budgets run before encoding
and cut the work down. Wire ceilings run last, on the encoded response.

| Check | Setting (default) | Applies to | When over |
|---|---|---|---|
| Inline sample budget | `JSONFLUX_SAMPLE_BYTE_BUDGET` (4096) | Sample rows on a reduced response | Fewer sample rows |
| Rows budget | `RESULT_QUERY_MAX_OUTPUT_ROWS` (500), `RESULT_QUERY_MAX_OUTPUT_BYTES` (256 KiB) | `rows` of a read | `output_too_large` error |
| Completed-call wire ceiling | `RESULT_COMPLETE_RESPONSE_WIRE_MAX_BYTES` (256 KiB, at least 2048) | A finished `call_operation` | `delivery="partial"` or `"unavailable"` |
| Knowledge search wire ceiling | `RESULT_COMPLETE_RESPONSE_WIRE_MAX_BYTES` (same value) | A `search_knowledge` response | lowest-ranked hits left out, `delivery="partial"`, `omitted_hits` |
| Read wire ceiling | `RESULT_QUERY_RESPONSE_WIRE_MAX_BYTES` (2 MiB) | A `result_query` response | `output_too_large` with `delivery=unavailable` |

**Measured on the real encoder.** Both wire ceilings use
`measure_delivered_wire_bytes` (`operations/delivery_budget.py`). An MCP
`tools/call` result goes on the wire twice: as the JSON text block (a JSON
string inside the JSON-RPC body, so quotes and non-ASCII characters are
escaped again) and as `structuredContent`. The function runs the same
encoders the transport runs and adds the lengths. It counts bytes, not
tokens.

**Delivery states of a completed call.** The operation already ran, so
its status stays `ok`:

- `complete` (or no `delivery` field): the whole response fits.
- `partial`: the response was too large. The inline result is dropped,
  the receipt (`audit_id`) and a readable handle are kept, and the caller
  reads the rows with `result_query`.
- `unavailable`: the response was too large and there is no readable
  handle. Only the receipt and the remediation text are left. The caller
  must not run the operation again.

A `result_query` response over its ceiling is refused with the
recoverable `output_too_large` error, `delivery=unavailable`. Nothing is
cut silently. More detail: [`mcp.md`](mcp.md#response-wire-ceiling-3641).

## Reading old records safely

During the move, the reader sees records written by older code. Two
cases are covered by `backend/tests/test_result_handle_reader_compat.py`,
which reads real encoded records through the store codec
(`msgspec.json.decode(..., type=_StoredPayload)`):

- **Unknown fields.** A record with fields the current struct does not
  declare decodes cleanly. msgspec skips unknown fields when it decodes
  into a `Struct`, and missing new fields get their defaults (for example
  `record_kind="rows"`).
- **Records over 16 MiB.** A 10000-row record written by the row-capped
  writer is over the cap. The read checks the byte length first and
  returns the "handle not readable" miss on every read path. The decoder
  is never called.

## Capacity benchmark

`backend/scripts/result_handle_capacity_benchmark.py` measures what large
results cost and derives recommended values for four limits. It runs in
one process with no Valkey and no network. Each profile in
`backend/scripts/result_handle_capacity_profiles.py` builds rows shaped
like a real vendor list response. The payload goes through the real
reducer, the real store codec, and the real wire encoder.

```bash
cd backend
uv run python scripts/result_handle_capacity_benchmark.py            # Markdown tables
uv run python scripts/result_handle_capacity_benchmark.py --format json
uv run python scripts/result_handle_capacity_benchmark.py --help
```

How each value is derived (the rules are pinned by
`backend/tests/test_result_handle_capacity_benchmark.py`):

- **Cache `maxmemory`:** room for `--full-size-handles` (default 32)
  records at the 16 MiB cap at the same time, plus 16 MiB for an empty
  Valkey, in 64 MiB steps. A record is charged at its jemalloc size
  class (four classes per doubling), so a 16 MiB record costs 20 MiB.
  This is an upper estimate: check the live value with
  `result_cache_memory_bytes{kind="used"}`.
- **Decoded bytes and nodes (reducer work guard):** the guard should not
  refuse a payload the store could still keep whole. Each profile's work
  per stored byte is scaled to a record that fills the 16 MiB cap, then
  multiplied by `--headroom` (default 1.25).
- **Wire ceilings:** the largest measured response times `--headroom`.
  For a read, that is the largest page the rows budget still allows
  (500 rows or 256 KiB of rows). The rows budget counts the rows encoded
  once, with no escaping. The wire carries the text block, where every
  non-ASCII character becomes a `\uXXXX` escape and is then escaped
  again, plus `structuredContent`. So a page that fills the rows budget
  costs about 2 wire bytes per rows byte for ASCII text, about 3.1 for
  3-byte characters (Japanese, Chinese), and about 4 for 2-byte
  characters (Cyrillic, Greek, accented Latin). Two profiles with
  non-ASCII messages (`cjk-events`, `cyrillic-events`) measure this. For
  a completed call, that is the reduced response with its handle.

### Results

Measured on 2026-10-17, seven profiles, 10000 rows per profile, Python
3.14, x86_64. "Action" is `raise` when the default is below the
recommendation, `lower` when it is more than twice the recommendation,
else `keep`. Decode time and heap depend on the machine.

| Setting | Current default | Needed | Recommended | Action | Basis |
|---|---:|---:|---:|---|---|
| `resultCache.maxmemory` | 805,306,368 | 687,865,856 | 738,197,504 | keep | 32 records at the cap (20971520 bytes each in Valkey) + baseline |
| `RESULT_REDUCTION_MAX_DECODED_BYTES` | 67,108,864 | 27,700,166 | 50,331,648 | keep | payload that fills the 16777216-byte record cap x 1.25 |
| `RESULT_REDUCTION_MAX_NODES` | 2,000,000 | 2,126,026 | 2,750,000 | raise | payload that fills the 16777216-byte record cap x 1.25 |
| `RESULT_COMPLETE_RESPONSE_WIRE_MAX_BYTES` | 262,144 | 22,687 | 65,536 | lower | largest completed response x 1.25 |
| `RESULT_QUERY_RESPONSE_WIRE_MAX_BYTES` | 2,097,152 | 1,038,460 | 1,310,720 | keep | largest full read page x 1.25 |

| Profile | Rows stored | Record bytes | Cache bytes | Nodes | Decoded bytes | Depth | Page rows | Page bytes | Page wire | Completed wire | Decode heap | Decode ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| vm-inventory | 10,000 / 10,000 | 3,082,468 | 3,145,728 | 230,003 | 2,464,877 | 3 | 500 | 153,667 | 338,688 | 8,267 | 10,055,990 | 55 |
| k8s-pods | 10,000 / 10,000 | 7,654,637 | 8,388,608 | 970,003 | 5,474,372 | 8 | 342 | 261,429 | 597,768 | 12,577 | 53,088,970 | 354 |
| nsx-segments | 10,000 / 10,000 | 4,248,159 | 5,242,880 | 370,003 | 3,399,430 | 5 | 500 | 211,835 | 466,524 | 9,398 | 16,282,871 | 77 |
| metric-stats | 7,050 / 10,000 | 16,776,789 | 16,777,216 | 1,741,353 | 27,699,460 | 3 | 111 | 260,646 | 576,920 | 22,687 | 53,066,548 | 427 |
| log-lines | 10,000 / 10,000 | 1,798,258 | 1,835,008 | 90,003 | 1,527,991 | 3 | 500 | 88,787 | 216,428 | 6,152 | 5,466,861 | 24 |
| cjk-events | 8,522 / 10,000 | 16,775,830 | 16,777,216 | 93,745 | 16,536,949 | 3 | 133 | 261,808 | 821,354 | 16,721 | 16,425,009 | 37 |
| cyrillic-events | 9,888 / 10,000 | 16,776,155 | 16,777,216 | 108,771 | 16,499,021 | 3 | 154 | 261,259 | 1,038,460 | 17,867 | 22,380,964 | 43 |

Peak heap for 4 parallel decodes of the largest record: 212,287,304 bytes.

### What the numbers mean

- **Cache `maxmemory`: keep `768mb`.** It holds 37 records at the cap at
  the same time, and far more typical ones. Three profiles fill the cap
  at 10000 rows; the other four store 1.8 to 7.7 MB.
- **Reducer decoded-bytes guard: keep 64 MiB.** The widest profile needs
  about 28 MB of guard work to fill one 16 MiB record.
- **Reducer node guard: raise to 2,750,000.** Nested rows (the pod list)
  need about 2.1 million nodes to fill one 16 MiB record. With the
  current 2,000,000, the guard refuses such a payload before it reaches
  the record cap. This guard belongs to the result-reduction work; this
  table records the value only, and the change ships with that work.
- **Completed-call wire ceiling: 64 KiB is enough for these profiles.**
  The largest reduced response was 22 KiB. Wider rows make the handle's
  field list longer, and an over-ceiling response still keeps its
  handle (`delivery="partial"`), so lowering it is low risk. Keep the
  current default until a wider real payload is measured.
- **Read wire ceiling: keep 2 MiB.** The wire size of a full page
  depends on its text. The ASCII profiles stay under 600 KB on the wire
  for a 256 KiB page. The Japanese messages take 821 KB and the Russian
  messages 1,038 KB for the same rows budget, because the text block
  escapes every non-ASCII character twice. A ceiling below about 1.25
  MiB would refuse reads that work today. This holds for the seven
  profiles measured. A page of other text needs its own measurement.
- **Worker memory.** Decoding one 16 MiB record takes about 53 MB of
  Python heap; four parallel decodes peak at about 212 MB. Size the
  backplane pod memory for `RESULT_QUERY_EXECUTOR_WORKERS` times that.
- **Worker time.** Decoding a full 16 MiB record took under half a
  second, well inside the 10 s deadline.

These are recommendations. This doc does not change any default.

## Telemetry: aggregate signals only

Result delivery may only emit **aggregate** signals: counts, sizes,
durations, and fixed reason codes. Never log or label raw rows, path
values (what a JSON Pointer read returns), vendor totals (for example a
`total_rows` that reveals an inventory size in a metric label), keys,
handle contents, or any value taken from a vendor response. Handle and
tenant ids are allowed in log fields for tracing; they are never metric
labels.

| Signal | Emitted today as | Allowed fields or labels |
|---|---|---|
| Admitted vs rejected records | metric `result_cache_records_total` | `outcome`, `namespace` |
| Cache occupancy | metric `result_cache_memory_bytes` | `kind` (`used`, `max`) |
| Where a read found its record | metric `result_cache_reads_total` | `source` (`v2`, `legacy`, `miss`) |
| Retained prefix length | log `result_handle_spilled` | `stored_rows`, `total_rows`, `namespace` |
| Record over the cap | logs `result_handle_document_over_record_cap`, `result_handle_record_over_cap` | `record_bytes`, `max_record_bytes` |
| Executor admission rejection | log `result_read_rejected` | `reason`, `scope` |
| Output encoded size | error fields on `output_too_large`; `extras.delivered_bytes` on a trimmed call | byte counts only |
| Delivery-partial reason | `delivery` and `extras` on the response; log `delivery_receipt_over_ceiling` | state, byte counts |
| Coverage distributions | not emitted yet | `coverage` (`complete`, `partial`) only |
| Queryable / non-queryable field counts | not emitted yet | counts only |
| Executor wait time | not emitted yet | duration only |
| Timeout and drain duration | not emitted yet | duration only |

A signal that is "not emitted yet" must follow the same rule when it is
added: a counter or histogram with only the labels in the last column.
Prefer metrics over parsing logs.

## Key files

| File | Role |
|---|---|
| `backend/src/meho_backplane/connectors/result_handle_store.py` | Store, record cap on write and read, cache metrics |
| `backend/src/meho_backplane/operations/result_read_admission.py` | Rate check, worker slots, tenant leases, the pool |
| `backend/src/meho_backplane/operations/result_query.py` | The read cores, the deadline, the read wire ceiling |
| `backend/src/meho_backplane/operations/delivery_budget.py` | Wire measurement and the `delivery` states |
| `deploy/charts/meho/charts/result-cache/` | The result-cache Valkey subchart |
| `backend/scripts/result_handle_capacity_benchmark.py` | The capacity benchmark |

## Known issues

- The benchmark profiles are built from fixed row shapes, not from
  captured customer data. Re-run it when a much wider vendor row shows
  up, and compare with the live `result_cache_memory_bytes` values.
- Five of the signals in the telemetry table are not emitted yet.

## References

- #3638 — the result delivery hardening work this doc closes.
- #3639 — dedicated result cache; #3640 — worker pool and admission;
  #3641 — wire ceilings and delivery states; #3642 — this doc and the
  benchmark.
- Valkey, `maxmemory` and eviction policies:
  https://valkey.io/topics/lru-cache/
- jemalloc size classes: https://jemalloc.net/jemalloc.3.html
- msgspec, unknown fields in `Struct`: https://msgspec.dev/structs
- Python `concurrent.futures`, `Future.cancel()`:
  https://docs.python.org/3.14/library/concurrent.futures.html
- OWASP API4:2023 Unrestricted Resource Consumption:
  https://owasp.org/API-Security/editions/2023/en/0xa4-unrestricted-resource-consumption/
- MCP tools, structured content:
  https://modelcontextprotocol.io/specification/2025-06-18/server/tools
