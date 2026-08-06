# Memory and knowledge

MEHO gives an operator (and their agents) two places to write things
down so they survive the session: **memory** for shorter-lived, scoped
state — an operator preference, a tenant convention, a target-specific
gotcha — and the **knowledge base** (kb) for durable, generalizable team
knowledge: vendor API patterns, known-good runbooks, post-incident
learnings. Both are searched the same way (hybrid lexical + semantic
retrieval), and both exist as MCP tools for agents and CLI verbs for
operators over one backplane.

This guide covers what goes where, the memory scope and TTL model, and
the search-before-you-write discipline that keeps both corpora from
fragmenting.

!!! note "Prerequisites, roles, and maturity"

    - A running backplane and a connected client
      ([Connect clients](../clients/index.md)).
    - Reading and personal writes need the **operator** role. Writing
      to a **tenant-shared** scope (memory `tenant`, or any kb entry)
      needs **tenant_admin**.
    - Memory and knowledge are **GA** — the 1.0 stability promise
      applies (see the
      [feature-maturity index](../reference/maturity.md#ga-features)).

## Which store?

| | **Memory** (G5) | **Knowledge / kb** (G4) |
|---|---|---|
| For | operator/tenant/target-scoped state | durable, generalizable team knowledge |
| Lifetime | can carry a TTL (expires) | durable until deleted |
| Scoped | five scopes, from personal to tenant-wide | tenant-wide |
| Write | `add_to_memory` / `meho remember` | `add_to_knowledge` / `meho kb add` |
| Search | `search_memory` / `meho recall --query` | `search_knowledge` / `meho kb search` |

The rule of thumb the tools themselves enforce: *"is this a shorter-lived
preference/convention/gotcha, or durable team knowledge?"* A session
note about *your* tenant preference is memory; a distilled "here's how
vCenter's session auth actually works" is knowledge.

!!! note "The kb has two names"

    Every non-MCP surface calls the knowledge base **`kb`** — the CLI
    verb is `meho kb`, the REST route is `/api/v1/kb`, the console is
    `/ui/kb`. Only the two agent tools are named `search_knowledge` /
    `add_to_knowledge`. There is no `/api/v1/knowledge` route.

## Memory: scopes

Every memory entry has a **scope** that decides who can see it. Pick the
narrowest scope that captures intent:

| Scope | Visible to | Needs |
|---|---|---|
| `user` | you, across every tenant | — |
| `user-tenant` | you, within this tenant (the CLI default) | — |
| `user-target` | you, for one target | `target_name` |
| `tenant` | everyone in the tenant | **tenant_admin** to write |
| `target` | everyone touching that target | `target_name` |

Write one with the CLI:

```bash
meho remember "prod-vc-1's DRS is manual — do not enable automation" \
  --scope target --target prod-vc-1 --tag ops
```

Or from an agent:

```json
// add_to_memory
{"body": "prod-vc-1's DRS is manual — do not enable automation",
 "scope": "target", "target_name": "prod-vc-1", "tags": ["ops"]}
```

The write returns the full entry so you can confirm it landed:

```json
{"id": "…", "slug": "prod-vc-1-drs-manual", "scope": "target",
 "body": "prod-vc-1's DRS is manual — do not enable automation",
 "metadata": {"tags": ["ops"]}, "expires_at": null, "created_at": "…"}
```

## Memory: TTL

Memory can **expire** — the difference from the durable kb. TTL is an
ISO-8601 duration (`P7D` = 7 days, `PT1H` = 1 hour):

- A `user`-scope write with **no** `ttl` picks up the backend default
  (7 days, `MEMORY_USER_DEFAULT_TTL_DAYS`) — a personal note self-cleans.
- Pass `--persist` (CLI) / `ttl: null` (MCP) to keep it forever.
- `tenant`- and `target`-scope writes default to **no expiry** — shared
  conventions are meant to last.

```bash
meho remember "debugging the flaky RKE2 upgrade" --scope user --ttl PT2H
meho remember "team convention: always --dry-run first" --scope tenant --persist
```

## Search either store

Retrieval fuses BM25 (lexical) and cosine (semantic) ranks. Search
memory scoped or unscoped:

```bash
meho recall --query "DRS automation" --scope target --target prod-vc-1
```

```json
// search_memory {"query": "DRS automation", "scope": "target"}
{"hits": [
  {"scope": "target", "slug": "prod-vc-1-drs-manual",
   "snippet": "prod-vc-1's DRS is manual — do not enable…", "score": 0.87}
]}
```

Search knowledge the same way:

```bash
meho kb search "vcenter session authentication"
```

Both search tools return a **snippet** and the natural key, not the full
body — fetch the full body with a resource read (`meho://memory/{scope}/{slug}`
or `meho://kb/{slug}`), or `meho recall <scope>/<slug>` / `meho kb show <slug>`.
Omitting `scope` on `search_memory` searches every scope you can read;
`search_knowledge` takes an optional `filters` object (e.g.
`{"kind": "kb-entry"}`) that narrows by metadata containment.

## Write knowledge

```bash
meho kb add vcenter-session-auth --body-file ./vcenter-session-auth.md
```

```json
// add_to_knowledge {"slug": "vcenter-session-auth", "body": "# vCenter session auth\n…"}
{"id": "…", "slug": "vcenter-session-auth", "body": "# vCenter session auth\n…",
 "metadata": {}, "created_at": "…", "updated_at": "…"}
```

Re-adding the same slug **updates in place** — a body-hash short-circuit
means an unchanged body costs only an `updated_at` bump (no re-embed), so
an agent can call it freely as "remember this."

## The discipline: search before you write

Both `add_*` tools say the same thing, and it is the single most
important habit: **`search_*` first.** Re-adding a note under a new slug
fragments the corpus and dilutes future retrieval. If a matching entry
exists, extend it (read it, merge, re-add with the same slug) instead of
creating a near-duplicate. The mirror habit on the read side: `search_*`
**before asking the operator** a question the corpus may already answer.

This is also how the checks **investigator** closes its loop — when it
diagnoses a red dashboard it writes its structured finding into tenant
memory under `checks-noise-<group-key>`, retrievable later via
`search_memory` (see [Watch your estate with sensors](sensors-quickstart.md#the-investigator-optional-deep-tier)).

## What can go wrong here

| Symptom | What it means | Fix |
|---|---|---|
| `add_to_memory` denied with `INVALID_PARAMS` on a `tenant` write | Writing tenant-shared memory needs **tenant_admin**; your role is operator. | Write it `user-tenant` first, then `meho promote <scope>/<slug> --to tenant` as a tenant_admin. |
| `add_to_memory` errors on a `target` / `user-target` write | Those scopes **require `target_name`** — the entry has nowhere to hang otherwise. | Add `--target <name>` (CLI) / `target_name` (MCP). |
| A `user`-scope note vanished after a week | Working as designed — `user` scope carries the 7-day default TTL. | Re-add with `--persist` (CLI) / `ttl: null` (MCP) for a durable note, or a longer `--ttl`. |
| `search_memory` misses an entry you know exists | It is scoped out of your view (another operator's `user` entry), or your `scope` filter excludes it. | Drop the `scope` filter to search everything you can read; check you wrote it to a shared scope if a teammate needs it. |
| No `delete` shows up in the agent tools for kb | Deletion is deliberately **REST/CLI-only** — there is no `add_to_knowledge` inverse on the agent surface. | `meho kb delete <slug>` (tenant_admin) or `DELETE /api/v1/kb/{slug}`. |
| A kb write "succeeds" but retrieval still returns the old text | You wrote a *new* slug instead of extending the existing one — now there are two entries. | `meho kb search` for the topic first; re-add under the existing slug to update in place. |

**Next:** [Audit forensics](audit-forensics.md).
