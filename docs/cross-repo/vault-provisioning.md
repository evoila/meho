<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Vault provisioning — consumer-side requirements

> Producer-side spec for what an operator's Vault deployment must
> provide before the MEHO backplane can run its federation chain
> against it. The actual provisioning lives on the consumer side
> ([`evoila-bosnia/claude-rdc-hetzner-dc`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc)
> in the dogfood case); this doc is the contract the consumer reads
> to know what to build, and the verification commands either side
> can run to prove the handshake works.

## Why this lives in `evoila/meho`

The federation chain is wired in `backend/src/meho_backplane/auth/vault.py`
and exercised on every authenticated request to `/api/v1/health`.
When the chassis changes the shape of "what Vault must accept" — a
new audience, a new mount path, a new KV layout — this document
changes in lock-step with the code; the consumer's provisioning
runbook doesn't need to know about chassis-side renames as long as
the contract on this page holds.

## What the backplane needs

Six distinct Vault surfaces. The first four ship via Goal #11's
cross-repo deps (consumer commitment #5 — see
[`#261`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/issues/261)
in the consumer repo). The fifth — the federation-proof test KV
path — is the surface most easily missed during provisioning. The
sixth — the **scheduler service token** — is a *separate* static-token
identity (not the JWT-login role) that the scheduler uses to read and
**write** agent `client_credentials` secrets; its write capability is
the one most easily under-provisioned (see surface 6).

### 1. JWT auth method — on a **dedicated mount**

The backplane authenticates with Vault's **JWT auth method**
(`auth.jwt.jwt_login` → `POST /auth/<mount>/login`; see
[`backend/src/meho_backplane/auth/vault.py`](../../backend/src/meho_backplane/auth/vault.py)),
forwarding the operator/service JWT for JWKS validation. It does
**not** use the interactive `vault login -method=oidc` flow. Mount the
JWT method at a **dedicated path** — `auth/jwt` matches the
backplane's default `VAULT_OIDC_MOUNT_PATH` (`jwt`):

```bash
vault auth enable -path=jwt jwt
vault write auth/jwt/config \
  oidc_discovery_url=https://<keycloak-host>/realms/<realm> \
  oidc_discovery_ca_pem=@<ca.pem> \
  default_role=meho-mcp
```

`oidc_discovery_ca_pem` is only needed when Keycloak presents an
internal-CA cert. Any mount path works — set `VAULT_OIDC_MOUNT_PATH`
to match (e.g. `-path=jwt-meho` ↔ `VAULT_OIDC_MOUNT_PATH=jwt-meho`).
The discovery URL must match `KEYCLOAK_ISSUER_URL` in the backplane's
ConfigMap exactly. Trailing slashes are normalised on the producer
side.

> **Do not put this on an `auth/oidc/` mount.** Once a mount is in
> OIDC-login mode (`oidc_discovery_url` + `oidc_client_id` set), Vault
> (confirmed on 1.21.2) rejects a `role_type=jwt` role on that same
> mount — login fails with `error configuring token validator:
> unsupported config type`, and setting both `oidc_discovery_url` and
> `jwks_url` on one config is rejected too. It also collides with
> operators who want the interactive `vault login -method=oidc` path
> on `auth/oidc/`: a single mount cannot serve both an OIDC-login role
> and the backplane's `role_type=jwt` role. A dedicated `jwt`-type
> mount serves `role_type=jwt` with `oidc_discovery_url` for JWKS with
> no conflict. (evoila/meho#553; consumer-side
> [`evoila-bosnia/claude-rdc-hetzner-dc#524`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/issues/524).)

### 2. Role `meho-mcp`

Bound to the Keycloak realm and to the backplane's audience, on the
dedicated `jwt` mount from surface 1. Default role name `meho-mcp`
per [`backend/src/meho_backplane/settings.py`](../../backend/src/meho_backplane/settings.py)'s
`VAULT_OIDC_ROLE`; configurable via env var if a different name fits
the operator's Vault conventions.

```bash
vault write auth/jwt/role/meho-mcp \
  role_type=jwt \
  user_claim=sub \
  bound_audiences=<keycloak-audience> \
  policies=meho-mcp \
  token_ttl=1h
```

