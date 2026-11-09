# `meho_backplane.kb` — tenant-scoped knowledge base service

> Durable map of the knowledge-base subsystem. Update in lock-step
> with code changes; stale entries are bugs.

## Overview

The knowledge-base (`kb`) service is the persistent, tenant-scoped
durable-team-knowledge layer. It has four callers:

- **REST** under `/api/v1/kb/...` (G4.1-T2). This includes
  `POST /api/v1/kb/import`, which a CI job of the tenant's own wiki calls
  after each merge (#5728, see [Import of pages sent in the request](#import-of-pages-sent-in-the-request-5728)).
  It also includes `POST /api/v1/kb/export`, which a CI job of the
  tenant's own wiki calls to get MEHO's confirmed and candidate generic lessons (#5730, see
  [Export of generic entries](#export-of-generic-entries-5730)).
- **MCP** via the `search_knowledge` + `add_to_knowledge` meta-tools
  in [`mcp/tools/knowledge.py`](../../backend/src/meho_backplane/mcp/tools/knowledge.py)
  (G4.1-T3). On a tenant that keeps its lessons in its own wiki, both
  tools also report on the wiki
  ([Wiki search](#wiki-search-5976)).
- **Console** under `/ui/kb` (`ui/routes/kb/routes.py`).
- **CLI** through the `meho kb` verb (G4.1-T4). The Go CLI calls the
  REST routes; `meho kb search` calls `POST /api/v1/retrieve` with
  `source="kb"`.

REST, MCP and the console call one
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
  changes the body or the metadata (the header) adds one, in the same
  transaction as the head. A revision keeps the body and the metadata
  it was written with, so one revision id names one exact state of the
  entry. A revision is never changed, so an earlier body stays readable by its
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

`KbRevision` is the ORM row in `kb_revision` (migration `0110`; migration
`0146` adds `entry_metadata`):
`id` (revision UUID), `tenant_id`, `entry_id` (the `documents.id`,
`ON DELETE CASCADE`), `revision_number` (1, 2, 3, ... per entry; unique
per entry; the highest is current), `body`, `body_hash`, `provenance`
(the write's provenance block, or `NULL`), `origin`, `embedding_model`,
`chunker_version` (`kb-md-heading-v1` for revisions written since
#3761), `entry_metadata` (the entry's metadata as of this revision;
`NULL` on a row stored before `0146`) and `created_at`.

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
- `tenant_id` and `shared` (#5726) — the tenant that owns the entry,
  and whether the entry is in the shared knowledge scope. A hit from
  another tenant is always a shared one.

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
For recorded rows, `source_kind` is `"directory"` (directory ingest),
`"direct"` (REST / MCP / console create) or `"import"` (a page sent to
`POST /api/v1/kb/import`), `relative_path` is the file path relative
to the ingest directory or the page path the caller sent (`None` for
direct writes), and `ingested_at` is the UTC write time. Rows written before
provenance existed, or rows whose `provenance` key has another shape,
read as `state="legacy"` with every other field `None`.

### `InvalidKbSlugError` (`kb/schemas.py`)

Subclass of `ValueError` raised by `validate_slug` when a slug fails
the `SLUG_PATTERN` shape rule: it starts with a lowercase letter, ends
with a lowercase letter or digit, and uses only lowercase letters,
digits, hyphens and dots. The route layer (T2) catches it and
translates to HTTP 422; the MCP tool (T3) catches it and re-raises
as `McpInvalidParamsError` (JSON-RPC `-32602`); the CLI (T4) catches
it and exits non-zero.

### `KbService` (`kb/service.py`)

Stateless, method-scoped service. Every public method opens its own
`AsyncSession` via `get_sessionmaker()` and commits synchronously
before returning. Seven verbs:

| Verb | What it does | Throws |
|---|---|---|
| `create_entry(tenant_id, slug, body, metadata=None, *, actor_sub=None, own_entries_only=False, may_set_curation_keys=None, may_confirm=True, expected_revision=None)` | Insert or replace one entry (explicit last-write-wins); returns `(entry, created)`. Validates slug, checks the lesson header, stamps attribution and `direct` provenance, runs `kb/revisions.py::write_entry`. | `InvalidKbSlugError` on bad slug; `LessonHeaderError` on a wrong header value; `KbSharedNotAllowedError`, `KbSharedSlugError` (shared scope) |
| `get_entry(tenant_id, slug)` | Natural-key SELECT; returns the latest `KbEntry` (with its current `revision_id`) or `None`. Also finds a shared entry of another tenant. | – |
| `get_revision(tenant_id, entry_id, revision_id)` | Exact retained read. Returns `KbEntryRevision` only when the revision belongs to the entry and both belong to the tenant; otherwise `None`, without saying why. | – |
| `list_entries(tenant_id, filter_pattern=None, limit=100, offset=0, order="slug")` | List entries; slug-sorted by default, `order="newest"` sorts by `updated_at`, newest first; SQL `LIKE` pattern forwarded verbatim. | `ValueError` on negative `limit`/`offset` |
| `delete_entry(tenant_id, slug)` | Delete the head and all its revisions in one transaction; returns whether a row existed. | Database errors propagate; nothing is removed |
| `search_entries(tenant_id, query, *, filters=None, limit=10, time_window=None, scope="all")` | Hybrid BM25 + cosine retrieval with `source=KB_SOURCE` pinned. `time_window` limits `confirmed_at` and the last write; `scope` is `all`, `tenant` or `shared`. | `ValueError` on negative `limit` |
| `ingest_directory(directory, tenant_id, dry_run=False)` | Read every `.md` file in sorted order, report slug conflicts without writing them, ingest the rest; idempotent. Per-file failures counted, run continues. | `KbIngestRootError` outside `KB_INGEST_ROOT` |

`kb/page_import.py::import_pages(request, *, tenant_id, actor_sub)` is
not a `KbService` method: the import route calls it directly. See
[Import of pages sent in the request](#import-of-pages-sent-in-the-request-5728).
`kb/page_export.py::export_pages(tenant_id)` is not one either: the
export route calls it directly. See
[Export of generic entries](#export-of-generic-entries-5730).

### `walk_kb_directory` (`kb/file_walker.py`)

The directory-walking helper `ingest_directory` calls. Reads
Markdown front-matter via `python-frontmatter` and yields a
`KbFileRecord(path, relative_path, slug, body, metadata)` for each
valid file, sorted by POSIX relative path (`os.walk` order depends
on the filesystem, so the walker sorts). `path` serves only to read
the file; `relative_path` is what gets stored and reported.
Per-file errors (binary file, unreadable bytes, invalid slug,
malformed front-matter) are caught inside the walker and appended
to the caller's `errors` list rather than raising.

The walker reads the header with a loader that keeps dates and
decimal numbers as the text the author wrote. `learned_at:
2026-11-03` is stored as `"2026-11-03"`, a date with a time as text
too, and `version: 3.10` as `"3.10"` (every unquoted decimal number
is text, so a field has one type). Values with quotes are stored
as they are. A search filter on such a field uses the text form.
This loader is used only when the file starts with a YAML header
(`---` on the first line). A JSON header and a file without a
header are read as before, so a `---` line later in a plain note
stays body text.
A header value that still cannot be written as JSON (for example
`!!binary`) fails that file with one short error that names the
file and the field. The check is in the walker, so a dry run and a
real run give the same error.

The walker reads only regular files whose real location is inside
the folder:

- It does not follow links. It skips a linked file and does not
  enter a linked folder.
- It skips pipes, sockets and other special files.
- The same rule applies to `.kb-ignore`. A linked `.kb-ignore` is
  not read, so its patterns do not apply.
- Each skipped file gets one `kb_file_skipped` warning log line with
  its relative path and the reason (`link`, `not a regular file` or
  `outside the root`). Skipped files are not counted as errors.
- The folder passed to the walker may itself be a link. The walker
  resolves it first.

## Control flow

### Write path — `create_entry`

1. `validate_slug(slug)` — fail closed on a bad slug before the
   substrate is touched. With `own_entries_only`, caller metadata
   with a curation key is refused here too.
2. `kb/revisions.py::write_entry` runs in three steps (#3761). It
   takes a function (`service.py::_direct_write_metadata`) that builds
   the metadata to store from the entry's current metadata, and calls
   it on every try, after the read:
   - **Read** the entry: head `body_hash` and metadata, latest
     revision, and the revision and indexing generation of its
     passages. No head row ⇒ `created=True`. Then the metadata
     function runs: with `own_entries_only`, an existing entry that
     `actor_sub` did not create raises `KbEntryNotOwnedError` and
     nothing is written; `merge_attribution(...)` (in
     `kb/attribution.py`) folds the attribution keys into the
     metadata to persist (when the caller sent no metadata and no
     actor, the existing metadata is kept); and
     `stamp_provenance(..., source_kind="direct")` replaces any
     caller-sent `provenance` key with the service's own block.
   - **Prepare** with no transaction open: when the passages do not
     belong to the current revision, chunker and model, split the body
     (`kb/passages.py`) and count tokens with the model's own tokenizer
     files. Then embed the head (only if its body changed) and every
     new passage in one model call (`prepare_index_batch`).
   - **Publish** in one short transaction: lock the head (PostgreSQL,
     `SELECT ... FOR UPDATE`), check the entry still matches what was
     read, write the head, add a `KbRevision` when the body differs
     from the latest revision or the metadata differs from the head's
     (next number, provenance, metadata, embedding model, chunker
     version), delete the old passage rows and insert
     the new ones, read the entry back, commit.
   An unchanged body reuses the latest revision and embeds nothing; its
   passages only get the new metadata. A change to the metadata alone
   (not counting the provenance block and `last_updated_by_sub`, which
   change on every write) adds a revision too, but embeds nothing
   again: the passages stay, and still point at the earlier revision
   that holds the same body. Changing a body back to an
   older text adds a new revision; it does not reuse the old one. A
   failed prepare or publish keeps the old head, revision and passages.
   Two writes to the same entry at the same time do not mix. When the
   entry moved between read and publish (another write committed, or
   its metadata changed; on SQLite, which has no row lock, this is
   also caught by the read-back after the writes), or the next
   `revision_number` is already taken, the write rolls back and starts
   again. It tries at most three times in total. Each rerun reads the
   entry again and runs the metadata function again, so the
   ownership check always decides against the entry the publish step
   then verifies. Two writers that both see a new slug as unused
   cannot both create it: the one that loses the race reads the
   other's entry on its rerun and is refused (the owner rule, or a
   stale revision because it named none), and that entry keeps
   its body and creator. The returned `revision_id` always names the
   body that was written.
3. Return `(_doc_to_entry(doc, revision_id), created)`. `created`
   comes from the read of the try that went through. The REST route
   maps it to **HTTP 201** (fresh slug) vs **200** (in-place
   overwrite) and records `audit_created`; the MCP / UI callers
   discard it.

### Every update names the revision it read (#4947)

`create_entry` takes `expected_revision`: the `revision_id` that a read
of the entry returned. It is an input, not a new tool. The check runs
in `write_entry` (`ExpectedRevision`), on every try, against the
entry as that try read it. The publish step verifies that state under
the head lock, so two writers that read the same revision cannot both
win.

- A write without `expected_revision` only creates. If the slug exists,
  it is refused like a stale write. So two writers that both think a
  slug is free cannot overwrite each other either.
- A write that names a revision other than the current one is refused.
  A write that names a revision for a deleted entry is refused too
  (`cause: gone`).
- The refusal is `KbStaleRevisionError` (`kb/stale_revision.py`). Its
  detail holds the current revision id and number, who changed it
  (`last_updated_by_sub`) and when (the revision's `created_at`), the
  metadata keys that differ from the revision the writer read, and a
  short line-by-line difference of the body. The difference is cut at
  40 lines and 2000 characters, the key list at 20 keys. `cause` is
  `stale`, `exists` or `gone`.
- The owner rule runs first: an operator who may not change the entry
  gets the ownership refusal, not the stale one.
- Stored text in the detail (the body difference, key names, the
  writer) is wrapped as untrusted text in `to_payload()`, like a read.
- REST: `409` with `detail` = that payload and `error: stale_revision`.
  MCP: `-32602` with the payload in `error.data`. CLI: `meho kb add
  --expected-revision <id>` sends it; the `409` is shown as text with
  the current revision and what changed. Console: the editor save and
  the uploads only create; a slug that exists is refused with the
  current revision number and a link to the entry. Editing in the
  console comes with the history work (#4950).
- Directory ingest (`write_entry` with no expectation) keeps its own
  conflict report and dry run, and updates by slug from files. The
  import job does the same.

Directory ingest uses a different rule (below): it never lets file
order pick a winner.

### Similar entries at write time (`kb/similar.py`)

A create of a **new slug** first looks for entries that already say
something similar. This keeps duplicates out of the store. The surfaces
call `check_new_entry(...)` before `create_entry`: MCP
`add_to_knowledge`, REST `POST /api/v1/kb`, CLI `meho kb add`, and the
console editor and uploads. `create_entry` itself does not change.
Directory ingest (`meho kb ingest`) and the import job do not run the
check; they keep their own dry run.

**Off by default.** The search runs only when the setting
`KB_SIMILAR_CHECK` is `true` (Helm: `config.kbSimilarCheck`). With it
off, MEHO does not search: nothing counts as similar, so a create of a
new slug is written as before, `create_anyway` changes nothing, and a
dry run answers `would_write: true` with an empty list. The console
upload page hides its "Create anyway" box. The MCP tool
`add_to_knowledge` then has no text about the check and no
`create_anyway` input; a call that still sends it is refused as an
invalid input (see "Tool text that follows a switch" in
[mcp.md](mcp.md)). It stays off until model tests show it helps.

How it decides:

1. The slug, the lesson header and the shared scope are checked first,
   with the same errors as the create.
2. When the tenant already has the slug, the write is an update. The
   check does not run, and the write goes on as before.
3. Otherwise it searches the kb passages of the tenant and of the shared
   scope (`retrieve` plus `add_shared_hits`) with the summary and the
   start of the body. When the header names `product` and `version`, a
   first search is narrowed to those values. Those entries are listed
   first (`match: same_product_version`), then the others
   (`match: close_meaning`), best score first, at most five
   (`SIMILAR_LIMIT`).
4. An entry counts only when its best passage has a cosine score of at
   least `KB_SIMILAR_MIN_SCORE` (setting `kb_similar_min_score`,
   default `0.85`). With the default embedding model a reworded copy
   scores about 0.9 and a different note on the same product about 0.8.

When similar entries exist, **nothing is written**. The answer
(`KbSimilarCheck`) lists each entry with its slug, entry id, current
`revision_id`, summary, product, version, status, a 200-character
snippet, the score, and `may_change`. `may_change` follows the owner
rule: the entry is in the caller's own tenant, and the caller may
change any entry (tenant admin, reviewer) or created it. A shared entry
of another tenant is listed with `may_change: false`. The writer then
updates one of the entries (the `revision_id` names the revision it
read), or sends the create again with `create_anyway: true`.

`dry_run: true` gives the same answer and writes nothing. `would_write`
says whether the same call without `dry_run` would write; for an
existing slug `slug_exists` is true and the list is empty. The check
runs the create's own checks first (`_checked_direct_metadata` and
`_refuse_if_not_own` in `service.py`), so a dry run fails with the same
error as the write would: a slug another principal owns, a reserved
metadata key, `shared: true` from a tenant that may not share, or a
missing or old `expected_revision` (`refuse_stale_revision` in
`stale_revision.py`; the write checks it again under the head lock).
It never reports `would_write: true` for a write the service refuses.

Per surface:

| Surface | Similar entries found | Dry run |
|---|---|---|
| MCP `add_to_knowledge` | normal result with `written: false`, `reason: similar_entries`; summary, snippet, product, version and status are wrapped as untrusted text | same, `reason: dry_run` |
| REST `POST /api/v1/kb` | HTTP `409` with `KbSimilarCheck` | HTTP `200` with `KbSimilarCheck` |
| CLI `meho kb add` | lists the entries, exit code 1 (`similar_entries`) | lists them, exit code 0 |
| Console editor and uploads | the editor modal and the upload result list the entries (`ui/templates/kb/_similar_entries.html`: slug, summary, product, version, status, revision, snippet, score, and whether the operator may change it) with a "Create anyway" box | — |

The answer stays small: at most five entries, each text cut at 200
characters, so it is far inside the MCP size limit.

The check is advice at write time, not a lock. Two writers that create
similar entries at the same moment both get through. The weekly upkeep
merges such duplicates later.

Tests: `tests/test_kb_similar.py` turns `KB_SIMILAR_CHECK` on;
`test_with_the_check_off_a_create_is_written_as_before` checks the off
state. The default test database is SQLite, which cannot run the
search. `tests/conftest.py::_no_similar_kb_entries` makes the search
find nothing in every test, unless the test requests the
`real_similar_check` fixture and mocks `retrieve` itself
(`tests/test_kb_similar.py`).

### Cross-principal write & delete semantics (#1845)

kb rows are **tenant-shared**. Any `tenant_admin` in a tenant may
overwrite or delete **any** other principal's slug in that tenant —
this is **wiki-like and intended**, not a bug. There is no `403` on a
same-slug cross-principal write by a `tenant_admin` (a `409
stale_revision` only shows that the writer did not read the latest
revision), and `DELETE`
is not principal-scoped (it removes whatever row matches the natural
key). This is deliberately different from `memory` DELETE, which
**is** principal-scoped (a cross-principal memory delete is a silent
no-op).

An `operator` writes only through the MCP `add_to_knowledge` tool
(REST `POST /api/v1/kb` needs `tenant_admin`). The tool calls
`create_entry(..., own_entries_only=True)` for every caller that may
not change any entry, and then the service. Who may change any entry
(the owner rule, `_may_change_any_entry` in `mcp/tools/knowledge.py`):
the owner of the entry, a `tenant_admin`, or a **reviewer**. A reviewer
is a person who can decide approvals in the tenant: a human principal
that is `operator` or higher, or has the `approver` capability (the
same rule as `_check_reviewer_role` in the approvals queue, one list
for approvals and reviews). Agent, service and runner principals are
never reviewers. A `read_only` reviewer passes the tool's role gate
too: the tool is registered with `approver_may_call=True`, so
`mcp/registry.py::tool_role_gate_met` lets a human with the `approver`
capability list and call it. Other `read_only` callers stay blocked.
REST `POST /api/v1/kb` and the console editor stay `tenant_admin` only.
Only a `tenant_admin` may set the curation keys
(`may_set_curation_keys`). For every other caller the service:

- refuses to overwrite an entry whose `created_by_sub` is missing or
  is another principal (`KbEntryNotOwnedError`; the tool answers with
  a permission refusal, `reason: entry_not_owned`, and nothing is
  written). The message says how to propose a change: save it under a
  new slug, or ask a reviewer or tenant admin. A row with no `created_by_sub` (directory ingest, or a row
  from before attribution) belongs to nobody, so an operator cannot
  change it either;
- refuses caller `metadata` with a key from `CURATION_METADATA_KEYS`
  (`source_kind`, the key the curated-slice search filter scopes to,
  and `shared`, see "Lesson header and shared scope";
  `KbReservedMetadataError`, `reason: reserved_metadata`). So an
  operator's entry cannot look curated;
- drops those keys from the stored entry when it changes an entry, also
  when the write sends no `metadata` (`_drop_review_marks`). A shared
  entry stays private until a tenant admin shares it again.

A caller that may not confirm (`create_entry(..., may_confirm=False)`;
the MCP tool passes `lesson_header.py::may_confirm`) stores a
`confirmed` entry as `candidate` when it changes it. So text that
nobody reviewed never keeps the marks of an earlier review.

An operator can still create new entries and change its own. A
`tenant_admin` keeps the wiki-like rule on both surfaces.

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
untouched (it keeps the attribution keys a row already has).

The two keys surface on **every** kb read surface:
`POST /api/v1/kb`, `GET /api/v1/kb/{slug}`, the `GET /api/v1/kb` list
preview, and `POST /api/v1/retrieve {source:"kb"}` hits (the retrieve
hit's `doc_metadata` is the same JSONB column).

### Write path — `ingest_directory`

`KbService.ingest_directory` delegates to
`kb/directory_ingest.py::ingest_directory`.

1. Confine the directory to `KB_INGEST_ROOT` (#101). Both paths are
   resolved with `os.path.realpath` first. Whole folder names are
   compared, so `<root>-old` is not inside `<root>`.
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

### Import of pages sent in the request (#5728)

The import and the export (next section) move lessons between MEHO and
the tenant's own wiki. MEHO never sends knowledge anywhere by itself.
It only answers a call. Two kinds of caller can get the export: the
tenant's own wiki job, with a login set up only for the export, and a
person with the operator or tenant admin role (`meho kb export`).
Without such a call, nothing leaves MEHO.

`POST /api/v1/kb/import` (`kb/page_import.py::import_pages`) takes the
pages in the request body. The caller has the pages on its own machine.
MEHO reads no folder on the server and fetches nothing, so it never
holds Git credentials. The caller is a CI job in the tenant's own wiki
repo: after every merge to main it sends the wiki's product pages with
`applies_to: product-and-version` and `status: confirmed`. The first
caller is the wiki job of the team that builds MEHO. A customer runs
the same kind of job against its own wiki. The job lives in the wiki repo, not here.

Body (`KbImportRequest`, unknown fields are a `422`):

```json
{"pages": [{"path": "products/<product>/<page>.md",
            "content": "---\ntype: fact\napplies_to: product-and-version\nstatus: confirmed\n---\nText."}],
 "dry_run": false,
 "include_candidates": false}
```

At most 1000 pages per request (`MAX_PAGES`) and 1,000,000 characters
per page (`MAX_PAGE_CHARS`). The whole request body must also stay
under `HTTP_MAX_REQUEST_BODY_BYTES` (32 MiB by default); a bigger body
gets `413`. A bigger wiki sends several requests. Two pages with the
same slug are found only when they sit in the same request.

Each page goes through these checks, in this order. A page that fails
one is skipped (`action: "skip"`), and `reason` says why:

1. **Path.** A relative path that ends in `.md`, with no `..`, no
   leading `/` and no `\`.
2. **Secret check** (`kb/secret_check.py::find_secret`). The generic
   secret shapes of the redaction engine (`redaction.md`): login
   headers, bearer tokens, JWTs, labelled passwords and keys
   (`api_key`), private key blocks, password hashes, Ceph keys, BIND key
   secrets and secrets in links. A placeholder such as `<password>` or
   `${TOKEN}` is not a secret, but only when the whole value is one
   placeholder, with at most quotes around it and punctuation after it:
   `password: "<password>",` passes, `password=made-up-value:${TOKEN}`,
   `password: made-up-value:${TOKEN}` and `password=Q7<k9Tz2LmN>p4` are
   still secrets, and a private key block is never a placeholder. Only
   the credential label or the header name in front of the value comes
   off before the check, never a part of the value. The reason names the pattern and
   the line, never the value. MEHO holds no list of customer names, host
   names or addresses: the wiki's CI job checks its own
   identifier list before it sends the pages, and refuses and reports a
   page with a listed identifier there.
3. **Lesson header.** The page starts with a YAML header that has
   `type`, `applies_to` and `status`. The header is read with
   `frontmatter.parse`, not `frontmatter.loads`, so a field named
   `content` or `handler` is an ordinary field; a key that is not text
   (an unquoted `on:` is the YAML value `true`) skips the page. Header fields with an empty value
   (`source:`, `summary: ""`, `tags: []`) are dropped first, so a page
   made from a template with empty fields is accepted. The values are
   checked as in [Lesson header and shared scope](#lesson-header-and-shared-scope-lesson_headerpy-kbsharedpy);
   `slug`, `kind` and `scope` are refused (`provenance` is replaced).
   An unquoted date stays a date (#5703).
4. **Candidates.** A page with `status: candidate` is skipped unless the
   request has `include_candidates: true`. An imported candidate gets the
   six-month expiry like any candidate.
5. **Customer facts.** A page with `applies_to: customer` is skipped when
   the caller's tenant is listed in `KB_SHARED_TENANT_IDS`, or when the
   page sets `shared: true`. Such facts live in the engagement's project
   folder.
6. **Slug.** The file name without `.md` (`products/vcenter/snapshot-limits.md`
   becomes `snapshot-limits`). A page whose `source` is
   `meho://kb/<slug>` goes to that entry instead, so a lesson that left
   MEHO through the export and comes back inside a wiki page is stored
   once. The slug must match the slug rule; another tenant's shared slug
   is refused (`KbSharedSlugError`).
7. **Text.** The page has text below the header.

**Conflicts.** When two or more pages end up with the same slug, all of
them are skipped and listed once in `conflicts` (`KbIngestConflict`:
slug and paths). A stored entry with that slug stays unchanged.

**Update by slug, safe to run twice.** Each remaining page is compared
with the stored entry of its slug in the caller's tenant:

- `create`: no entry yet.
- `update`: the body or a header field differs.
- `unchanged`: same body and same header. Nothing is written, so a
  second run of the same pages adds no revision and does not move
  `updated_at`.

A write stores the header, a provenance block with `source_kind:
"import"` and the page path, and the owner keys: `created_by_sub` stays
the entry's first author (the import login for a new entry), and
`last_updated_by_sub` is the caller. `shared` is a MEHO setting that
the export leaves out of the page, so it stays as stored when the page
does not name it; a page that names `shared` sets it (and the usual
rule applies: only a tenant in `KB_SHARED_TENANT_IDS` may set it to
true). A write that fails skips that page with the error type (not its
message); the other pages go on.

**Dry run.** `dry_run: true` runs every check and the comparison, and
writes nothing. The answer is the same as a real run would give: one
row per page (`create`, `update`, `unchanged` or `skip` with the reason),
sorted by path.

**Answer** (`KbImportResult`): `dry_run`, `created_count`,
`updated_count`, `unchanged_count`, `skipped_count`, `pages` and
`conflicts`. The audit row has op id `kb.import`, class `write`,
`dry_run` and the four counters, never the page text.

#### The import login

The route admits a `tenant_admin`, or a login whose token carries the
claim `kb_import_only: true` (`Operator.kb_import_only`,
`auth/jwt.py::_extract_kb_import_only`). Such a login can do nothing
else:

- every other REST route, and every method but `POST` on the import
  path, answers `403 kb_import_only_scope_violation`
  (`middleware.py::_reject_kb_limited_login_elsewhere`, keyed on
  `KB_IMPORT_ROUTE`, the pair `POST` + `KB_IMPORT_PATH`), whatever role
  the token names;
- every MCP call answers the same `403` (`mcp/auth.py::verify_mcp_jwt_and_bind`);
- the console needs an interactive sign-in, which a client-credentials
  login cannot do.

The claim takes rights away, so it fails closed the other way round: a
value MEHO cannot read (a number, a list, `"yes"`) also limits the
login. Only no claim, `false` or `"false"` leaves a login unlimited.

Set the login up in Keycloak as a confidential client with only
**Service accounts** on (client credentials; no standard flow, no
direct access grants), and these mappers on the client (the same kind
as in `docs/cross-repo/keycloak-agent-client.md`):

| Mapper | Claim | Value |
|---|---|---|
| Audience | `aud` | the backplane audience (`KEYCLOAK_AUDIENCE`) |
| Hardcoded claim | `tenant_id` | the one tenant the pages go to |
| Hardcoded claim | `tenant_role` | `read_only` |
| Hardcoded claim | `principal_kind` | `service` |
| Hardcoded claim (JSON type boolean) | `kb_import_only` | `true` |

The client gets no `meho_tenants` claim, so the login works in one
tenant only. The client secret lives in the wiki repo's CI
secrets, never in MEHO.

#### The call from the CI job

The job gets a token with the client credentials grant, then sends the
pages. A dry run first is a good habit: it shows every page that would
be skipped, with the reason.

```bash
TOKEN=$(curl -s -d grant_type=client_credentials \
  -d client_id="$MEHO_IMPORT_CLIENT_ID" -d client_secret="$MEHO_IMPORT_CLIENT_SECRET" \
  "$KEYCLOAK_ISSUER_URL/protocol/openid-connect/token" | jq -r .access_token)

# One {"path", "content"} object per page; the job picks the pages
# (confirmed, product-and-version) and runs its identifier check first.
find products -name '*.md' | sort | while read -r f; do
  jq -n --arg path "$f" --rawfile content "$f" '{path: $path, content: $content}'
done | jq -s '{pages: ., dry_run: false}' > import.json

curl -sf -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  --data @import.json "$MEHO_URL/api/v1/kb/import" | jq '.pages[] | select(.action == "skip")'
```

The job fails when the call fails. It reports the skipped pages from
the answer.

### Export of generic entries (#5730)

`POST /api/v1/kb/export` (`kb/page_export.py::export_pages`) returns
the caller's confirmed and candidate generic entries as Markdown pages. It is the
mirror of the import. The caller is a CI job in the tenant's own wiki
repo: it writes the pages into the wiki and opens a merge request there,
and a person reviews it. MEHO writes nothing, and never holds Git
credentials. The first caller is the wiki job of the team that builds
MEHO. A customer runs the same kind of job against its own wiki. The job lives in the
wiki repo, not here.

The route has no body. It is a `POST` because `GET /api/v1/kb/export`
would hide an entry with the slug `export` from `GET /api/v1/kb/{slug}`.

**Which entries.** Only the caller's own tenant (no shared entries of
another tenant), and only entries with `applies_to:
product-and-version`. Then, by `status`:

- `confirmed` or `candidate`: a page, when the entry has the complete
  lesson header (`type`, `applies_to` and `status`, the fields the
  import needs: `page_import.py::REQUIRED_HEADER_FIELDS`). A candidate
  keeps `status: candidate`, so a person reviews it in the wiki; this
  is how a lesson from `add_to_knowledge` reaches the wiki (#5976). An
  entry without one of the fields (a direct write may leave out `type`)
  is listed in `skipped` with the missing fields. Set the field in MEHO
  to export it. So the import never refuses an exported page.
- `retired` or `replaced`: a page to remove (`remove`). The header
  does not matter here.
- any other status (`disputed`) or no status: neither.
  The page in the wiki stays as it was. A deleted entry gives no page
  to remove either.

**Where the entry came from** (its [provenance](#provenance)):

- `source_kind: "direct"` (an operator wrote it in MEHO): exported.
- `source_kind: "import"` or `"directory"`: left out, without a
  row in the answer. These came from pages, so a page never goes round
  in a circle.
- no provenance (`state: "legacy"`, written before MEHO recorded it):
  listed in `skipped` with the reason. Write the entry again in MEHO to
  export it.

`plan_entry` holds this rule for one entry; `add_to_knowledge` uses it
too (see [Wiki search](#wiki-search-5976)).

**The page.** The file name is `<slug>.md` (`path` in the answer). The
page is the YAML lesson header, then the entry's text with a single
newline at the end. The header holds only the lesson header fields, in
the order of `lesson_header.py::LESSON_HEADER_FIELDS`, and leaves out
empty values. MEHO's own keys (`provenance`, `created_by_sub`,
`last_updated_by_sub`, passage data and any other metadata key) stay in
MEHO. An entry with no `source` gets `source: meho://kb/<slug>`, so
the import takes the page back to the same entry, also when a reviewer
moves the file to another folder of the wiki. So the same entries give
the same bytes on every export, and a second export changes only what
changed.

**Secret check.** Each page goes through the same generic secret check
as the import (`kb/secret_check.py::find_secret`, header and text). A
page that fails it is listed in `skipped`, and the reason names the
pattern and the line, never the value.

**Answer** (`KbExportResult`): `page_count`, `remove_count`,
`skipped_count`, `pages` (`path`, `slug`, `content`), `remove` (`path`,
`slug`, `status`) and `skipped` (`slug`, `reason`), each sorted by
slug. All matching entries come in one answer. The audit row has op id
`kb.export`, class `read` and the three counters, never the page text.

**The round trip.** After the merge request is merged, the import job
sends the page back. When the page differs from the entry (for example
the added `source`), the import updates the entry. It then has
`source_kind: "import"`, so the next export leaves it out until an
operator changes it in MEHO again. When the page is the same, the
import writes nothing, and the next export gives the same bytes, so the
wiki sees no change. The entry's `shared` setting stays as it was in
both cases: the page does not carry it, and the import keeps the
stored value when a page does not name it (see
[the import](#import-of-pages-sent-in-the-request-5728), "Update by
slug"). So a lesson that every tenant can read stays readable after
the round trip.

#### The export login

The route admits an `operator` or a `tenant_admin`, or a login whose
token carries the claim `kb_export_only: true`
(`Operator.kb_export_only`, `auth/jwt.py::_extract_kb_export_only`).
Such a login can only read the export. It is limited in the same way as
the import login: every other REST route, and every method but `POST`
on the export path, answers `403 kb_export_only_scope_violation`
(`middleware.py::_reject_kb_limited_login_elsewhere`, keyed on
`KB_EXPORT_ROUTE`); every MCP call answers the same `403`; and the
claim fails closed (a value MEHO cannot read limits the login). A token
with both `kb_import_only` and `kb_export_only` reaches no route.

Set it up in Keycloak like the import login, as its own client, with
`kb_export_only` in place of `kb_import_only`:

| Mapper | Claim | Value |
|---|---|---|
| Audience | `aud` | the backplane audience (`KEYCLOAK_AUDIENCE`) |
| Hardcoded claim | `tenant_id` | the one tenant to export |
| Hardcoded claim | `tenant_role` | `read_only` |
| Hardcoded claim | `principal_kind` | `service` |
| Hardcoded claim (JSON type boolean) | `kb_export_only` | `true` |

#### `meho kb export` and the call from the CI job

`meho kb export <directory> [--dry-run] [--json]`
(`cli/internal/cmd/kb/export.go`, role `operator`) calls the route and
writes the answer into a folder on the caller's machine:

- each page is written to `<directory>/<slug>.md`: `add` when the file
  is new, `change` when its bytes differ, `unchanged` when they are the
  same (the file is not written);
- the file of each page to remove is removed when it is there;
- no other file in the folder is touched;
- a name that is not one plain `.md` file name, or a file that is a
  link or a folder, stops the run before any file is written.

`--dry-run` lists what would be added, changed and removed, and writes
nothing (it does not create the folder either). `--json` prints the same
lists as JSON. Skipped entries are listed with their reason.

The CI job can do the same with `curl`, as for the import. Use a folder
of its own for the exported pages:

```bash
TOKEN=$(curl -s -d grant_type=client_credentials \
  -d client_id="$MEHO_EXPORT_CLIENT_ID" -d client_secret="$MEHO_EXPORT_CLIENT_SECRET" \
  "$KEYCLOAK_ISSUER_URL/protocol/openid-connect/token" | jq -r .access_token)

curl -sf -X POST -H "Authorization: Bearer $TOKEN" "$MEHO_URL/api/v1/kb/export" > export.json
mkdir -p from-meho
jq -c '.pages[]' export.json | while read -r page; do
  jq -j .content <<<"$page" > "from-meho/$(jq -r .path <<<"$page")"
done
jq -r '.remove[].path' export.json | while read -r name; do rm -f "from-meho/$name"; done
jq '.skipped' export.json   # report these in the merge request
```

Then the job runs its identifier check on the pages, commits them to a
branch and opens the merge request.

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

### Lesson header and shared scope (`lesson_header.py`, `kb/shared.py`)

Knowledge and memory entries accept the same header in `metadata`
(`meho_backplane/lesson_header.py`). A wrong value raises
`LessonHeaderError` (REST `422`, MCP invalid params, a counted error
for a file in a directory import) and nothing is written.

| Field | Values |
|---|---|
| `type` | `fact`, `lesson`, `decision`, `rule`, `person-and-role`, `preference`, `observation` |
| `applies_to` | `tenant`, `customer`, `target`, `product-and-version`, `team`, `person` |
| `status` | `candidate`, `confirmed`, `disputed`, `replaced`, `retired` |
| `learned_at`, `confirmed_at`, `expires_at` | a date, a datetime or ISO 8601 text; stored as ISO 8601 text with a time zone |
| `product`, `version`, `source`, `author`, `summary` | text (a number such as `9.0` is stored as text) |
| `tags` | a list of text |

An `observation` is a story of what happened, such as a lab field
note. It is never a confirmed lesson. Fields that are absent stay
absent. A directory import carries the header from the YAML
front-matter; quote dates or leave them unquoted, both work.

MEHO keeps four names for itself: `slug`, `provenance`, `kind` and
`scope`. The header never uses them. A direct write refuses `slug`,
`kind` and `scope` in the metadata (`provenance` is replaced, as
before); a directory import also lets `slug` through, as the slug
override it always was.

**Expiry.** An entry with `status: candidate` gets `learned_at` (the
stored one, else now) and, when it names no `expires_at`, an
`expires_at` six months after `learned_at`. A re-write or re-import of
the same note keeps these dates. A confirmed entry gets no expiry.
Nothing deletes or hides an expired entry: the weekly upkeep job (below)
sets its `status` to `retired`, and the entry stays readable. The
console entry page shows every header field the entry has in a "Lesson
header" list (`lesson_header.py::header_rows`).

**Owner.** The owner is `created_by_sub` (see above).

**Who confirms.** Only a reviewer (a person who can decide approvals in
the tenant, see the owner rule above) or a tenant admin may write
`status: confirmed` (`lesson_header.py::may_confirm`). The MCP
`add_to_knowledge` tool refuses it for any other caller, also for the
owner of the entry, with a permission refusal,
`reason: confirm_needs_reviewer`, and nothing is written. The memory
service applies the same rule to every memory write (REST, MCP and the
console). REST
`POST /api/v1/kb` and the directory import are tenant admin only, so an
import carries the status a reviewer set in the tenant's own wiki. The upkeep
job never confirms a candidate.

### Weekly upkeep (`lessons/upkeep.py`)

A background job keeps the store clean. It runs once per
`KNOWLEDGE_UPKEEP_INTERVAL_SECONDS` (one week by default), counted from
its last recorded run, and can be turned off with
`KNOWLEDGE_UPKEEP_ENABLED=false`. On PostgreSQL an advisory lock lets
one replica at a time run it. Each run:

1. **Merges duplicates.** Entries of one tenant with exactly the same
   body are one entry. Case and spaces count, because `MODE=prod` and
   `MODE=PROD` say different things. The job keeps a `confirmed` copy
   first, else the oldest, and gives each other copy `status: replaced`
   and `replaced_by: <slug of the kept entry>`. Before the change it
   locks both entries and reads them again. When either was edited or
   deleted after the scan, it leaves both as they are.
2. **Retires expired entries.** An entry whose `expires_at` has passed
   gets `status: retired`.
3. **Refreshes used confirmed entries.** A `confirmed` knowledge or
   memory entry that a session used in the last interval gets
   `confirmed_at` set to the time of that use. "Used" is defined in
   [reflex-adoption.md](reflex-adoption.md#learning-numbers): a search
   gave the entry to a session, and the session then ran an operation
   that succeeded.
4. **Lists used candidates for a reviewer**, with the number of
   different sessions and different targets that used each one. Copies
   of one story are not independent proof, so two searches in one
   session count once.
5. **Measures** the four learning numbers for the interval.

The job changes only metadata, through `kb/revisions.py::update_head_metadata`
(head and passages, under the row lock). A change of the metadata adds
a new revision with the same body, as for any write that changes the
metadata, so a write that names the revision read before it is refused
as stale. The refusal names the job as the writer
(`last_updated_by_sub = system:knowledge-upkeep`); the creator of the
entry stays. It deletes no entry. It deletes recorded
retrievals older than 180 days.

**The report.** One `INTERNAL` audit row per tenant, with
`operator_sub = system:knowledge-upkeep` and `path = knowledge.upkeep`.
The payload lists what was merged (`merged`: slug and `into`), retired
(`retired`), refreshed (`refreshed`) and what waits for a reviewer
(`for_review`), each list at most 100 items with full counts
(`merged_count` and so on), plus the four numbers (`numbers`). Read it
with `meho audit query` and the principal `system:knowledge-upkeep`. A
row with the totals goes to the system tenant
`00000000-0000-0000-0000-000000005733`; the schedule reads it as the
last run. A tenant's changes and its report row commit in one
transaction. When the report cannot be written, the changes roll back
too, so the next run makes and reports them. The row with the totals is
written only when every tenant committed.

**Shared scope.** An entry with `shared: true` in its metadata is
readable from every tenant on the instance, if its owner tenant is
listed in `KB_SHARED_TENANT_IDS` (comma-separated tenant ids; empty by
default, so nothing is shared). Only reading is shared:

- the entry stays in its owner tenant (`KbEntry.tenant_id` is the
  owner's id);
- `get_entry`, `list_entries`, `search_entries` and `get_revision` of
  another tenant include it (`kb/shared.py::visible_to`; search asks
  each owner tenant for its `shared` rows and merges by score, in
  `kb/service.py::add_shared_hits`);
- `POST /api/v1/retrieve` with `source="kb"` (what `meho kb search`
  calls) adds the same shared hits through `add_shared_hits`;
- a caller filter `shared` other than `true` (for example
  `filters={"shared": false}`) leaves out every shared entry of another
  tenant;
- when a tenant has the same slug itself, its own entry wins;
- a tenant that is not listed cannot set `shared: true`
  (`KbSharedNotAllowedError`, REST `403`);
- no other tenant can write a slug that is a shared entry of the owner
  (`KbSharedSlugError`, REST `403`), and `delete_entry` only ever
  touches the caller's own tenant;
- only the JSON value `true` counts. The SQL test (`_IsShared`) does not
  cast, so an old row with text under `shared` cannot fail a query.

`shared` is a curation key: an operator on the MCP tool cannot set it.
Customer facts never go into the shared scope. They belong in the
customer's own tenant or instance.

### Read path — `get_entry` / `list_entries`

Both are natural SQL — no retrieval. `get_entry` is a natural-key
SELECT; `list_entries` is `SELECT ... WHERE visible_to(tenant) AND
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

`search_knowledge` with a `slug` argument (#4138) runs the same read
as `meho://kb/{slug}` (`mcp/resources/kb.py::read_kb_entry`) and
returns `{"entry": {...}}`. It has the same shape, the same tenant
check, the same "not found" error and the same untrusted wrapper. It
is there for MCP clients that give the model tools but no resources.
With `slug` the other arguments are ignored. A call with neither
`query` nor `slug` gets `-32602`.

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
   the same way they matched entries. The rules for these metadata
   keys:
   - **All keys must match.** A hit must have every key in its
     metadata, with the same value. A key the entry does not have
     drops the entry. The retriever checks this with PostgreSQL
     `metadata @> filters`.
   - **Values are simple values:** text, a number, `true` / `false`,
     or `null`, or a list. A list matches an entry that holds every
     listed item: `{"tags": ["vm", "snapshot"]}` keeps entries that have
     both tags (and maybe more). The check compares top-level keys only.
   - Example: `{"kind": "kb-entry", "source_kind": "evoila-distilled"}`
     keeps only curated kb entries. `{"product": "vsphere",
     "version": 8}` keeps only entries whose metadata has both
     `product: "vsphere"` and `version: 8`.
2. Call `meho_backplane.retrieval.retriever.retrieve` with
   `source=KB_SOURCE`, the query, the `kind` filter, the `time_window`
   and `limit=CANDIDATE_LIMIT` (50). The retriever maps `source="kb"` (and
   `kind="kb-entry"`) to the current passage rows and drops a head
   once its entry has passages, so no entry shows twice. The time
   window is checked in the same SQL, before the `LIMIT` (see
   [Search filters](#search-filters-5726)).
3. Unless `scope="tenant"`, add the hits of the shared scope
   (`add_shared_hits`, with the same filters and time window). With
   `scope="shared"` the own tenant is searched only for its `shared`
   rows, and not at all when the tenant is not a listed owner.
4. `_group_by_entry` folds the ranked rows into one `KbEntrySearchHit`
   per entry, best first, up to `limit` entries, each with up to 3
   passages and their locators.

### Search filters (#5726)

`search_knowledge` and `search_memory` take the same named filters on
the lesson header (`mcp/tools/_search_filters.py`):

| Filter | Meaning | How it is checked |
|---|---|---|
| `product`, `version`, `status` | The header field has exactly this value. | Metadata containment (`@>`). |
| `tags` | The entry has every listed tag. | Metadata containment with a list. |
| `confirmed_after`, `confirmed_before` | The header `confirmed_at` is in this range (both ends included). An entry without a readable `confirmed_at` is left out. | `_TIME_WINDOW_PREDICATE_SQL` in the retriever. |
| `updated_after`, `updated_before` | The last write is in this range (both ends included). | The `updated_at` column, same predicate. For knowledge this is the passage row, which every write of the entry refreshes. |

`search_knowledge` also takes `scope`: `all` (default), `tenant` or
`shared`. A named filter wins over the same key in `filters`.

Example: the confirmed lessons for one product version from the last
90 days are `status="confirmed"`, `product`, `version` and
`confirmed_after=<the date 90 days ago>`.

The tool texts only say that the filters run before ranking and that a
date alone is midnight UTC. The default MCP tool list has a size limit
(`tests/test_mcp_tool_text_budget.py`), so the details stay here.

A time limit is ISO 8601 text. A date alone means midnight UTC; a time
without an offset is UTC. An unreadable value, or an `after` later
than its `before`, is refused with JSON-RPC `-32602`. Every filter runs
inside the candidate SQL, before ranking cuts the candidates, so a
filtered-out entry never pushes a match out of the top 50.

The `confirmed_at` check reads stored text. Like the Memory expiry
check, it tests the ISO 8601 shape and `pg_input_is_valid` before any
cast, so an old row with an odd date cannot fail the query.

The REST and CLI search (`POST /api/v1/retrieve`, `meho kb search`)
and the console search do not take the named filters yet.

`search_knowledge` (MCP) wraps every snippet, passage text and
heading title (`locator.heading_path`) as untrusted text and keeps its response inside
`RESULT_COMPLETE_RESPONSE_WIRE_MAX_BYTES` using the shared wire
measurement; when it has to leave lower-ranked hits out it says
`delivery: "partial"` and `omitted_hits`.

The retrieval substrate enforces tenant scoping at SQL level via the
`documents.tenant_id` filter. Combined with `source=KB_SOURCE` being
pinned inside `search_entries`, cross-tenant or cross-source reads
are structurally impossible.

### Wiki search (#5976)

A tenant may keep its lessons as pages in its own wiki: a Git repo of
Markdown pages the tenant owns. A knowledge index
loads the wiki (outside MEHO), and MEHO reaches the index as a docs
collection ([doc-collections.md](doc-collections.md)). Then a lesson
lives in two places: the wiki is the source of truth, and the store
keeps a copy. The import (#5728) brings the wiki's pages in; the export
(#5730) hands lessons written in MEHO, as pages for review, to the
tenant's own job that calls it.

**The setting.** `tenant.knowledge_wiki_collection` (migration `0150`)
holds the key of the docs collection, per tenant. `NULL` (the default)
means lessons live in MEHO only, and nothing below runs: both tools
answer exactly as before. It is set through the settings file,
`policies.knowledge_wiki_collection` ([settings.md](settings.md)),
written by `tenancy/policies.py::set_knowledge_wiki_collection` and read
at call time with no cache (`kb/wiki_search.py::wiki_collection_of`).

**The merged search** (`kb/wiki_search.py::search_wiki`). After the
store search, `search_knowledge` (MCP) and `POST /api/v1/retrieve` with
`source="kb"` (`meho kb search`) search the collection with the same
query and `limit`:

1. The caller needs the docs add-on (`meho-docs` capability).
2. A search with `filters`, header filters, a time window or
   `scope="shared"` (REST: `kind` or `metadata_filters`) does not search
   the wiki: the wiki cannot apply them.
3. The same entitlement and readiness check as `search_docs`
   (`resolve_entitled_ready_collection`, capability
   `meho-docs:<collection>`), then the same docs search service
   (`docs_search/service.py::search_docs`). The backend gets the
   caller's token, as for `search_docs`.
4. A wiki hit whose page the store's hits hold as an imported copy is
   left out (`duplicate_count`). Only the caller's own tenant's hits
   count: a shared hit of another tenant comes from that tenant's wiki.
   The page is matched by its whole path: the copy's provenance
   `relative_path` must equal the hit's `document_id` or the path after
   `blob/<commit>/` in its `source_url` / `upstream_url`. A page with
   the same file name in another folder stays in the answer. A wiki hit
   is only one part of a page, and the import does not record which
   repository a page came from. So the hit's text must be part of the
   stored entry's text, and then the whole wiki page is compared: it is
   read once per search through `read_docs` (mode `page`) with the hit's
   `read_handle`. A page is named by the hit's `document_id` and source
   links, so pages at the same path in another repository or at another
   commit are each read. Its text without the lesson header must be the
   stored entry's whole text (spaces and line breaks aside, Unicode in
   form NFC). A page at the same path in another repository that only
   shares a paragraph with the copy stays, and so does a longer page
   whose one paragraph is the copy. When the page cannot be read in full
   (the collection offers no read, the read is refused or fails, or the
   page is cut or has more to read), the wiki hit stays. The read counts
   toward the caller's read limit at the backend.
   The store hit stays: it has the slug for a full read and is what the
   recall before an operation uses.

The answer:

- MCP: the wiki hits follow the store hits in `hits`. Each is a
  `DocsChunk` with `source: "wiki"`, `collection` set and its
  `source_url` (the source link the index sent, for a Git page its path
  and commit), its text wrapped as untrusted. The store hits keep their
  shape. `wiki` holds `collection`, `searched`, `reason`, `message`,
  `hit_count` and `duplicate_count`. The size limit counts the whole
  answer, `wiki` included. When the answer is too large, the
  lowest-ranked hits are left out first, so wiki hits go before store
  hits.
- REST: `RetrieveResponse.wiki` is a `KbWikiSearch` with the hits in
  `wiki.hits`; `null` on every other call. `meho kb search` prints the
  wiki hits after the table, or the line that says why the wiki was not
  searched.

When the wiki is not searched, the store's hits still come back, and
`wiki.searched` is `false` with a `reason`: `docs_addon_off`,
`filtered`, `not_entitled`, `unknown_collection`,
`collection_not_ready` (not ready or turned off) or `index_unavailable`
(the backend raised `CorpusUnavailable`: down, an error answer or a
bad answer). `wiki.message` says the same in one sentence. Any other
error fails the whole search, as before.

**Audit.** The search's own audit row (op id `search_knowledge` on MCP,
the retrieve row on REST) gets `collection` (the key `search_docs`
binds), `wiki_status` (`searched` or the reason) and `wiki_hit_count`.
The raw query is still only hashed.

**`add_to_knowledge`.** The tool stores every lesson as before, with
provenance `source_kind: "direct"`. On a tenant with the setting, its
answer also has `wiki` (`kb/wiki_search.py::wiki_note_for`):
`collection`, `export` and `message`. `export` is what the export does
with the stored entry (`page_export.py::plan_entry`):

- `page`: "Stored. This tenant keeps its lessons in the wiki behind
  collection `<key>`. This lesson will also appear there as a candidate
  page for review." (or "confirmed page" for a confirmed entry);
- `skip`: the lesson does not go there yet, and why (a missing header
  field, no provenance, a secret);
- `remove`: the entry is retired or replaced, so its page goes;
- `none`: the lesson stays in MEHO (not `applies_to:
  product-and-version`, or a status that is not sent).

Only lessons written in MEHO go out. Copies of wiki pages
(`source_kind: "import"`) never go back, so an older copy never
overwrites a wiki page.

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
- `meho_backplane.redaction.patterns` for the secret check of the page
  import (`kb/secret_check.py`).
- The same secret check for the page export, and `PyYAML`
  (`yaml.safe_dump`) for the header of an exported page.

Transports that call into this service:

- `meho_backplane.api.v1.kb` (REST, G4.1-T2), also for the page import.
  The page export goes through the same module.
- `meho_backplane.mcp.tools.knowledge` (MCP, G4.1-T3).
- `meho_backplane.ui.routes.kb.routes` (console).
- The Go CLI in `cli/internal/cmd/kb/` calls the REST routes, not this
  service.

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

Otherwise none outstanding. Every `filters` key other than `kind` is
passed to `retrieve` as a metadata containment filter.

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
  `<<UNTRUSTED_AGENT_TEXT` guard envelope, so the model reads stored
  text as data, not as instructions.
