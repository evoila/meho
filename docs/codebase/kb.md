# `meho_backplane.kb` — tenant-scoped knowledge base service

> Durable map of the knowledge-base subsystem. Update in lock-step
> with code changes; stale entries are bugs.

## Overview

The knowledge-base (`kb`) service is the persistent, tenant-scoped
durable-team-knowledge layer. It backs three transports:

- **REST** under `/api/v1/kb/...` (G4.1-T2).
- **MCP** via the `search_knowledge` + `add_to_knowledge` meta-tools
  in [`mcp/tools/knowledge.py`](../../backend/src/meho_backplane/mcp/tools/knowledge.py)
  (G4.1-T3).
- **CLI** through the `meho kb` verb (G4.1-T4).

All three transports funnel through one
[`KbService`](../../backend/src/meho_backplane/kb/service.py)
instance. Rows live in the shared `documents` table under
`source = 'kb'` — the same table that backs `memory` and any future
indexed corpus — which lets hybrid BM25 + cosine retrieval span the
read path uniformly. The kb-shaped vocabulary (slug, snippet,
`KbEntry`) is owned by this package; the substrate's `(tenant_id,
source, source_id)` natural-key contract is enforced inside the
service so every later transport stays consistent.

Each kb entry has three parts:

- The **head**: one `documents` row. `meho://kb/{slug}` reads it. A new
  body overwrites it.
