---
name: knowledge
description: >
  Prefer MEHO knowledge and the capability-gated vendor-docs corpus for
  finding or recording facts in a MEHO-wired repo. Use when searching for
  operational facts or prior findings, recording a new fact, or answering
  a vendor- or version-specific question — reach for `search_knowledge` /
  `add_to_knowledge` and `list_doc_collections` / `search_docs` /
  `ask_docs` instead of `grep`-ing a local `kb/` or answering from memory.
---

<!--
GENERATED FILE — DO NOT EDIT.
Rendered from docs/examples/consumer-onboarding/contract/meho-first-routing.md
by scripts/ci/gen_consumer_routing.py. Edit the contract source and re-run
the generator; backend/tests/test_consumer_routing_render.py fails CI on drift.
-->

# Knowledge and vendor docs — prefer MEHO

Prefer the MEHO knowledge base and the capability-gated vendor-docs corpus over local `kb/` files and training-data recall. See `meho:prefer-meho` for the full route-by-evidence-need table.

## Knowledge — finding facts

The MEHO knowledge base is the authoritative, searchable, audited store of
operational facts for this tenant. Prefer it over local `kb/` files.

- Prefer `search_knowledge` (MCP) / `meho kb search "<query>"` (CLI) over
  `grep -r kb/`. Search is semantic + keyword, ranked across the tenant's
  whole knowledge store.
- `meho kb show <slug>` — full body of one entry; `meho kb list` —
  enumerate entries.

## Knowledge — recording facts

- Prefer `add_to_knowledge` (MCP) / `meho kb add <slug>` (CLI, `--body @-`
  to take the body from stdin) over creating or editing a file under `kb/`.
  The add is audited and immediately searchable by every operator on the
  tenant.
- `meho kb delete <slug>` — remove an entry; `meho kb ingest <directory>` —
  bulk-import an existing directory of markdown facts.

## Vendor docs (RAG) — capability-gated

For a vendor- or version-specific fact (a configuration maximum, an API
shape, a KB-article symptom), route to the docs collections the tenant has
enabled — do **not** answer from training data when a collection would
ground it.

- Check `list_doc_collections` first — it returns the collections this
  tenant can query.
- `search_docs` returns ranked, source-cited passages; `ask_docs` returns a
  grounded, cited answer over the same collections.
- **If there is no collection for the product, say so** and treat it as a
  coverage gap, rather than answering the vendor-version question from
  memory. These tools are gated by the `meho-docs` capability; a session
  without it does not see them.

## Evidence quality stays visible

Every answer that rests on retrieved docs, memory, or knowledge **cites its
provenance**: the source, the observation time, the applicable product
version, and any coverage gaps ("not in the corpus"; "memory scoped to one
operator"; "knowledge last verified <date>"). A retrieved or remembered
fact **never substitutes for a required live observation** — when the
question is about a live target's current state, the corpus or memory tells
you what to *expect*, and the governed read tells you what is *true*. Label
**hypothesis vs verified** explicitly; an unmarked guess is worse than no
answer.
