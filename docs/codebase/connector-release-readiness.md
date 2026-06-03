# Connector release readiness

How to honestly describe a connector's ship state in release notes, the
operator kb, and Goal-tracker bodies — distinguishing the three layers
that have to land before a connector executes end-to-end against a real
operator's target in production.

This document is the lasting artifact from the v0.3.0 consumer-feedback
triage. The triage caught a consumer-side kb mis-supersede that was
shipped in the morning and corrected the same afternoon, by exactly the
"read the source per connector" discipline the consumer's own kb had
distilled the day before. The lesson belongs upstream too.

## The three states

A connector ships through three layers. **Release notes / kb / Goal-tracker
text must say which layer the release ships, not the next layer up.**

### State 1 — Dispatch + catalog only

What's shipped:

- Connector class registered via `register_connector_v2(...)` in
  [connectors/registry.py](../../backend/src/meho_backplane/connectors/registry.py).
- Operations register into `endpoint_descriptor` (typed: via
  [`register_typed_operation`](../../backend/src/meho_backplane/operations/typed_register.py);
  ingested: via the G0.7 review pipeline). `operation_group` rows
  written alongside, indexed by the hybrid BM25 + cosine search.
- `search_operations(connector_id, query)`, `list_operation_groups(connector_id)`,
  and `call_operation(connector_id, op_id, target, params)` all return
  cataloged ops.
- Per-op `description`, `safety_level`, `requires_approval`,
  `llm_instructions` metadata is curated and surfaces through the MCP +
  REST API.
- Integration tests (testcontainers / k3d / mock-loader) exercise the
  dispatch path against an **injected loader** — not against real
  per-target credentials.

What's NOT shipped at this state:

- The default loader (e.g. `load_kubeconfig_from_vault`,
  `load_session_credentials_from_vault`, `load_credentials_from_vault`)
  is a `NotImplementedError` stub.
- `operations/call <op_id> target=...` against a real operator-context
  Vault path raises `NotImplementedError` in production.

How to verify a connector is at this state (and not further along):

```bash
# 1. Class registered:
rg "register_connector_v2.*product=.<connector-name>" backend/src/
# 2. Ops registered:
rg "op_id=.<connector-name>\." backend/src/meho_backplane/connectors/<connector-name>/
# 3. Loader stubbed:
rg "raise NotImplementedError" backend/src/meho_backplane/connectors/<connector-name>/
```

