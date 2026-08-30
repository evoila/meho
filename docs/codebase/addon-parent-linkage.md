# Add-on out-of-process audit parent-linkage (#3028)

How a paired add-on's multi-step orchestration, driven from *outside* the
backplane, collapses into a single audit-replay subtree — the out-of-process
counterpart to the in-process lineage #2086 gave approval-gated and composite
dispatches.

## Overview

`#2086` established the in-process rule: an operation that resumes after an
approval re-binds `parent_audit_id` and `agent_session_id` before it
dispatches, so the executed op's audit row nests under the call that parked it
instead of orphaning as a second replay root. `/audit/sessions/{id}/replay`
anchors on `agent_session_id` and descends by `parent_audit_id`, so that
re-bind is all it takes to make the chain one tree.

A paired add-on (the first consumers are meho-automation and meho-ssp)
orchestrates from outside the backplane. It calls `call_operation` several
times for one unit of work, but there is no in-process parent audit row to hang
those dispatches off — each would land as an independent replay root. This
subsystem synthesises the missing parent.

The first `call_operation` a paired service principal issues under a given
`work_ref` **opens an orchestration run**:

- one `addon_orchestration_run` row, keyed by `(keycloak_client_id, work_ref)`,
  carrying a freshly minted `session_id` (the replay anchor) and
  `anchor_audit_id`;
- one `audit_log` "orchestration-root" row (`method=ORCHESTRATION`,
  `path=addon.orchestration`) written under those ids, with no parent.

Every later dispatch for the same `work_ref` **resolves** that run and binds
its `session_id` + `anchor_audit_id` around the dispatch, so the DISPATCH row
`write_audit_row` writes carries the shared session id and back-links to the
anchor. Replay then reconstructs the orchestration and all its resulting
dispatches as one subtree.

**Authorization — linkage only from the paired principal for its own
work_refs.** Linkage is offered only to a `PrincipalKind.SERVICE` principal
whose recovered `client_id` resolves to a live `addon_pairing` in the same
tenant. A run is keyed by the caller's *own* `keycloak_client_id`, so:

- a non-paired caller (a user, an agent, an unpaired service account) gets no
  linkage — dispatches keep their pre-#3028 independent-row behaviour;
- a *different* paired principal presenting the same `work_ref` string opens or
  resolves its own run and can never attach to — or observe — another add-on's
  subtree.

Audit stays synchronous append-only (v0.1-spec §6): the anchor is an ordinary
audit row carrying only its own columns. No back-reference column is added to
`audit_log`; the parent linkage rides the existing `parent_audit_id` /
`agent_session_id` / `work_ref` columns.

## Key types

- `AddonOrchestrationRun` (`db/models.py`) — the per-`(client_id, work_ref)`
  anchor row. `session_id` + `anchor_audit_id` are the durable copies re-bound
  on every later dispatch, the out-of-process analogue of `ApprovalRequest`'s
  `{request_audit_id, agent_session_id}`. Unique index
  `addon_orchestration_run_client_work_ref_idx` on `(keycloak_client_id,
  work_ref)` is both the race-safe resolve-or-open key and the isolation
  boundary. Migration `0082`.
- `operations/addon_orchestration.py` — the linkage service:
  - `resolve_or_open_orchestration_run(operator, work_ref)` → `OrchestrationRun
    | None`. Runs the pairing check, then a SELECT-first resolve-or-open
    (a lost race raises the unique-index `IntegrityError`, caught and retried
    as a SELECT). Returns `None` for any caller not eligible for linkage.
  - `bound_parent_linkage(run)` — async context manager that token-sets
    `agent_session_id_var` + `parent_audit_id_var` and resets them in a
    `finally`. Mirrors `approval_queue._dispatch_resume_with_bound_context`.
  - `ORCHESTRATION_METHOD` / `ORCHESTRATION_PATH` — the anchor row's
    `method` / `path`.
- `Operator.client_id` (`auth/operator.py`) — the caller's OAuth `clientId`,
  recovered at JWT verification by `_extract_client_id` (`auth/jwt.py`) from the
  `service-account-<clientId>` username marker (the same #3178 marker the
  `service` classification keys on). `None` for interactive users and any token
  without the marker. For a paired add-on it equals
  `addon_pairing.keycloak_client_id`.
