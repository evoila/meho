<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# meho-docs add-on — operator provisioning runbook + routing convention

> Cross-repo handshake between `evoila/meho` (this repo — the backplane
> that **federates** `search_docs` queries to the external corpus) and
> two consumer sides: the operator's **Keycloak realm** (grants the
> `meho-docs` capability per tenant) and the ops team's **external
> vendor-document corpus** (the search service the backplane forwards
> to). Neither consumer side lives in this repo, so this page is the
> upstream-side tracker for what each side must do before an operator
> can use the add-on.

This page is for a **tenant_admin / deployment operator** who needs to
turn the `meho-docs` add-on on for a tenant, point the backplane at the
corpus, and prove it works. It is not the codebase walkthrough — for how
the route, MCP tool, CLI verb, and capability gate are wired internally,
see [`docs/codebase/docs-search.md`](../codebase/docs-search.md).

## What meho-docs is

`meho-docs` is an **optional, tenant-provisioned add-on** that exposes a
`search_docs` retrieval surface over the **external vendor-document
corpus** the ops team runs (product manuals, vendor KB articles, design
and reference guides — e.g. "NSX config maximums for 9.0"). When a tenant
provisions the add-on, its operators get three faces of the same surface:

- the REST route `POST /api/v1/search_docs`,
- the MCP tool `search_docs` (+ the companion resource
  `meho://docs/{product}/{version}/{chunk_id}` for the full text of a
  hit), and
- the CLI verb `meho docs search`.

**Federated, not ingested.** This is the key distinction from MEHO's own
knowledge layer. `search_docs` does **not** copy the vendor corpus into
MEHO's Postgres+pgvector substrate. Each query is proxied through the
backplane to the corpus service, forwarding the operator's JWT so the
corpus authenticates and audits the call as the operator. MEHO gains no
vector-store dependency on the corpus and never holds a stale copy of it.

Routing every query through the backplane (rather than letting clients
hit the corpus directly) buys three properties in one place:

