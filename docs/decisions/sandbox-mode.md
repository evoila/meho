# Sandbox mode: a standing grant may also cover deletes on an instance for disposable test systems (decision)

**Status:** decided — program owner, 2026-10-19. This record: 2026-10-20.
**Issue:** [#4622](https://github.com/evoila/meho/issues/4622), step 2. Source: the [step 1 review](https://github.com/evoila/meho/issues/4622#issuecomment-6286729259).
**Initiative:** [#4621](https://github.com/evoila/meho/issues/4621) — sandbox mode for disposable test systems
**Adds one exception to:** requirement 1 of [governed-delete-operations.md](governed-delete-operations.md), on a sandbox instance only
**Same pattern as:** the boot-only switch for delegated decisions ([#3693](https://github.com/evoila/meho/issues/3693))

## The decision

- A MEHO instance can run in **sandbox mode**. It is only for an instance that holds disposable test systems.
- In sandbox mode, a person may give a service principal a standing grant that also covers deletes. So an automated test can build, change and delete its own test systems, and nothing waits for a person.
- Sandbox mode is off by default. It is set when the instance is installed, and it stays the same for the life of the instance's database.
- Every other instance keeps today's rules, unchanged.
- A sandbox runs on its own site: its own MEHO, Keycloak and Vault, and no network route to any other system. Only disposable systems are registered there as targets. These walls take the place of a person's approval of each delete. They belong to the site, not to MEHO code.
- A grant names a target (for example a vCenter), not the VMs behind it, and MEHO cannot tell a disposable VM from another one. That is why the site must hold only disposable systems.
- On a sandbox, AI agents sign in as service principals, on purpose. #3325 and #3980 describe service principals as fixed automation, such as build pipelines; this record does not change that for other instances.

## Words used

- **Disposable system:** a system built for one test and deleted after it.
- **Service principal:** a machine login: a Keycloak client that signs in with its own secret.
- **Standing grant:** a person's approval, given in advance, for one service principal, one operation and one target, or one kind of target (a target selector, [#3349](https://github.com/evoila/meho/issues/3349)).
- **Sandbox mode:** one setting for the whole instance. It says that the instance holds only disposable systems.
- **Delete sign:** how the code sees that an operation deletes something: the safety level `destructive`, the HTTP verb `DELETE`, the tag `destructive`, or a delete name (`DELETE:*`, `*.delete`, `*.destroy`, `*.remove`, `*.purge`).
- **Preview hash:** a check value of a preview result. A `destructive` call must send the value that its preview gave.
- **Blast radius:** what a delete affects: the object, its child objects, and whether it can be undone.
- **Multi-step operation:** one operation that runs several steps (the code calls it a composite). Each step that writes passes its own check, the step gate.

## The one requirement it narrows

Requirement 1 of the delete decision says today: "Mandatory human approval, always. No standing grant applies, no self-approval, no autonomous-session approval." The new wording:

> 1. **Mandatory human approval, always — except on an instance in sandbox mode.** There, a standing grant may cover a delete by a service principal. A person creates that grant in the web console, and it expires within 30 days. The rest of this requirement is unchanged: no self-approval, no autonomous-session approval.

The same exception applies wherever that record says that no standing grant can cover a delete, and to the cell "service principal, destructive" in the rules table of #3325.

## What stays

- **Requirement 2, the preview hash.** A `destructive` call needs the preview hash of a `preview_operation` of the same call, also under a grant. Today MEHO checks the hash only when a call waits for a person, so the grant path gets the same check. It runs before MEHO records the grant use, so a refused call leaves no "auto-approved" audit row and no broadcast.
- **Requirement 3, the blast radius.** For a call that a grant covers, it moves from the approval card to the audit row. The grant-use audit row (the row that stands in for a person's decision) stores the preview hash and the blast radius in `payload`. Not in `raw_payload`: MEHO clears that after 90 days by default.
- **Requirement 4, the `destructive` level,** stays as it is.
- **Satellites never get deletes** ([satellite-write-path.md](satellite-write-path.md)). Their checks read only the safety level, never a grant.
- **People, agents and token calls keep their rules** (#3325, #3980). Sandbox mode changes nothing for them. Today, also on a sandbox, a person's delete waits for an approval, and an agent login is refused a delete.
- **A run approval (#4126) never covers a delete**, also in sandbox mode.
- **One list of delete signs.** The setting `SERVICE_GRANT_DELETE_SHAPED_PATTERNS` stays, and sandbox mode uses the same signs.

## Guards at boot

- Two settings, read at boot only: `MEHO_SANDBOX_MODE` (default `false`) and `MEHO_SANDBOX_MODE_CONFIRM`. No request, REST route, MCP tool, CLI command or console page can change them.
- MEHO refuses to start, with a plain message, when:
  1. the mode is on, and `MEHO_SANDBOX_MODE_CONFIRM` is missing or is not the host name of the MCP resource URI (the public address of the instance, which MEHO already checks at boot);
  2. the mode is on, and no dot-separated part of that host name is exactly `sandbox`: `meho.sandbox.example` passes, `meho.example` and `meho-sandbox.example` are refused;
  3. the mode stored in the database is not the mode of the settings.
- **Fixed for the life of the database.** MEHO stores the mode at the first start of a new database. To change the mode, install a new instance with a new database. The upgrade that brings this change stores "off" in a database that already has audit rows, so an existing instance can never become a sandbox.
- Why fixed: a grant for a delete step of a multi-step operation would keep working after a switch to "off", because at call time such a step shows no delete sign.
- The guards stop three mistakes: settings copied to another instance (its host name has no `sandbox` part), a sandbox pointed at another instance's database, and a backup restored on another instance (the stored mode differs). They do not stop a person who sets every value on purpose. That is by design.

## Grants in sandbox mode

- **A delete grant** is a grant for an operation with a delete sign. **Only a tenant admin creates it, in the web console.** REST and the CLI refuse it, also with a person's token: an AI tool can use a person's login, so a token is never treated as a person (#3325, #3980). When #3980 adds its mark for "came from the web console", this check uses that mark.
- **Every delete grant expires within 30 days** of its creation. Without an expiry, or with a later one, MEHO refuses it. The check sits in `ServicePrincipalGrantService.create`, so REST and the console both get it.
- A delete grant may name one target or a target selector, like any other grant: test systems are often registered during the run.
- Grants for other operations keep today's rules.
- **Multi-step operations.** A delete step inside a multi-step operation is checked as `dangerous` at its step gate. Like every step that writes, it needs its own grant. In sandbox mode a person may create that grant, with the rules above. The step needs no preview hash of its own, as today. A step or child call (a call that a multi-step operation makes to another operation) at the `destructive` level stays refused, because it cannot carry a preview hash.
- The list of steps that need grants (`GET /api/v1/operations/governed-subops`) shows delete steps as grantable when the mode is on.

## Display and audit mark

- `/ready` gets a top-level field `"sandbox_mode": "on"` or `"off"`, for every caller. It sits outside `features` on purpose: when the metrics token (an optional secret for monitoring) is set, `features` is hidden from callers without it, and the mode must be readable on every instance. `features.approval_queue` says it too.
- `GET /api/v1/health` gets the same field, and `meho status` prints one line when the mode is on.
- Every console page shows a banner when the mode is on: "Sandbox mode: agents may delete systems here without a person's approval."
- The chart gets `sandboxMode.enabled` (default `false`) and `sandboxMode.confirmHost`. With the mode on, `helm template` fails when `confirmHost` is not `ingress.host`, and the chart notes print a warning.
- **Audit mark:** a new column `audit_log.sandbox_mode` (true or false, default false). One default in the audit model sets it for every writer. The whole database has one mode, but rows leave it (exports, log stores), and the mark goes with them.

## The verdict table

Test 1 (`backend/tests/test_sandbox_mode_verdict_table.py`) checks the verdict (runs, waits or refused) for every cell of one table: principal kind (person, agent, agent run hosted by MEHO, service principal, satellite runner) × safety level (safe, caution, dangerous, destructive) × `requires_approval` (yes, no) × **delete sign at call time (none, HTTP `DELETE`, tag `destructive`)** × grant (none, matching). It checks a direct call and a step gate.

- The delete-sign dimension is new. Example: an ingested `DELETE` operation is `dangerous`, and today no grant can clear it, because the call-time check refuses the verb.
- Mode off: every cell equals today's verdict. The table is checked in.
- Mode on: only cells with "service principal, level `destructive` or a delete sign, matching grant" may change, and only from "waits" to "runs". For `destructive`, "runs" needs the right preview hash (the "Calls" item below). At a step gate, `destructive` stays refused.
- A delete step of a multi-step operation shows no delete sign at call time, so its cells do not change. Its change is at grant creation (the "Grants" items below).

## Measurement plan

- **Mode off, on every instance that does not set the mode:** the full test suite passes with only the test edits named below, and the verdict table gives the same verdicts as before the change. After the release, `/ready` on every instance we run shows `"sandbox_mode": "off"`.
- **Mode on, on the first sandbox instance, over its first 20 test runs** (a test run builds, changes and deletes its own disposable systems through MEHO):
  1. zero approval requests from service principals;
  2. every grant-use audit row of a `destructive` call holds a preview hash and a blast radius;
  3. every audit row carries the mark: no row has `sandbox_mode = false`;
  4. the site's network logs show no call from the sandbox to an address outside its own range.
- If one measure fails, a person revokes the delete grants, so deletes wait for a person again, until the cause is fixed.
- After the 20th run, the results are added to this record, and the program owner keeps or changes the decision.

## Alternatives not taken

- **Exceptions per operation, per target or per connector:** many small rules are harder to see and to test, and each one would exist on every instance.
- **A grant flag ("may cover deletes") on any instance:** it would bring delete grants to instances with real systems. The safety comes from what the instance holds, so the switch belongs to the instance.
- **The "proofs are code" runner** (a fixed runner runs only reviewed test scripts, and each delete still waits for a person, for example in a daily batch, #3322): deletes would still wait, which the program owner decided against for disposable systems. It also needs a new runner and a review flow for the scripts.

## Acceptance criteria for the code of #4622

- [ ] **Settings:** `MEHO_SANDBOX_MODE` (default `false`) and `MEHO_SANDBOX_MODE_CONFIRM` are `Settings` fields read at boot, like `approval_allow_delegated_decision`. `grep -rn "environ.*MEHO_SANDBOX_MODE" backend/src` finds only `settings.py`.
- [ ] **Boot refusals,** each with a plain message and a test: the mode on without the confirm value; a confirm value that is not the host of the MCP resource URI; a host with no `sandbox` part; a stored mode that is not the setting (both ways).
- [ ] **Stored mode:** a new database stores the mode at its first start. A test upgrades a database that has audit rows, starts it with the mode on, and gets the refusal.
- [ ] **Normal boot:** with the mode off and no sandbox settings, the existing boot tests pass without edits.
- [ ] **Grants, mode off:** a grant with any delete sign is still refused with 422, in REST and in the console. These stay green without edits: `test_service_grants.py::test_create_refuses_delete_shaped_by_pattern`, `::test_create_refuses_destructive_tagged_descriptor`, `::test_create_refuses_destructive_tier_descriptor`, `::test_create_refuses_past_and_naive_expiry`, and `test_api_v1_service_grants.py::test_create_review_rejects_bad_op`.
- [ ] **Grants, mode on, console:** a tenant admin creates a delete grant with an expiry within 30 days (one test for each delete sign: level, verb, tag, name). Without an expiry, or beyond 30 days, `ServicePrincipalGrantService.create` raises `GrantValidationError`, and the console shows the error.
- [ ] **Grants, mode on, REST:** a delete grant gets 403 `delete_grant_console_only` with a person's token, and 403 `human_principal_required` with an agent, service or runner token (#4619).
- [ ] **Grants, mode on, other operations:** a grant without a delete sign keeps today's rules (expiry optional) in REST, CLI and console.
- [ ] **CLI:** `meho service-principals grants create` still refuses a delete operation id before it calls the server, with a new message: delete grants are made in the web console, and only in sandbox mode. Must change: `cli/internal/cmd/service-principal/grants_test.go::TestBuildGrantCreateBodyRejectsUnsafeInput` checks the new message.
- [ ] **Step list:** with the mode on, `GET /api/v1/operations/governed-subops` marks as grantable the delete steps that it marks not grantable today; with the mode off it gives today's answers. `test_governed_subops.py::test_classify_flags_delete_shaped` and `::test_vmware_wiring_is_derived_from_manifests` stay green; copies of them with the mode on are added.
- [ ] **Calls, mode on:** a `destructive` call under a matching grant without `preview_hash` is refused; a wrong hash gives `result_preview_hash_mismatch`; the right hash runs.
- [ ] **Order:** a refused call leaves no grant-use audit row and no `approval.auto_approved` broadcast.
- [ ] **Audit content:** the grant-use audit row of a `destructive` call holds the preview hash and the blast radius in `payload`.
- [ ] **Other delete signs, mode on:** an operation with the verb `DELETE` or the tag `destructive`, below the `destructive` level, runs under a matching grant. With the mode off it waits, as today.
- [ ] **Steps, mode on:** a step gate or child call at the `destructive` level is still refused under a matching grant. `vmware.composite.vm.destroy` by a service principal, with grants for the operation and its delete step and the right preview hash, runs on both paths (vCenter 9 with the REST step, older vCenter with the SOAP step) with no approval request.
- [ ] **Unchanged, mode on:** a person, an agent login and an agent run hosted by MEHO get the same delete verdict as with the mode off. A satellite gets no destructive call, also with a matching grant (a copy of `test_connectors_vmware_rest_vm_destroy.py::test_satellite_mint_refuses_op_not_safe` with the mode on).
- [ ] **Verdict table:** test 1 as described above, with the mode-off table checked in.
- [ ] **Audit mark:** `audit_log.sandbox_mode` (boolean, not null, default `false`) with an Alembic migration; existing rows read `false`; one model default sets it. Every writer in the inventory of `test_audit_event_class.py` and `test_audit_wrapper_cutover.py` writes `true` with the mode on and `false` with it off.
- [ ] **`/ready`:** a top-level `"sandbox_mode"` for every caller, also without the metrics token, and `features.approval_queue.sandbox_mode`. Must change, because they pin the exact top-level keys: `test_metrics_access.py::test_ready_open_by_default`, `::test_correct_bearer_admits_ready`, `::test_ready_without_valid_bearer_serves_verdict_only`. `test_features.py` gets tests for the new key.
- [ ] **`meho status`:** `GET /api/v1/health` has `sandbox_mode`; `meho status` prints one line when it is on and nothing new when it is off (a Go test in `cli/`).
- [ ] **Console:** every page shows the banner with the mode on, and no banner with it off.
- [ ] **Chart:** `helm template` fails when `sandboxMode.enabled` is true and `sandboxMode.confirmHost` is not `ingress.host`; the default renders no sandbox setting; the notes warn when the mode is on.
- [ ] **Texts:** with the mode off, every reason text stays word for word (`test_service_principal_grant_gate.py::test_service_safety_gate_reason_destructive_parks_always` stays green). With it on, the texts say that a grant made in the console may cover the delete. Fixed texts that say "no standing grant" (for example the description of `vmware.composite.vm.destroy`) add "except on an instance in sandbox mode".
- [ ] **Docs:** a "Sandbox mode" section in `docs/codebase/service-principal-grants.md`, `docs/codebase/approvals.md` and `docs-site/guides/approvals-and-break-glass.md`.
- [ ] **Unchanged suites stay green without edits:** `test_service_principal_grant_gate.py`, `test_permission_resolver.py`, `test_agent_delegated_permissions.py`, `test_destructive_preview_binding.py`, `test_operations_subop_policy_gate.py`, `test_runner_satellite_tier.py`, `test_connectors_vmware_rest_vm_destroy.py`, `test_connectors_vmware_rest_composites_write_e2e.py`, and the connector tests that say no grant covers a delete (Harbor robot, bind9, Windows DNS, pfSense, vCenter tagging).
- [ ] **Full run:** in `backend/`, `uv run pytest tests/ -q -n auto`, `uv run ruff check src/ tests/`, `uv run ruff format --check src/ tests/` and `uv run mypy src/` pass; `cd cli && go test ./...` passes.
