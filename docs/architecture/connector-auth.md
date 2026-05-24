# Connector auth — the per-target credential read identity (decision)

**Status:** proposed (gates Goal [#214](https://github.com/evoila/meho/issues/214) execution)
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

## Options

### Option A — Operator-context (forward the operator's JWT) ✅ recommended

The loader calls the already-built
[`vault_client_for_operator(operator)`](../../backend/src/meho_backplane/auth/vault.py#L198),
which forwards the operator's validated Keycloak JWT to Vault's JWT/OIDC auth
method (the existing `meho-mcp` role) and reads `target.secret_ref` as a KV-v2
secret under the operator's identity. This is exactly what the `vault-1.x`
connector already does for every op
([vault/ops.py:294](../../backend/src/meho_backplane/connectors/vault/ops.py#L294)).

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
   wired" ([vmware_rest/session.py:99](../../backend/src/meho_backplane/connectors/vmware_rest/session.py#L99),
   [_shared/vcf_auth.py:182](../../backend/src/meho_backplane/connectors/_shared/vcf_auth.py#L182),
   [kubernetes/kubeconfig.py:86](../../backend/src/meho_backplane/connectors/kubernetes/kubeconfig.py#L86)).
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
([connectors/vault/connector.py:311](../../backend/src/meho_backplane/connectors/vault/connector.py#L311)).
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
     ([vmware_rest/session.py:86](../../backend/src/meho_backplane/connectors/vmware_rest/session.py#L86))
   - `VcfCredentialsLoader` ([_shared/vcf_auth.py:165](../../backend/src/meho_backplane/connectors/_shared/vcf_auth.py#L165))
   - `KubeconfigLoader` ([kubernetes/kubeconfig.py:53](../../backend/src/meho_backplane/connectors/kubernetes/kubeconfig.py#L53))

   **Pass the full `Operator`, not just `raw_jwt`** — `vault_client_for_operator`
   takes an `Operator`, and the future `per_user` model needs `operator.sub` to
   key the secret path. `Operator` is frozen, so there's no confused-deputy risk.

2. **`auth_headers` stops discarding `raw_jwt`** and threads the operator down to
   the loader. Today `auth_headers(target, raw_jwt)` does `del raw_jwt`
   ([vmware_rest/connector.py:270](../../backend/src/meho_backplane/connectors/vmware_rest/connector.py#L270)).
   Cleanest fix: the dispatcher already has the full `Operator`; thread the
   `Operator` (not just `raw_jwt`) from `dispatch_ingested`
   ([_branches.py:167](../../backend/src/meho_backplane/operations/_branches.py#L167))
   → `_request_json`/`_post_json` → `auth_headers` → `_session_token` → loader.
   This is an `HttpConnector` ABC-surface change
   ([adapters/http.py:103,119,159](../../backend/src/meho_backplane/connectors/adapters/http.py#L103))
   affecting every HTTP connector — do it once in the base + vmware as the canary,
   then fan out. `CredentialsCache.get(target)` →
   `get(target, operator)` ([_shared/vcf_auth.py:251](../../backend/src/meho_backplane/connectors/_shared/vcf_auth.py#L251)).

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
them per target — keyed on `target.name` alone, with no operator dimension.
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
  `(target, connector-instance)` lifetime — every subsequent cache hit
  skips Vault entirely, which is the cache's whole point.

### The four cache implementations

| Connector | File | Cache | Notes |
| --- | --- | --- | --- |
| Shared VCF helper (vROps, vRLI, Fleet) | `backend/src/meho_backplane/connectors/_shared/vcf_auth.py` | `CredentialsCache` | Credential dict cache; per-target `{username, password}` consumed via HTTP Basic by vROps/Fleet and via session-establish by vRLI. |
| vRLI | `backend/src/meho_backplane/connectors/vcf_logs/connector.py` | `_session_tokens` (in `_session_token`) | Bearer-token cache; the session id from `POST /api/v2/sessions`. |
| vcf-automation (provider plane) | `backend/src/meho_backplane/connectors/vcf_automation/connector.py` | `_provider_tokens` (in `_provider_session_token`) | `X-VMWARE-VCLOUD-ACCESS-TOKEN` JWT cache. |
| vcf-automation (tenant plane) | `backend/src/meho_backplane/connectors/vcf_automation/connector.py` | `_tenant_tokens` (in `_tenant_session_token`) | `{"token": ...}` body-value cache. |
| vmware-rest | `backend/src/meho_backplane/connectors/vmware_rest/connector.py` | `_session_tokens` (in `_session_token`) | `vmware-api-session-id` cache; the G3.9 precedent every other cache here copied. |

All five cache sites apply the same fail-closed guard before the cache
lookup: an empty `operator.raw_jwt` raises `VaultCredentialsReadError`
without touching the cache. That mirrors the loader path's identical guard
at
[`vault_creds.py:188`](../../backend/src/meho_backplane/connectors/_shared/vault_creds.py#L188)
and exists as defense-in-depth — every consuming connector's
`auth_headers` already rejects system-initiated calls (`raw_jwt == ""`)
at the boundary, but a future regression in that boundary check must not
open a silent cache-hit path.

### Why not key the cache on the operator too

Under `shared_service_account` the underlying credential is shared. A
`(target.name, operator.sub)` cache key would multiply the cache by every
operator who has ever dispatched against the target, all entries holding
byte-identical credentials. Storage waste, identical wire traffic, no
extra audit fidelity (Vault still attributes only the first read per
operator-cache-entry).

### When this changes — `per_user` / `impersonation`

Both of those auth models are explicitly out of scope for State 2 (see
`Goal #214` and the boundary checks in each connector's `auth_headers`).
When either lands, the cache key MUST extend to `(target.name,
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
