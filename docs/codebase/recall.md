# `meho_backplane.recall` — what the team knows, shown without a search

> Durable map of recall. Update in lock-step with code changes; stale
> entries are bugs.

## Overview

An agent should not have to remember to search knowledge and memory.
So MEHO shows a few entries itself, in two places:

- **Before an operation.** The MCP tools `preview_operation` and
  `call_operation` add a `team_knowledge` block to their answer when the
  call names a target that resolves. The block holds the newest
  knowledge entries for the target's product and version, and the newest
  Memory notes saved for the target.
- **At the start.** The MCP start text (`initialize.instructions`) and
  the prompt of a hosted run carry the caller's newest Memory notes from
  their own scopes and the tenant scope.

**Off by default.** Both places show something only when the setting
`TEAM_KNOWLEDGE_RECALL` is `true` (Helm: `config.teamKnowledgeRecall`).
With it off, `preview_operation`, `call_operation`, the MCP start text
and the hosted prompt are exactly as they were before recall, and MEHO
reads nothing for them. It stays off until model tests show it helps.

All shown text is data, not instructions. It sits inside one
untrusted-text envelope (`untrusted_text.wrap_untrusted_text`), the same
marker that `search_knowledge` and `search_memory` use on their
snippets.

## The `team_knowledge` block

| Field | Meaning |
|---|---|
| `status` | `found`, `none` (nothing matches) or `unavailable` (the lookup failed, so nothing is known). |
| `text` | Only on `found`. One envelope with one entry per line pair. |
| `shown` | Only on `found`. Entries in `text`. |
| `left_out` | Only on `found`. Entries left out to stay under the size limit. |
| `note` | MEHO's own short text: how to read a full entry and search for more. |

Each entry shows its kind (`knowledge` or `memory`), its uri
(`meho://kb/<slug>` or `meho://memory/id/<id>`), its status, its
confirmed date and its source. "not given" and "not confirmed" stand in
for missing values. Then comes the header `summary`, or the start of the
body.

Which entries:

- **Knowledge:** at most 3. The header `product` equals the target's
  product (upper and lower case are the same). The target's version is
  the version the probe found (`fingerprint.version`), else the version
  the operator typed (`version`), read by
  `connectors/resolver.py` `resolve_target_version`. The header `version`
  is missing, or the target has no version, or the target's version
  equals it or starts with it and a dot (`8.0` and `8.x` match `8.0.2`). The
  tenant's own entries and the shared entries it may read
  (`kb/shared.py`); its own entry wins on the same slug, also when that
  own entry has another product header or none (its uri reads the own
  entry, so the shared one is never shown in its place). Confirmed
  entries come first, then the newest.
- **Memory:** at most 2 notes with the target's exact name, from the
  `target` and `user-target` scopes. The memory read rules apply
  (`MemoryService.list_memories` with `target_name`): another user's
  notes and expired notes are never shown.
- **Never shown:** `type: observation`, and `status` `disputed`,
  `replaced` or `retired`, and knowledge whose `expires_at` has passed.

When the block is added:

- Only when the call names a target that resolves. No target, or an
  unknown target, gives no block.
- Not on a `rate_limited` answer, and not on a check of a background
  call (`run_id`). A call started with `async: true` gets the block on
  its first answer, the `pending` one, so the agent sees it before the
  long operation ends.
- A `call_operation` answer keeps its wire limit
  (`result_complete_response_wire_max_bytes`): when the block would push
  the answer over it, the answer goes out without the block. The block
  never makes a result `partial`.
- A failed lookup gives `status: unavailable`. It never fails the
  operation's answer.

Only the MCP tools add the block. The REST routes, the console, runbooks
and hosted runs call the operation functions in `operations/meta_tools.py`
directly and do not get it.

## The Memory band of the start text

`memory_band(operator)` returns a short band: the heading
`## Recent Memory notes`, one sentence that says the notes are data, and
one envelope with the newest notes (at most 5) of the `user`,
`user-tenant` and `tenant` scopes the caller may read. It reads each of
the three scopes on its own, so many target notes cannot push them out.
Target notes come before an operation instead. With no note the band is left out. A failed
read gives a band that says the notes could not be loaded.

- MCP: `mcp/server.py` `_session_instructions` puts it after the tenant
  preamble, before the vendor-text band.
- Hosted runs: `agent/hosted_context.py` `assemble_hosted_context` puts
  it after the conventions.

## Size limits

| Limit | Value |
|---|---|
| Text of one entry | 280 characters (`MAX_ENTRY_CHARS`) |
| Source of one entry | 120 characters |
| `team_knowledge.text`, envelope included | 2000 characters (`MAX_BLOCK_CHARS`) |
| Whole Memory band | 1500 characters (`MAX_BAND_CHARS`) |

An entry that does not fit is left out whole, and packing goes on with
the next one.

## Key code

- `backend/src/meho_backplane/recall.py` — the queries, the filters, the
  packing, `recall_for_target`, `memory_band`.
- `backend/src/meho_backplane/mcp/tools/_operation_recall.py` —
  `with_recall`: resolves the target and adds the block to an MCP answer.
- `backend/tests/test_recall.py` — the tests. They turn the setting on;
  `test_with_recall_off_nothing_new_is_shown` checks the off state.

## Known limits

- Knowledge matches on the exact product name. A target product and a
  note product with different names (for example `vcenter` and
  `vsphere`) do not match.
- Memory matches on the target's name, not on its aliases.
- Knowledge looks at the newest 25 entries for the product, and Memory
  at the newest 200 notes for the target. An older entry is not shown
  when that many newer ones match.
