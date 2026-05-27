<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# VCF Private AI Foundation deployment — wiring MEHO to a PAIF endpoint

> Cross-repo handshake between `evoila/meho` (this repo, the
> backplane that **routes** agent runs to PAIF when a tenant policy
> resolves a tier to a PAIF backend) and the operator's VCF Private
> AI Foundation appliance + Identity Provider realm (consumer side;
> not a single repo — every air-gapped deployment has its own PAIF
> appliance and IdP).
>
> This page is the upstream-side **tracker** for the appliance- and
> IdP-side configuration each consumer must provision before
> registering the PAIF backend in a tenant policy. The configuration
> itself is operator-applied in the PAIF admin console, the IdP
> Admin Console (Keycloak / Okta / Authentik / Auth0), and the
> backplane's Helm values; what lives here is the recipe the operator
> follows and the verification commands either side can run to prove
> the contract holds.

## Why this doc exists

VCF Private AI Foundation (PAIF) is the
[Initiative #806](https://github.com/evoila/meho/issues/806) §C4-d
target: the **air-gapped enterprise** path where every agent run
stays inside the customer's cluster boundary. The agent runtime's
G11.5 resolver routes a tier to PAIF via the OpenAI-compat backend
seam (#1077) plus a bearer-token provider that exchanges OIDC
client credentials for a short-lived access token. The mechanics
are documented at
[`docs/codebase/agent-runtime.md`](../codebase/agent-runtime.md#vcf-private-ai-foundation-backend-g115-t4-1078);
**this** doc is the operator-facing recipe that walks through:

1. Confirming the PAIF appliance is reachable from the backplane pod.
2. Registering an OIDC client in the IdP realm PAIF trusts.
3. Wiring the four required env vars into the backplane's Helm
   values (or external-secret / sealed-secret pipeline).
4. Verifying the chain end-to-end with a smoke run.

The backplane cannot fail open on misconfigured PAIF settings —
`default_vcf_paif_backend_builder()` raises `AgentRunError` naming
every missing setting at first agent invocation. This doc is the
contract that specifies what the appliance + IdP must produce so the
backplane's fail-closed posture is not the diagnostic.

## Scope: what this doc does and does not cover

- **In scope:** the **wiring** between an already-deployed PAIF
  appliance, an already-running IdP realm, and the MEHO backplane.
  IdP client registration, env-var wiring, and verification.
- **Out of scope:** provisioning the PAIF appliance itself (VCF
  Private AI Foundation lifecycle is a VMware / Broadcom operator
  responsibility — see the
  [Broadcom deployment docs](https://techdocs.broadcom.com/us/en/vmware-cis/private-ai/foundation-with-nvidia/9-0/private-ai-foundation-9-x/what-is-private-ai-services/deploying-model-endpoints.html)).
  Standing up the IdP itself (Keycloak / Okta / Authentik) is also
  out of scope; this doc assumes the IdP that PAIF already trusts.
- **Not yet shipped:** an operator/admin CLI that provisions the
  OIDC client + writes the Helm values from one command. Initiative
  #806 §C4-d filed the appliance-side deployer as a **separate
  cross-repo workstream** (the Goal #800 architecture decision is to
  keep MEHO's surface to the **runtime** wiring; appliance lifecycle
  belongs in the operator's existing IaC). When that CLI lands, this
  doc grows a "managed path" section pointing at it.

## Prerequisites

- A reachable VCF Private AI Foundation appliance with **at least
  one chat-completion model endpoint deployed** (vLLM engine; the
  default for chat completions per Broadcom techdocs). Embedding
  endpoints (Infinity) and llama.cpp CPU-fallback endpoints are not
  used by the agent runtime today.
- The appliance host is reachable from the backplane pod's network
  namespace — verified by `kubectl exec -n meho deploy/backplane --
  curl -sk https://<paif-host>/api/v1/compatibility/openai/v1/models`
  (a 401 here is the **expected** outcome before this doc's IdP
  client lands; a connection refused / DNS failure means the
  prerequisite is not met).
- The IdP that PAIF trusts (configured at PAIF deploy time, surfaced
  via `https://<paif-host>/env.json` per Broadcom developer docs).
  Confirmed by the operator who deployed the appliance — this doc
  does not change which IdP PAIF talks to.
- IdP admin access sufficient to register a new OIDC client. The
  exact verb varies per IdP (Keycloak: realm-admin role on the
  realm; Okta: SuperAdmin on the org; Authentik: realm admin).
- The MEHO backplane chart already deploys against the cluster
  (chassis-era prerequisites met — see
  [`docs/cross-repo/rke2-infra-coordination.md`](./rke2-infra-coordination.md)).

## Step 1 — Verify PAIF's OpenAI-compat surface is reachable

```bash
# From inside the cluster (e.g. via kubectl exec into any pod with curl)
PAIF_HOST="https://pais.airgap.local"
curl -sk "${PAIF_HOST}/api/v1/compatibility/openai/v1/models" -i | head -20
```

**Expected outcome before Step 2:** HTTP `401 Unauthorized` (with a
`WWW-Authenticate: Bearer ...` header). PAIF is reachable and the
OpenAI-compat surface is mounted at the documented sub-path
(`/api/v1/compatibility/openai/v1/` — pinned as
`VCF_PAIF_OPENAI_COMPAT_BASE_PATH` in
[`backend/src/meho_backplane/agent/models.py`](../../backend/src/meho_backplane/agent/models.py)
so a future Broadcom-side change is a single-line edit there).

**Failure modes:**

- `curl: (6) Could not resolve host` → DNS not configured. Add a
  per-cluster `CoreDNS` entry or set up a hostAlias on the
  backplane deployment.
- `curl: (7) Failed to connect` → the cluster has no route to the
  PAIF VLAN. Operator's network team responsibility — outside the
  scope of this doc.
- `HTTP/2 404` against `…/models` → the appliance is reachable but
  the OpenAI-compat surface is not enabled. Confirm with the PAIF
  operator that the compat surface is mounted (Broadcom default is
  **on**; some deployments disable it for licensing reasons).

## Step 2 — Register an OIDC client for the backplane → PAIF integration

The backplane is a **service-to-service** caller (a daemon, not an
interactive user), so the right OAuth grant is `client_credentials`.
The PAIF developer docs name Authorization Code with PKCE as the
"preferred" grant — that's correct **for interactive clients**;
the `client_credentials` grant is the daemon equivalent and is
supported by every IdP PAIF integrates with (Keycloak, Okta,
Authentik, Auth0, Azure AD all support both grants on the same
realm).

### Keycloak

In the Keycloak Admin Console for the realm PAIF trusts:

1. **Clients → Create client.**
   - **Client type:** `OpenID Connect`.
   - **Client ID:** `meho-backplane-paif` (suggested; matches the
     `VCF_PAIF_OIDC_CLIENT_ID` value the backplane reads).
   - **Always display in console:** off (this is a confidential
     service client, not a user-facing app).
2. **Capability config:**
   - **Client authentication:** **on** (this is a confidential
     client; the backplane sends a `client_secret` on every token
     request).
   - **Authorization:** off (the backplane doesn't run the
     authorization-services UMA flow).
   - **Authentication flow:** disable everything **except**
     **Service accounts roles** (this is the Keycloak label for the
     `client_credentials` grant).
3. **Save**, then in the **Credentials** tab, copy the **Client
   Secret**. This is the `VCF_PAIF_OIDC_CLIENT_SECRET` value the
   backplane reads — handle it via Vault / external-secret /
   sealed-secret, not a literal in the Helm values file.
4. **(Optional) Service accounts roles tab:** assign any
   realm-level roles PAIF's authorization layer expects. The default
   is no extra role — the bare access token suffices for
   `/api/v1/compatibility/openai/v1/chat/completions` on most PAIF
   deployments. Confirm with the PAIF operator if your appliance
   enforces fine-grained scopes.

The token endpoint URL the backplane POSTs to is:

```
https://<keycloak-host>/realms/<realm>/protocol/openid-connect/token
```

This is the `VCF_PAIF_OIDC_TOKEN_URL` value.

### Okta / Authentik / Auth0 / Azure AD

The pattern is the same — a confidential client with the
`client_credentials` grant enabled — but the console paths differ:

- **Okta:** Applications → Create App Integration → API Services →
  copy the Client ID + Secret + token endpoint
  (`https://<org>.okta.com/oauth2/default/v1/token`).
- **Authentik:** Applications → Providers → Create OAuth2/OpenID
  Provider → set Client type "Confidential" → grant type "Client
  Credentials" → token endpoint at
  `https://<authentik>/application/o/token/`.
- **Auth0:** Applications → APIs → Machine to Machine → Authorize a
  new M2M client → grant `client_credentials` → token endpoint at
  `https://<tenant>.auth0.com/oauth/token`.
- **Azure AD:** App registrations → Register an application → API
  permissions → Grant admin consent → Client credentials secret →
  token endpoint at
  `https://login.microsoftonline.com/<tenant-id>/oauth2/v2.0/token`.

In every case, the four values the backplane needs are: the
**token endpoint URL**, the **client id**, the **client secret**,
and (optionally) the **scope** the IdP expects.

## Step 3 — Wire the env vars into the backplane

The backplane reads six env vars to construct the PAIF backend (see
[`backend/src/meho_backplane/settings.py`](../../backend/src/meho_backplane/settings.py)
field declarations + the loader at the bottom of the same file):

| Env var                          | Required? | Example                                                                       | Source           |
|----------------------------------|-----------|-------------------------------------------------------------------------------|------------------|
| `VCF_PAIF_BASE_URL`              | yes       | `https://pais.airgap.local/api/v1/compatibility/openai/v1/`                   | PAIF operator    |
| `VCF_PAIF_MODEL`                 | optional  | `openai:meta-llama/Llama-3.1-8B-Instruct` (default)                           | PAIF model id    |
| `VCF_PAIF_OIDC_TOKEN_URL`        | yes       | `https://kc.airgap.local/realms/meho/protocol/openid-connect/token`           | IdP token URL    |
| `VCF_PAIF_OIDC_CLIENT_ID`        | yes       | `meho-backplane-paif`                                                         | IdP client id    |
| `VCF_PAIF_OIDC_CLIENT_SECRET`    | yes       | *(via Vault / external-secret / sealed-secret — **never** in plain Helm values)* | IdP client secret |
| `VCF_PAIF_OIDC_SCOPE`            | optional  | `paif`                                                                        | IdP scope        |

The base URL **must include** the compat sub-path
(`/api/v1/compatibility/openai/v1/`). The backplane does not
auto-append it — pointing the generic `OPENAI_BASE_URL` setting at a
PAIF host without the sub-path is a config error the operator
fixes by switching to the PAIF env vars above.

### Helm values pattern (with external-secret for the client secret)

```yaml
# values.airgap.yaml — air-gapped tenant overlay
backplane:
  env:
    - name: VCF_PAIF_BASE_URL
      value: "https://pais.airgap.local/api/v1/compatibility/openai/v1/"
    - name: VCF_PAIF_OIDC_TOKEN_URL
      value: "https://kc.airgap.local/realms/meho/protocol/openid-connect/token"
    - name: VCF_PAIF_OIDC_CLIENT_ID
      value: "meho-backplane-paif"
    - name: VCF_PAIF_OIDC_CLIENT_SECRET
      valueFrom:
        secretKeyRef:
          name: meho-backplane-paif-secret
          key: client_secret
    # Optional:
    # - name: VCF_PAIF_OIDC_SCOPE
    #   value: "paif"
    # - name: VCF_PAIF_MODEL
    #   value: "openai:meta-llama/Llama-3.1-8B-Instruct"
```

The `meho-backplane-paif-secret` Kubernetes Secret is the contract
between the operator's secret pipeline (Vault / external-secrets /
sealed-secrets) and this chart. The chart itself does not provision
the Secret — the operator does, via whichever pipeline they already
use for `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`.

### Why no `--all-knobs` admin-CLI

The backplane is deliberately a *runtime* consumer of these
settings — appliance lifecycle and IdP client registration are
operator-side workstreams owned by the operator's existing IaC
(Terraform / Pulumi / hand-rolled Ansible). A future `meho deploy
paif` CLI verb that wraps Steps 1–3 is filed against Initiative
#806's cross-repo workstream, not this task. The trade-off is
deliberate: the MEHO backplane's PAIF support stays a 6-env-var
configuration surface, not a moving-target operator CLI.

## Step 4 — Register the PAIF backend in tenant policy

Once the env vars are wired and the backplane pod has rolled,
tenant policy resolves a tier to PAIF by:

1. Building a `BackendBuilder` via
   [`default_vcf_paif_backend_builder`](../../backend/src/meho_backplane/agent/models.py)
   (single-PAIF-endpoint deploys) or
   [`vcf_paif_backend_builder`](../../backend/src/meho_backplane/agent/models.py)
   directly (multi-PAIF deploys, with per-tenant `base_url` /
   `bearer_token_provider`).
2. Registering the builder in the resolver's `backends` mapping
   with **`is_saas_egress=False`** (PAIF is on-prem by
   definition — the air-gapped tenant's `allow_egress=False`
   policy then resolves to it without tripping
   `EgressViolationError`).
3. Wiring the tenant's `TenantModelPolicy.tiers` to map every
   tier to the PAIF backend id.

The persistence wiring for tenant policies (loading from the
database, hot-reloading on policy changes) is deferred to a
follow-up task — see the `TODO(G11.5-T2)` markers in
[`backend/src/meho_backplane/agent/models.py`](../../backend/src/meho_backplane/agent/models.py)
and
[`backend/src/meho_backplane/agent/run.py`](../../backend/src/meho_backplane/agent/run.py).
For now, the resolver is constructed at app boot from in-process
defaults; tenant operators wanting PAIF routing today extend the
`build_resolver(...)` call site directly.

## Step 5 — Verify end-to-end

### 5.1 — IdP token acquisition

From inside the backplane pod (or any pod with `curl` and a route
to the IdP):

```bash
curl -sk -X POST "${VCF_PAIF_OIDC_TOKEN_URL}" \
  -d "grant_type=client_credentials" \
  -d "client_id=${VCF_PAIF_OIDC_CLIENT_ID}" \
  -d "client_secret=${VCF_PAIF_OIDC_CLIENT_SECRET}" \
  ${VCF_PAIF_OIDC_SCOPE:+-d "scope=${VCF_PAIF_OIDC_SCOPE}"} \
  | jq '{access_token: (.access_token | .[0:16] + "..."), expires_in, token_type}'
```

**Expected:** a JSON body with `access_token` (truncated for
safety), `expires_in` (typically 300–3600), `token_type: "Bearer"`.

**Failure modes:**

- HTTP 401 `{"error":"invalid_client"}` — the client id or
  secret is wrong. Re-check Step 2.
- HTTP 400 `{"error":"unsupported_grant_type"}` — the IdP client
  was registered without `client_credentials` enabled. Re-check
  Step 2's "Service accounts roles" / "Client Credentials" toggle.
- HTTP 400 `{"error":"invalid_scope"}` — the IdP rejected the
  `scope` parameter (some IdPs strictly enforce a registered
  scope list). Either register the scope IdP-side or omit
  `VCF_PAIF_OIDC_SCOPE` from the Helm values.

### 5.2 — PAIF inference with the acquired token

Reusing the token from 5.1 (export as `$TOKEN`):

```bash
curl -sk "${VCF_PAIF_BASE_URL}models" \
  -H "Authorization: Bearer ${TOKEN}" | jq '.data[] | .id'
```

**Expected:** a JSON list of model ids the appliance hosts (e.g.
`"meta-llama/Llama-3.1-8B-Instruct"`). Confirms PAIF accepts the
IdP-issued bearer.

### 5.3 — Agent run end-to-end

The backplane integration test
`backend/tests/test_agent_vcf_paif_backend.py::test_resolver_routes_air_gapped_tenant_to_paif`
proves the full chain (policy → resolver → backend → token
acquisition) end-to-end with respx-mocked HTTP — no SaaS host is
reachable from the test environment. For a live test against the
real PAIF appliance, the operator runs the same scenario from a
short-lived in-cluster job or `kubectl exec`:

```bash
# Inside the backplane pod
uv run python - <<'PY'
import asyncio
from meho_backplane.agent import default_vcf_paif_backend_builder
model = default_vcf_paif_backend_builder()
print("PAIF model id:", model.model_name)
print("PAIF base URL:", model.client.base_url)
PY
```

**Expected:** prints the configured model id and a base URL
containing `/api/v1/compatibility/openai/v1/`. Proves the settings
load, the OIDC provider builds (no eager token fetch yet — the
openai SDK lazy-resolves the bearer on first request), and the
model carries the PAIF profile.

## Egress posture verification (air-gapped contract)

The whole point of PAIF is **no SaaS egress**. The verification
that the backplane never falls back to a SaaS LLM in the air-gapped
posture is a property of the resolver's `is_saas_egress` flag, not
of this doc — but operators can confirm at the network layer with:

```bash
# A NetworkPolicy that denies egress to the public Internet except
# the in-cluster IdP + PAIF.
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: meho-backplane-airgap
spec:
  podSelector:
    matchLabels: { app.kubernetes.io/name: meho-backplane }
  policyTypes: ["Egress"]
  egress:
    - to:
        - namespaceSelector:
            matchLabels: { name: kube-system }   # CoreDNS
        - ipBlock: { cidr: "10.0.0.0/8" }         # in-cluster only
    # No 0.0.0.0/0 — SaaS egress is denied at the kube layer too.
```

If the backplane ever tries to reach `api.anthropic.com` or
`api.openai.com` from an air-gapped tenant, the connection blocks
at the NetworkPolicy and the agent loop surfaces a typed error.
This is **belt-and-suspenders** alongside the resolver's egress
check — the resolver enforces the policy at config time, the
NetworkPolicy catches a misconfigured deploy at runtime.

## Rollback

If the PAIF deploy needs to be unwound (the appliance is moved,
the IdP realm is reconfigured, the operator wants to switch back
to OpenAI-compat directly, …):

1. **Remove PAIF from tenant policy.** Re-deploy with the affected
   tenant's policy routing tiers back to the previous backend
   (Anthropic, generic OpenAI-compat). The PAIF env vars can stay
   wired — they only take effect when a policy registers the
   backend.
2. **Roll the backplane.** The new policy takes effect on pod
   restart.
3. **Revoke the IdP client (optional).** If the OIDC client is no
   longer needed, disable it in the IdP Admin Console. Existing
   in-flight access tokens expire on their normal `expires_in`
   schedule; no force-revocation is possible from the backplane
   side (which is the right OIDC posture — token revocation is the
   IdP's responsibility).
4. **Optionally:** delete the `meho-backplane-paif-secret`
   Kubernetes Secret to prevent the env-var lookup from finding
   stale values on the next chart upgrade.

## References

- Initiative [#806](https://github.com/evoila/meho/issues/806) §C4-d
  (this task: VCF PAIF backend + cross-repo deployer doc).
- Task [#1078](https://github.com/evoila/meho/issues/1078)
  (G11.5-T4 — the backend + this doc).
- Sibling tasks: [#1075](https://github.com/evoila/meho/issues/1075)
  (resolver), [#1077](https://github.com/evoila/meho/issues/1077)
  (OpenAI-compat backend the PAIF backend builds on).
- Code grounding:
  [`backend/src/meho_backplane/agent/models.py`](../../backend/src/meho_backplane/agent/models.py)
  — `vcf_paif_backend_builder`, `vcf_paif_bearer_provider`,
  `OidcClientCredentialsTokenProvider`, `TokenAcquisitionError`,
  `VCF_PAIF_OPENAI_COMPAT_BASE_PATH`.
- Architecture grounding:
  [`docs/codebase/agent-runtime.md`](../codebase/agent-runtime.md#vcf-private-ai-foundation-backend-g115-t4-1078)
  — VCF PAIF section.
- VMware / Broadcom docs:
  [Private AI Services API](https://developer.broadcom.com/xapis/vmware-private-ai-service-api/latest/) —
  the OpenAI compatibility surface, base path, OIDC bearer
  requirement.
  [Deploying model endpoints](https://techdocs.broadcom.com/us/en/vmware-cis/private-ai/foundation-with-nvidia/9-0/private-ai-foundation-9-x/what-is-private-ai-services/deploying-model-endpoints.html) —
  the engine set (vLLM / Infinity / llama.cpp).
- OAuth 2.0 `client_credentials` grant:
  [RFC 6749 §4.4](https://datatracker.ietf.org/doc/html/rfc6749#section-4.4).
