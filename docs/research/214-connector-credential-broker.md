# Connector credential brokering — research + execution-gap map (Goal #214)

Research artifact for Goal [#214](https://github.com/evoila/meho/issues/214)
(connector parity). Answers one question: **what has to land for a MEHO REST
connector to execute against a real vendor target, and how should the
per-target credential read be designed** so the agent/operator never handles
the secret value, with per-operator RBAC + audit.

Two halves:

1. **§1 Execution-gap map** — what is wired vs stubbed in the dispatch chain
   today, with `file:line` evidence (re-derived from the tree on 2026-05-22,
   not from prior session notes).
2. **§2–§7 Research** — industry patterns + standards for secrets brokering,
   operator- vs workload-scoped access, multi-vendor session lifecycle,
   comparable systems, audit/least-privilege, and testing without real
   secrets.

The gating architecture decision this research feeds is written up separately
in [docs/architecture/connector-auth.md](../architecture/connector-auth.md).

---

## §1 — Execution-gap map: wired vs stubbed

**Finding (verified):** the dispatch substrate is fully wired end-to-end. The
production gap is the **per-target credential read** in each REST/k8s
connector's loader, plus the **operator-identity threading** that would let
that loader read Vault under the operator's identity. One connector
(`vault-1.x`) already does the operator-context Vault read and is the working
reference; `bind9-ssh-9.x` executes but via a *different* (embedded-credential)
path that does not generalise (see the bind9 note below).

### The dispatch chain (operator → vendor API)

| Stage | Status | Evidence |
|---|---|---|
| Agent/CLI/REST entry → `call_operation` | ✅ wired | [operations/meta_tools.py:455](../../backend/src/meho_backplane/operations/meta_tools.py#L455) — resolves target via `resolve_target`, binds `audit_target_id`, calls `dispatch` |
| Target resolution (name/alias → DB row) | ✅ wired | [targets/resolver.py](../../backend/src/meho_backplane/targets/resolver.py); `call_operation` calls it at [meta_tools.py:491](../../backend/src/meho_backplane/operations/meta_tools.py#L491) |
| `dispatch(...)` — lookup → validate → policy → resolve → branch → reduce → audit | ✅ wired | [operations/dispatcher.py:469](../../backend/src/meho_backplane/operations/dispatcher.py#L469) (8-phase flow) |
| Connector resolution by fingerprint | ✅ wired | [connectors/resolver.py](../../backend/src/meho_backplane/connectors/resolver.py); called from [dispatcher.py:525](../../backend/src/meho_backplane/operations/dispatcher.py#L525) via `_resolve_connector_instance` |
| Source-kind branches (`ingested`/`typed`/`composite`) | ✅ wired | [operations/_branches.py:119](../../backend/src/meho_backplane/operations/_branches.py#L119) |
| `operator.raw_jwt` carried to connector transport | ✅ wired | `dispatch_ingested` reads `raw_jwt = operator.raw_jwt` and forwards it to `_request_json`/`_post_json` — [_branches.py:167](../../backend/src/meho_backplane/operations/_branches.py#L167) |
| HTTP transport injects auth via `auth_headers(target, raw_jwt)` | ✅ wired | [adapters/http.py:143](../../backend/src/meho_backplane/connectors/adapters/http.py#L143) calls `await self.auth_headers(target, raw_jwt)` |
| **REST connector `auth_headers` uses `raw_jwt`** | ❌ **discards it** | `vmware_rest` does `del raw_jwt` then `self._session_token(target)` — [vmware_rest/connector.py:270](../../backend/src/meho_backplane/connectors/vmware_rest/connector.py#L270) |
| **Loader receives operator identity** | ❌ **signature is `loader(target)`** | `VsphereSessionLoader = Callable[[VsphereTargetLike], Awaitable[dict[str,str]]]` — [vmware_rest/session.py:86](../../backend/src/meho_backplane/connectors/vmware_rest/session.py#L86). No `operator`/`raw_jwt` parameter. |
| **Default loader does the Vault read** | ❌ **`NotImplementedError` stub** | [vmware_rest/session.py:99](../../backend/src/meho_backplane/connectors/vmware_rest/session.py#L99); shared VCF stub [_shared/vcf_auth.py:182](../../backend/src/meho_backplane/connectors/_shared/vcf_auth.py#L182); k8s [kubernetes/kubeconfig.py:86](../../backend/src/meho_backplane/connectors/kubernetes/kubeconfig.py#L86) |
| Operator-context Vault read primitive | ✅ **exists, proven** | `vault_client_for_operator(operator)` JWT/OIDC-logs-in with `operator.raw_jwt`, yields an authed hvac client, revokes on exit — [auth/vault.py:198](../../backend/src/meho_backplane/auth/vault.py#L198) |
| KV-v2 read under operator identity | ✅ **exists, proven** | Vault connector op: `async with vault_client_for_operator(operator) as client: client.secrets.kv.v2.read_secret_version(path=..., mount_point=...)` — [vault/ops.py:294](../../backend/src/meho_backplane/connectors/vault/ops.py#L294) |

### The precise gap, in one sentence

`operator.raw_jwt` already reaches the connector's `auth_headers(target, raw_jwt)`,
but every REST connector **drops it** and every credential loader's signature is
`loader(target)` — so the loader cannot reach the already-built
`vault_client_for_operator(operator)` primitive to perform the per-target Vault
read. Closing the gap = (a) thread the operator (or `raw_jwt`) from
`auth_headers`/`_session_token` into the loader, and (b) implement the loader as
a KV-v2 read of `target.secret_ref` under `vault_client_for_operator`.

### Connector-by-connector state (against the 3-state rubric)

Per [docs/codebase/connector-release-readiness.md](../codebase/connector-release-readiness.md):

| Connector | Loader | State | Executes against a real DB-backed target? |
|---|---|---|---|
| `vault-1.x` | operator-context KV-v2 read, live ([vault/ops.py:294](../../backend/src/meho_backplane/connectors/vault/ops.py#L294)) | **3** | ✅ — the reference chain |
| `bind9-ssh-9.x` | reads `target.secret_ref` as a **dict** (`secret_ref.get("password")`) — [bind9/ops_record.py:912](../../backend/src/meho_backplane/connectors/bind9/ops_record.py#L912) | **2 (with caveat)** | ⚠️ see note |
| `vmware-rest-9.0` | `load_session_credentials_from_vault` — `NotImplementedError` | **1** | ❌ (mock loader in tests only) |
| `nsx-4.2`, `harbor-2.x`, `sddc-manager-9.0` | per-connector `session.py` stub | **0.5–1** | ❌ |
| `vcf-operations/logs/fleet/automation-9.0` | shared `_shared/vcf_auth.py` stub | **0.5–1** | ❌ |
| `k8s-1.x` | `load_kubeconfig_from_vault` — `NotImplementedError` | **1** | ❌ (mock loader in tests only) |

**bind9 caveat (correction to the "bind9 works end-to-end" claim).** bind9's
credential read is `_sudo_password_from_target(target)`, which requires
`target.secret_ref` to be a **dict** and reads `secret_ref.get("sudo_password")
or secret_ref.get("password")`
([ops_record.py:912-934](../../backend/src/meho_backplane/connectors/bind9/), same in
[ops_config.py:430](../../backend/src/meho_backplane/connectors/bind9/)). But the
persisted `targets.secret_ref` column is **`Text` (a string)**
([alembic/versions/0004_create_targets_and_audit_target_id.py:139](../../backend/alembic/versions/0004_create_targets_and_audit_target_id.py#L139)),
and the `Target` Pydantic schema declares `secret_ref: str | None`
([targets/schemas.py:104](../../backend/src/meho_backplane/targets/schemas.py#L104)).
So bind9's loader proves **dispatch + SSH transport + atomic-apply**, but its
credential model (embedded dict) is *inconsistent with the persisted target
shape* and is **not** the Vault-broker model the REST connectors need. bind9 is
not a usable template for this Goal; the `vault-1.x` op path is.

### Proof the only gap is the production loader

Every connector's E2E/auth test passes by **injecting a fake loader**, which is
exactly what isolates the production loader as the single missing piece:

- vmware: `VmwareRestConnector(session_loader=_stub_loader)` and an explicit
  test asserting the default raises (`pytest.raises(NotImplementedError,
  match=r"deliberate stub.*#214")`) —
  [tests/test_connectors_vmware_rest_auth.py:106,143](../../backend/tests/test_connectors_vmware_rest_auth.py)
- nsx: `instance._session_loader = _nsx_session_loader  # bypass Vault` —
  [tests/test_connectors_nsx_e2e.py:223](../../backend/tests/test_connectors_nsx_e2e.py#L223)
- vcf-*/sddc/harbor: same injected-loader shape in their `_e2e` / `_auth` tests.

### vcsim is NOT a real-vCenter E2E (scoping consequence)

The vmware integration test is **respx-mocked**, not a live `vmware/vcsim`
container — because govmomi's vcsim does not serve the modern `/api/about`
endpoint `fingerprint()` calls (it 404s):
[tests/integration/test_connectors_vmware_rest_vcsim.py:4-16](../../backend/tests/integration/test_connectors_vmware_rest_vcsim.py).
**Consequence for Goal #214:** the "one real call" proof cannot be vcsim for the
modern REST surface. It must be either (a) a recorded-fixture E2E (respx replay
of a captured real-vCenter exchange) for CI, plus (b) an opt-in smoke against a
lab vCenter, gated behind an env-marker so secret-free CI stays green.

---

## §2 — Secrets brokering: "the operator/agent never sees the value"

The load-bearing requirement (CLAUDE.md postulate; v0.1-spec §6/§7; secret-broker
issue [#581](https://github.com/evoila/meho/issues/581)) is that the credential
value never enters the agent's context, the operator's shell, or any transcript.
The backplane is the credentialed intermediary; the operator submits *intent*
(`call_operation(..., target)`), and the backplane resolves the secret
server-side.

### Static (KV v2) vs dynamic secrets

- **KV v2** stores an operator-managed static secret (username/password,
  kubeconfig, API key). Use only for secrets that *must* be static — vendor
  service accounts that the backplane cannot mint. KV v2 adds versioning + soft
  delete vs v1. ([Understand static and dynamic secrets](https://developer.hashicorp.com/vault/tutorials/get-started/understand-static-dynamic-secrets))
- **Dynamic secrets** are generated on demand with a short TTL and auto-expire;
  prefer them whenever the backend supports a Vault secrets engine (databases,
  cloud IAM, SSH, PKI). A 1-hour TTL means a leaked credential is useless within
  the hour. ([Manage dynamic credential leases](https://developer.hashicorp.com/vault/tutorials/db-credentials/manage-dynamic-leases), [Tune the lease TTL](https://developer.hashicorp.com/vault/docs/troubleshoot/tune-lease-ttl))

**MEHO fit:** vCenter/NSX/SDDC/Harbor/VCF service accounts are *vendor-owned*
static credentials — KV v2 is the correct day-1 store (matches the consumer's
`targets.yaml` `secret_ref` convention). Dynamic secrets are a later option only
where a vendor has a Vault secrets engine (e.g. a future LDAP/AD-backed account).
Design the loader so a dynamic-secret backend is a *different loader*, not a
different call site.

### Vault auth method: JWT/OIDC vs AppRole

- **JWT/OIDC.** Vault treats the IdP (Keycloak) as a trusted third party and
  needs no pre-deployed secret. The `user_claim` becomes the Identity entity
  alias; `groups_claim` maps to group aliases — both available for policy
  scoping. ([JWT/OIDC auth](https://developer.hashicorp.com/vault/docs/auth/jwt))
- **AppRole.** A workload (the backplane) authenticates with a `role_id` +
  `secret_id`. This re-introduces the **secret-zero problem** — how to deliver
  the first secret securely — solved with response-wrapping the `secret_id` via
  `-wrap-ttl`. ([AppRole best practices](https://developer.hashicorp.com/vault/docs/auth/approle/approle-pattern), [Response wrapping](https://developer.hashicorp.com/vault/docs/concepts/response-wrapping))

**MEHO already uses JWT/OIDC** for the Vault connector
([auth/vault.py:198](../../backend/src/meho_backplane/auth/vault.py#L198)),
forwarding the operator's *own* validated Keycloak JWT to Vault's JWT auth
method. This is the secret-zero–free path and is the existing, proven primitive.

### Per-operator RBAC through a single role: ACL policy templating

A single Vault role (the existing `meho-mcp` role) can still enforce
**per-operator** path scoping via **templated ACL policies**. Vault renders
identity attributes into policy paths at evaluation time:

- `{{identity.entity.aliases.<mount accessor>.name}}` — the operator's
  `user_claim` (their identity).
- `{{identity.entity.metadata.<key>}}`, `{{identity.groups.names.<name>.id}}`,
  etc. ([Policy templating](https://developer.hashicorp.com/vault/docs/concepts/policies))

Example: `path "secret/data/targets/{{identity.entity.aliases.<accessor>.name}}/*"`
scopes each operator to their own target secrets through one role. Wildcards/globs
are not permitted in a template's rendered output (prevents injection).

**Consequence for the gating decision:** operator-context JWT login yields
per-operator **RBAC** (via templated policy) *and* per-operator **audit** (Vault
logs the operator's entity), through the single role MEHO already configured —
not just weaker attribution. This is the decisive point in
[connector-auth.md](../architecture/connector-auth.md).

### Response wrapping, namespaces, secret-zero

- **Response wrapping** (cubbyhole single-use token, short TTL, unwrap-once,
  tamper-evident) is the right primitive for the [#581](https://github.com/evoila/meho/issues/581)
  store-to-store *secret move* (the agent gets a wrapping token / hash, never the
  value), but is **not needed** for the per-target read on this Goal — the
  backplane reads and *uses* the credential server-side without ever returning it.
  ([Response wrapping](https://developer.hashicorp.com/vault/docs/concepts/response-wrapping))
- **Namespaces** (Vault Enterprise/HCP) isolate tenants; relevant if MEHO's
  multi-tenancy ever maps tenants to Vault namespaces. Out of scope for v0.x
  (OSS Vault; `vault_namespace` setting already plumbed in `_build_client`).

---

## §3 — Operator-scoped vs workload-scoped credential access

Two identities could perform the per-target Vault read:

1. **Operator-context** — forward the operator's Keycloak JWT to Vault
   (`vault_client_for_operator(operator)`). RBAC = operator's templated Vault
   policy; Vault's audit log attributes the read to the operator's entity. This
   is RFC 8693 **impersonation** in spirit: the backplane presents the
   operator's own token, so Vault sees the operator.
   ([RFC 8693 §1.1](https://datatracker.ietf.org/doc/html/rfc8693))
2. **Workload-context** — the backplane authenticates as itself (AppRole) and
   reads under its own broad policy. Attribution to the operator exists only in
   MEHO's *own* audit log, not in Vault's. This is RFC 8693 **delegation** in
   spirit (the backplane acts on behalf of the operator, recording the actor
   separately) — but without token exchange, the `act`/`may_act` linkage is not
   carried into Vault. ([RFC 8693 impersonation vs delegation](https://datatracker.ietf.org/doc/html/rfc8693))

**RFC 8693 token exchange** formalises the on-behalf-of pattern: a service
exchanges a `subject_token` (the user) — optionally with an `actor_token` (the
service) — at an STS for a token suited to the downstream call, and the `act` /
`may_act` claims preserve the delegation chain for attribution. MEHO does not
need a full STS today; the operator-JWT-forward already gives per-operator
attribution at Vault for free. Token exchange becomes relevant only if MEHO later
needs *vendor-side* per-operator identity (impersonation auth model, §4).

**Short-lived credentials / workload identity** (the SPIFFE/SPIRE direction) are
the principled long-term answer where a downstream supports it: the workload
proves identity and receives an ephemeral credential, eliminating long-lived
shared secrets. For vendor APIs that only accept a static service account
(vCenter/NSX/etc.), the static-KV + operator-context-read pattern is the
pragmatic floor; the loader abstraction must not preclude a future
short-lived/dynamic loader.

---

## §4 — Multi-vendor session lifecycle + the auth-model taxonomy

### Session establishment + caching (already implemented, just credential-blocked)

The REST connectors already implement per-target session lifecycle correctly;
they are only missing the credential the session-establish call needs:

- **vSphere REST:** `POST /api/session` (modern) with HTTP Basic → returns a
  `vmware-api-session-id` token reused on subsequent calls; falls back to legacy
  `POST /rest/com/vmware/cis/session` on 404 (vcsim/older vCenter). Token cached
  per `target.name` under an `asyncio.Lock`; revoked via `DELETE` on `aclose()`.
  ([vmware_rest/connector.py:309](../../backend/src/meho_backplane/connectors/vmware_rest/connector.py#L309); confirmed against [VMware vSphere Automation REST API Programming Guide 8.0](https://techdocs.broadcom.com/us/en/vmware-cis/vsphere/vsphere-sdks-tools/8-0/vmware-vsphere-automation-rest-programming-guide-8-0.html))
- **vRLI / NSX / VCF:** session-login POST → token, with single-retry-on-401
  around downstream calls; the shared round-trip is `vcf_session_login`
  ([_shared/vcf_auth.py:354](../../backend/src/meho_backplane/connectors/_shared/vcf_auth.py#L354)).
- **Credential caching:** `CredentialsCache` loads `{username, password}`
  once-per-target with a lock + missing-key error contract
  ([_shared/vcf_auth.py:212](../../backend/src/meho_backplane/connectors/_shared/vcf_auth.py#L212)).

**Expiry handling:** the connectors deliberately do *not* proactively refresh on
the ~5-min vSphere idle timeout; a 401 surfaces to the caller as a clean retry.
Explicit 401-driven re-login is deferred. The credential read happening
per-session-establish (not per-request) means an operator-context read only fires
on first use / re-login — important for the per-operator-JWT path (the operator's
JWT must be live at session-establish time).

### Auth-model taxonomy (only one mode wired)

`AuthModel` enum: `shared_service_account` / `per_user` / `impersonation`
([targets/schemas.py](../../backend/src/meho_backplane/targets/schemas.py),
`AuthModel` re-exported from `connectors/schemas.py`). Every REST connector's
`auth_headers` accepts only `shared_service_account` (or `None`) and raises
`NotImplementedError` naming the target + mode for the others
([vmware_rest/connector.py:271-277](../../backend/src/meho_backplane/connectors/vmware_rest/connector.py#L271)).

- **shared_service_account** — one vendor service account per target, stored in
  Vault, read by the backplane. The day-1 model; what this Goal wires.
- **per_user** — each operator has their own vendor credential. Needs a
  per-operator secret path (templated policy does the scoping) and a loader that
  keys on operator identity, not just target.
- **impersonation** — the backplane authenticates as a privileged account and
  asks the vendor to act *as* the operator (vendor-side impersonation; e.g.
  vCenter SSO delegation). Needs vendor-specific support and likely RFC 8693–style
  token exchange. Defer until a concrete consumer need exists (no speculative
  build).

---

## §5 — Comparable systems

How other "broker between operators and many backends" systems handle the
credential, who the downstream identity is, and how attribution is kept.

- **HashiCorp Boundary** — holds per-target credentials in *credential stores*
  (static, or a Vault store backed by dynamic secrets). Two modes: *brokering*
  returns the secret to the user; **credential injection** does not — "the user
  never sees the credential required to authenticate to the target." The
  **worker** (proxy) authenticates to the target on the user's behalf using the
  *target's* credential; the operator's Boundary identity gates the session and
  is what audit attributes. ([credential management](https://developer.hashicorp.com/boundary/docs/concepts/credential-management), [Vault store](https://developer.hashicorp.com/boundary/docs/vault), [auditing](https://developer.hashicorp.com/boundary/docs/concepts/auditing)) — **the closest 1:1 analogue to MEHO's narrow waist; "injection, never broker" is the default to copy.**
- **Teleport** — a central CA issues short-lived, **identity-bound** certs
  instead of holding shared target secrets; there is no reusable secret to see,
  "any connection and action can be traced back to a user or a service," and
  time-based expiry replaces revocation. ([architecture](https://goteleport.com/docs/reference/architecture/authentication/)) — pushes attribution all the way to the target; relevant only for backends that accept identity certs (not vCenter today).
- **Steampipe** — per-connection static creds, **in plaintext in `.spc` config**;
  no server-side broker; attribution is whatever the cloud account logs. ([connection config](https://steampipe.io/docs/reference/config-files/connection)) — the **anti-pattern**: it's exactly the embedded-credential shape (cf. bind9's dict `secret_ref`) that Vault-backed brokering should replace. The one reusable idea is its clean "one connection = one target scope" abstraction.
- **Terraform/Pulumi providers** — modern pattern is **ephemeral resources**
  (TF v1.10+): fetch a credential from Vault at apply time, feed it to another
  provider, and it is **not persisted to state/plan**. ([ephemeral resources](https://registry.terraform.io/providers/hashicorp/vault/latest/docs/guides/using_ephemeral_resources), [TF 1.10 ephemeral values](https://www.hashicorp.com/en/blog/terraform-1-10-improves-handling-secrets-in-state-with-ephemeral-values)) — the discipline "the secret is a transient in-memory value, never written to a durable artifact" maps directly onto MEHO's loader (never log it, never put it in `OperationResult`/audit).
- **Backstage** — plugins choose, **per call**, between forwarding the user
  principal (`auth.getPluginRequestToken({ onBehalfOf })`) and acting as their
  own service identity (`auth.getOwnServiceCredentials()`). ([service-to-service auth](https://backstage.io/docs/auth/service-to-service-auth/), [proxy modes](https://backstage.io/docs/plugins/proxying/)) — **"as operator vs as broker" should be a first-class, explicit choice, not an implicit default** (informs the auth-model axis).
- **SPIFFE/SPIRE** — workloads get short-lived auto-rotated **SVIDs** via the
  Workload API with **no bootstrap secret**: two-level attestation (node + workload)
  *is* the proof of identity. ([SPIFFE concepts](https://spiffe.io/docs/latest/spiffe-about/spiffe-concepts/), [SPIRE concepts](https://spiffe.io/docs/latest/spire-about/spire-concepts/)) — the principled answer to "secret zero" if MEHO ever needs the backplane to authenticate to Vault without a planted credential (attestation instead of AppRole `secret_id`).
- **API gateways (Kong / Apigee)** — config holds a **secret reference**
  (`vault://…` / encrypted KVM key), resolved + injected at request time with a
  TTL/refresh; the operator and the config repo never hold cleartext. ([Kong secrets mgmt](https://developer.konghq.com/gateway/secrets-management/), [Kong Vault refs](https://developer.konghq.com/gateway/entities/vault/), [Apigee KVM](https://docs.apigee.com/api-platform/cache/key-value-maps)) — validates MEHO's `target.secret_ref` = a Vault reference resolved at dispatch (not embedded value).

### Synthesis — patterns reusable for MEHO's Vault-backed waist

1. **Inject, never broker, by default** (Boundary): the dispatch tier reads the
   per-target credential from Vault and *uses* it server-side; the agent/operator
   never sees cleartext. Reserve returning a value for the explicit [#581](https://github.com/evoila/meho/issues/581) break-glass move.
2. **Treat the resolved secret as ephemeral state** (Terraform): never in logs,
   audit rows, result handles, or any durable artifact.
3. **Config holds references, not values** (Kong/Apigee): `target.secret_ref` is a
   Vault path resolved at dispatch — *not* an embedded dict (the bind9 / Steampipe
   shape to avoid).
4. **Make "as operator vs as broker" explicit** (Backstage): the auth-model on the
   target is a first-class choice, never an implicit default — which is exactly the
   `AuthModel` enum's job.
5. **Prefer short-lived/dynamic where the backend supports it** (Boundary/TF):
   keep the loader abstraction open to a dynamic-secret loader; don't hard-wire
   static KV as the only path.
6. **Solve secret-zero with attestation if a workload identity is ever needed**
   (SPIFFE/SPIRE) rather than planting an AppRole `secret_id`.
7. **Use RFC 8693 delegation (not impersonation) for OAuth-speaking vendors** if
   vendor-side per-operator identity is ever required — `subject_token` (operator)
   + `actor_token` (broker) keeps dual attribution. ([RFC 8693 §1.1](https://datatracker.ietf.org/doc/html/rfc8693#section-1.1))

---

## §6 — Audit + least privilege for credential USE

- **Audit the use, never the value.** MEHO's dispatcher already writes a
  synchronous, append-only audit row per op with `params_hash` (never raw
  params) and the resolved `target_id`
  ([dispatcher.py `_execute_and_audit`](../../backend/src/meho_backplane/operations/dispatcher.py#L329);
  `compute_params_hash` in `operations/_validate.py`). The credential value is
  never an op param, so it is never in the audit row.
- **Vault-side audit.** Vault audit devices HMAC-SHA256 all string
  request/response fields with a per-device salt, so secret values are not in
  plaintext in Vault's own logs; operators can still match a known value via
  `/sys/audit-hash`. Never disable HMAC in production.
  ([Audit devices](https://developer.hashicorp.com/vault/docs/audit))
- **No-secret-in-logs in MEHO.** `Operator.raw_jwt` is `Field(repr=False)` so it
  cannot leak via structlog's `repr()` of bound values
  ([auth/operator.py:88](../../backend/src/meho_backplane/auth/operator.py#L88));
  bind9's safe-sudo primitive streams the password via stdin, never argv/history/
  logs ([bind9/connector.py:266](../../backend/src/meho_backplane/connectors/bind9/connector.py#L266)).
  Any new loader MUST keep `{username,password}`/kubeconfig out of every log
  event and every `OperationResult` — the existing `CredentialsCache` logs only
  `target`/`host`, never the dict.
- **Least privilege.** Per-operator templated policy (§2) scopes each operator's
  Vault reads to their own target secrets through one role; the backplane never
  needs a god-mode Vault token under the operator-context model.
- **Rotation.** `CredentialsCache.invalidate(target)` already exists for
  post-rotation cache busting ([_shared/vcf_auth.py:280](../../backend/src/meho_backplane/connectors/_shared/vcf_auth.py#L280));
  a rotation event must invalidate the cached credential *and* drop the cached
  session token (the connectors hold both). Automatic rotation is out of scope for
  this Goal. ([Secret rotation](https://developer.hashicorp.com/vault/tutorials/db-credentials/database-creds-rotation))

---

## §7 — Testing without real secrets

- **Recorded fixtures (respx replay).** The vmware integration test already
  replays a captured vCenter exchange via respx instead of a live appliance —
  the pattern to extend: capture once against a lab vCenter, scrub secrets,
  replay in CI. ([test_connectors_vmware_rest_vcsim.py](../../backend/tests/integration/test_connectors_vmware_rest_vcsim.py))
- **Injected loader = secret-free unit/E2E gate.** All connector tests inject a
  fake loader (§1). Keep that: the production loader's *own* test exercises the
  Vault read against a **Vault dev-mode harness** (the rubric's State-2 bar),
  not a recorded fixture, so the live KV-v2 path is actually proven.
- **vcsim limits.** vcsim does not serve modern `/api`; it is usable only for
  legacy `/rest` paths and govmomi-backed inventory, not the REST `fingerprint`
  path. Do not gate the "real call" proof on vcsim. (§1)
- **Lab-vCenter smoke, opt-in.** A real end-to-end run belongs behind an
  env-gated marker (`MEHO_LAB_VCENTER=…`) so default CI needs no secret and the
  secret-free gate stays green; the consumer's lab vCenter is the target.
- **Contract tests.** Pin the loader's I/O contract (`secret_ref` → KV-v2 path →
  `{username,password}`) and the auth-model boundary (`per_user`/`impersonation`
  raise) as fast unit tests independent of any network.

---

## Sources

- HashiCorp Vault — [JWT/OIDC auth method](https://developer.hashicorp.com/vault/docs/auth/jwt)
- HashiCorp Vault — [ACL policy templating](https://developer.hashicorp.com/vault/docs/concepts/policies)
- HashiCorp Vault — [Static vs dynamic secrets](https://developer.hashicorp.com/vault/tutorials/get-started/understand-static-dynamic-secrets)
- HashiCorp Vault — [Manage dynamic credential leases](https://developer.hashicorp.com/vault/tutorials/db-credentials/manage-dynamic-leases) · [Tune lease TTL](https://developer.hashicorp.com/vault/docs/troubleshoot/tune-lease-ttl)
- HashiCorp Vault — [AppRole best practices](https://developer.hashicorp.com/vault/docs/auth/approle/approle-pattern) · [Response wrapping](https://developer.hashicorp.com/vault/docs/concepts/response-wrapping)
- HashiCorp Vault — [Audit devices](https://developer.hashicorp.com/vault/docs/audit)
- IETF — [RFC 8693 OAuth 2.0 Token Exchange](https://datatracker.ietf.org/doc/html/rfc8693)
- Broadcom/VMware — [vSphere Automation REST API Programming Guide 8.0](https://techdocs.broadcom.com/us/en/vmware-cis/vsphere/vsphere-sdks-tools/8-0/vmware-vsphere-automation-rest-programming-guide-8-0.html)
- (Comparable systems: Boundary, Teleport, Steampipe, Terraform/Pulumi, Backstage, SPIFFE/SPIRE, Kong/Apigee — see §5, sourced inline.)
