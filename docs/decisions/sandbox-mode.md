# Sandbox mode: a standing grant may also cover deletes on an instance for disposable test systems (decision)

**Status:** decided — program owner, 2026-10-19. This record: 2026-10-20, revised the same day after a second review. Amended 2026-10-26: a scoped login on a shared vCenter.
**Issue:** [#4622](https://github.com/evoila/meho/issues/4622), step 2. Source: the [step 1 review](https://github.com/evoila/meho/issues/4622#issuecomment-6286729259).
**Initiative:** [#4621](https://github.com/evoila/meho/issues/4621) — sandbox mode for disposable test systems
**Adds one exception to:** requirement 1 of [governed-delete-operations.md](governed-delete-operations.md), on a sandbox instance only
**Same pattern as:** the boot-only switch for delegated decisions ([#3693](https://github.com/evoila/meho/issues/3693))
**Amended 2026-10-28:** a boot setting lets REST and the CLI create delete grants too ([#5251](https://github.com/evoila/meho/issues/5251)); see "Grants in sandbox mode".

## The decision

- A MEHO instance can run in **sandbox mode**. It is only for an instance that holds disposable test systems.
- In sandbox mode, a person may give a service principal a standing grant that also covers deletes. So an automated test can build, change and delete its own test systems, and nothing waits for a person.
- Sandbox mode is off by default. It is set when the instance is installed, and it stays the same for the life of the instance's database.
- Every other instance keeps today's permission decisions: the same calls run, wait or are refused. Two things change there too: `/ready` and `GET /api/v1/health` show a new field (`"off"`), and every audit row gets a new mark (`false`).
- **An older MEHO build can never use a delete grant**, also after a rollback. Delete grants are stored in a new table that older builds never read (see "Older builds and rollback").
- A sandbox runs on its own site: its own MEHO, Keycloak and Vault, and no network route to any other system. Only disposable systems are registered there as targets. These walls take the place of a person's approval of each delete. They belong to the site, not to MEHO code.
- The walls protect other sites. They do not protect one test from another on the same sandbox, or the sandbox's own control systems (see "Limits").
- On a sandbox, AI agents sign in as service principals, on purpose. #3325 and #3980 describe service principals as fixed automation, such as build pipelines; this record does not change that for other instances.
- **A target on a shared vCenter** (amended 2026-10-26). A sandbox may also have a target on a shared vCenter, but only with a scoped login. For that target, the scoped login's permission scope is the wall. MEHO cannot check it, because a grant names a target, not the VMs behind it. So the scoped login must reach only disposable systems. The sandbox still holds only disposable systems: the vCenter itself is not one, but through the scoped login MEHO reaches nothing else.

## Words used

- **Disposable system:** a system built for one test and deleted after it.
- **Service principal:** a machine login: a Keycloak client that signs in with its own secret.
- **Standing grant:** a person's approval, given in advance, for one service principal, one operation and one target, or one kind of target (a target selector, [#3349](https://github.com/evoila/meho/issues/3349)).
- **Delete grant:** a standing grant that only sandbox mode allows: for an operation with a delete sign, or for a delete step of a multi-step operation. Every other grant is an **ordinary grant** and works as today.
- **Sandbox mode:** one setting for the whole instance. It says that the instance holds only disposable systems.
- **Delete sign:** how the code sees that an operation deletes something: the safety level `destructive`, the HTTP verb `DELETE`, the tag `destructive`, or a delete name (`DELETE:*`, `*.delete`, `*.destroy`, `*.remove`, `*.purge`; [#4632](https://github.com/evoila/meho/issues/4632) adds vendor action names such as `Destroy_Task`).
- **Preview hash:** a check value of a preview result. A `destructive` call must send the value that its preview gave.
- **Blast radius:** what a delete affects: the object, its child objects, and whether it can be undone.
- **Multi-step operation:** one operation that runs several steps (the code calls it a composite). Each step that writes passes its own check, the step gate.
- **Older build:** a MEHO release from before sandbox mode. It does not know the mode.
- **Shared vCenter:** a vCenter that also holds systems that are not disposable.
- **Scoped login:** a vCenter login whose role reaches only the sandbox's own folder, resource pool and datastore.

## The one requirement it narrows

Requirement 1 of the delete decision says today: "Mandatory human approval, always. No standing grant applies, no self-approval, no autonomous-session approval." The new wording:

> 1. **Mandatory human approval, always — except on an instance in sandbox mode.** There, a standing grant may cover a delete by a service principal. A tenant admin creates that grant in the web console, or, with the setting `MEHO_SANDBOX_DELETE_GRANTS_FROM_API` on, through REST or the CLI, and it expires within 30 days. The rest of this requirement is unchanged: no self-approval, no autonomous-session approval.

The same exception applies wherever that record says that no standing grant can cover a delete, and to the cell "service principal, destructive" in the rules table of #3325. The 30 days are answer 2 in "Answered (2026-10-20)".

## What stays

- **Requirement 2, the preview hash.** A `destructive` call needs the preview hash of a `preview_operation` of the same call, also under a grant. Today MEHO checks the hash only when a call waits for a person, so the grant path gets the same check. It runs before MEHO records the grant use, so a refused call leaves no "auto-approved" audit row and no broadcast.
- **Requirement 3, the blast radius, as strict as today.** Today a `destructive` call without a valid blast radius cannot wait for approval: MEHO refuses it with `blast_radius_required` (`docs/codebase/approvals.md`). Under a delete grant, MEHO builds and checks the blast radius the same way, before it records the grant use. A missing or malformed one stops the call, with no grant-use row. The grant-use audit row stores the preview hash and the blast radius in `payload`. Not in `raw_payload`: MEHO clears that after 90 days by default.
- **Deletes below `destructive`** (a `DELETE` verb, a `destructive` tag, a delete name, a delete step) need no preview hash and no blast radius, as today when a person approves them. Their evidence: the grant-use row stores what an approval request would show a person (the proposed effect), and the call's own audit row keeps the operation, the target, a hash of the parameters and the result. The grant-use row of a step also names the audit row of its multi-step operation.
- **Requirement 4, the `destructive` level,** stays as it is.
- **Satellites never get deletes** ([satellite-write-path.md](satellite-write-path.md)). Their checks read only the safety level, never a grant.
- **People, agents and token calls keep their rules** (#3325, #3980). Sandbox mode changes nothing for them. Today, also on a sandbox, a person's delete waits for an approval, and an agent login is refused a delete.
- **A run approval (#4126) never covers a delete**, also in sandbox mode.
- **One list of delete signs.** The setting `SERVICE_GRANT_DELETE_SHAPED_PATTERNS` stays, and sandbox mode uses the same signs.

## Older builds and rollback

- A rollback replaces the program, not the database. MEHO lets an older build run against a newer database on purpose (`docs/codebase/migrations.md`, "Rollback contract").
- An older build does not check the stored mode, and nothing we add now can make it check. Its call-time grant check does not read delete names, and a step of a multi-step operation shows it no delete sign. So it would use a delete grant if it found one in its grant table.
- **The rule:** a delete grant is stored only in a new table, `sandbox_delete_grant`. Ordinary grants stay in `service_principal_grant`, the only grant table that older builds read.
- So an older build on a sandbox database finds no delete grant. Every delete and every delete step waits for a person, as on any other instance. This also holds while old and new builds run side by side during an update.
- MEHO with the mode off never reads or writes `sandbox_delete_grant` either.
- An older build can still start there. It shows no banner, and its audit rows get the mark `false` (the column's database default). The measurement plan finds such rows. The sandbox site only rolls forward.
- The code needs #4632 first. Without it, a vendor delete step such as `Destroy_Task` has no delete sign, so its grant would be an ordinary grant that an older build uses.

## Guards at boot

- Three settings, read at boot only: `MEHO_SANDBOX_MODE` (default `false`), `MEHO_SANDBOX_MODE_CONFIRM` and `MEHO_SANDBOX_DELETE_GRANTS_FROM_API` (default `false`, see "Grants in sandbox mode"). No request, REST route, MCP tool, CLI command or console page can change them.
- MEHO refuses to start, with a plain message, when:
  1. the mode is on, and `MEHO_SANDBOX_MODE_CONFIRM` is missing or is not the host name of the MCP resource URI (the public address of the instance, which MEHO already checks at boot);
  2. the mode is on, and no dot-separated part of that host name is exactly `sandbox`: `meho.sandbox.example` passes, `meho.example` and `meho-sandbox.example` are refused;
  3. the mode stored in the database is not the mode of the settings.
- **The stored mode.** A new one-row table holds it. The upgrade that adds the table stores "off" in every database that existed before it, also in one with no audit rows: an empty history does not prove a new install. Only a database that this release, or a later one, creates from empty can hold "on".
- **Stored once, before any work.** As its first database step at start, before it serves a request or starts a background task, MEHO stores its setting with one insert that only one process can win. Then it reads the stored mode back. So two processes that start at once cannot store different modes: the one whose setting differs refuses to start.
- **Fixed for the life of the database.** To change the mode, install a new instance with a new database. An instance with real systems must never become a sandbox, and all audit rows of one database carry the same mark.
- The guards stop three mistakes: settings copied to another instance (its host name has no `sandbox` part), a sandbox pointed at another instance's database, and a backup restored on an instance with the other mode.
- **What they do not stop.** The host name confirms the settings, not the network walls. A copy of a sandbox database, with matching settings, passes every guard on another site. **Site rule:** a sandbox database and its backups never leave the sandbox site. The guards also do not stop a person who sets every value on purpose. That is by design.

## Grants in sandbox mode

- **Only a tenant admin creates a delete grant, in the web console.** REST and the CLI refuse it, also with a person's token: an AI tool can use a person's login, so a token is never treated as a person (#3325, #3980).
- **The server checks where the request came from.** The check reads a mark that only MEHO's server sets, from its own console session. No token claim, header or request field can set it. #3980 adds this mark (`web_session` on the caller). If this code lands first, the console route sets the same fact in server code, and #3980 replaces it with the mark.
- **A console session does not prove that a person clicked.** An AI tool can drive a browser too. Keeping AI tools out of people's console sessions stays an operating rule. MEHO cannot check it.

With the setting `MEHO_SANDBOX_DELETE_GRANTS_FROM_API` on, REST and the CLI may create them too, with a tenant admin's token. The setting is read at boot and cannot be changed by any request. Decided by the program owner on 2026-10-28: a sandbox instance needs no human step, and the walls of the sandbox site are the control.
- **Every delete grant expires within 30 days** of its creation. Without an expiry, or with a later one, MEHO refuses it. The check sits in `ServicePrincipalGrantService.create`, so REST and the console both get it.
- **Reminders and renewal.** The sandbox banner shows how many delete grants expire within 7 days, with a link to them. A person reviews the delete grants of one service principal together and renews them as one set. Each renewal is checked like a new grant. MEHO never renews a grant by itself.
- A delete grant may name one target or a target selector, like any other grant: test systems are often registered during the run.
- Ordinary grants keep today's rules.
- **Multi-step operations.** A delete step inside a multi-step operation is checked as `dangerous` at its step gate. Like every step that writes, it needs its own grant: in sandbox mode, a delete grant. The step needs no preview hash of its own, as today. A step or child call (a call that a multi-step operation makes to another operation) at the `destructive` level stays refused, because it cannot carry a preview hash.
- The list of steps that need grants (`GET /api/v1/operations/governed-subops`) shows delete steps as grantable when the mode is on.

## Limits: what the walls do not protect

- A grant names a target (for example a vCenter), not the VMs behind it. MEHO cannot tell a disposable VM from another one, and it does not check that a test deletes only its own systems. That is why the site must hold only disposable systems.
- So tests on one sandbox can delete each other's systems. Tests that must not touch each other use different targets, or run one after the other.
- The sandbox's own control systems (its MEHO, Keycloak and Vault, and what they run on) must not sit inside a target that has delete grants. For example, they do not run as VMs in the vCenter that the tests use.
- On a shared vCenter, the sandbox's own control systems may run as VMs, but only outside the scoped login's folder, resource pool and datastore. The scoped login has no rights on them.

## Display and audit mark

- `/ready` gets a top-level field `"sandbox_mode": "on"` or `"off"`, for every caller. It sits outside `features` on purpose: when the metrics token (an optional secret for monitoring) is set, `features` is hidden from callers without it, and the mode must be readable on every instance. `features.approval_queue` says it too.
- `GET /api/v1/health` gets the same field, and `meho status` prints one line when the mode is on.
- Every console page shows a banner when the mode is on: "Sandbox mode: agents may delete systems here without a person's approval."
- The chart gets `sandboxMode.enabled` (default `false`) and `sandboxMode.confirmHost`. With the mode on, `helm template` fails when `confirmHost` is not `ingress.host`, and the chart notes print a warning.
- **Audit mark:** a new column `audit_log.sandbox_mode` (true or false). Its database default is `false`, so an older build, which does not know the column, can still write audit rows. One default in the audit model sets it for every writer. The whole database has one mode, but rows leave it (exports, log stores), and the mark goes with them.

## The verdict table

Test 1 (`backend/tests/test_sandbox_mode_verdict_table.py`) checks the verdict (runs, waits or refused) for every cell of one table: principal kind (person, agent, agent run hosted by MEHO, service principal, satellite runner) × safety level (safe, caution, dangerous, destructive) × `requires_approval` (yes, no) × **delete sign at call time (none, HTTP `DELETE`, tag `destructive`, delete name only)** × **grant (none, ordinary, delete grant)**. It checks a direct call and a step gate.

- Example for the delete sign: an ingested `DELETE` operation is `dangerous`, and today no grant can clear it, because the call-time check refuses the verb.
- "Delete name only" pins a gap of today: the call-time check does not read names, so only grant creation keeps an ordinary grant away from such an operation. #4632 adds vendor action names to the call-time check.
- Mode off: every cell equals today's verdict, and a delete grant counts as no grant. The table is checked in.
- Mode on: only cells with "service principal, delete grant" may change, and only from "waits" to "runs". For `destructive`, "runs" needs the right preview hash and a valid blast radius. At a step gate, `destructive` stays refused. Cells with an ordinary grant do not change.

## Measurement plan

- **Mode off, on every instance that does not set the mode:** the full test suite passes with only the test edits named below, and the verdict table gives the same verdicts as before the change. After the release, `/ready` on every instance we run shows `"sandbox_mode": "off"`.
- **On the first sandbox instance, before its first test run:**
  1. **Network walls, tested actively.** From inside the sandbox, a person tries to reach addresses outside it, over IPv4 and IPv6, and over HTTPS, SSH and DNS. Every try must fail. Quiet network logs alone prove nothing. Repeat this after every network change. One exception: the sandbox may reach the address of a shared vCenter, for its scoped-login target only. Every other try to reach an address outside the sandbox must still fail.
  2. **Rollback drill.** On the sandbox site, start the last release before sandbox mode against a copy of the sandbox database. A service principal's delete and a delete step must wait for a person. Then delete the copy.
  3. **Grant failures.** An expired grant, a revoked grant, a grant for another target or operation, and a grant revoked between two steps of one multi-step operation: the call or step does not run, and no "auto-approved" row is written.
  4. **Permission scope, for a target on a shared vCenter.** Run through the sandbox MEHO with the scoped login:
     - the lists of VMs, hosts, datastores and networks show only the sandbox's own objects;
     - a change outside the scope fails with a permission error (for example a power-off of a control VM, or a file write on another datastore);
     - a delete outside the scope is only previewed, never sent;
     - a person makes delete grants on that target only after this test passed;
     - the test runs again after every change to the role or the login. If it fails, a person revokes the delete grants on that target until the cause is fixed.
- **Over the first 20 completed test runs.** A completed run builds, changes and deletes its own disposable systems through MEHO, and its clean-up is confirmed. A run with failed calls does not count: zero approval requests can also mean that nothing ran.
  1. zero approval requests from service principals;
  2. after each run, its systems are gone, and no object that the run did not create was changed or deleted (an inventory of the targets before and after the run);
  3. the runs include deletes below the `destructive` level (for example a `DELETE` call and a delete step), and their audit rows hold the evidence named in "What stays";
  4. every grant-use audit row of a `destructive` call holds a preview hash and a blast radius;
  5. every audit row carries the mark: no row has `sandbox_mode = false`.
- If one measure fails, a person revokes the delete grants, so deletes wait for a person again, until the cause is fixed.
- After the 20th completed run, the results are added to this record, and the program owner keeps or changes the decision.

## Alternatives not taken

- **Exceptions per operation, per target or per connector:** many small rules are harder to see and to test, and each one would exist on every instance.
- **A grant flag ("may cover deletes") on any instance:** it would bring delete grants to instances with real systems. The safety comes from what the instance holds, so the switch belongs to the instance.
- **The "proofs are code" runner** (a fixed runner runs only reviewed test scripts, and each delete still waits for a person, for example in a daily batch, #3322): deletes would still wait, which the program owner decided against for disposable systems. It also needs a new runner and a review flow for the scripts.
- **A "minimum version" mark in the database, checked at start:** no such check exists today, and builds from before the mark do not read it. So it cannot stop the older builds that matter. The separate table works against every build, old or new.

## Answered (2026-10-20)

The program owner answered on 2026-10-20, in the approving review of [pull request #4633](https://github.com/evoila/meho/pull/4633). All five choices stay as written.

1. **Delete grants only in the web console.** Yes: no automation and no CLI can create a delete grant, also not with a person's token.
   The decision of 2026-10-28 ([#5251](https://github.com/evoila/meho/issues/5251)) changes this when `MEHO_SANDBOX_DELETE_GRANTS_FROM_API` is on: then a tenant admin's token may create one through REST or the CLI.
2. **30 days for a delete grant.** Yes: every delete grant expires within 30 days of its creation.
3. **Wording for other instances.** The wording stays: AI agents on service principal logins are on purpose on a sandbox, and the record changes nothing for other instances.
4. **The review after 20 completed runs.** Yes: the results go into this record, and the program owner keeps or changes the decision.
5. **Rollback safety by the separate grant table.** Yes: delete grants are stored only in `sandbox_delete_grant`, so an older build never finds one.

## Acceptance criteria for the code of #4622

- [ ] **Order:** the code lands after #4632 (#4619 is already merged).
- [ ] **Settings:** `MEHO_SANDBOX_MODE` (default `false`) and `MEHO_SANDBOX_MODE_CONFIRM` are `Settings` fields read at boot, like `approval_allow_delegated_decision`. `grep -rn "environ.*MEHO_SANDBOX_MODE" backend/src` finds only `settings.py`.
- [ ] **Boot refusals,** each with a plain message and a test: the mode on without the confirm value; a confirm value that is not the host of the MCP resource URI; a host with no `sandbox` part; a stored mode that is not the setting (both ways).
- [ ] **Stored mode:** a test upgrades a database from the previous revision, with no rows at all, starts it with the mode on, and gets the refusal. A new database stores the mode at its first start. Two starts at once with different settings: one stores its mode, the other refuses. MEHO stores and checks the mode before it serves a request or starts a background task.
- [ ] **Normal boot:** with the mode off and no sandbox settings, the existing boot tests pass without edits.
- [ ] **Delete grant table:** `sandbox_delete_grant`, a new table with an Alembic migration. With the mode on, a delete grant of each kind (level, verb, tag, delete name, vendor action name, delete step) is stored there, never in `service_principal_grant`. List, revoke, the orphan check and the console grant page cover both tables.
- [ ] **Older builds:** with the mode off, MEHO never queries `sandbox_delete_grant` (a test counts the queries over the whole verdict table: zero). With the mode off, on a database that holds delete grants of each kind, every delete and every delete step waits. This stands in for an older build, and for old and new builds side by side: both read only `service_principal_grant`.
- [ ] **Grants, mode off:** a grant with any delete sign is still refused with 422, in REST and in the console. These stay green without edits: `test_service_grants.py::test_create_refuses_delete_shaped_by_pattern`, `::test_create_refuses_destructive_tagged_descriptor`, `::test_create_refuses_destructive_tier_descriptor`, `::test_create_refuses_past_and_naive_expiry`, and `test_api_v1_service_grants.py::test_create_review_rejects_bad_op`.
- [ ] **Grants, mode on, console:** a tenant admin creates a delete grant with an expiry within 30 days (one test for each delete sign: level, verb, tag, name). Without an expiry, or beyond 30 days, `ServicePrincipalGrantService.create` raises `GrantValidationError`, and the console shows the error.
- [ ] **Console mark:** `create` accepts a delete grant only with the console mark that the server sets. A request field, a header or a token claim that claims the console does not count (a test tries each one over REST and gets 403).
- [ ] **Grants, mode on, REST:** a delete grant gets 403 `delete_grant_console_only` with a person's token, and 403 `human_principal_required` with an agent, service or runner token (#4619).
- [ ] **Grants, mode on, other operations:** a grant without a delete sign keeps today's rules (expiry optional) in REST, CLI and console.
- [ ] **Reminders and renewal:** the banner counts the delete grants that expire within 7 days. Renewing the set of one service principal checks each grant like a new one. No code renews a grant by itself: a test moves the clock past the expiry, and the grant no longer clears a call.
- [ ] **CLI:** `meho service-principals grants create` still refuses a delete operation id before it calls the server, with a new message: delete grants are made in the web console, and only in sandbox mode. Must change: `cli/internal/cmd/service-principal/grants_test.go::TestBuildGrantCreateBodyRejectsUnsafeInput` checks the new message.
- [ ] **Step list:** with the mode on, `GET /api/v1/operations/governed-subops` marks as grantable the delete steps that it marks not grantable today; with the mode off it gives today's answers. `test_governed_subops.py::test_classify_flags_delete_shaped` and `::test_vmware_wiring_is_derived_from_manifests` stay green; copies of them with the mode on are added.
- [ ] **Calls, mode on:** a `destructive` call under a matching delete grant without `preview_hash` is refused; a wrong hash gives `preview_hash_mismatch`; a missing or malformed blast radius gives `blast_radius_required`; the right hash with a valid blast radius runs.
- [ ] **Order:** each refused call above leaves no grant-use audit row and no `approval.auto_approved` broadcast.
- [ ] **Failed audit write:** when the grant-use audit row cannot be committed, the call does not run.
- [ ] **Grant states:** an expired grant, a revoked grant, and a grant for another target, operation or service principal do not clear a call. A grant revoked between two steps of one multi-step operation stops the second step.
- [ ] **Audit content:** the grant-use row of a `destructive` call holds the preview hash and the blast radius in `payload`; for a delete below `destructive`, it holds the proposed effect. The grant-use row of a step names the audit row of its multi-step operation.
- [ ] **Other delete signs, mode on:** an operation with the verb `DELETE`, the tag `destructive` or a delete name, below the `destructive` level, runs under a matching delete grant. With the mode off it waits, as today.
- [ ] **Steps, mode on:** a step gate or child call at the `destructive` level is still refused under a matching grant. `vmware.composite.vm.destroy` by a service principal, with grants for the operation and its delete step and the right preview hash, runs on both paths (vCenter 9 with the REST step, older vCenter with the SOAP step) with no approval request.
- [ ] **Unchanged, mode on:** a person, an agent login and an agent run hosted by MEHO get the same delete verdict as with the mode off. A satellite gets no destructive call, also with a matching grant (a copy of `test_connectors_vmware_rest_vm_destroy.py::test_satellite_mint_refuses_op_not_safe` with the mode on).
- [ ] **Verdict table:** test 1 as described above, with the mode-off table checked in.
- [ ] **Audit mark:** `audit_log.sandbox_mode` (boolean, not null, database default `false`) with an Alembic migration; existing rows read `false`, and so does a row written without the column, as an older build writes it. One model default sets it. Every writer in the inventory of `test_audit_event_class.py` and `test_audit_wrapper_cutover.py` writes `true` with the mode on and `false` with it off.
- [ ] **`/ready`:** a top-level `"sandbox_mode"` for every caller, also without the metrics token, and `features.approval_queue.sandbox_mode`. Must change, because they pin the exact top-level keys: `test_metrics_access.py::test_ready_open_by_default`, `::test_correct_bearer_admits_ready`, `::test_ready_without_valid_bearer_serves_verdict_only`. `test_features.py` gets tests for the new key.
- [ ] **`meho status`:** `GET /api/v1/health` has `sandbox_mode`; `meho status` prints one line when it is on and nothing new when it is off (a Go test in `cli/`).
- [ ] **Console:** every page shows the banner with the mode on, and no banner with it off.
- [ ] **Chart:** `helm template` fails when `sandboxMode.enabled` is true and `sandboxMode.confirmHost` is not `ingress.host`; the default renders no sandbox setting; the notes warn when the mode is on.
- [ ] **Texts:** with the mode off, every reason text stays word for word (`test_service_principal_grant_gate.py::test_service_safety_gate_reason_destructive_parks_always` stays green). With it on, the texts say that a grant made in the console may cover the delete. Fixed texts that say "no standing grant" (for example the description of `vmware.composite.vm.destroy`) add "except on an instance in sandbox mode".
- [ ] **Docs:** a "Sandbox mode" section in `docs/codebase/service-principal-grants.md`, `docs/codebase/approvals.md` and `docs-site/guides/approvals-and-break-glass.md`. The "Rollback contract" in `docs/codebase/migrations.md` says that older builds see no delete grant.
- [ ] **Unchanged suites stay green without edits:** `test_service_principal_grant_gate.py`, `test_permission_resolver.py`, `test_agent_delegated_permissions.py`, `test_destructive_preview_binding.py`, `test_operations_subop_policy_gate.py`, `test_runner_satellite_tier.py`, `test_connectors_vmware_rest_vm_destroy.py`, `test_connectors_vmware_rest_composites_write_e2e.py`, and the connector tests that say no grant covers a delete (Harbor robot, bind9, Windows DNS, pfSense, vCenter tagging).
- [ ] **Full run:** in `backend/`, `uv run pytest tests/ -q -n auto`, `uv run ruff check src/ tests/`, `uv run ruff format --check src/ tests/` and `uv run mypy src/` pass; `cd cli && go test ./...` passes.
