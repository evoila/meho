# Connector auth — the per-target credential read identity (decision)

**Status:** proposed (gates Goal [#214](https://github.com/evoila/meho/issues/214) execution). Option A is what the code does now: the loaders take the `Operator` and read through `vault_client_for_operator` (see [What the code does now](#what-the-code-does-now)).
**Date:** 2026-05-22
**Context doc:** [docs/research/214-connector-credential-broker.md](../research/214-connector-credential-broker.md)

## The decision this resolves

Every MEHO REST/k8s connector resolves a per-target credential from Vault before
it can call the vendor API. The loader that does this is a deliberate
`NotImplementedError` stub today (see the research doc §1). Before any loader is
implemented, one architectural question must be answered because it shapes the
loader signature, the RBAC model, and the audit trail:

> **What identity performs the per-target Vault read — the operator's, or the
> backplane's own service identity?**

## What the code does now

- The loader types take the `Operator`: `KubeconfigLoader` in [`kubernetes/kubeconfig.py`](../../backend/src/meho_backplane/connectors/kubernetes/kubeconfig.py) and `VcfCredentialsLoader` in [`_shared/vcf_auth.py`](../../backend/src/meho_backplane/connectors/_shared/vcf_auth.py).
- `HttpConnector.auth_headers(target, operator)` in [`adapters/http.py`](../../backend/src/meho_backplane/connectors/adapters/http.py) gets the full `Operator`.
- The shared loaders in [`_shared/vault_creds.py`](../../backend/src/meho_backplane/connectors/_shared/vault_creds.py) (for example `load_basic_credentials`) open `vault_client_for_operator(operator)` and read the target's `secret_ref`.
- A `secret_ref` with a scheme prefix (`<kind>:<ref>`) is read from another credential store through [`_shared/credential_backend.py`](../../backend/src/meho_backplane/connectors/_shared/credential_backend.py) (`CREDENTIAL_BACKEND`). A bare path is read from Vault KV-v2.
- The VMware connectors named below (`vmware_rest`, `_shared/vcf_auth.py` users) ship in evoila's enterprise pack, not in this repository ([decision record](../decisions/2026-10-28-own-connectors-leave-the-public-tree.md)). Their file references below are kept as the history of the proposal.

The rest of this page is the proposal as written, with its reasoning.

## Options

### Option A — Operator-context (forward the operator's JWT) ✅ recommended

The loader calls the already-built
[`vault_client_for_operator(operator)`](../../backend/src/meho_backplane/auth/vault.py),
which forwards the operator's validated Keycloak JWT to Vault's JWT/OIDC auth
method (the existing `meho-mcp` role) and reads `target.secret_ref` as a KV-v2
secret under the operator's identity. This is exactly what the `vault-1.x`
connector already does for every op
([vault/ops.py](../../backend/src/meho_backplane/connectors/vault/ops.py)).

- **RBAC:** the operator's Vault policy. A single role enforces *per-operator*
  path scoping via **ACL policy templating** —
  `path "secret/data/targets/{{identity.entity.aliases.<accessor>.name}}/*"`
  scopes each operator to their own target secrets without per-operator roles.
  ([Vault policy templating](https://developer.hashicorp.com/vault/docs/concepts/policies))
- **Audit:** Vault's own audit log attributes every read to the operator's
  Identity entity (the JWT `user_claim`), HMAC-hashing the values. MEHO's audit
  row already records the operator + `target_id`. Dual attribution, for free.
- **Secret-zero:** none — JWT/OIDC trusts Keycloak as a third party; no AppRole
  `secret_id` to bootstrap.
- **Blast radius:** a per-request Vault token, scoped to the operator's policy,
  revoked on context exit. A compromised backplane cannot read more than the
  *currently-acting operator* could.
- **RFC 8693 framing:** impersonation-flavoured (Vault sees the operator).

### Option B — Backplane-service-identity (AppRole)

The backplane authenticates to Vault as itself (AppRole `role_id`+`secret_id`)
and reads target secrets under one broad backplane policy.

- **RBAC:** the backplane's policy (necessarily broad — it must read every
  target's secret). No per-operator scoping at Vault.
- **Audit:** Vault attributes every read to the backplane; per-operator
  attribution exists only in MEHO's own audit log.
- **Secret-zero:** reintroduced — the `secret_id` must be delivered securely
  (response-wrapping / Vault Agent). ([AppRole pattern](https://developer.hashicorp.com/vault/docs/auth/approle/approle-pattern))
- **Blast radius:** a compromised backplane holds a token that can read *every*
  target's credential.
- **RFC 8693 framing:** delegation-flavoured, but without token exchange the
  `act`/`may_act` linkage never reaches Vault.

## Recommendation: Option A (operator-context)

Four reasons, in priority order:

1. **The primitive already exists and is proven in production.**
   `vault_client_for_operator` + the KV-v2 read is the live `vault-1.x` path
   (rubric State 3). Option A reuses it; Option B builds a parallel auth path
   MEHO does not currently have.
2. **It is the stubs' stated intent.** Every stub's docstring and error message
   says "the *operator-context* per-target Vault credential read is not yet
   wired" (`vmware_rest/session.py`, now in the enterprise pack,
   [_shared/vcf_auth.py](../../backend/src/meho_backplane/connectors/_shared/vcf_auth.py),
   [kubernetes/kubeconfig.py](../../backend/src/meho_backplane/connectors/kubernetes/kubeconfig.py)).
3. **Per-operator RBAC *and* audit through one role**, via templated policy — the
   research-doc §2 finding. Option B can't match the attribution without extra
   machinery (token exchange).
4. **Smaller blast radius + no secret-zero.** Aligns with the comparable-systems
   synthesis (Boundary "inject, never broker"; secret-zero-free).

This is also the lowest-friction default: it is less new code than Option B, not
more.

### The one carve-out: system-initiated calls have no operator JWT

Background/scheduled work (e.g. the topology scheduler) runs as a synthesised
system operator with `raw_jwt=""`
([connectors/vault/connector.py](../../backend/src/meho_backplane/connectors/vault/connector.py)).
Such a call **cannot** perform an operator-context Vault read. For v0.x this is
acceptable and explicit: **system-initiated calls that need a vendor credential
are out of scope** — today the only system caller (the readiness probe) hits
Vault's *unauthenticated* health endpoint and forwards no token. A loader that
receives an operator with empty `raw_jwt` MUST fail closed with a clear error
("operator-context credential read requires an authenticated operator;
target=…"), never silently fall back. A backplane-AppRole fallback for
system-initiated connector calls is a **later, additive** option to file only
when a concrete need exists — not built speculatively now.

## Blast radius of implementing Option A (signature changes)

The change is mechanical and bounded; `operator.raw_jwt` already reaches
`auth_headers`, it is just dropped:

1. **Loader type aliases** gain an `Operator` parameter:
   - `VsphereSessionLoader = Callable[[VsphereTargetLike], Awaitable[dict]]` →
     `Callable[[VsphereTargetLike, Operator], Awaitable[dict]]`
     (`vmware_rest/session.py`, now in the enterprise pack)
   - `VcfCredentialsLoader` ([_shared/vcf_auth.py](../../backend/src/meho_backplane/connectors/_shared/vcf_auth.py))
   - `KubeconfigLoader` ([kubernetes/kubeconfig.py](../../backend/src/meho_backplane/connectors/kubernetes/kubeconfig.py))

   **Pass the full `Operator`, not just `raw_jwt`** — `vault_client_for_operator`
   takes an `Operator`, and the future `per_user` model needs `operator.sub` to
   key the secret path. `Operator` is frozen, so there's no confused-deputy risk.

2. **`auth_headers` stops discarding `raw_jwt`** and threads the operator down to
   the loader. Today `auth_headers(target, raw_jwt)` does `del raw_jwt`
   (`vmware_rest/connector.py`, now in the enterprise pack).
   Cleanest fix: the dispatcher already has the full `Operator`; thread the
   `Operator` (not just `raw_jwt`) from `dispatch_ingested`
   ([_branches.py](../../backend/src/meho_backplane/operations/_branches.py))
   → `_request_json`/`_post_json` → `auth_headers` → `_session_token` → loader.
   This is an `HttpConnector` ABC-surface change
   ([adapters/http.py](../../backend/src/meho_backplane/connectors/adapters/http.py))
   affecting every HTTP connector — do it once in the base + vmware as the canary,
   then fan out. `CredentialsCache.get(target)` →
   `get(target, operator)` ([_shared/vcf_auth.py](../../backend/src/meho_backplane/connectors/_shared/vcf_auth.py)).

3. **The loader body** becomes: `async with vault_client_for_operator(operator)
   as client: read KV-v2 at target.secret_ref → return {"username","password"}`
   (or kubeconfig YAML for k8s). Mirror the `vault/ops.py` unwrap +
   error-class contract.

4. **Tests** keep injecting fake loaders; the new loaders' own tests exercise the
   live read against a Vault dev-mode harness (rubric State-2 bar).

### What does NOT change

The dispatch chain, resolver, target model, audit, policy gate, JSONFlux hook,
catalog, and the agent surface are untouched — this is purely the
credential-read leaf of the chain.

## Consequences

- Connectors move from rubric **State 1 → State 2** (`shared_service_account`
  live) one at a time, reusing one shared Vault-read helper.
- `per_user` / `impersonation` auth models stay deferred (they raise a clear
  boundary error today); Option A's "pass the full `Operator`" choice keeps
  `per_user` cheap to add later.
- A **deploy-time prerequisite** is created: the Vault `meho-mcp` role's policy
  must grant operators read on their target secret paths (templated policy), and
  the operator's Keycloak→Vault Identity entity must exist. This is an operator
  onboarding/runbook item, not code — documented in the deploy runbook
  [`docs/cross-repo/connector-vault-policy.md`](../cross-repo/connector-vault-policy.md)
  (templated ACL policy recipe + Keycloak→Vault identity prerequisite +
  verification).

## Cache scoping under `shared_service_account`

Each connector that holds Vault-sourced credentials or session tokens caches
them per target, with no operator dimension. The key is the target's
`(tenant_id, id)` (`target_cache_key` in
`backend/src/meho_backplane/connectors/_shared/cache_key.py`), so two
same-named targets in two tenants never share an entry. Login tokens use
`credential_cache_key` instead: two targets in one tenant with the same
secret and the same login address share one token and one login.
This is intentional under the only auth model State 2 supports
(`shared_service_account`) and load-bearing in the docstrings of every cache
site. Restating it here so future review (human and bot) doesn't relitigate
it as a cross-operator leak.

### The contract

For a target tagged `shared_service_account`:

- The KV-v2 secret at `target.secret_ref` is **the** service-account
  credential pair the connector authenticates with. The same `(username,
  password)` is used for every operator who dispatches against the target —
  by design, because the service account itself is shared.
- The vendor-session token (vRLI bearer, vSphere `vmware-api-session-id`,
  vcf-automation provider JWT, vcf-automation tenant token) is minted from
  that shared credential pair. A token returned to operator *A* would be
  byte-identical to the token minted for operator *B* on the very next call
  — caching it once is the right shape, not a leak.
- Per-operator attribution still lands in MEHO's own audit log (the
  `dispatch` row) and in Vault's audit log on the **first** read per
  caller, target and connector instance. A cached entry is handed to a
  caller only after that caller's own Vault read of the target's secret
  passed once in this process. Later calls of the same caller skip Vault,
  which is the cache's whole point; a new caller reads once first, so
  Vault's answer reaches every caller also when the cache is warm. The
  mechanism is `SecretReaders` in
  `backend/src/meho_backplane/connectors/_shared/secret_readers.py`
  (see `docs/codebase/connectors-shared-secret-readers.md`).

### The cache implementations

| Connector | File | Cache | Notes |
| --- | --- | --- | --- |
| Shared VCF helper (used by the VMware connectors of the enterprise pack) | `backend/src/meho_backplane/connectors/_shared/vcf_auth.py` | `CredentialsCache` | Credential dict cache; per-target `{username, password}`. |
| harbor | `backend/src/meho_backplane/connectors/harbor/connector.py` | `_creds_cache` (in `_load_credentials`) | Credential dict cache; per-target `{username, password}` consumed via HTTP Basic. |
| SSH adapter (every connector built on `SshConnector`) | `backend/src/meho_backplane/connectors/adapters/ssh.py` | `_connections` (in `_connect`) | Open SSH connection pool, keyed on `(tenant_id, id, caller kind, caller sub)`. Not a credential cache: an entry is a logged-in session, so it is handed back only to the caller whose own Vault read opened it. The caller kind keeps the check runner, a call without a token (a runbook verification) and an operator-less probe in their own slots, also when they run under a person's `sub`. `invalidate_credentials` drops every caller's connection to the target. |

The other public connectors follow the same pattern: a `SecretReaders` list
and an `invalidate_credentials` hook. They are `argocd`, `azure`, `entra`,
`gcloud`, `github`, `hetzner_robot`, `keycloak`, `kubernetes`, `phpipam`,
`proxmox`, `rabbitmq` and the profiled connectors (`connectors/profiled.py`).
The vRLI, vcf-automation, vmware-rest and sddc-manager caches ship with the
VMware connectors in the enterprise pack.

Every cache site applies a fail-closed guard before the cache lookup so a
cache hit can never short-circuit past the loader and return
previously-primed credentials to a caller that could not itself resolve
them. The **primary** fail-closed gate lives one layer deeper, in the
Vault credential backend's `load_secret_data` in
[`vault_creds.py`](../../backend/src/meho_backplane/connectors/_shared/vault_creds.py),
which refuses an empty `operator.raw_jwt` before any Vault call;
the cache guards are the **second** layer — defense-in-depth.

The guard has two parts:

- **Who has read.** Every cache keeps a `SecretReaders` list: the callers
  (by kind and `operator.sub`) whose own Vault read of the target's secret
  passed in this process. A cache hit counts only for such a caller. For
  any other caller the cache method runs the credential loader under that
  caller's login first; the loader's answer decides, and a refused read
  raises. A person, the check runner and the system operator are each
  their own caller: the check runner has its own token and Vault role but
  may run a Sensor under its creator's `sub`, so its kind
  (`check_runner_dispatch`) is part of the key and it never counts as
  that person. The vendor login stays shared: a new
  caller gets the cached token after their read, without a second login
  (some vendors allow only one active session per user). A target change
  (`invalidate_credentials`) forgets the readers of that target together
  with its entries. The synthesised system operator is never recorded, so
 it runs the loader every time; on a live Vault that read fails
 closed, because its placeholder `raw_jwt` is not a valid Keycloak
  JWT.
- **The older pre-checks stay.** The session-token and VCF caches reject
  an **empty** `operator.raw_jwt` at the cache method
  (`VaultCredentialsReadError` without touching the cache), and the
  credential caches that keyed the fast path off `is_system_operator`
  keep the system operator out of the cache write as before. A caller who
  has read keeps the cold-load → cache → reuse path.

(Each consuming connector's `auth_headers` enforces a separate constraint —
the `auth_model == "shared_service_account"` boundary — and does not itself
reject the system caller; the loader and the cache guards do.)

### Cache eviction — establish-auth failures (#2396)

The credential caches above are populated on the **first** load and are
otherwise long-lived: the credential dict is written *before* the login
attempt (e.g. `harbor` fills `_creds_cache` in `_load_credentials`
before the first vendor call), and the session-token invalidation seam
(`invalidate_session`, the #2067 dispatch-path recovery) deliberately leaves
the credential cache intact — a mid-session 401 means the *token* expired, not
that the credential is wrong. That left one gap: a credential rejected at
**establish** time (a rotated/stale password the login POST 401/403s) stayed
cached, so an operator's out-of-band restage never took effect until a
backplane restart.

Since #2396 the dispatcher evicts the cached credential on an establish-auth
failure. Each caching connector exposes a duck-typed
`invalidate_credentials(target)` hook (the establish-time companion to
`invalidate_session`) that pops its `_creds_cache` entry under the credential
lock, or delegates to `CredentialsCache.invalidate` for the shared-VCF-helper
consumers. The dispatcher calls it — `getattr`-guarded — from **both**
`ConnectorAuthError` arms (first-establish and post-`invalidate_session`
recovery), so the **next** dispatch after a restage re-reads Vault and
converges with no restart. Eviction is not paired with an immediate retry: at
failure time the restage has not happened yet, so a retry would only replay the
rejected bytes; convergence needs the operator's restage between dispatches.

The profiled connectors (and, in the enterprise pack, `nsx`, `vmware-rest`,
`hcx`, `salt`, the SRM / vSphere Replication base and `vro`) cache only the session token and re-read the
service-account credential from Vault on every establish, so a restage already
converges on the next cold-session dispatch. Their `invalidate_credentials`
hook drops the session and forgets who has read the target's secret, so after
a target change every caller reads the new secret before the next login.

### Why not key the cache on the operator too

Under `shared_service_account` the underlying credential is shared. A
`(target key, operator.sub)` cache key would multiply the cache by every
operator who has ever dispatched against the target, all entries holding
byte-identical credentials. Storage waste, identical wire traffic, no
extra audit fidelity (Vault still attributes only the first read per
operator-cache-entry).

The SSH connection pool is the one cache keyed on the caller. It does not
hold the credential; it holds an open, logged-in session. Serving that
session to a second caller would skip the Vault read for that caller, so
the pool keeps one connection per caller and target. A caller is the kind
of login (a person or machine, a call without a token, the check runner,
or the system caller) plus the `sub`. A Sensor runs under its creator's
`sub` with the check runner's own token and Vault role, and a runbook
verification runs under the assigned person's `sub` with no token, so
Vault refuses its read. Each gets its own slot and never the person's
session. The cost is one SSH login per caller per target instead of one
per target.

### When this changes — `per_user` / `impersonation`

Both of those auth models are explicitly out of scope for State 2 (see
`Goal #214` and the boundary checks in each connector's `auth_headers`).
When either lands, the cache key MUST extend to `(target key,
operator.sub)` because the credential pair (or kubeconfig user, or
impersonation chain) becomes per-operator. Until then, threading
`operator.sub` into the key is speculative complexity — the cache shape
should match the credential reality.

The fail-closed guard at the top of each cache method already enforces
the half of the contract that survives the auth-model transition (the
operator must be authenticated); only the key composition changes when
the auth-model boundary widens.

## Open question for the human (approval gate)

Confirm **Option A (operator-context)**. If per-operator Vault policy
administration is judged too heavy for the dogfood phase and a single broad
backplane policy is preferred short-term, say so — that selects Option B and
changes the first Initiative's RBAC/runbook tasks (but not the loader call
sites, which still receive the `Operator`).
