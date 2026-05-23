<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# vmware-rest op surface onboarding — operator recipe

> Operator-facing recipe for executing `vmware-rest-9.0` ops against a
> **real** vCenter — the rubric **State 2** wiring G3.9 ships
> ([Initiative #939](https://github.com/evoila/meho/issues/939)). With
> the credential broker wired, `meho operation call <op> target=…`
> reads the vSphere service account out of Vault under the operator's
> identity and establishes a real vCenter session. The connector code
> lives in
> [`backend/src/meho_backplane/connectors/vmware_rest/`](../../backend/src/meho_backplane/connectors/vmware_rest/);
> the deploy prerequisite is the Vault-policy runbook
> [`connector-vault-policy.md`](./connector-vault-policy.md).

## What this surface is

The `vmware-rest-9.0` connector is a **hand-rolled `HttpConnector`
subclass** over the vSphere Automation REST API. It dispatches under the
`(product="vmware", version="9.0", impl_id="vmware-rest")` registry
triple — connector id `vmware-rest-9.0` — and serves both the
spec-ingested REST ops (`vcenter.yaml` → `endpoint_descriptor` rows) and
the hand-coded composites listed in
[`v0.3.0-feedback-reply-vmware-13-ops.md`](./v0.3.0-feedback-reply-vmware-13-ops.md).

Every op dispatches through the same `POST /api/v1/operations/call`
route the agent surface uses — auth, policy, audit, broadcast, and
JSONFlux all run as documented in [CLAUDE.md](../../CLAUDE.md). There is
**no `vmware` CLI verb tree and no per-op MCP tool**: operators reach
vmware ops through the generic `meho operation call` verb or the
[agent meta-tool path](#the-agent-meta-tool-path) (CLAUDE.md postulate 5
— the narrow waist).

### What "State 2" means here

Per the
[connector release-readiness rubric](../codebase/connector-release-readiness.md):

- **State 1** (where this connector was before G3.9): dispatch + catalog
  only — ops indexed and searchable, but the default credential loader
  raised `NotImplementedError`, so a real `operation call` failed.
- **State 2** (what G3.9-T3 ships): the default loader performs the live
  operator-context Vault read for the **`shared_service_account`** auth
  model. `operation call <op> target=…` executes end to end against a
  real vCenter.
- **State 3** (not yet): every advertised auth model wired; full catalog
  in production rotation. `per_user` / `impersonation` still raise a
  clear boundary error (see [Scope](#scope--state-2-shared_service_account-only)).

## Prerequisites

- **A reachable vCenter 8.5+.** The connector's
  `supported_version_range` is `>=8.5,<10.0`. It POSTs to the modern
  `POST /api/session` and falls back to the legacy
  `POST /rest/com/vmware/cis/session` only on a 404 (real vCenter serves
  both). Older vCenter (< 8.5) is out of range; the pyvmomi-typed
  connector covers that surface separately.
- **A vSphere service account.** A least-privilege vCenter local or SSO
  account whose `{username, password}` the connector uses to mint the
  per-target `vmware-api-session-id` session token. Scope it to the
  read/write surface the operator workflows need — never a full
  Administrator unless a write composite demands it.
- **The service account stored in Vault** at the target's `secret_ref`
  KV-v2 path (see [Storing the service account](#storing-the-service-account-in-vault)).
- **The Vault policy + Keycloak→Vault identity** from the deploy runbook
  [`connector-vault-policy.md`](./connector-vault-policy.md) — without
  it the live read returns Vault 403 (`VaultRoleDeniedError`). This is
  the load-bearing deploy prerequisite for State 2.
- **A registered vmware target** carrying `product="vmware"`, `host`,
  `port` (optional, defaults to 443), `secret_ref` (the Vault path), and
  `auth_model="shared_service_account"`.
- **An operator session.** `meho login <backplane-url>` writes the
  session token the CLI reuses. The operator's JWT is what the
  connector forwards to Vault — the read runs under *their* identity, so
  the operator needs both the backplane `operator` role and a Vault
  policy that grants read on the target's `secret_ref`.

## Target + auth model

The shipped connector's only auth model is `shared_service_account`: one
vSphere service-account credential per target, stored in Vault, read
under the acting operator's identity. The credential's field shape is the
HTTP-basic pair vCenter's `POST /api/session` expects:

| Secret field | Meaning |
| --- | --- |
| `username` | the vSphere service account (e.g. `svc-meho@vsphere.local`) |
| `password` | its password |

Both fields are required; a secret missing either raises
`VaultCredentialsReadError` naming the target + field (never a bare
`KeyError`), surfaced as a clean dispatch error.

### Storing the service account in Vault

Use the per-target convention from the
[Vault-policy runbook](./connector-vault-policy.md) §1 — the path is
scoped to the operator identity segment the templated policy renders:

```text
secret/data/targets/<operator-identity>/<target-name>
```

Write it with the `meho vault kv put` op (the same dispatch path,
audited):

```console
$ meho vault kv put --target rdc-vault secret \
    targets/<operator-identity>/vcenter-lab-01 \
    --data @secret_ref.json
```

`secret_ref.json` shape:

```json
{
  "username": "svc-meho@vsphere.local",
  "password": "<vsphere-service-account-password>"
}
```

The credential is read **server-side** and used to establish the session;
it is never returned to the operator or the agent, never logged, and
never rides an `OperationResult` (the chassis keeps it out of every log
event — see [No credential ever leaves the backplane](#no-credential-ever-leaves-the-backplane)).

### Registering the target

```console
$ meho targets create \
    --name vcenter-lab-01 \
    --product vmware \
    --host vc.lab.evba \
    --secret-ref secret/targets/<operator-identity>/vcenter-lab-01 \
    --auth-model shared_service_account
```

Verify it round-trips — `probe` exercises the live credential read +
session establish:

```console
$ meho targets probe vcenter-lab-01
ok — vmware vcenter 9.0.0.0 reachable (GET /api/about)
```

`probe` runs the full chain: Vault read (operator-context) → `POST
/api/session` (HTTP basic) → `GET /api/about` → reachable. A failure
carries a distinct reason: a Vault 403 means the
[policy/identity prerequisite](./connector-vault-policy.md) is missing; a
session 401 means the stored service-account credential is wrong; a
transport error means vCenter is unreachable.

## Calling an op

There is no vmware-specific verb; use the generic `operation call`:

```console
$ meho operation call vmware-rest-9.0 GET:/vcenter/vm \
    --target vcenter-lab-01 --json | jq .result

$ meho operation call vmware-rest-9.0 vmware.composite.datastore.usage \
    --target vcenter-lab-01 --json | jq .result
```

The first call against a target establishes the session (one
`POST /api/session`); subsequent calls reuse the cached
`vmware-api-session-id` token until the connector is torn down (which
revokes it via `DELETE /api/session`). The connector does **not**
proactively refresh on the ~5-minute vSphere idle timeout — a 401 from a
later call surfaces to the caller as a clean retry signal (deferred per
the connector's *Session lifecycle* note).

Set-shaped responses (the VM/datastore lists) come back as a JSONFlux
result handle when they exceed the inline threshold; drill in with
`result_query` / `result_aggregate` / `result_export` (CLAUDE.md §4).

## The agent meta-tool path

Agents never see a `vmware` tool. Per [CLAUDE.md](../../CLAUDE.md)
postulate 5, an agent reaches every vmware op through the narrow-waist
meta-tools:

```text
search_connectors(query="vsphere vcenter")        → finds vmware-rest-9.0
list_operation_groups(connector_id="vmware-rest-9.0")
                                                  → inventory / vm / storage / …
search_operations(
    connector_id="vmware-rest-9.0",
    query="list virtual machines",
    group="inventory",
)                                                  → top hit: GET:/vcenter/vm
call_operation(
    connector_id="vmware-rest-9.0",
    operation_id="GET:/vcenter/vm",
    target={"name": "vcenter-lab-01"},
    params={},
)
```

`call_operation` and `meho operation call` dispatch the identical route,
credential read, audit row, and broadcast event. The credential read
happens under the **operator's** identity in both cases — the agent's
session carries the operator JWT the connector forwards to Vault.

## No credential ever leaves the backplane

The State-2 wiring is built so the vSphere service-account credential is
ephemeral in-memory state:

- The shared loader
  ([`connectors/_shared/vault_creds.py`](../../backend/src/meho_backplane/connectors/_shared/vault_creds.py))
  logs only the target name, host, and the requested *field names* —
  never a value.
- The connector's session-establish + revoke log events carry the target
  name, host, and session path — never the credential or the session
  token.
- The credential is consumed to build the HTTP-basic header for `POST
  /api/session` and then dropped; it never enters the `OperationResult`,
  the audit row payload, or the broadcast event.

This is asserted by the recorded-fixture E2E
([`tests/test_connectors_vmware_rest_credread.py`](../../backend/tests/test_connectors_vmware_rest_credread.py)):
a canary password is seeded into the (faked) Vault read and asserted
absent from the result, every captured log event, and the broadcast
payload.

## Scope — State 2 (`shared_service_account` only)

G3.9-T3 ships the `shared_service_account` auth model only. The
connector's `auth_headers` raises a clear `NotImplementedError` naming
the target + requested mode for any other `auth_model`:

- **`per_user`** (each operator has their own vSphere credential) is a
  natural extension of the runbook's per-operator templated path —
  deferred until a concrete need exists.
- **`impersonation`** (the backplane authenticates as a privileged
  account and acts *as* the operator) needs vendor-side support and is
  deferred.

System-initiated calls (background/scheduled work with no operator JWT)
**cannot** perform an operator-context read — the loader fails closed
with a clear error (the carve-out in
[`connector-auth.md`](../architecture/connector-auth.md)).

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `status=error … VaultRoleDeniedError` | The `meho-mcp` role's policy doesn't grant the operator read on the target's `secret_ref`, or the operator's Vault entity-alias doesn't match their JWT `sub`. | Fix the policy/identity per [`connector-vault-policy.md`](./connector-vault-policy.md) §2/§3 — not the connector. Verify with that doc's §4 read check. |
| `status=error … VaultCredentialsReadError … missing required field 'password'` | The Vault secret at `secret_ref` has `username` but not `password`. | Re-write the secret with both fields (see [Storing the service account](#storing-the-service-account-in-vault)). |
| `status=error … operator-context credential read requires an authenticated operator` | A system-initiated call (no operator JWT) hit the loader. | Out of scope for v0.x — connector ops require an operator session. |
| `RuntimeError … vsphere session establish failed … HTTP 401` | The stored service-account credential is wrong (Vault read succeeded, vCenter rejected the login). | Confirm the `{username, password}` in Vault are valid vSphere credentials; rotate if needed. |
| `RuntimeError … POST /rest/com/vmware/cis/session returned HTTP 404` | Both the modern and legacy session endpoints 404'd — the host isn't a vCenter REST surface (or a reverse proxy strips `/api`). | Confirm the target host serves the vSphere Automation REST API. |
| `status=error … unknown_op` | connector_id or op_id drift. | Use the full `vmware-rest-9.0` connector id; list ops via `meho connector list` / `search_operations`. |
| `status=denied` on a write composite | `read_only` role, or a `requires_approval` op hit the (v0.2 deny-only) policy gate. | Use an `operator`-role token; write composites that require approval are gated until G10's approval queue lands. |

## References

- Initiative: [#939 G3.9 Connector credential broker](https://github.com/evoila/meho/issues/939); Goal [#214](https://github.com/evoila/meho/issues/214) (connector parity).
- Tasks that shipped the credential broker: [#940](https://github.com/evoila/meho/issues/940) (operator threading), [#941](https://github.com/evoila/meho/issues/941) (shared Vault-creds helper), [#942](https://github.com/evoila/meho/issues/942) (this State-2 wiring + E2E + onboarding), [#943](https://github.com/evoila/meho/issues/943) (Vault-policy deploy runbook).
- Decision: [`docs/architecture/connector-auth.md`](../architecture/connector-auth.md) (operator-context Vault read). Research: [`docs/research/214-connector-credential-broker.md`](../research/214-connector-credential-broker.md).
- Deploy prerequisite (Vault policy + Keycloak→Vault identity): [`connector-vault-policy.md`](./connector-vault-policy.md).
- Release-readiness rubric: [`docs/codebase/connector-release-readiness.md`](../codebase/connector-release-readiness.md).
- Connector code: [`backend/src/meho_backplane/connectors/vmware_rest/`](../../backend/src/meho_backplane/connectors/vmware_rest/) (`connector.py` session lifecycle, `session.py` credential loader, `_mount.py` modern/legacy mount mapping). Shared loader: [`connectors/_shared/vault_creds.py`](../../backend/src/meho_backplane/connectors/_shared/vault_creds.py).
- Tests: recorded-fixture E2E [`tests/test_connectors_vmware_rest_credread.py`](../../backend/tests/test_connectors_vmware_rest_credread.py); opt-in live lab smoke [`tests/integration/test_connectors_vmware_rest_lab_smoke.py`](../../backend/tests/integration/test_connectors_vmware_rest_lab_smoke.py); auth/session unit suite [`tests/test_connectors_vmware_rest_auth.py`](../../backend/tests/test_connectors_vmware_rest_auth.py).
- vmware op model framing: [`v0.3.0-feedback-reply-vmware-13-ops.md`](./v0.3.0-feedback-reply-vmware-13-ops.md). vSphere Automation REST API: [VMware vSphere Automation REST API Programming Guide 8.0](https://techdocs.broadcom.com/us/en/vmware-cis/vsphere/vsphere-sdks-tools/8-0/vmware-vsphere-automation-rest-programming-guide-8-0.html).
- Onboarding-doc precedents: [`bind9-onboarding.md`](./bind9-onboarding.md), [`kubernetes-onboarding.md`](./kubernetes-onboarding.md), [`vault-onboarding.md`](./vault-onboarding.md), [`docs/cross-repo/README.md`](./README.md).
