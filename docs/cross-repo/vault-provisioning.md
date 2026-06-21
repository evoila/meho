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
| Agent-principal registration fails — REST `502 scheduler_vault_write_error`, or MCP `meho_agent_principals_register` JSON-RPC `-32602` "scheduler Vault write failed" — while reads/listing still work | **Under-scoped surface 6** — `VAULT_SCHEDULER_TOKEN` policy grants read but not write on the agent-credentials path, so the secret-mint write is denied (Vault 403). Reads stay healthy because the read path is read-only-sufficient. | Verify surface 6 (`vault token capabilities "$VAULT_SCHEDULER_TOKEN" secret/data/agents/<name>/credentials`); widen the `meho-scheduler` policy to include `create`+`update` on `secret/data/agents/*/credentials` |

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
