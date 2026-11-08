# `meho_backplane.kb` — tenant-scoped knowledge base service

> Durable map of the knowledge-base subsystem. Update in lock-step
> with code changes; stale entries are bugs.

## Overview

The knowledge-base (`kb`) service is the persistent, tenant-scoped
durable-team-knowledge layer. It has four callers:

- **REST** under `/api/v1/kb/...` (G4.1-T2). This includes
  `POST /api/v1/kb/import`, which the team wiki's CI job calls after
  each merge (#5728, see [Import of pages sent in the request](#import-of-pages-sent-in-the-request-5728)).
- **MCP** via the `search_knowledge` + `add_to_knowledge` meta-tools
  in [`mcp/tools/knowledge.py`](../../backend/src/meho_backplane/mcp/tools/knowledge.py)
  (G4.1-T3).
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
| `create_entry(tenant_id, slug, body, metadata=None, *, actor_sub=None, own_entries_only=False, may_set_curation_keys=None)` | Insert or replace one entry (explicit last-write-wins); returns `(entry, created)`. Validates slug, checks the lesson header, stamps attribution and `direct` provenance, runs `kb/revisions.py::write_entry`. | `InvalidKbSlugError` on bad slug; `LessonHeaderError` on a wrong header value; `KbSharedNotAllowedError`, `KbSharedSlugError` (shared scope) |
| `get_entry(tenant_id, slug)` | Natural-key SELECT; returns the latest `KbEntry` (with its current `revision_id`) or `None`. Also finds a shared entry of another tenant. | – |
| `get_revision(tenant_id, entry_id, revision_id)` | Exact retained read. Returns `KbEntryRevision` only when the revision belongs to the entry and both belong to the tenant; otherwise `None`, without saying why. | – |
| `list_entries(tenant_id, filter_pattern=None, limit=100, offset=0, order="slug")` | List entries; slug-sorted by default, `order="newest"` sorts by `updated_at`, newest first; SQL `LIKE` pattern forwarded verbatim. | `ValueError` on negative `limit`/`offset` |
| `delete_entry(tenant_id, slug)` | Delete the head and all its revisions in one transaction; returns whether a row existed. | Database errors propagate; nothing is removed |
| `search_entries(tenant_id, query, *, filters=None, limit=10)` | Hybrid BM25 + cosine retrieval with `source=KB_SOURCE` pinned. | `ValueError` on negative `limit` |
| `ingest_directory(directory, tenant_id, dry_run=False)` | Read every `.md` file in sorted order, report slug conflicts without writing them, ingest the rest; idempotent. Per-file failures counted, run continues. | `KbIngestRootError` outside `KB_INGEST_ROOT` |

`kb/page_import.py::import_pages(request, *, tenant_id, actor_sub)` is
not a `KbService` method: the import route calls it directly. See
[Import of pages sent in the request](#import-of-pages-sent-in-the-request-5728).

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
     from the latest revision (next number, provenance, embedding
     model, chunker version), delete the old passage rows and insert
     the new ones, read the entry back, commit.
   An unchanged body reuses the latest revision and embeds nothing; its
   passages only get the new metadata. Changing a body back to an
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
   then verifies. Two operators that both see a new slug as unused
   cannot both create it: the one that loses the race reads the
   other's entry on its rerun and is refused, and that entry keeps
   its body and creator. The returned `revision_id` always names the
   body that was written.
3. Return `(_doc_to_entry(doc, revision_id), created)`. `created`
   comes from the read of the try that went through. The REST route
   maps it to **HTTP 201** (fresh slug) vs **200** (in-place
   overwrite) and records `audit_created`; the MCP / UI callers
   discard it.

This is an **explicit replace**: the newest write wins, with no
expected-revision check. A compare-and-set API waits until a real
multi-writer workflow needs it. Directory ingest uses a different
rule (below): it never lets file order pick a winner.

### Cross-principal write & delete semantics (#1845)

kb rows are **tenant-shared**. Any `tenant_admin` in a tenant may
overwrite or delete **any** other principal's slug in that tenant —
this is **wiki-like and intended**, not a bug. There is no `403`/`409`
on a same-slug cross-principal write by a `tenant_admin`, and `DELETE`
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
  operator's entry cannot look curated.

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

`POST /api/v1/kb/import` (`kb/page_import.py::import_pages`) takes the
pages in the request body. The caller has the pages on its own machine.
MEHO reads no folder on the server and fetches nothing, so it never
holds Git credentials. The first caller is a CI job in the team wiki
repo: after every merge to main it sends the wiki's product pages with
`applies_to: product-and-version` and `status: confirmed`. The job
lives in the team wiki repo, not here.

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
   `${TOKEN}` is not a secret. The reason names the pattern and the
   line, never the value. MEHO holds no list of customer names, host
   names or addresses: the team wiki's CI job checks its own
   identifier list before it sends the pages, and refuses and reports a
   page with a listed identifier there.
3. **Lesson header.** The page starts with a YAML header that has
   `type`, `applies_to` and `status`. Header fields with an empty value
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
`last_updated_by_sub` is the caller. A write that fails skips that page
with the error type (not its message); the other pages go on.

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
  (`middleware.py::_reject_kb_import_only_elsewhere`, keyed on
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
tenant only. The client secret lives in the team wiki repo's CI
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
Nothing deletes or hides an expired entry yet: the date is recorded and
returned on every read surface. The console entry page shows every
header field the entry has in a "Lesson header" list
(`lesson_header.py::header_rows`).

**Owner.** The owner is `created_by_sub` (see above).

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
     or `null`. The check compares top-level keys only.
   - Example: `{"kind": "kb-entry", "source_kind": "evoila-distilled"}`
     keeps only curated kb entries. `{"product": "vsphere",
     "version": 8}` keeps only entries whose metadata has both
     `product: "vsphere"` and `version: 8`.
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
- `meho_backplane.redaction.patterns` for the secret check of the page
  import (`kb/secret_check.py`).

Transports that call into this service:

- `meho_backplane.api.v1.kb` (REST, G4.1-T2), also for the page import.
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