The `bound_audiences` value must match `KEYCLOAK_AUDIENCE` in the
backplane's ConfigMap. (Substitute `jwt` with your chosen mount path
if you didn't use the default.)

### 3. Policy `meho-mcp`

Grants the role the read paths the backplane exercises. v0.1 needs
**read** on the entire `secret/meho/*` subtree because operators
delegate per-secret authorisation to their Keycloak group
membership (groups → Vault external-group bindings → richer
policies) rather than per-path Vault policy:

```hcl
path "secret/data/meho/*" {
  capabilities = ["read"]
}
path "secret/metadata/meho/*" {
  capabilities = ["read"]
}
```

Both paths are required: KV v2 splits the secret value (`/data/`)
from its metadata (`/metadata/`); the backplane reads `/data/`
through hvac's `secrets.kv.v2.read_secret_version`, and the read
also touches `/metadata/` to honour `raise_on_deleted_version`.

### 4. KV v2 mount at `secret/`

The default Vault convention. Configurable via `vault.paths.kv` in
the chart (default `secret/meho`); the backplane reads under that
prefix.

```bash
vault secrets enable -path=secret -version=2 kv  # if not already
```

Existing labs that already have `secret/` mounted as KV v2 (the
default in fresh Vault installs) don't need to do this; the policy
above will Just Work.

### 5. Federation-proof test KV path `secret/meho/test/federation`