- **Central audit.** Every query lands one `audit_log` row
  (`op_class=read`), so `meho audit query` / who-touched surface it. The
  op id is **uniform across all three faces**: REST, CLI, and MCP all
  audit `search_docs` under `meho.docs.search` and `ask_docs` under
  `meho.docs.ask`. A who-touched / `meho audit query op_id=meho.docs.search`
  filter therefore catches every query regardless of the face it came in
  on — including the MCP agent surface (see
  [Audit row visible](#audit-row-visible-who-touched)). The raw query is
  stored only as a SHA-256 hash — never in the clear.
- **JWT federation handled once.** Operator-JWT forwarding lives in the
  backplane's corpus client, not in every consumer.
- **Mandatory product/version scope enforced centrally.** A docs query
  without a binary `product` + `version` scope is rejected, fail-closed,
  so no caller can accidentally run an unfiltered corpus-wide query.

### meho-docs vs the lightweight knowledge base (`search_knowledge`)

These are two genuinely different surfaces, and the one-word gap between
them is exactly why this add-on carries the `docs` noun:

| | `search_docs` (meho-docs) | `search_knowledge` (the kb) |
|---|---|---|
| **What it holds** | Vendor-published documentation: product manuals, vendor KB, design/reference guides | THIS team's distilled knowledge: lab conventions, known-good runbooks, post-incident learnings |
| **Source** | External corpus the ops team maintains | MEHO's own Postgres+pgvector substrate |
| **Shape** | Large, vendor-authored, batch-ingested externally | Small, operator-authored, fast-changing |
| **In MEHO** | **Federated** — proxied per query, never copied in | **Ingested** — lives in MEHO's tenant-scoped store |
| **Availability** | Only when the tenant has provisioned the add-on | Always (core retrieval) |

The noun is the routing signal: "what does the vendor's documentation
say" → `search_docs`; "how does this team do it" → `search_knowledge`.
See the [routing convention](#routing-convention) below for the one line
agents carry.

## Provisioning

Two things must be true for a tenant's operators to use the add-on: the
tenant must be **granted the `meho-docs` capability** (consumer-side, in
the realm), and the backplane must be **pointed at the corpus**
(deploy-side, in the backplane's settings).

### 1. Grant the `meho-docs` capability (realm side)

The backplane reads a tenant's provisioned add-ons from a **JWT claim**
on the operator's access token (G4.5-T1, #1519). The claim name is
configurable via the backplane setting `JWT_CAPABILITIES_CLAIM_NAME`
(default `capabilities`).

To provision the add-on for a tenant, a realm admin adds a protocol
mapper so that operators of that tenant receive `meho-docs` in the
capabilities claim. Accepted claim shapes (the backplane tolerates all
three):

- **list of strings** — the canonical Keycloak multivalued-claim shape,
  e.g. `"capabilities": ["meho-docs"]` (add more keys as more add-ons
  ship);
- **single string** — e.g. `"capabilities": "meho-docs"`, for a realm
  that emits a scalar mapper for a single provisioned add-on;
- **absent** — the tenant has no add-on; the backplane resolves the
  capability set to empty (fail-closed). Any non-string/array JSON type
  is logged under `malformed_capabilities_claim` and also resolves to
  empty.

This is the **same realm wiring shape** as the `tenant_id` / `tenant_role`
mappers — see [`keycloak-tenant-claims.md`](keycloak-tenant-claims.md)
for the protocol-mapper recipe (group-attribute or script mapper). The
capabilities claim follows that same pattern: drive it off a tenant/group
attribute so a tenant_admin can grant or revoke the add-on without a code
change.

Fail-closed throughout: an absent, malformed, or empty claim means **not
provisioned**. The gate never grants access on its own — the backplane
re-validates the JWT and the corpus federation enforces the real
boundary, so a forged claim can change only what a client *shows*, never
what the server *allows*.

### 2. Point the backplane at the corpus (deploy side)

The corpus federation client (G4.5-T2, #1520) is configured by four
settings on the backplane deployment (env vars in parentheses):

| Setting (env var) | Default | Meaning |
|---|---|---|
| `corpus_url` (`CORPUS_URL`) | `""` (add-on off) | Absolute URL of the external corpus search endpoint. **Empty means the add-on is not configured**: every query fails closed as `CorpusUnavailable` → HTTP 503 at the route. Set this to turn federation on. |
| `corpus_audience` (`CORPUS_AUDIENCE`) | `""` | Optional RFC 8707 resource indicator (`aud`) the corpus binds the forwarded token to. Empty forwards no audience. |
| `corpus_timeout_seconds` (`CORPUS_TIMEOUT_SECONDS`) | `10.0` | Bound on the corpus HTTP request (connect / read / write). A slow corpus raises `CorpusUnavailable` rather than blocking the event loop. |
| `corpus_require_filters` (`CORPUS_REQUIRE_FILTERS`) | `true` | REQUIRE_FILTERS posture: when on, a query missing `product` or `version` is rejected `422` before any corpus call. Leave on — fail-closed scope discipline. |

`corpus_url` is the one switch that turns the deploy side on. The other
three have safe defaults; leave `corpus_require_filters` on.

> **Note on the external corpus rename.** The corpus historically branded
> "MEHO.Knowledge" is being renamed to **meho-docs** on the ops side. That
> rename is **external infra** (the collection/service the ops team runs),
> not an `evoila/meho` change — nothing in this repo was ever named
> "MEHO.Knowledge". It is tracked on the consumer repo
> [`evoila-bosnia/claude-rdc-hetzner-dc`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc)
> (consumer-side reference: [#1178](https://github.com/evoila/meho/issues/1178)).
> The in-repo name (`meho-docs` capability key, `search_docs` surface) is
> greenfield and canonical from commit one, independent of when the
> external rename lands.

## Verify

Once both sides are provisioned, prove the add-on end-to-end. The
contract is: the surface is **present and returns cited chunks on a
provisioned tenant, absent on an unprovisioned one, and every query is
audited**.

### Present + cited chunks (provisioned tenant)

CLI verb (operator logged in as a provisioned tenant's operator):

```bash
# meho docs appears in --help only when the tenant has the capability
meho --help | grep -A1 '  docs'

# A scoped query returns ranked cited chunks (chunk text + source_url +
# chunk_id + document_id). --product and --version are mandatory.
meho docs search "config maximums" --product nsx --version 9.0

# Raw JSON shows the full DocsChunk shape (chunk id, document id, score,
# source url):
meho docs search "config maximums" --product nsx --version 9.0 --json
```

MCP face (an agent session against a provisioned tenant):

- `search_docs` appears in `tools/list` and a `tools/call` returns
  ranked chunks, each carrying `source_url` / `chunk_id` / `document_id`.
- The companion resource `meho://docs/{product}/{version}/{chunk_id}`
  (read via `resources/read`) returns the full text of a hit when the
  agent kept only the citation from an earlier turn.

### Absent (unprovisioned tenant)

For a tenant **without** the `meho-docs` capability, the surface tells
the truth — it is absent, not greyed-out:

```bash
# meho docs is hidden from --help...
meho --help | grep -c '  docs'        # → 0

# ...and every verb refuses with a typed error before any network call:
meho docs search "anything" --product nsx --version 9.0
# → addon_not_provisioned (exit 5): the meho-docs add-on is not
#   provisioned for your tenant; ask a tenant_admin to enable the
#   `meho-docs` capability
```

On the MCP face the same true-absence holds: `search_docs` is **not** in
`tools/list`, and a `tools/call` naming it directly is rejected with a
403-class error before the handler runs. `meho://tenant/{id}/info`
returns a `capabilities` array that does **not** contain `meho-docs` —
that array is the one source of truth for what the tenant has provisioned
(MCP clients and the CLI read it rather than re-deriving from the JWT).

### Audit row visible (who-touched)

Every `search_docs` query — from any of the three faces — writes one
`audit_log` row (`op_class=read`), and the op id is the **same canonical
token regardless of face**:

| Face | op id | How |
|---|---|---|
| REST route `POST /api/v1/search_docs` | `meho.docs.search` | The route binds it via the chassis audit middleware (`audit_op_id="meho.docs.search"`). |
| CLI verb `meho docs search` | `meho.docs.search` | The verb calls the REST route, so it shares the route's op id. |
| MCP tool `search_docs` | `meho.docs.search` | The handler binds `audit_op_id="meho.docs.search"`; the MCP dispatcher lifts that contextvar into the persisted row's op id (G4.5-T8, #1549). The bare tool name still drives `classify_op` broadcast sensitivity, so `op_class=read` is unchanged. |

Because the op id is uniform, one filter catches every face — including
the MCP agent surface, the primary place agents reach the corpus. Surface
the rows with the audit verbs (the raw query is never stored; only its
SHA-256 hash plus the product/version scope and hit count):

```bash
# All search_docs queries — REST, CLI, and MCP — in one pass:
meho audit query --op-id 'meho.docs.search' --since 24h

# Or, if the corpus result cited a known target, see who touched it
# (target-anchored — face-agnostic):
meho audit who-touched <target> --since 24h
```

> The MCP `ask_docs` tool (the synthesis fast-follow, G4.5-T7) audits
> uniformly too: op id `meho.docs.ask` across REST / CLI / MCP. Filter
> `--op-id 'meho.docs.ask'` for those rows, or `--op-id 'meho.docs.*'`
> for both search and ask.

Whichever face produced it, a docs query that hit the corpus produces a
row with `op_class=read`, the bound `product` / `version` scope, the
query hash, and the hit count — never the query text.

## Routing convention

Agents (and operators) carry one line to decide which retrieval surface
to reach for:

> **Ask the team first — `search_knowledge` / `search_memory` —
> escalate to `search_docs` only on a miss or an explicit vendor-fact
> need.**

This matches the boundary the shipped `search_docs` tool description
steers agents toward (G4.5-T4, #1523): use `search_docs` for **vendor
reference** (what the documentation says), `search_knowledge` for **how
THIS team does something** (lab conventions, known-good runbooks,
post-incident learnings), and `search_memory` for **cross-session state**
(what you or the operator established earlier in this or a prior
session). The noun is the signal — reach for the team's own knowledge and
memory first, and only escalate to the vendor corpus when those miss or
when the question is explicitly "what does the vendor's documentation
say".

## References

- Codebase walkthrough (how the surface is wired):
  [`docs/codebase/docs-search.md`](../codebase/docs-search.md).
- Realm-side claim recipe (the capabilities claim follows this pattern):
  [`keycloak-tenant-claims.md`](keycloak-tenant-claims.md).
- Initiative: [#1518](https://github.com/evoila/meho/issues/1518)
  (G4.5 meho-docs add-on). Tasks: capability gate
  [#1519](https://github.com/evoila/meho/issues/1519), corpus client +
  settings [#1520](https://github.com/evoila/meho/issues/1520),
  `search_docs` route [#1521](https://github.com/evoila/meho/issues/1521),
  MCP tool + resource [#1523](https://github.com/evoila/meho/issues/1523),
  CLI verb [#1524](https://github.com/evoila/meho/issues/1524).
- Parent Goal: [#215](https://github.com/evoila/meho/issues/215).
- Consumer side (external corpus rename + provisioning):
  [`evoila-bosnia/claude-rdc-hetzner-dc`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc);
  consumer-side corpus reference
  [#1178](https://github.com/evoila/meho/issues/1178).