**v0.3.0 examples at this state:** none after G3.10. (`k8s-1.x` was at
this state through v0.3.0; G3.10-T4 [#948](https://github.com/evoila/meho/issues/948)
wired its live loader and moved it to State 2. `vmware-rest-9.0` was at
this state through v0.3.0; G3.9-T3 [#942](https://github.com/evoila/meho/issues/942)
wired its live loader and moved it to State 2.)

**Honest release-notes language:**

> *"Kubernetes typed connector dispatch + catalog (13 ops indexed; loader
> wiring tracked under [#214](https://github.com/evoila/meho/issues/214))."*

**Dishonest release-notes language** (the v0.3.0 mistake — fixed in [T7
amendment #735](https://github.com/evoila/meho/issues/735)):

> *"Kubernetes typed connector (13 ops, k3d CI)."*

This reads as "production-ready" because k3d CI is a strong signal and
"13 ops" doesn't include the qualifier. Adopters who read this and ran
`operations/call k8s.namespace.list target=<their-vault-backed-target>`
got `NotImplementedError`, not the operator inventory they expected.

### State 2 — Loader-wired, single auth model

What's shipped (on top of State 1):

- The default loader reads real operator-context per-target Vault
  credentials for **one** `auth_model` (e.g. `shared_service_account`).
- Production dispatch path `operations/call <op_id> target=...`
  executes end-to-end for targets with that auth_model.
- Tests exercise the live loader against a real Vault dev-mode harness
  (not just a mock).

What's NOT shipped:

- Other auth_models (`per_user`, `impersonation`) still raise a clear
  boundary error.

**Examples at this state:** `bind9-ssh-9.x` (the SSH transport
loads credentials inline in the connector — no separate `session.py`
stub; the credential read for `shared_service_account` is live). The
consumer correctly noted this as the "closest to parity" connector.
`vmware-rest-9.0` joined this state via G3.9-T3
[#942](https://github.com/evoila/meho/issues/942): its default loader
[`load_session_credentials_from_vault`](../../backend/src/meho_backplane/connectors/vmware_rest/session.py#L116)
now performs the live operator-context KV-v2 read via the shared
[`load_basic_credentials`](../../backend/src/meho_backplane/connectors/_shared/vault_creds.py)
helper; `shared_service_account` executes against a real vCenter.
Operator recipe: [`vmware-rest-onboarding.md`](../cross-repo/vmware-rest-onboarding.md).
`k8s-1.x` joined this state via G3.10-T4
[#948](https://github.com/evoila/meho/issues/948): its default loader
[`load_kubeconfig_from_vault`](../../backend/src/meho_backplane/connectors/kubernetes/kubeconfig.py)
performs the live operator-context KV-v2 read (kubeconfig YAML under
the `kubeconfig` field) and parses the result into the dict shape
`kubernetes_asyncio.config.new_client_from_config_dict` accepts;
`shared_service_account` executes against a real cluster. The
kubeconfig is structurally different from the `{username, password}`
the REST connectors consume — it's a single YAML document — so the
loader reuses the lower-level `vault_client_for_operator` primitive
directly rather than the `load_basic_credentials` helper, while still
applying the same fail-closed contract (empty `operator.raw_jwt` →
`VaultCredentialsReadError`) and the same no-secret-in-logs discipline.
Operator recipe: [`kubernetes-onboarding.md`](../cross-repo/kubernetes-onboarding.md).

**Honest release-notes language:**

> *"bind9 typed-SSH connector — 11 ops, atomic-apply discipline.
> `shared_service_account` auth model live; `per_user` tracked under
> #N."*

### State 3 — Full production execution

What's shipped (on top of State 2):

- All advertised `auth_model`s are wired and execute.
- The connector's full op catalog is in production rotation for the
  operator workflows it covers.
- Operator onboarding doc at `docs/cross-repo/<connector>-onboarding.md`
  has been written, reviewed, and validates against a real deploy.

**v0.3.0 examples at this state:** `vault-1.x` (the JWT-federated auth
flow is the only auth_model; loader is live; consumer confirmed
`operations/call vault.kv.read target={"name":"rdc-vault"} ...` returns
`status: ok` in 66–80 ms via both REST and MCP).

**Honest release-notes language:**

> *"vault-1.x typed op surface ready for production
> (`jwt-federated` auth model, full ops catalog)."*

## State map of the v0.3.0 connectors

| Connector | State | Loader | Production-execute? |
|---|---|---|---|
| `vault-1.x` | 3 | JWT-federated, live | ✅ |
| `bind9-ssh-9.x` | 2-3 | `shared_service_account` inline | ✅ |
| `k8s-1.x` | 2 | [`load_kubeconfig_from_vault`](../../backend/src/meho_backplane/connectors/kubernetes/kubeconfig.py) — live operator-context Vault read (G3.10-T4 [#948](https://github.com/evoila/meho/issues/948)) | ✅ (`shared_service_account`) |
| `vmware-rest-9.0` | 2 | [`load_session_credentials_from_vault`](../../backend/src/meho_backplane/connectors/vmware_rest/session.py#L116) — live operator-context Vault read (G3.9-T3 [#942](https://github.com/evoila/meho/issues/942)) | ✅ (`shared_service_account`) |
| `vcf-automation-9.0` | 2 | [`load_credentials_from_vault`](../../backend/src/meho_backplane/connectors/vcf_automation/session.py) — live operator-context Vault read via the shared [`load_basic_credentials`](../../backend/src/meho_backplane/connectors/_shared/vault_creds.py) helper; `operator` threaded through the bespoke dual-plane auth (G3.10-T3 [#947](https://github.com/evoila/meho/issues/947)) | ✅ (`shared_service_account`) |
| `vcf-operations-9.0` (vROps) | 2 | Shared [`_shared/vcf_auth.load_credentials_from_vault`](../../backend/src/meho_backplane/connectors/_shared/vcf_auth.py) — live operator-context Vault read (G3.10-T2 [#946](https://github.com/evoila/meho/issues/946)) | ✅ (`shared_service_account`) |
| `vcf-logs-9.0` (vRLI) | 2 | Shared [`_shared/vcf_auth.load_credentials_from_vault`](../../backend/src/meho_backplane/connectors/_shared/vcf_auth.py) — live operator-context Vault read (G3.10-T2 [#946](https://github.com/evoila/meho/issues/946)) | ✅ (`shared_service_account`) |
| `vcf-fleet-9.0` | 2 | Shared [`_shared/vcf_auth.load_credentials_from_vault`](../../backend/src/meho_backplane/connectors/_shared/vcf_auth.py) — live operator-context Vault read (G3.10-T2 [#946](https://github.com/evoila/meho/issues/946)) | ✅ (`shared_service_account`) |
| `nsx-4.2` | 2 | [`load_session_credentials_from_vault`](../../backend/src/meho_backplane/connectors/nsx/session.py) — live operator-context Vault read (G3.10-T1 [#945](https://github.com/evoila/meho/issues/945)) | ✅ (`shared_service_account`) |
| `sddc-manager-9.0` | 2 | [`load_credentials_from_vault`](../../backend/src/meho_backplane/connectors/sddc_manager/session.py) — live operator-context Vault read (G3.10-T1 [#945](https://github.com/evoila/meho/issues/945)) | ✅ (`shared_service_account`) |
| `harbor-2.x` | 2 | [`load_credentials_from_vault`](../../backend/src/meho_backplane/connectors/harbor/session.py) — live operator-context Vault read (G3.10-T1 [#945](https://github.com/evoila/meho/issues/945)) | ✅ (`shared_service_account`) |
| `kubernetes-asyncio-1.x` | 2 (shadow) | Same as `k8s-1.x` | ✅ (`shared_service_account`) |

State 0.5 = `register_connector_v2` called but no ops registered yet.
Since [T5 #733](https://github.com/evoila/meho/issues/733), these
connectors surface in `GET /api/v1/connectors` with
`group_count: 0, operation_count: 0` (built-in, `tenant_id: null`)
so operators see `connector registered ⇒ visible in list` rather
than waiting for the first ingested or typed op to land.

The discovery meta-tools agree with the listing on this state. Since
[#1482](https://github.com/evoila/meho/issues/1482),
`list_operation_groups` / `search_operations` raise a typed
`ConnectorNotIngestedError` for a State-0.5 connector_id instead of the
opaque unknown-connector error: over MCP it is a `-32602` carrying
`error.data.reason="connector_not_ingested"` and the same
`meho connector ingest …` `next_step` hint the `state="registered"`
listing row emits; over REST it is a `404` with a structured `detail`
of the same shape. A genuinely unknown connector_id stays
distinguishable (`reason="unknown_connector"` over MCP, plain-string
`detail` over REST), so an agent can self-correct "run ingest" without
confusing it with "no such connector". The
registered-vs-unknown discriminator is
[`connector_class_registered`](../../backend/src/meho_backplane/operations/_lookup.py)
(in-memory v2-registry probe, no DB round-trip); the shared hint is
[`next_step_for_registered_connector`](../../backend/src/meho_backplane/operations/ingest/list_connectors.py).

## Why this is hard to get right

The k3d / testcontainers test pattern injects an `override_loader` at
test boundary. Tests pass. CI is green. The release-notes writer sees
"13 ops + k3d CI" and writes "13 ops, k3d CI" in the release body. An
adopter reads "k3d CI" as a production-execute signal. The catalog
indexes the op. `search_operations` finds it. `call_operation` dispatches
to the loader. The default loader raises `NotImplementedError`. The
adopter is surprised.

This isn't carelessness on anyone's part — it's the inherent shape of a
catalog-first connector architecture. The fix is **vocabulary discipline**
in the release-notes, kb, and Goal-tracker layers. Naming the three
states explicitly (and citing the open Goal that tracks the next-layer
work) lets adopters read "what state is this in?" off the release-notes
language directly.

## The release-notes convention going forward

Every connector-related entry in `CHANGELOG.md` cites the state and the
live auth_models. Examples:

- *"feat(connectors): G3.X — Foo typed connector dispatch + catalog (N
  ops indexed; loader wiring tracked under #M)."*
- *"feat(connectors): Foo typed connector — `service_account` auth
  model live; `per_user` tracked under #M."*
- *"feat(connectors): Foo typed connector ready for production (all
  advertised auth models live)."*

Codified in [CHANGELOG.md](../../CHANGELOG.md) under "How entries are
added" by [T7 #735](https://github.com/evoila/meho/issues/735).

## How the consumer-feedback triage caught the gap (2026-05-20)

The RDC operator team:

1. Deployed `meho:v0.3.0` to their `rke2-infra/meho` lab the day it
   shipped (REV 22→23, chart digest `sha256:08c934d6…`, git_sha
   `602ab880…`).
2. Ran a full read-only smoke; all 6 of 6 checks green.
3. Read the v0.3.0 release notes. The framing *"G3.2 Kubernetes typed
   connector (13 ops, k3d CI)"* + the apparent +31/-23 line delta on
   `connectors/kubernetes/kubeconfig.py` between the v0.2.1 and v0.3.0
   tags led to the natural conclusion *"the loader is wired upstream
   now; the v0.2.1 'binary by auth model' framing is no longer true."*
4. Shipped a kb supersede on that conclusion (consumer PR #637 at
   12:50 UTC).
5. Around 14:30 UTC, source-read the actual loader. Found it was still
   a `NotImplementedError` stub; the line delta was a docstring +
   error-message refactor, not a wire-up.
6. Corrected the kb supersede the same afternoon (consumer PR #643).
7. Filed the consolidated v0.3.0 feedback report to the meho team.

The recursion (their own kb caught their own assumption-from-release-notes
within hours) is the discipline working — *"the hook finds the error
fast"*, not *"the error never happens"*.

This doc captures the upstream half: making the vocabulary so explicit
that the assumption isn't there to make.

## References

- Consumer feedback report dated 2026-05-20 from
  `evoila-bosnia/claude-rdc-hetzner-dc`
- [CHANGELOG.md](../../CHANGELOG.md) — the v0.3.0 section + the
  convention codified by [T7 #735](https://github.com/evoila/meho/issues/735)
- [Goal #214 (Connector parity)](https://github.com/evoila/meho/issues/214)
  — the open Goal that loader-wiring tracks under (T6 #734 amends the
  body to reframe the curated-composite-ops model)
- Tickets from this triage:
  - [#728 (T1)](https://github.com/evoila/meho/issues/728) — operation_count rollup bug
  - [#729 (T2)](https://github.com/evoila/meho/issues/729) — schema hardening
  - [#730 (T3)](https://github.com/evoila/meho/issues/730) — uvicorn proxy-headers
  - [#731 (T4a)](https://github.com/evoila/meho/issues/731) — kill auto-derive `when_to_use` default
  - [#732 (T4b)](https://github.com/evoila/meho/issues/732) — curate group strings
  - [#733 (T5)](https://github.com/evoila/meho/issues/733) — surface State-0.5 connectors
  - [#734 (T6)](https://github.com/evoila/meho/issues/734) — Goal #214 body reframe
  - [#735 (T7)](https://github.com/evoila/meho/issues/735) — CHANGELOG amendment + convention
  - [#727 (S1)](https://github.com/evoila/meho/pull/727) — sddc-manager + harbor stub-message fix (the small PR shipped during this triage)