- `AddonPairingService.get_by_client_id(client_id)` (`operations/addon_pairing.py`)
  — the pairing lookup by the globally-unique clientId; returns the row
  (carrying its `tenant_id` for the caller to cross-check).

## Control flow

`_call_operation_impl` (`operations/meta_tools.py`), shared by the MCP tool and
the REST route, is the single hook:

1. bind the per-op `work_ref` arg onto `work_ref_var` (unchanged, #1657);
2. read the *effective* work_ref (the arg or an ambient `Meho-Work-Ref`
   header) off `work_ref_var`;
3. if non-empty, `resolve_or_open_orchestration_run(operator, work_ref)`:
   - not a `SERVICE` principal, or no `client_id` → `None`;
   - `client_id` has no live pairing, or the pairing is another tenant's →
     `None`;
   - otherwise SELECT the existing run, or open one (insert the run row + write
     the anchor audit row in one transaction);
4. if a run came back, enter `bound_parent_linkage(run)` on an `AsyncExitStack`
   so `agent_session_id_var` + `parent_audit_id_var` are set for the dispatch;
5. `dispatch(...)` runs; `write_audit_row` reads those vars into the DISPATCH
   row's columns; the exit stack resets them.

Opening a run writes the run row and the anchor audit row in the same session
so a run can never reference an anchor id that has no `audit_log` row (which
would re-scatter replay into per-dispatch roots).

## Dependencies

- `operations/_audit.py` — the `agent_session_id_var` / `parent_audit_id_var` /
  `work_ref_var` contextvars and the `write_audit_row` reader. Unchanged by
  this task; the linkage only binds vars it already reads.
- `audit_query/replay.py` — `replay_session` (recursive CTE) is unchanged: it
  already descends `parent_audit_id`, so a correctly-parented dispatch row
  surfaces under the anchor with no query change.
- `operations/addon_pairing.py` — `get_by_client_id` + the `_is_unique_violation`
  dialect-portable helper.
- `auth/jwt.py` / `auth/operator.py` — the `client_id` recovery seam.

## Known issues / follow-ups

- **No dedicated REST read surface** for orchestration runs. Operators reach a
  run's subtree the existing way: `GET /api/v1/audit?work_ref=<ref>` surfaces
  the rows (all carrying the synthesized `session_id`), then
  `GET /api/v1/audit/sessions/{session_id}/replay` renders the tree. A
  first-class "list my orchestration runs" endpoint is a possible follow-up,
  not required by the task.
- **clientId recovery is username-derived.** `client_id` is stripped from the
  `service-account-<clientId>` marker, not read from a dedicated `azp` claim.
  That keeps it consistent with the #3178 service classification and needs no
  new Keycloak mapper, but a realm that stops emitting the marker would drop
  the linkage (the dispatch would simply fall back to an independent audit
  row — fail-open into the pre-#3028 behaviour, never a wrong subtree).
- **Run rows are not reaped.** An orchestration run row is small and durable;
  a retention sweep (mirroring approval-request TTLs) is a later concern.
- This generalises the "verify the caller's client id against the pairing's
  `keycloak_client_id`" hardening item named in
  [`addon-pairing.md`](addon-pairing.md) §Known issues — the same clientId ↔
  pairing binding, applied to audit parent-linkage.

## References

- Initiative #2900 (add-on pairing contract, scope item 4), Task #3028.
- #2086 — the in-process replay-as-tree lineage this extends
  (`operations/approval_queue.py` `_dispatch_resume_with_bound_context` is the
  re-bind precedent).
- #3025 / [`addon-pairing.md`](addon-pairing.md) — the pairing identity plane
  this linkage authorizes against.
- #3178 — service-account (`principal_kind=service`) classification, the
  `client_id` recovery seam's basis.
- v0.1-spec §6 (synchronous append-only audit), §4 (JSONFlux); CLAUDE.md
  postulate 7 (audit lineage).