- The **retained revisions**: `kb_revision` rows. Every write that
  changes the body adds one, in the same transaction as the head. A
  revision is never changed, so an earlier body stays readable by its
  id through `meho://kb/id/{entry}/revision/{revision}` until the entry
  is deleted (#3753).
- The **passages** (#3761): `documents` rows with `source='kb-passage'`,
  heading-aware pieces of the **current** revision only. Search ranks
  these, not the head. See
  [`retrieval.md`](./retrieval.md#what-search-indexes-for-kb-current-passages-3761).

The kb is **durable team knowledge** in contrast to `memory` which is
ephemeral session / policy state — the two-system split is
deliberate, and the MCP tool descriptions teach the model when to
write to which (`add_to_knowledge` for generalizable runbooks /
vendor API patterns / post-incident learnings; `add_to_memory` for
session notes and per-target settings).

## Key types

### `KbEntry` (`kb/schemas.py`)

Frozen Pydantic value object that mirrors one `documents` row in
kb-shaped vocabulary: `id`, `tenant_id`, `slug` (renamed from
`source_id`), `body`, `metadata`, `provenance`, `created_at`,
`updated_at`, `revision_id`. `id` is the entry UUID; `revision_id` is
the revision that holds this body (`None` only for a row with no
stored revision). `provenance` is a typed `KbProvenance` read from
`metadata["provenance"]` (see "Provenance" below). The
substrate columns the kb does not expose (`source`, `kind`,
`body_hash`, `tokens`, `embedding`) stay invisible to callers. Write
attribution rides `metadata` as `created_by_sub` /
`last_updated_by_sub` (see "Cross-principal write & delete semantics"
below) rather than as dedicated columns — no schema migration, and the
keys surface on every read surface that already returns `metadata`.

### `KbRevision` (`db/models.py`) and `KbEntryRevision` (`kb/schemas.py`)

`KbRevision` is the ORM row in `kb_revision` (migration `0110`):
`id` (revision UUID), `tenant_id`, `entry_id` (the `documents.id`,
`ON DELETE CASCADE`), `revision_number` (1, 2, 3, ... per entry; unique
per entry; the highest is current), `body`, `body_hash`, `provenance`
(the write's provenance block, or `NULL`), `origin`, `embedding_model`,
`chunker_version` (`kb-md-heading-v1` for revisions written since
#3761) and `created_at`.

`origin` is `write` for a row stored by a kb write, or
`backfill-current` for the one row migration `0110` copied from each
entry that existed then. A backfill row is the body the entry had at
migration time, not a historical version, and its `embedding_model` /
`chunker_version` are `NULL` because they were never recorded. The
migration does not invent older versions.

`KbEntryRevision` is the frozen read model `get_revision` returns: the
same fields plus the entry's `slug`, with `provenance` as a typed
`KbProvenance`.

### `KbEntrySearchHit`, `KbPassageEvidence`, `KbPassageLocator` (`kb/schemas.py`)

One search hit per entry. It has `slug`, `snippet`, `metadata`, the
per-signal scores of its best passage (`fused_score`, `bm25_score`,
`cosine_score`, `bm25_rank`, `cosine_rank`), and since #3761:

- `passages` — up to 3 matching passages, best first. Each is a
  `KbPassageEvidence`: the whole passage `text`, its own scores, and a
  `KbPassageLocator` (`entry_id`, `revision_id`, opaque `passage_id`,
  `ordinal`, `passage_count`, `heading_path`, `start`, `end`,
  `chunker_version`, `embedding_model`, `uri`). `start`/`end` are code
  point offsets into the revision body; `uri` is the exact read
  `meho://kb/id/{entry}/revision/{revision}`.
- `coverage` — `complete` when `passages` hold every passage of the
  revision, else `partial`.

`snippet` is the first ~200 characters of the best passage
(`_SNIPPET_CHARS`). An entry indexed before passages existed comes
back with no passages and a snippet of its body.

### `KbIngestionResult` (`kb/schemas.py`)

Summary of one `ingest_directory` run. Five counters that partition
every discovered `.md` file into exactly one bucket: `inserted_count`,
`updated_count`, `skipped_count`, `error_count`, `conflict_count`.
`errors` holds the per-file failure messages (each starts with the
file path relative to the ingest directory). `conflicts` holds one
`KbIngestConflict(slug, relative_paths)` per slug that two or more
files claim. Both lists are in sorted order, so the same directory
gives the same result on every run.

### `KbProvenance` (`kb/schemas.py`, written by `kb/provenance.py`)

Where an entry came from. `state` is `"recorded"` or `"legacy"`.
For recorded rows, `source_kind` is `"directory"` (directory ingest)
or `"direct"` (REST / MCP / console create), `relative_path` is the
file path relative to the ingest directory (`None` for direct
writes), and `ingested_at` is the UTC write time. Rows written before
provenance existed, or rows whose `provenance` key has another shape,
read as `state="legacy"` with every other field `None`.

### `InvalidKbSlugError` (`kb/schemas.py`)

Subclass of `ValueError` raised by `validate_slug` when a slug fails
the `SLUG_PATTERN` shape rule (lowercase letter start, kebab-case,
ends with letter or digit). The route layer (T2) catches it and
translates to HTTP 422; the MCP tool (T3) catches it and re-raises
as `McpInvalidParamsError` (JSON-RPC `-32602`); the CLI (T4) catches
it and exits non-zero.

### `KbService` (`kb/service.py`)

Stateless, method-scoped service. Every public method opens its own
`AsyncSession` via `get_sessionmaker()` and commits synchronously
before returning. Seven verbs:

| Verb | What it does | Throws |
|---|---|---|
| `create_entry(tenant_id, slug, body, metadata=None, *, actor_sub=None)` | Insert or replace one entry (explicit last-write-wins); returns `(entry, created)`. Validates slug, stamps attribution and `direct` provenance, delegates to `index_document`. | `InvalidKbSlugError` on bad slug |
| `get_entry(tenant_id, slug)` | Natural-key SELECT; returns the latest `KbEntry` (with its current `revision_id`) or `None`. | – |
| `get_revision(tenant_id, entry_id, revision_id)` | Exact retained read. Returns `KbEntryRevision` only when the revision belongs to the entry and both belong to the tenant; otherwise `None`, without saying why. | – |
| `list_entries(tenant_id, filter_pattern=None, limit=100, offset=0)` | List entries; slug-sorted; SQL `LIKE` pattern forwarded verbatim. | `ValueError` on negative `limit`/`offset` |
| `delete_entry(tenant_id, slug)` | Delete the head and all its revisions in one transaction; returns whether a row existed. | Database errors propagate; nothing is removed |
| `search_entries(tenant_id, query, filters=None, limit=10)` | Hybrid BM25 + cosine retrieval with `source=KB_SOURCE` pinned. | – |
| `ingest_directory(directory, tenant_id, dry_run=False)` | Read every `.md` file in sorted order, report slug conflicts without writing them, ingest the rest; idempotent. Per-file failures counted, run continues. | `KbIngestRootError` outside `KB_INGEST_ROOT` |

### `walk_kb_directory` (`kb/file_walker.py`)

The directory-walking helper `ingest_directory` calls. Reads
Markdown front-matter via `python-frontmatter` and yields a
`KbFileRecord(path, relative_path, slug, body, metadata)` for each
valid file, sorted by POSIX relative path (`Path.rglob` order depends
on the filesystem, so the walker sorts). `path` is only used to read
the file; `relative_path` is what gets stored and reported.
Per-file errors (binary file, unreadable bytes, invalid slug,
malformed front-matter) are caught inside the walker and appended
to the caller's `errors` list rather than raising.

## Control flow

### Write path — `create_entry`

1. `validate_slug(slug)` — fail closed on a bad slug before the
   substrate is touched.
2. One natural-key `get_entry` SELECT — yields the **created-vs-
   overwrite** signal (no prior row ⇒ `created=True`) and the prior
   `created_by_sub` to preserve.
3. `merge_attribution(...)` (in `kb/attribution.py`) folds the
   attribution keys into the metadata to persist. When the caller sent
   no metadata and no actor, the existing metadata is kept.
4. `stamp_provenance(..., source_kind="direct")` replaces any
   caller-sent `provenance` key with the service's own block.
5. `kb/revisions.py::write_entry` runs in three steps (#3761):
   - **Read** the entry: head `body_hash`, latest revision, and the
     revision and indexing generation of its passages.
   - **Prepare** with no transaction open: when the passages do not
     belong to the current revision, chunker and model, split the body
     (`kb/passages.py`) and count tokens with the model's own tokenizer
     files. Then embed the head (only if its body changed) and every
     new passage in one model call (`prepare_index_batch`).
   - **Publish** in one short transaction: lock the head (PostgreSQL,
     `SELECT ... FOR UPDATE`), check the entry still matches what was
     read, write the head, add a `KbRevision` when the body differs
     from the latest revision (next number, provenance, embedding
     model, chunker version), delete the old passage rows and insert
     the new ones, read the entry back, commit.
   An unchanged body reuses the latest revision and embeds nothing; its
   passages only get the new metadata. Changing a body back to an
   older text adds a new revision; it does not reuse the old one. A
   failed prepare or publish keeps the old head, revision and passages.
   Two writes to the same entry at the same time do not mix. When the
   entry moved between read and publish (another write committed; on
   SQLite, which has no row lock, this is also caught by the read-back
   after the writes), or the next `revision_number` is already taken,
   the write rolls back and runs again from the start, up to three
   times. The returned `revision_id` always names the body that was
   written.
6. Return `(_doc_to_entry(doc, revision_id), created)`. The REST route maps
   `created` to **HTTP 201** (fresh slug) vs **200** (in-place
   overwrite) and records `audit_created`; the MCP / UI callers
   discard it.

This is an **explicit replace**: the newest write wins, with no
expected-revision check. A compare-and-set API waits until a real
multi-writer workflow needs it. Directory ingest uses a different
rule (below): it never lets file order pick a winner.

### Cross-principal write & delete semantics (#1845)

kb rows are **tenant-shared with no per-row ownership check**. Any
`tenant_admin` in a tenant may overwrite or delete **any** other
principal's slug in that tenant — this is **wiki-like and intended**,
not a bug. Operators must not assume per-author ownership: there is no
`403`/`409` on a same-slug cross-principal write, and `DELETE` is not
principal-scoped (it removes whatever row matches the natural key).
This is deliberately different from `memory` DELETE, which **is**
principal-scoped (a cross-principal memory delete is a silent no-op).

To make a row self-describing about authorship without an audit-log
join, every kb write stamps two keys into `documents.doc_metadata`
(no schema migration — they ride the existing JSONB and surface on
every read surface that returns `metadata`):

- **`created_by_sub`** — the OIDC `sub` that first created the row.
  Set once and **preserved verbatim** across every later overwrite,
  including a cross-principal one. A pre-attribution row (created
  before this shipped) is backfilled with the first overwriter's sub.
- **`last_updated_by_sub`** — the OIDC `sub` of whoever last wrote the
  row. Rewritten on every attributed write.

Attribution is derived from the **verified** `Operator.sub`, never
from request JSON: `merge_attribution` strips any caller-supplied
`created_by_sub` / `last_updated_by_sub` from the create-body
`metadata` before stamping, so an operator cannot forge authorship.
An unattended re-index that passes `actor_sub=None` leaves attribution
untouched (it does not erase a previously-attributed row's keys).

The two keys surface on **every** kb read surface:
`POST /api/v1/kb`, `GET /api/v1/kb/{slug}`, the `GET /api/v1/kb` list
preview, and `POST /api/v1/retrieve {source:"kb"}` hits (the retrieve
hit's `doc_metadata` is the same JSONB column).

### Write path — `ingest_directory`

`KbService.ingest_directory` delegates to
`kb/directory_ingest.py::ingest_directory`.

1. Confine the directory to `KB_INGEST_ROOT` (#101).
2. Pre-walk: `list(walk_kb_directory(directory, errors=errors))` reads
   every valid `.md` file, in sorted relative-path order, before any
   write. Per-file walk failures land in `errors`.
3. `_find_slug_conflicts(records)` groups records by slug. Every slug
   with two or more files becomes a `KbIngestConflict`. None of those
   files is written, so an existing entry with that slug stays exactly
   as it was. This also applies in `dry_run`.
4. For each remaining record, `_ingest_one` does:
   - SELECT existing row by natural key.
   - Compute `compute_body_hash(body)` and compare.
   - Classify as `inserted` / `updated` / `skipped`.
   - On `dry_run=False`, call `write_entry` (same as `create_entry`,
     so a changed body also stores a revision) with the front-matter
     metadata plus a `directory` provenance block (relative path and
     one `ingested_at` timestamp for the whole run).
5. Per-file commits — a failing file in the middle of the corpus
   does **not** roll back the successful files preceding it. That
   trade-off (per-file commit overhead vs. all-or-nothing) is the
   load-bearing reason the service is stateless. Conflicts are found
   before step 4, so no conflict group is ever half-written.

The REST route binds `audit_conflict_count` with the other counters.
The CLI prints a `conflicted:` counter and a `conflicts` list.

### Provenance

`metadata["provenance"]` is owned by the service. Each write replaces
any value a caller or front-matter block put there:

```json
{"source_kind": "directory", "relative_path": "runbooks/vm-restart.md",
 "ingested_at": "2026-03-04T05:06:07Z"}
```

No absolute host path is stored; `stamp_provenance` rejects absolute
or `..` paths. Older directory-ingested rows may still carry a
`source_path` key with an absolute path. They are not rewritten: they
read as `provenance.state="legacy"`, and the next ingest of the same
file replaces their metadata (dropping `source_path`).

### Read path — `get_entry` / `list_entries`

Both are natural SQL — no retrieval. `get_entry` is a natural-key
SELECT; `list_entries` is `SELECT ... WHERE tenant_id=? AND
source='kb' ORDER BY source_id LIMIT ? OFFSET ?`, with an optional
`AND source_id LIKE :pattern` clause. Both add a correlated subquery
(`current_revision_id()`) for the highest-numbered revision, which
fills `KbEntry.revision_id`. They always return the latest body.

### Read path — `get_revision` (exact selected read)

`meho://kb/id/{entry}/revision/{revision}` (MCP, `operator` role,
checked by the registry) parses both UUIDs, then calls
`KbService.get_revision`. The service joins `kb_revision` to
`documents` and filters on revision id, entry id, `tenant_id` on both
tables, and `source='kb'`. The handler wraps `body` in the
`<<UNTRUSTED_AGENT_TEXT` envelope, like the slug read. A malformed
UUID, an unknown id, a revision of a different entry, another
tenant's revision and a deleted entry all answer `-32602` with the
same message, `kb revision not found`.

The legacy `meho://kb/{slug}` read is unchanged: it returns the latest
body, now with `revision_id` so a caller can cite that exact body.
`add_to_knowledge` and `GET /api/v1/kb/{slug}` return the same field.

### Delete path — `delete_entry`

`kb/revisions.py::delete_entry_rows` first locks the head row, the
same way a write does (`SELECT ... FOR UPDATE` on PostgreSQL). Then it
deletes the entry's passage rows, its `kb_revision` rows and the head
`documents` row in one transaction, all by the locked head id. A delete
that overlaps a write waits until that write has committed, so it also
removes the passages and the revision the write added. Without the
lock, the new passages could stay searchable and cite a head that no
longer exists. The passages are the searchable evidence, so search and
every exact read stop at the same commit. If a statement or the commit
fails, the transaction rolls back: the entry and its revisions stay
readable, the error propagates, and the caller can retry. The
`ON DELETE CASCADE` on `entry_id` also covers any other path that
deletes a kb `documents` row. A later create with the same slug gets a
new entry UUID and starts again at revision 1.

There is no automatic retention or age-off for revisions. They last
until the entry is deleted.

### Read path — `search_entries`

1. Translate optional `filters['kind']` into the substrate's `kind`
   argument; every other key becomes a `metadata_filters` term. Passage
   rows carry the entry's metadata, so these filters match passages
   the same way they matched entries.
2. Call `meho_backplane.retrieval.retriever.retrieve` with
   `source=KB_SOURCE`, the query, the `kind` filter and
   `limit=CANDIDATE_LIMIT` (50). The retriever maps `source="kb"` (and
   `kind="kb-entry"`) to the current passage rows and drops a head
   once its entry has passages, so no entry shows twice.
3. `_group_by_entry` folds the ranked rows into one `KbEntrySearchHit`
   per entry, best first, up to `limit` entries, each with up to 3
   passages and their locators.

`search_knowledge` (MCP) wraps every snippet, passage text and
heading title (`locator.heading_path`) as untrusted text and keeps its response inside
`RESULT_COMPLETE_RESPONSE_WIRE_MAX_BYTES` using the shared wire
measurement; when it has to leave lower-ranked hits out it says
`delivery: "partial"` and `omitted_hits`.

The retrieval substrate enforces tenant scoping at SQL level via the
`documents.tenant_id` filter. Combined with `source=KB_SOURCE` being
pinned inside `search_entries`, cross-tenant or cross-source reads
are structurally impossible.

## Dependencies

Direct:

- `meho_backplane.db.engine.get_sessionmaker` for the async session.
- `meho_backplane.db.models.Document` and `KbRevision` ORM types.
- `meho_backplane.retrieval.indexer.{prepare_index_batch, get_passage_tokenizer, compute_body_hash}`
  for the write path (through `kb/revisions.py`), and `kb/passages.py`
  for the split.
- `meho_backplane.retrieval.retriever.retrieve` for `search_entries`.
- `python-frontmatter` (transitive via `kb/file_walker.py`) for
  `.md` front-matter parsing.

Transports that call into this service:

- `meho_backplane.api.v1.kb` (REST, G4.1-T2).
- `meho_backplane.mcp.tools.knowledge` (MCP, G4.1-T3).
- `meho_backplane.cli.kb` (CLI, G4.1-T4).

## Known issues

- The console detail page reads `metadata["path"]` for its "source
  path" field, a key no writer sets. It shows nothing for every row.
- Legacy rows keep their absolute `source_path` until re-ingested.
- A write that overlaps other writes to the same entry three times in
  a row gives up and raises the last error (`IntegrityError` or the
  stale-head error). Nothing is stored for the failed write; the caller
  can retry.
- An entry written before #3761 has no passages until its next write;
  until then search returns its whole-body head. Running
  `meho kb ingest` again (or re-adding the entry) builds them.

Otherwise none outstanding. The kb-shaped vocabulary is stable; the substrate's
metadata-filter surface is reserved for future v0.2.next work — a
caller passing extra keys to `filters` today silently has them
ignored (substrate cap, not a kb-package decision).

## References

- Initiative #331 (G4.1) — kb T1 service shipped via #430.
- [`examples/kb_writeback/`](../../examples/kb_writeback/) and
  [`examples-kb-writeback.md`](./examples-kb-writeback.md) — runnable
  sample showing investigation → kb write-back → retrieval (R3 of
  Initiative #807, G11.6 reference patterns).
- [`memory.md`](./memory.md) — the durable session/policy memory
  service that shares the `documents` substrate but enforces
  different scope encoding + RBAC.
- [`untrusted-text-envelope.md`](./untrusted-text-envelope.md) — the
  `meho://kb/{slug}` resource serves `body` inside the
  `<<UNTRUSTED_AGENT_TEXT` guard envelope (stored prompt-injection
  hardening at the LLM-facing read boundary).