**This is the surface most easily missed.** The producer-side
`/api/v1/health` handler reads `secret/meho/test/federation` on
every authenticated call to prove the federation chain works
end-to-end (Keycloak JWT → Vault OIDC login → KV read). The
producer-side path is hard-coded at
[`backend/src/meho_backplane/api/v1/health.py:68`](../../backend/src/meho_backplane/api/v1/health.py#L68)
and intentionally not configurable in v0.1 — per-route customisation
of which secret to read lands with the first connector post-Goal-2.

The data at this exact path **must exist** when smoke leg #4
("Vault federation works") is run, otherwise the read fails with
`vault.read_ok=false` + `detail="read_failed: InvalidPath"` and the
smoke verifier exits 1 even though the entire rest of the
federation chain (auth, login, role binding, policy) is healthy.
The diagnostic is misleading because the failure looks like a chain
break when it's actually a missing fixture.

Provisioning command (one-time, value content immaterial):

```bash
vault kv put secret/meho/test/federation \
  purpose="MEHO v0.1 federation-proof fixture" \
  note="Read by /api/v1/health; absence breaks smoke.sh leg #4"
```

The value's *content* is never read by the backplane — only the
presence of the path is asserted (via hvac's KV v2 metadata
read returning a non-error response with a `version` field). Any
non-empty key set works; the two keys above are a self-documenting
default the lab admin can grep for during incident response.

### 6. Scheduler service token — read **and write** on agent credentials

The scheduler is operator-less (no Keycloak JWT to forward to Vault's
JWT auth method), so it authenticates with a **static service token**
passed via `VAULT_SCHEDULER_TOKEN`
([`backend/src/meho_backplane/settings.py`](../../backend/src/meho_backplane/settings.py)).
This is a *distinct* identity from the `meho-mcp` JWT-login role
(surfaces 1–3): it is bound to its own narrow policy on the
agent-credentials KV path
(`scheduler_agent_vault_path_pattern`, default
`secret/data/agents/*/credentials`).

The capability scope must be **read + write**, not read-only:

- **Read** — the scheduler resolves an agent's `client_credentials`
  secret Vault-first before firing a scheduled invocation
  ([`read_agent_secret`](../../backend/src/meho_backplane/scheduler/vault_credentials.py)).
- **Write** (`create` + `update`) — agent-principal **registration**
  mints the Keycloak-generated client secret into Vault under the same
  token
  ([`write_agent_secret`](../../backend/src/meho_backplane/scheduler/vault_credentials.py),
  called from
  [`AgentPrincipalService.register`](../../backend/src/meho_backplane/auth/agent_principals.py)).
  A read-only token makes Vault answer the write with a 403, which the
  backplane surfaces as a `502 scheduler_vault_write_error` (REST/UI) or
  a JSON-RPC `-32602` "scheduler Vault write failed" (MCP
  `meho_agent_principals_register`); registration then rolls back the
  just-created Keycloak client so no unschedulable agent is left behind.

A **dead token** — revoked, expired, or one whose periodic lease was
lost — produces the *same* Vault 403 on that write, so the policy scope
is not the only thing to check. Since evoila/meho#2652 the broker
disambiguates the two before answering: when Vault answers the write
with a **403** it fires `auth/token/lookup-self` on the same token
(Vault answers that endpoint for a live token whose policy set grants
`read` on it — the built-in `default` policy does, and the mint command
below attaches it — and 403s for an invalid one) and stamps the outcome
on the raised error. **Keep that capability reachable:** a live token
minted with `-no-default-policy` and no explicit
`auth/token/lookup-self` grant also 403s the probe, so it would be
reported dead when the real fault is the policy. The `meho-scheduler`
policy below grants it explicitly rather than relying on `default`.
Only a 403 on the probe condemns the token — any other Vault
status (503 sealed, 502 upstream, 500, 429 standby) says nothing about
it, so an outage keeps the policy-scope wording rather than ordering a
needless re-mint. A dead token then
surfaces as `scheduler_vault_token_invalid: the scheduler Vault
token is invalid or expired …` on every surface — REST detail, MCP
`-32602` message, and the `/ui/agents/principals` register banner —
naming the **re-mint**, not the policy. A live token keeps the
policy-scope wording above unchanged. The probe is diagnosis only: the
write is never retried. See the two rows in *Failure modes* below.

```hcl
# Policy: meho-scheduler
# KV v2 splits the secret value (/data/) from its metadata (/metadata/).
# The write path mints + updates the value; the metadata read is needed
# for create_or_update_secret's check-and-set bookkeeping.
path "secret/data/agents/*/credentials" {
  capabilities = ["create", "read", "update"]
}
path "secret/metadata/agents/*/credentials" {
  capabilities = ["read"]
}
# The dead-token probe (evoila/meho#2652) reads lookup-self after a 403
# on the write. The built-in `default` policy already grants this, but
# granting it explicitly keeps the probe sound for a token minted with
# `-no-default-policy` — without it a live token 403s the probe and gets
# reported dead.
path "auth/token/lookup-self" {
  capabilities = ["read"]
}
```

Mint the token bound to that policy and feed it to the backplane Pod as
`VAULT_SCHEDULER_TOKEN` (e.g. a long-lived periodic token, or a token a
Vault Agent sidecar renews into the env var):

```bash
vault policy write meho-scheduler meho-scheduler.hcl
vault token create -policy=meho-scheduler -period=768h -field=token
```

Substitute the path prefix if you set a non-default
`SCHEDULER_AGENT_VAULT_PATH_PATTERN`. Unset `VAULT_SCHEDULER_TOKEN`
disables the Vault path entirely — the scheduler then falls back to the
`SCHEDULER_AGENT_SECRET_ENV_PATTERN` env-var convention (an *unset*
token is not an error; an *under-scoped* one is).

#### Token renewal — the periodic-token fuse (#2328)

The `-period=768h` above mints a **periodic** token. A Vault periodic
token expires `period` after its **last renewal** — so a token that is
never renewed carries a built-in ~32-day fuse: when it blows, every
Vault-first credential read returns 403 and the scheduler silently skips
every fire.

Since #2328 the backplane renews the token itself: it fires a
best-effort `auth/token/renew-self` after every successful agent-secret
read/write, so at scheduler-tick frequency a periodic token with any
sane `period` never expires while the pod runs. It also runs
`auth/token/lookup-self` at startup and hourly and logs a dead or
unreachable token loudly (`scheduler_vault_token_dead` /
`scheduler_vault_token_unreachable`); the healthy path logs
`scheduler_vault_token_verified` with the token's `ttl` / `expire_time`.
Watch `expire_time` advance across renewals to confirm the loop is
alive:

```bash
vault token lookup -accessor "$ACCESSOR"   # expire_time should advance
```

For this to work the policy must allow the token to renew itself. The
default `token/renew-self` capability is granted to every token, so no
policy change is required; a policy that explicitly `deny`s
`auth/token/renew-self` breaks renewal (the WARN
`scheduler_vault_token_renew_failed` surfaces it).

**Re-mint without a pod restart.** A token fed via the
`VAULT_SCHEDULER_TOKEN` env var is frozen for the pod's lifetime — a
re-mint requires patching the Secret and restarting the pod. To let a
Vault Agent sidecar (or an operator) re-mint the token live, point
`VAULT_SCHEDULER_TOKEN_FILE` at the sidecar's token sink file instead:
the scheduler re-reads that file on **every** use, so a rewritten token
is picked up without a restart. When both are set the file wins; an
unreadable/empty file falls through to `VAULT_SCHEDULER_TOKEN`.

**When the fuse has already blown.** Renewal and the startup/hourly
self-lookup shorten time-to-notice, but they do not resurrect a token
that is already dead — and until #2652 the *registration* path did not
say so: a dead token and an under-scoped policy both produced the
policy-widening message, sending operators to verify a policy that was
already correct. Registration now runs the same `auth/token/lookup-self`
probe whenever Vault answers the write with a 403 and emits
`scheduler_vault_token_invalid` with the re-mint remediation instead.
The two rows in *Failure modes* below are the decision table.

### 7. Bounding the check-runner principal (#2642)

Only relevant if you set the chart's `checkRunner.*` block (the in-process
sensor check-runner's service principal) on a `credentialBackend: vault`
install. **Do this first — enabling `checkRunner.*` without it widens what
background dispatch can read.**

Role `meho-mcp` above is deliberately loose: `user_claim=sub` with
`bound_audiences` as its *only* binding and no `bound_subject` /
`bound_claims`, paired with a policy that grants read on the whole
`secret/data/meho/*` subtree. Any Keycloak principal whose token carries
`aud=<keycloak-audience>` is therefore accepted by it, including a
confidential service-account client. `check_runner_jwt()` mints the
runner's token with `audience=KEYCLOAK_AUDIENCE`
(`backend/src/meho_backplane/auth/runner_identity.py`), and the realm
recipe in [`docs/deploying.md`](../deploying.md) tells you to give the
client the matching audience mapper. Net effect of turning the flag on
with the role as provisioned: Vault accepts the runner principal as-is,
and every scheduled evaluation runs with read on **all** target
credentials under `secret/meho/*`. That silently removes the
"system-initiated calls cannot perform an operator-context Vault read"
carve-out the rest of the credential layer is built around.

Pick one of two guardrails before enabling the flag.

**Option A (preferred) — a distinct audience and a dedicated role.** Give
the runner client its own audience mapper (e.g. `meho-check-runner`)
**instead of** the backplane audience mapper the realm recipe asks for, and
provision a separate role + policy scoped to the secrets Sensors actually
evaluate. *Instead of*, not *in addition to*: Keycloak's Audience protocol
mapper calls `token.addAudience(...)`, so mappers accumulate — a runner
client carrying both mappers still emits `aud` containing
`<keycloak-audience>`, and `meho-mcp` keeps accepting it because
`bound_audiences` matches if *any* audience in the token matches.

```bash
vault policy write meho-check-runner - <<'EOF'
path "secret/data/meho/sensors/*" {
  capabilities = ["read"]
}
path "secret/metadata/meho/sensors/*" {
  capabilities = ["read"]
}
EOF

vault write auth/jwt/role/meho-check-runner \
  role_type=jwt \
  user_claim=sub \
  bound_audiences=meho-check-runner \
  bound_subject=<runner-client-service-account-sub> \
  policies=meho-check-runner \
  token_ttl=1h
```

Substitute the subtree your Sensors' `secret_ref`s actually live under.
Note the backplane resolves one role name from `VAULT_OIDC_ROLE` for every
JWT login, so Option A currently requires either a per-deployment split or
that you point `VAULT_OIDC_ROLE` at the narrower role and widen it back for
the interactive path — until MEHO grows a per-principal role setting,
Option B is the operationally simpler answer on a single-role install.

**Option B — tighten `meho-mcp` so it does not accept the runner.** Vault's
`bound_*` parameters are allowlists and have no negation, so "reject the
runner" has to be written as "require a claim value only an operator token
carries". The runner's token then fails role validation and cannot resolve
target credentials at all — i.e. today's carve-out is kept, and credentialed
Sensors stay `unknown` on Vault.

**`preferred_username` with a `*` glob is not that restriction.** Under
`bound_claims_type=glob` Vault reads bound values as globs "with `*`
matching any number of characters"
([JWT auth API][vault-jwt-api]), so `{"preferred_username":"*"}` is
satisfied by any value that is present at all. Keycloak's
`client_credentials` grant issues the token as the client's own
service-account user, whose username is `service-account-` + the client id
(`ServiceAccountConstants.SERVICE_ACCOUNT_USER_PREFIX`, applied by
`ClientManager` when service accounts are enabled), so the runner's token
carries an ordinary non-empty `preferred_username` and matches the glob.
Applying that recipe overwrites the live role and leaves it accepting
exactly what it accepted before.

Bind on a value the runner's service account does not have. The portable
choice is a dedicated realm role, reached through a JSON pointer because
Keycloak nests realm roles under `realm_access.roles`:

```bash
# Realm side, once. Grant the role to the humans (or the group) that
# legitimately read Vault through MEHO. Do NOT grant it to the check-runner
# client's service-account user — service accounts carry realm_access.roles
# too (default-roles-<realm>, offline_access, …), so the claim's *presence*
# proves nothing; only this specific value does.
kcadm.sh create roles -r <realm> -s name=meho-operator

vault write auth/jwt/role/meho-mcp \
  role_type=jwt \
  user_claim=sub \
  bound_audiences=<keycloak-audience> \
  bound_claims='{"/realm_access/roles":"meho-operator"}' \
  policies=meho-mcp \
  token_ttl=1h
```

Note what is deliberately **absent**: no `bound_claims_type`. Its default is
`string`, under which bound values "will be treated as literals and must
match exactly" ([JWT auth API][vault-jwt-api]). Vault normalises both the
bound value and the claim to lists and accepts when any bound value equals
any claim value, so against Keycloak's `realm_access.roles` **array** this
reads as "the token's realm roles must contain exactly `meho-operator`".

If every operator reaches the backplane through a known set of Keycloak
clients, an exact `azp` allowlist is equivalent and needs no new role —
`azp` is "the OAuth client the token was issued for"
(`JsonWebToken.issuedFor`), so it is `CHECK_RUNNER_CLIENT_ID` on the
runner's token and the operator-facing client id on an operator's:

```bash
  bound_claims='{"azp":["<operator-facing-client-id>","<cli-client-id>"]}'
```

Either binding gates **every** `meho-mcp` login, not only the runner's —
including the `/api/v1/health` federation proof, which dispatches
`vault.kv.read` under the calling operator's own JWT. An operator without
the role (or arriving through an unlisted client) gets
`login_failed: VaultRoleDeniedError`, the same way a mismatched
`bound_audiences` presents.

Verify against real tokens before relying on it. Read what the runner
actually carries first — a binding on a claim the runner happens to satisfy
is the failure mode this section exists to prevent:

```bash
# 1. The runner token's claims. Expect preferred_username =
#    "service-account-<runner-client-id>", azp = the runner client id, and
#    realm_access.roles WITHOUT meho-operator.
python3 - "$RUNNER_TOKEN" <<'PY'
import base64, json, sys
payload = sys.argv[1].split(".")[1]
payload += "=" * (-len(payload) % 4)
claims = json.loads(base64.urlsafe_b64decode(payload))
print(json.dumps({k: claims.get(k) for k in ("azp", "preferred_username", "realm_access")}, indent=2))
PY

# 2. What Vault does with it. Option B is in force when this errors with
#    `claim "/realm_access/roles" does not match any associated bound claim
#    values` rather than returning a token. A token here means the binding
#    is not constraining — go back to step 1.
vault write auth/jwt/login role=meho-mcp jwt="$RUNNER_TOKEN"

# 3. An operator token must still succeed, or you have locked out the
#    interactive path and /api/v1/health with it.
vault write auth/jwt/login role=meho-mcp jwt="$OPERATOR_TOKEN"
```

[vault-jwt-api]: https://developer.hashicorp.com/vault/api-docs/auth/jwt

## Verification

Run from any host with the operator's Vault token (`vault login`
against the operator's Keycloak identity, OR a service-account
JWT bound to the same role).

```bash
# 1. JWT auth method exists with correct discovery URL
#    (auth/jwt/ — the dedicated mount, NOT auth/oidc/)
vault read -format=json auth/jwt/config \
  | jq '{oidc_discovery_url, default_role}'

# 2. Role exists with correct audience binding
vault read -format=json auth/jwt/role/meho-mcp \
  | jq '{bound_audiences, user_claim, role_type, policies}'

# 3. Policy grants meho/* read
vault policy read meho-mcp

# 4. KV mount is v2 at the expected prefix
vault secrets list -format=json \
  | jq '."secret/" | {type, options}'
# Expect: type="kv", options.version="2"

# 5. Federation-proof fixture exists
vault kv get -format=json secret/meho/test/federation \
  | jq '.data.metadata.version'
# Expect: a positive integer (the KV-v2 version number).
# A "No value found at secret/data/meho/test/federation" error is
# the smoking gun for missing surface #5.

# 6. Scheduler token can WRITE agent credentials (the surface most
#    easily under-provisioned). Run with the scheduler service token:
vault token capabilities "$VAULT_SCHEDULER_TOKEN" \
  secret/data/agents/example/credentials
# Expect: a set including "create" and "update" (e.g.
# "create read update"). A bare "read" is the live cause of
# meho_agent_principals_register failing — widening the policy to
# include create+update unblocks registration with no code change.
```

When commands 1-5 all return non-error output and the JSON
extractions are populated, the backplane's federation chain has
everything it needs from Vault.

## Failure modes the consumer should expect

| Failure | Diagnostic on the producer side | Consumer-side fix |
| --- | --- | --- |
| `/api/v1/health` returns `vault.reachable=false`, `detail="login_failed: VaultUnreachableError"` | TCP / TLS / timeout to `VAULT_ADDR`. The readiness probe `/ready` also reports `vault` as unhealthy. | Verify `VAULT_ADDR` resolves and is reachable from the backplane Pod's network policy (egress to Vault is in the chart's NetworkPolicy by default — check `networkPolicy.vaultCIDR`) |
| `/api/v1/health` returns `vault.reachable=false`, `detail="login_failed: VaultRoleDeniedError"` | Vault accepted the connection but rejected the JWT for the configured role. | Verify surface 2 (role binding). Most often: `bound_audiences` doesn't match the audience the backplane is forwarding (cross-check `KEYCLOAK_AUDIENCE` env var against `vault read auth/jwt/role/meho-mcp`) |
| `/api/v1/health` returns `vault.reachable=false`, `detail="login_failed: …"`; Vault server log shows `error configuring token validator: unsupported config type` | The `meho-mcp` `role_type=jwt` role was created on an `auth/oidc/` mount that's in OIDC-login mode. Vault (≥1.21.2) refuses `role_type=jwt` on an OIDC-configured mount. | Move the JWT method + role to a **dedicated mount** per surface 1 (`vault auth enable -path=jwt jwt`) and set `VAULT_OIDC_MOUNT_PATH` to match. Do not co-locate with an interactive-OIDC-login mount. (evoila/meho#553) |
| `/api/v1/health` returns `vault.reachable=true`, `vault.read_ok=false`, `detail="read_failed: InvalidPath"` | **Missing surface 5** — federation-proof KV path doesn't exist. | Run the provisioning command from surface 5 above |
| `/api/v1/health` returns `vault.reachable=true`, `vault.read_ok=false`, `detail="read_failed: Forbidden"` | **Missing surface 3** — policy doesn't grant read on `secret/meho/*`. | Verify surface 3 (`vault policy read meho-mcp`); the policy must include both `secret/data/meho/*` and `secret/metadata/meho/*` |
| `/api/v1/health` returns `vault.reachable=true`, `vault.read_ok=false`, `detail="read_failed: KeyError"` or `read_failed: TypeError` | Vault returned an unexpected payload shape — KV-v1 mount instead of v2, proxy mangling the response, etc. | Verify surface 4 (KV v2 mount); the mount must be type `kv` with `options.version="2"` |
| Agent-principal registration fails — REST `502 scheduler_vault_write_error`, or MCP `meho_agent_principals_register` JSON-RPC `-32602` "scheduler Vault write failed" — while reads/listing still work | **Under-scoped surface 6** — `VAULT_SCHEDULER_TOKEN` policy grants read but not write on the agent-credentials path, so the secret-mint write is denied (Vault 403). The backplane probed `auth/token/lookup-self` after that 403 and did **not** get a 403 back, so the token is live (or Vault could not answer the probe — check for an outage first) and the policy is the remaining fault. Reads stay healthy because the read path is read-only-sufficient. | Verify surface 6 (`vault token capabilities "$VAULT_SCHEDULER_TOKEN" secret/data/agents/<name>/credentials`); widen the `meho-scheduler` policy to include `create`+`update` on `secret/data/agents/*/credentials` |
| Agent- or runner-principal registration fails with `scheduler_vault_token_invalid: the scheduler Vault token is invalid or expired …` (REST 502 detail, MCP `-32602` message, `/ui/agents/principals` register banner) | **Dead surface-6 token** — the `VAULT_SCHEDULER_TOKEN` value was revoked, hit its TTL, or is a periodic token whose lease lapsed (a `-period=768h` token expires that long after its *last* renewal, so a scheduler that was down for a month comes back with a dead token). Vault answers both this and the under-scoped-policy case with a 403; the backplane told them apart by getting a 403 from `auth/token/lookup-self` too. **Policy, path pattern, and mount are irrelevant here — do not widen the policy.** | Confirm with `VAULT_TOKEN="$VAULT_SCHEDULER_TOKEN" vault token lookup` (errors on a dead token). Re-mint against the same policy with the surface-6 command above (`vault token create -policy=meho-scheduler -period=768h -field=token`), then update the deployment secret the backplane reads (`VAULT_SCHEDULER_TOKEN`, or the file `VAULT_SCHEDULER_TOKEN_FILE` points at). The file sink is re-read on every use, so a Vault-Agent sidecar rewrite needs no pod restart; an env-var change does need a rollout. Watch the `scheduler_vault_token_verified` log event (`ttl_seconds` / `expire_time`) to confirm recovery |

## References

- Scheduler service-token read/write path (surface 6): [`docs/codebase/scheduler.md`](../codebase/scheduler.md), [`backend/src/meho_backplane/scheduler/vault_credentials.py`](../../backend/src/meho_backplane/scheduler/vault_credentials.py)
- Producer-side handler: [`backend/src/meho_backplane/api/v1/health.py`](../../backend/src/meho_backplane/api/v1/health.py)
- Producer-side Vault client: [`backend/src/meho_backplane/auth/vault.py`](../../backend/src/meho_backplane/auth/vault.py)
- Backplane settings (env-var contract): [`backend/src/meho_backplane/settings.py`](../../backend/src/meho_backplane/settings.py)
- Cross-repo handshake (cluster-side): [`./rke2-infra-coordination.md`](./rke2-infra-coordination.md)
- Smoke leg #4 contract: [`../acceptance/smoke.md`](../acceptance/smoke.md)
- Consumer-side parent ticket: [`evoila-bosnia/claude-rdc-hetzner-dc#293`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/issues/293) — Vault OIDC federation to Keycloak (surfaces 1-4)
- Dedicated-jwt-mount correction: [evoila/meho#553](https://github.com/evoila/meho/issues/553); consumer-side implementation [`evoila-bosnia/claude-rdc-hetzner-dc#524`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/issues/524)
- Vault JWT/OIDC auth docs: <https://developer.hashicorp.com/vault/docs/auth/jwt> (note: the **JWT** method — `role_type=jwt` — is distinct from OIDC-login mode on the same backend)
