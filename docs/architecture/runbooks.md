<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Runbooks (G12)

> Read [CLAUDE.md](../../CLAUDE.md) for the postulates that scope this substrate. Sister to [operations-substrate.md](operations-substrate.md) — runbook step bodies dispatch through the G0.6 dispatcher that doc owns, and the audit-log correlation columns this doc covers are the same `run_id` / `step_id` the engine binds around those dispatches.
>
> Covers Goal [#1195](https://github.com/evoila/meho/issues/1195). The three Initiatives that landed the substrate are [G12.1 #1196](https://github.com/evoila/meho/issues/1196) (schemas + migration + audit-log correlation), [G12.2 #1197](https://github.com/evoila/meho/issues/1197) (template lifecycle), and [G12.3 #1198](https://github.com/evoila/meho/issues/1198) (run lifecycle + adherence floor). The matching authoring-side doc is [`docs/runbooks/authoring.md`](../runbooks/authoring.md) — same surface, different audience (this doc is for substrate reviewers; the authoring doc is for the senior + agent pair walking a procedure through to publish).
>
> What this doc does **not** cover: the authoring UX, the CLI verbs (G12.5), session priming (G12.4), or per-tenant runbook galleries. Those are downstream consumers of this substrate.

## What this substrate does

One sentence: stores procedures as published, versioned, immutable templates; lets one operator at a time run them step by step through a verify-gated state machine; and records every operation_call dispatch + every abort against the same `audit_log` rows the rest of the chassis writes, correlated by `run_id` + `step_id`.

A runbook captures tribal knowledge a senior runs twice a year and a junior has never run — cert rotation, host onboarding, vault unseal-after-restart — as a database row the substrate can hand to a less-experienced operator and gate step by step. The framing argument is [#1195](https://github.com/evoila/meho/issues/1195); the determinism postulate the verify and substitution surfaces inherit is [#1177](https://github.com/evoila/meho/issues/1177).

Five run-lifecycle MCP tools (REST parallels at `/api/v1/runbooks/runs/*`):

```
meho.runbook.start(template_slug, target, params?) -> {kind: "current_step", run_id, current_step, ...}
meho.runbook.next(run_id, last_verified, verify_response?) -> {kind: "current_step" | "completed", ...}
meho.runbook.abort(run_id, reason) -> {run_id, state: "abandoned", abandoned_at}
meho.runbook.list_runs(filter?, assignee?, status?, limit?) -> [{run_id, template_slug, ...}]
meho.runbook.reassign(run_id, new_assignee) -> {run_id, assigned_to, reassigned_at}  # TENANT_ADMIN
```

The six template-lifecycle tools (`meho.runbook.draft_template` / `.edit_template` / `.publish_template` / `.deprecate_template` / `.list_templates` / `.show_template`) live on the same MCP surface from G12.2 and are documented in the authoring counterpart.

The dotted names are canonical as of #1612; the original flat `runbook_*` names remain callable as deprecated aliases for one release (removed in v0.14.0), and the template id is `template_slug` on every tool (`slug` accepted as a deprecated alias on the template verbs for the same window). See [`docs/codebase/mcp.md`](../codebase/mcp.md) §Tool naming grammar.

## The three entities

Migration [`0034_runbook_tables_and_audit_correlation`](../../backend/alembic/versions/0034_runbook_tables_and_audit_correlation.py) provisions all three tables in one revision; the same migration adds the `run_id` / `step_id` correlation columns to `audit_log`.

### `runbook_templates`

One row per `(tenant_id, slug, version)`. Source: [`db/models.py::RunbookTemplate`](../../backend/src/meho_backplane/db/models.py).

| Column                   | Type        | Notes                                                                                                                              |
|--------------------------|-------------|------------------------------------------------------------------------------------------------------------------------------------|
| `id`                     | UUID PK     |                                                                                                                                    |
| `tenant_id`              | UUID NOT NULL | Hard-scoped — every read is tenant-filtered at the service layer.                                                                  |
| `slug`                   | Text        | Operator-facing identifier (e.g. `"vcenter-9.0-cert-rotation"`).                                                                   |
| `version`                | Int         | Per-slug monotonic. `1` on draft; bumped by fork-on-edit; never decremented.                                                       |
| `title`                  | Text        | Operator-facing display name (e.g. `"vCenter 9.0 certificate rotation"`).                                                          |
| `description`            | Text        | One-paragraph summary of the procedure shown alongside the title in list views.                                                    |
| `target_kind`            | Text NULL   | Optional classifier for what the run's `target` refers to (`"host"`, `"cluster"`, etc.). `NULL` when the procedure is target-agnostic. |
| `steps`                  | JSONB       | The full step list + verify gates + substitution allowlist (validated against [`schemas.RunbookTemplateBody`](../../backend/src/meho_backplane/runbooks/schemas.py) at every write — the Pydantic class validates the `steps` column, there is no `body` column). |
| `status`                 | Text CHECK  | `'draft'` / `'published'` / `'deprecated'`. Closed vocabulary, DB-enforced.                                                        |
| `created_by`, `edited_by`| Text        | Operator subjects. Authoring audit lives on the row alongside the body.                                                            |
| `created_at`, `edited_at`| timestamptz |                                                                                                                                    |

`(tenant_id, slug, version)` is the canonical natural key (enforced by the only unique index on the table, `runbook_templates_tenant_slug_version_idx`). The **one-draft-per-slug** invariant is **not** DB-enforced — the model's own docstring at [`db/models.py`](../../backend/src/meho_backplane/db/models.py) (the indexes section on `RunbookTemplate`) calls out "no partial-index split". Instead the invariant is enforced at the service layer in [`RunbookTemplateService.create_draft()`](../../backend/src/meho_backplane/runbooks/service.py) (lines 144-170): the method calls `_resolve_latest_version` for the slug and raises `DuplicateDraftError` if any row exists. The check is read-then-write inside a single async session that does not lock the slug, so two concurrent `create_draft` calls racing on the same fresh slug can both pass the existence check and both insert — the unique index on `(tenant_id, slug, version)` is the backstop that turns the second insert into an integrity error (both inserts land with `version=1`). The TOCTOU window is narrow and authoring is a low-frequency human action; the discipline accepts it rather than pay the cost of an advisory lock or a partial unique index.

### `runbook_runs`

One row per started run, pinned to the `(template_slug, template_version)` that was the latest published at start time. Source: [`db/models.py::RunbookRun`](../../backend/src/meho_backplane/db/models.py).

| Column            | Type        | Notes                                                                                                                       |
|-------------------|-------------|-----------------------------------------------------------------------------------------------------------------------------|
| `run_id`          | UUID PK     | The opaque handle every tool returns and every subsequent call passes back.                                                 |
| `tenant_id`       | UUID NOT NULL |                                                                                                                             |
| `template_slug`, `template_version` | Text, Int | Pinned at start time. Template edits after start do not move the run; in-flight runs finish on the version they started on. |
| `assigned_to`     | Text NOT NULL | The single assignee. Mutated by `reassign_run`; refused on `next_step` for any other caller (per [#1198](https://github.com/evoila/meho/issues/1198)). |
| `target`          | Text        | The run's subject — the host, the cluster, the cert thumbprint. Substituted into step bodies as `${run.target}`.            |
| `params`          | JSONB       | The flat substitution context for `${run.params.X}`. Validated at start (every referenced key must be present) and frozen for the run's lifetime. |
| `state`           | Text CHECK  | `'in_progress'` / `'completed'` / `'abandoned'`. Closed vocabulary, DB-enforced.                                            |
| `started_by`, `started_at`  | Text, timestamptz | The original operator + when the run started. Outlives reassign — `assigned_to` may change, `started_by` does not.   |
| `completed_at`, `abandoned_at` | timestamptz NULL | Mutually exclusive; both `NULL` for `in_progress`.                                                              |

### `runbook_run_step_states`

One row per `(run_id, step_id)` — there are `len(template.steps)` of these for every run, all created at start time. Source: [`db/models.py::RunbookRunStepState`](../../backend/src/meho_backplane/db/models.py).

| Column            | Type        | Notes                                                                                                                       |
|-------------------|-------------|-----------------------------------------------------------------------------------------------------------------------------|
| `run_id`          | UUID FK     | Composite PK with `step_id`.                                                                                                |
| `step_id`         | Text        | The template-author-defined slug (`"revoke-old-cert"`, `"rotate-credentials"`), not a UUID — same shape `audit_log.step_id` carries. |
| `state`           | Text CHECK  | `'pending'` / `'in_progress'` / `'verified'` / `'failed'`. The verify state machine below transitions through these.        |
| `started_at`, `verified_at` | timestamptz NULL |                                                                                                                  |
| `verify_response` | JSONB NULL  | The persisted answer to the step's verify gate — operator `{"type":"confirm","answer":"yes"}` for a confirm step or the dispatched call's actual result for an operation_call step. Replay-grade — every transition's input is on the row. |

At `start_run` time the service inserts one row per step: the first marked `in_progress` (`started_at = now`), the rest `pending`. Forward references like "step 5" do not appear in any tool's response (see [The opacity contract](#the-opacity-contract)) — but the row exists, so the database can answer `"how many steps did this run touch?"` after the fact.

## The opacity contract

The load-bearing adherence mechanism. **`meho.runbook.next` returns the body of exactly one step — the one the operator is currently on, with `${run.target}` / `${run.params.X}` resolved — and the agent cannot see step 3 while the run is on step 2 because the response shape has no field for it.** Verification gating, session priming, and `meho.runbook.list_runs` projections are downstream consequences of this invariant, not parallel safety layers.

Why opacity is the only mechanism that matters: an operator who can see the whole template can — and will, under pressure — skip ahead, run step 3 out of order, infer the verify shape from a future step and pre-answer it, or copy-paste step 5's command into a terminal while the substrate believes they are still on step 2. None of those moves are *attacks*; they are the path-of-least-resistance shape adherence loses to. Removing the substrate's *ability* to surface future steps removes the temptation by construction. This is [#1198](https://github.com/evoila/meho/issues/1198)'s framing argument and [#1177](https://github.com/evoila/meho/issues/1177)'s determinism-over-expressivity call applied to the run surface.

Opacity is enforced redundantly at **four layers**, because no single layer is the trust boundary — if one layer leaks, the others catch it.

1. **Schema layer.** [`runs_schemas.StepBody`](../../backend/src/meho_backplane/runbooks/runs_schemas.py) carries one step's `id` / `title` / `body` / `type` / `op_id?` / `params?` / `verify` and **no fields that could carry an adjacent or future step.** The Pydantic model is `frozen=True` and there is no overload that returns multiple bodies. The schema is the floor.
2. **Function-signature layer.** [`engine.current_step_body(template, step_id, *, target, params)`](../../backend/src/meho_backplane/runbooks/engine.py) returns exactly one substituted `StepBody`. By signature it cannot return a list. The regression test `test_current_step_body_returns_only_one_step` walks the serialised result and asserts no other step ids appear.
3. **Service layer.** [`run_service.RunbookRunService.next_step()`](../../backend/src/meho_backplane/runbooks/run_service.py) constructs the response as a `NextStepResponse` — the discriminated union of `CurrentStepResponse` (one body) and `RunCompletedResponse` (terminal marker, no step content). There is no third response variant. The service hands the engine one step id at a time; it never asks the engine for "the next two steps" or "the step list".
4. **Transport layer.** Both REST ([`api/v1/runbook_runs.py`](../../backend/src/meho_backplane/api/v1/runbook_runs.py), T5 [#1311](https://github.com/evoila/meho/issues/1311)) and MCP ([`mcp/tools/runbook_runs.py`](../../backend/src/meho_backplane/mcp/tools/runbook_runs.py), T6 [#1313](https://github.com/evoila/meho/issues/1313)) type their responses as `NextStepResponse`. The MCP tool's load-bearing description spells out the contract in capitals (`THE OPACITY CONTRACT`, `WHEN A STEP FAILS`, `SINGLE-ASSIGNEE`, `no skip, no force_advance`), and a regression test pins down the verbatim strings so a future "polishing" edit cannot dilute them.

The `meho.runbook.list_runs` projection ([`runs_schemas.RunSummary`](../../backend/src/meho_backplane/runbooks/runs_schemas.py)) is the same shape on the read-many path — it carries `current_step_id` (so a UI can render "step 3: drain-node") but never the step body. The id is enough for routing; the body is the part adherence cares about.

## The verify state machine

Every step-state row walks the same automaton. The state column is the source of truth; the engine reads it on every `next_step` call to decide what's allowed.

```
                       (start_run)
                          │
                          ▼
                  ┌─── pending ───┐
                  │               │
            (engine advances     (engine advances
             to this step)        to this step)
                  │               │
                  ▼               ▼
            in_progress     in_progress
                  │               │
        (verify.type=          (verify.type=
            'confirm')         'operation_call')
                  │               │
                  ▼               ▼
           answer == "yes"?   _matches(actual, expect)?
                  │               │
            ┌─────┴─────┐   ┌─────┴─────┐
            │           │   │           │
            ▼           ▼   ▼           ▼
        verified    failed verified  failed
            │           │   │           │
            │           │   │           │
            ▼           ▼   ▼           ▼
        advance     abort   advance   abort
        to next     OR      to next   OR
        step        reassign step     reassign
                    (no             (no
                    force_advance,  force_advance,
                    no skip)        no skip)
```

The transitions are owned by [`engine.advance()`](../../backend/src/meho_backplane/runbooks/engine.py) (pure function, no DB) and applied by [`run_service.RunbookRunService.next_step()`](../../backend/src/meho_backplane/runbooks/run_service.py) (the only module that writes the table).

What the diagram does **not** show, by design:

- **No `skip` transition.** There is no `runbook_skip_step` tool. A step the substrate can't verify stays `in_progress`; the operator's only path forward is `meho.runbook.abort`.
- **No `force_advance` transition.** There is no `runbook_force_advance` tool. A step marked `failed` does not have a path back to `in_progress` — the operator either aborts or asks a senior to `reassign` to themselves and decide.
- **No `set_state` transition.** There is no `runbook_set_step_state` tool. The state column is mutated only by the engine + service; the surface is "advance one" (`next_step`) or "tear down" (`abort_run`). The acceptance gate at the Initiative DoD enforces this with a lint-time grep.

This is the no-DSL discipline made structural. A substrate that exposes "skip" or "force-advance" can be operated *around* the verify gate; a substrate without those exits can only be operated *through* it.

The `verify_response` JSONB column on every step-state row is the **replay-grade** record of how the step transitioned. For a `confirm` step it carries the operator's literal `{"type":"confirm","answer":"yes" | "no" | "escalate"}`; for an `operation_call` step it carries `{"type":"operation_call","matched":<bool>,"actual":<dispatched-call-result>}` — the dispatched call's raw result, retained verbatim for the mismatch-case forensics. The audit reviewer can reconstruct exactly what the operator was asked and exactly what they (or the dispatcher) answered, version-pinned to the template the run pinned at start.

## Verify minimalism

`confirm` and `operation_call` are the only two verify shapes the substrate accepts. There is no JSONPath. There are no comparison operators (`<`, `>=`, `contains`). There is no boolean composition (`AND` / `OR`). The publish-time validator at [`schemas.Verify`](../../backend/src/meho_backplane/runbooks/schemas.py) refuses anything richer; the engine has no code path to evaluate it if it slipped through.

Why so flat:

- **`confirm`** — the substrate shows the `prompt` text to the operator and only an affirmative answer advances. The *human* is the oracle. This is the right shape whenever a check needs judgement (a screenshot looks right, a banner shows the expected version string, the cluster console reports green). Anything that needs more than equality belongs in a `confirm`.
- **`operation_call`** — the substrate dispatches a real call (`op_id` + `params`) through the same [`call_operation()`](../../backend/src/meho_backplane/operations/meta_tools.py) the agent surface uses, and compares the result against `expect` by **structural equality + presence match**: every key in `expect` must be present in the result with structurally equal value. Extra keys in the result are ignored. Dicts compared recursively. Lists compared element-wise. Scalars compared by `==`. The match implementation is [`engine._matches()`](../../backend/src/meho_backplane/runbooks/engine.py).

A DSL was deliberately rejected at design time per [#1177](https://github.com/evoila/meho/issues/1177). Expressivity at the verify layer would have turned every runbook into a tiny program a contributor could break in unobservable ways; equality + presence is small enough that a reviewer can read it once and trust it forever.

## The substitution pipeline

Two allowlisted patterns, applied at advance time:

- `${run.target}` — the run's `target` column (the host, the cluster, the cert thumbprint).
- `${run.params.X}` — one of the run's `params` keys, where `X` matches `[a-z_][a-z0-9_]*`. Nested paths like `${run.params.x.y}` are not allowed.

Defense in depth at two layers:

- **Publish time.** [`schemas.validate_substitutions()`](../../backend/src/meho_backplane/runbooks/schemas.py) walks the whole template body recursively (dict values *and* keys) and refuses publish if any `${...}` token that is not one of the two allowlisted patterns appears. This is the gate the authoring flow ([`docs/runbooks/authoring.md`](../runbooks/authoring.md)) hits when a senior tries to drop a `${secrets.api_key}` into a step body.
- **Advance time.** [`substitution.resolve_substitutions()`](../../backend/src/meho_backplane/runbooks/substitution.py) replaces the two allowed patterns when the engine builds the `StepBody`. Any other `${...}` pattern in the body passes through verbatim — *because the publish gate guaranteed nothing else can be present*. The runtime helper is deliberately storage-blind and stays narrow; the publish gate is the gate, and the runtime is the application.

The two layers enforce the same allowlist independently. A bypass at publish (a CHECK constraint disabled, a migration corruption) is still caught at advance because the runtime helper has no code path that resolves a non-allowlisted pattern. A regression at advance (someone "improves" `_resolve_string` to handle `${env.X}`) is still caught at publish because the publish validator rejects the pattern before the template lands.

## Dispatcher correlation

Every `operation_call` step dispatch — both the step execution itself and the dispatched verify call — writes one row to `audit_log` with `run_id` and `step_id` populated. This is the audit floor that lets G8 reconstruct the full dispatch lineage of a run after the fact.

How the columns get populated:

1. The service binds [`operations._audit.run_id_var`](../../backend/src/meho_backplane/operations/_audit.py) and `step_id_var` (G12.1-T2 [#1294](https://github.com/evoila/meho/issues/1294)) before it calls `call_operation()`. Both are `ContextVar` — bound at the service boundary, propagated automatically down the async call chain that `call_operation()` triggers (validation, policy, JSONFlux reduction, audit, broadcast).
2. The chassis audit writer in [`audit.py`](../../backend/src/meho_backplane/audit.py) reads both contextvars at write time and stamps the values onto the dedicated `audit_log.run_id` / `audit_log.step_id` columns (provisioned by migration `0034`). The values also land in the JSON `payload` mirror, but the column-level write is the indexed path G8's query surface uses.
3. After the dispatched call returns, the service unbinds (the `ContextVar.set()` / `.reset()` token pair). Subsequent audit writes outside a runbook step execution see `None` and leave the columns NULL.

Audit query path: `SELECT * FROM audit_log WHERE run_id = ?` reconstructs the full dispatch lineage of a run — every `operation_call` step's dispatch, plus the row [`run_service.abort_run()`](../../backend/src/meho_backplane/runbooks/run_service.py) writes directly when the run terminates. The audit-query surface ([G8.1, `query_audit`](audit.md)) accepts `run_id` as a filter; the operator-facing CLI ([G8.2 #219](https://github.com/evoila/meho/issues/219)) will surface it as `meho audit query --run-id <uuid>` when the verb ships.

`confirm` verifies do *not* dispatch a call — the operator is the oracle — so they write no audit row of their own. Their answer lands in `runbook_run_step_states.verify_response`; the audit story for a confirm-only runbook is "the step-state rows themselves are the audit." A run that mixes confirm and operation_call steps yields a mixed audit shape: rows in `audit_log` for the operation_call dispatches, rows in `runbook_run_step_states` for everything else.

## Single-assignee enforcement

One person at a time owns a run. The substrate refuses every other shape.

- **Start auto-assigns.** [`run_service.RunbookRunService.start_run()`](../../backend/src/meho_backplane/runbooks/run_service.py) sets `assigned_to = operator.sub` unconditionally. There is no `assignee` parameter on `meho.runbook.start` — you cannot start a run on someone else's behalf.
- **`next_step` refuses non-assignees with 403.** The check at [`run_service._require_run_assignee()`](../../backend/src/meho_backplane/runbooks/run_service.py) runs before any state-machine logic. **TENANT_ADMIN callers who are not the assignee still get 403** — the role bypass is deliberately not wired in. The right path for a senior to take over is `meho.runbook.reassign` (admin-only); the wrong path is "operate as admin around the assignee check," which would silently corrupt the audit story (`audit_log.operator_sub` would attribute the dispatch to the admin while `runbook_runs.assigned_to` still names the junior).
- **`abort_run` widens to assignee-or-admin.** A senior who finds a stuck run someone else owns can abort it without first reassigning to themselves — the route layer passes a `caller_is_admin=True` flag to the service, which checks `caller_sub == run.assigned_to OR caller_is_admin`. The audit row the abort writes carries the admin's `operator_sub`, so the override is visible in the trail.
- **`reassign_run` is admin-only.** The route gate at [`api/v1/runbook_runs.py`](../../backend/src/meho_backplane/api/v1/runbook_runs.py) requires `TENANT_ADMIN`; the MCP tool's `required_role` is the same. After reassign, the previous assignee's next `meho.runbook.next` call is a 403; the new assignee's next call advances.

There are exactly two paths to take over a stuck run:

- **Senior reassigns to self.** The senior reads the current state via `meho.runbook.list_runs` (which returns every in-progress run for `TENANT_ADMIN`, only the caller's own runs for `OPERATOR`), decides the right move, then `meho.runbook.reassign(run_id, new_assignee=<self>)` and continues forward.
- **Junior aborts, senior restarts.** The junior calls `meho.runbook.abort(run_id, reason="<why>")` to terminate the stuck run with an auditable reason; the senior starts a fresh run on the same template against the same target.

There is no parallel observer mode. A senior watching a junior do a cert rotation observes through the audit trail (the dispatch rows the junior writes as they advance) or by reading `meho.runbook.list_runs` and pulling the current step from the run's recorded state. The substrate offers no `runbook_watch_run` or live-attach surface — that would have required a publish-subscribe layer the determinism postulate's minimalism call ruled out.

## The post-completion exception

The opacity floor is "while the run is in progress, the operator can read one step at a time and nothing else." There is one carve-out, owned by G12.3-T4 [#1309](https://github.com/evoila/meho/issues/1309): once a run reaches `state ∈ {completed, abandoned}`, the operator who ran it can read the *whole* template for post-mortem.

Mechanics:

- The predicate lives at [`run_service.RunbookRunService.can_show_template_post_completion()`](../../backend/src/meho_backplane/runbooks/run_service.py) (lines 712-748). It checks `EXISTS(runbook_runs WHERE tenant_id = ? AND assigned_to = ? AND template_slug = ? AND template_version = ? AND state IN ('completed', 'abandoned'))` — pinned to the *exact* `(slug, version)` the operator finished, not the slug across all versions. The predicate keys on `assigned_to` at terminal state, **not** on `started_by` — after a reassign, only the final assignee (the operator accountable for the run's outcome) gets the post-completion read; the original starter does **not** retroactively inherit it. So a junior who started a run and then handed it off to a senior via `meho.runbook.reassign` no longer holds the carve-out once the senior completes or abandons it. This matches the substrate's broader "the assignee is the operator of record" posture (`assigned_to` is mutable; `started_by` is immutable and lives on the row purely for forensics).
- The decision lives at the boundary, not the service. [`api/v1/runbook_templates.py::_show_template_operator()`](../../backend/src/meho_backplane/api/v1/runbook_templates.py) (and its MCP twin [`_show_template_operator_path()`](../../backend/src/meho_backplane/mcp/tools/runbooks.py) in `mcp/tools/runbooks.py`, called from `_show_template_handler`) first runs the role gate (`OPERATOR` callers get 403 by default), then calls the predicate, and *lifts* the 403 to a 200 with the body when the predicate returns `True`. The service is intentionally caller-identity-blind — the boundary has the JWT, the boundary makes the call. The split keeps the predicate pure (testable in isolation, no JWT machinery) while keeping the gating decision auditable at the surface.

Why `in_progress` does not qualify: the opacity floor is what makes adherence real *during* the run. An operator who can read the whole template mid-run can skip ahead just as freely as a substrate that surfaces every step. The post-completion lift only fires after the state has settled — by then the procedure either succeeded (and the operator earned the right to study it) or failed (and the operator earned the right to debug what they ran). Either way, the run is done, and the lift cannot affect adherence on the run it's attached to.

Why version-specific: completing v1 of `vcenter-cert-rotation` does not authorize reading v2. The carve-out is for *the procedure the operator actually ran*, not for everything that ever lived under the slug. A senior who reissues a template at v2 with a different verify shape has not retroactively granted the junior who finished v1 permission to read the new procedure.

## Status transitions for templates

The template lifecycle has its own three-state machine, owned by G12.2. Recapped here so the run-side reader knows which template versions can be started against.

- **`draft`.** Mutable in place. `meho.runbook.edit_template(template_slug, body)` overwrites the body; `version` does not move. Exactly one draft per slug at any time — service-enforced, not DB-enforced ([`RunbookTemplateService.create_draft()`](../../backend/src/meho_backplane/runbooks/service.py) at lines 144-170 refuses a second draft via the `_resolve_latest_version` existence check; see also the invariant statement on `_load_draft` at lines 477-498). The senior can keep appending across multiple sessions — `meho.runbook.show_template` reads the current draft back, the next `meho.runbook.edit_template` persists the update.
- **`published`.** Immutable. The body is pinned; the row is the source of truth for every run started against this `(slug, version)`. `meho.runbook.publish_template(template_slug, version)` flips the status; idempotent on already-published rows.
- **`deprecated`.** Read-only. New `meho.runbook.start` against this version is refused with `DeprecatedTemplateError`. In-flight runs continue — they were pinned to the version at start time and they finish on it.

Editing a published version triggers **fork-on-edit**: `meho.runbook.edit_template(template_slug, body)` when there is no open draft creates a new row at `max(version) + 1` with `status='draft'`, returns a `forked_from` block carrying `in_flight_run_count` (the number of in-progress runs still pinned to the version being forked from). The forked draft is independent — editing it does not retroactively change the in-flight runs' step list. See [`docs/runbooks/authoring.md`](../runbooks/authoring.md) for the full senior-facing semantics; the algebra lives in [`runbooks/service.py`](../../backend/src/meho_backplane/runbooks/service.py).

What this means for `meho.runbook.start`:

- Against a slug with at least one `published` (non-deprecated) version → pinned to the latest such version.
- Against a slug whose only versions are `deprecated` → refused with `DeprecatedTemplateError` (the operator asks the senior to publish a fresh version).
- Against a slug that has no published or deprecated row (drafts only) → refused with `TemplateNotFoundError` (drafts are not a start target by design).
- Against a non-existent slug → refused with `TemplateNotFoundError`.

## Out-of-band considerations

The runbook substrate is deliberately small. The following are **not** part of G12, by design, and the surface refuses them at publish / start time:

- **Branching steps / conditional flow.** Steps are an ordered list. If a procedure genuinely branches, that is two templates, not one with a conditional. The publish-time validator has no `if` node; the engine has no `evaluate_branch` code path.
- **Parallel steps.** Single-threaded execution by design. Each step's verify gate must pass before the next step is shown; the gating does not compose with parallelism.
- **Step-level templating engine beyond `${run.target}` / `${run.params.X}`.** No `${env.X}`, no `${secrets.X}`, no nested-path `${run.params.x.y}`, no shell-style `$VAR`. The two allowlisted patterns are the whole grammar.
- **Verify DSL.** No JSONPath. No `<` / `>=` / `contains`. No `AND` / `OR`. Per [#1177](https://github.com/evoila/meho/issues/1177).
- **Auto-escalation / paging / inbox surfaces.** Humans coordinate handoffs in chat. A stuck run is escalated by the junior pinging the senior in Slack; the senior reassigns via `meho.runbook.reassign` and takes over. The substrate ships no notification, no inbox, no "needs attention" queue.
- **Orchestrator semantics.** MEHO is the substrate; the agent (Claude or a sibling) is the orchestrator. The substrate gates one step at a time; the agent decides what to ask the operator and how to render the answer. A "runbook engine" that drives the procedure on the agent's behalf is out of scope for the entire Goal.

Each item is a deliberate "no" — the surface that was rejected was considered and ruled out, not forgotten. The combined effect is a substrate small enough to reason about end to end in one sitting; expressivity that would have made the substrate larger lives in the templates themselves (a senior writes more steps) or in the agent layer (the orchestrator asks better questions).

## Cross-references

- Goal [#1195](https://github.com/evoila/meho/issues/1195) — G12 Runbooks (the outcome).
- Initiative [#1196](https://github.com/evoila/meho/issues/1196) — G12.1 schemas + migration + audit-log correlation.
- Initiative [#1197](https://github.com/evoila/meho/issues/1197) — G12.2 template lifecycle.
- Initiative [#1198](https://github.com/evoila/meho/issues/1198) — G12.3 run lifecycle + adherence floor (the load-bearing initiative).
- G12.3 Tasks: [#1300](https://github.com/evoila/meho/issues/1300) (T1 schemas), [#1301](https://github.com/evoila/meho/issues/1301) (T2 engine + substitution), [#1308](https://github.com/evoila/meho/issues/1308) (T3 service), [#1309](https://github.com/evoila/meho/issues/1309) (T4 show_template carve-out), [#1311](https://github.com/evoila/meho/issues/1311) (T5 REST), [#1313](https://github.com/evoila/meho/issues/1313) (T6 MCP).
- G12.1 audit correlation: [#1294](https://github.com/evoila/meho/issues/1294).
- G12.2 publish-time substitution validator: [#1295](https://github.com/evoila/meho/issues/1295).
- Design source: [#1191](https://github.com/evoila/meho/issues/1191).
- Determinism postulate: [#1177](https://github.com/evoila/meho/issues/1177).
- Sibling architecture docs: [`docs/architecture/audit.md`](audit.md) (the audit query surface this doc's correlation feeds), [`docs/architecture/mcp.md`](mcp.md) (the MCP tool registry the run tools register against), [`docs/architecture/operations-substrate.md`](operations-substrate.md) (the dispatcher every `operation_call` step routes through).
- Authoring counterpart: [`docs/runbooks/authoring.md`](../runbooks/authoring.md) — the senior + agent walking a procedure through to publish.
- The CLI doc (`docs/cli/runbook.md`) lands separately with G12.5.
