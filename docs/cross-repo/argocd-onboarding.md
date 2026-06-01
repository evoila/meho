<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# ArgoCD op surface onboarding — operator recipe

> Operator-facing recipe for the G3.12 `argocd-api-3.x` op surface — how
> to register an `argocd` target (host, port, the Vault bearer-token
> `secret_ref`), the six curated read ops, the `GET /api/version` probe,
> and the **deferred write surface**. The op handlers live in
> [`backend/src/meho_backplane/connectors/argocd/`](../../backend/src/meho_backplane/connectors/argocd/);
> the engineering-facing companion is
> [`docs/codebase/connectors-argocd.md`](../codebase/connectors-argocd.md).
> This doc is the cookbook an RDC operator reads when they want MEHO to
> see what ArgoCD sees, instead of reaching for the `argocd` CLI or raw
> `kubectl` against the `argocd` namespace.

## What this surface is

The `argocd-api-3.x` connector is a **typed** connector: hand-coded
handlers over `httpx`, registered into the G0.6 `endpoint_descriptor`
table at backplane startup. It dispatches under the
`(product="argocd", version="3.x", impl_id="argocd-api")` registry
triple — the connector id `argocd-api-3.x` — plus a `("argocd", "", "")`
wildcard so a freshly-registered, unfingerprinted target still resolves.

The `-api` discriminator in the impl_id leaves room for a future
transport sibling (e.g. a gRPC variant) without breaking the resolver's
tie-break ladder; G3.12 ships only the REST transport against
`argocd-server`.

The v0.10 op surface (Initiative
[#1387](https://github.com/evoila/meho/issues/1387)) is **read-only** —
the GitOps-visibility working set:

| Group | op_id | Endpoint | Returns |
| --- | --- | --- | --- |
| `argocd-apps` | `argocd.app.list` | `GET /api/v1/applications` (opt. `projects[]`, `selector`) | `{items, metadata}` — apps + sync/health status |
| `argocd-apps` | `argocd.app.get` | `GET /api/v1/applications/{name}` (opt. `project`) | one app's full spec + status |
| `argocd-apps` | `argocd.app.diff` | `GET /api/v1/applications/{name}/managed-resources` | `{items: [ResourceDiff]}` — `liveState`/`targetState` per resource |
| `argocd-apps` | `argocd.app.resource_tree` | `GET /api/v1/applications/{name}/resource-tree` | `{nodes, orphanedNodes, hosts, shardsCount}` |
| `argocd-projects` | `argocd.appproject.list` | `GET /api/v1/projects` | `{items, metadata}` — AppProjects + allow-lists |
| `argocd-repos` | `argocd.repo.list` | `GET /api/v1/repositories` | `{items, metadata}` — repos + connectionState |

Six ops total, every one `safety_level="safe"`, `requires_approval=False`,
`read-only` tag. They dispatch through the same
`POST /api/v1/operations/call` route the agent surface uses — auth,
policy, audit, broadcast, and JSONFlux all run as documented in
[CLAUDE.md](../../CLAUDE.md) §6. Operators reach them via the
per-connector `meho argocd …` verb tree (the primary surface — see [the
CLI invocation surface](#the-cli-invocation-surface)), or the equivalent
generic `meho operation call argocd-api-3.x <op_id> …` verb; agents reach
them via the narrow-waist meta-tools (see [the agent meta-tool
path](#the-agent-meta-tool-path)).

## Prerequisites

- **A reachable `argocd-server`.** The connector talks to ArgoCD's REST
  gateway (`argocd-server`, the same endpoint the ArgoCD web UI and the
  `argocd` CLI hit). Supported server versions: ArgoCD `>=2.0,<4.0`
  (`supported_version_range`), validated against the 3.x API shapes.
- **An ArgoCD API bearer token.** ArgoCD authenticates every request
  with a JWT **bearer token** — `Authorization: Bearer <token>`. Mint a
  long-lived account or project token (`argocd account generate-token`
  for an account token, or a `project` token scoped to the AppProjects
  MEHO should see). Read-only RBAC is sufficient for this op surface —
  the token only needs `get`/`list` on `applications`, `projects`, and
  `repositories`. Scope the token to read-only so a stolen credential
  can't mutate the GitOps state.
- **A registered `argocd` target.** The ops resolve a target slug
  server-side to a row in the `targets` table. The target carries
  `product="argocd"`, `host`, `port` (`443` for a TLS `argocd-server`),
  `secret_ref` (the Vault path holding the token), `auth_model=
  shared_service_account`, and `preferred_impl_id="argocd-api"`.
- **`auth_model = shared_service_account`** — the only auth model this
  connector ships. The bearer token is a shared service-account
  credential; `auth_headers` rejects any other `auth_model` with a clear
  `NotImplementedError`. The token is read out of Vault under the
  operator's identity at op time (operator-context Vault read — the
  operator's JWT authenticates the *read*, not the ArgoCD request).
- **An operator session.** `meho login <backplane-url>` writes the
  session token the CLI reuses. Every dispatch verb needs `operator`
  role minimum.

## Target + auth model

The shipped connector's auth model is `shared_service_account` over an
HTTPS bearer token. The token is stored in **Vault** at a per-target path
under the operator tenant's KV-v2 mount, then materialised onto the
target row's `secret_ref` column.

| Field | `secret_ref` key | Notes |
| --- | --- | --- |
| **API token (only shape)** | `token` | The opaque ArgoCD JWT, stored verbatim. Unlike Harbor's Basic auth there is **no** `username` component — the credential is a single string. |

The connector's
[`load_credentials_from_vault`](../../backend/src/meho_backplane/connectors/argocd/session.py)
reads exactly the `token` field; a Vault secret missing that key raises a
`RuntimeError` naming the target and the missing key at dispatch time
(fail-fast — never an opaque ArgoCD 401). The token is cached per target
after first use, never logged (the `argocd_credentials_loaded` log event
carries `target` + `host` only — no secret value), and never rides back
in the `OperationResult` envelope.

### Storing the bearer token in Vault

Use the tenant's KV-v2 path convention `<tenant>/argocd/<host>`. The
secret carries the single `token` field. `meho vault kv put` takes the
`<mount> <path>` positional pair (`ExactArgs(2)` — here mount `secret`,
logical path `rdc-hetzner-dc/argocd/api-token`, no `/data/` infix) and
the secret body as a JSON object via `--data` (inline or `@<file>`):

```console
$ meho vault kv put --target rdc-vault secret \
    rdc-hetzner-dc/argocd/api-token \
    --data "{\"token\":\"$(cat ./argocd-token.txt)\"}"
```

`argocd-token.txt` is the raw token string emitted by
`argocd account generate-token --account meho-readonly` (or the project
token); `--data @<file>` accepts a JSON object file directly if you'd
rather not inline it. The federation chain's `meho-mcp` Vault policy
already grants the operator read on `secret/<tenant>/*`, so no per-target
policy edit is needed (see [`vault-onboarding.md`](./vault-onboarding.md)
for the federation setup).

### Registering the target

Targets are registered by importing a `targets.yaml` descriptor — `meho
targets import <file>` (`ExactArgs(1)`) is the verb every sibling
onboarding doc uses; there is no `meho targets create`. Add an entry:

```yaml
# targets.yaml
targets:
  - name: rdc-argocd
    product: argocd
    host: argocd.rdc-hetzner-dc.example.com
    port: 443
    secret_ref: secret/data/rdc-hetzner-dc/argocd/api-token
    auth_model: shared_service_account
    preferred_impl_id: argocd-api
```

Then import it:

```console
$ meho targets import targets.yaml
```

The `secret_ref` value matches the Vault path above with the KV-v2
`/data/` infix (Vault's KV-v2 API path shape — distinct from the
`<mount> <path>` positional form `meho vault kv put` takes, which omits
`/data/`). `preferred_impl_id: argocd-api` pins the resolver to this
connector (defensive — there is only one `argocd` impl today, but it
future-proofs against a sibling transport).

### Verify — the `GET /api/version` probe

```console
$ meho targets probe rdc-argocd
ok — argocd reachable; argocd-server v3.3.9
```

`probe()` delegates to `fingerprint()`, which hits the **unauthenticated**
`GET /api/version` endpoint on `argocd-server`. This is the right
reachability surface for two reasons:

1. ArgoCD exposes no dedicated composite health endpoint comparable to
   Harbor's `/api/v2.0/health`; `/api/version` is the cheap, always-on
   probe.
2. It is unauthenticated, so a target probes green **before** its Vault
   token is configured — you can register and reachability-check a target,
   then add the credential, then run an op. A reachable fingerprint maps
   to `ProbeResult(ok=True)`; an unreachable one carries the structured
   error string (e.g. `ConnectError: …`) as `reason`.

The fingerprint surfaces ArgoCD's `Version` as the canonical version plus
the bundled build-tool versions (`BuildDate`, `KustomizeVersion`,
`HelmVersion`, `KubectlVersion`) under `extras` — the same view
`argocd version -o json` shows for the server block.

## The CLI invocation surface

The primary operator surface is the per-connector `meho argocd …` verb
tree — a thin Cobra-over-HTTP layer (`cli/internal/cmd/argocd/`) that
pre-bakes `connector_id="argocd-api-3.x"` so you don't retype the
connector id on every call, mirroring the sibling `meho keycloak …`
(#1395), `meho harbor …` (#622), and `meho nsx …` (#615) trees. Per
CLAUDE.md postulate 5 these alias verbs are operator-only ergonomics —
they are not mirrored on the MCP surface. All six read ops have a verb;
each `meho argocd …` invocation and its generic `meho operation call`
equivalent dispatch the identical `POST /api/v1/operations/call` route.
The target slug is whatever you registered.

```console
# List every app + its sync/health status:
$ meho argocd app list --target rdc-argocd

# Narrow to one project / a label selector:
$ meho argocd app list --target rdc-argocd --project payments --selector env=prod

# One app's full spec + status:
$ meho argocd app get --target rdc-argocd --name guestbook

# The desired-vs-live drift (the read-only `argocd app diff <app>` equivalent):
$ meho argocd app diff --target rdc-argocd --name guestbook

# The reconciled resource tree with per-node health:
$ meho argocd app resource-tree --target rdc-argocd --name guestbook

# AppProjects + their allow-lists:
$ meho argocd appproject list --target rdc-argocd

# Configured repos + their connection state:
$ meho argocd repo list --target rdc-argocd

# Machine-readable envelope for jq (every verb takes --json):
$ meho argocd repo list --target rdc-argocd --json \
    | jq '.result.items[] | {repo, status: .connectionState.status}'
```

Every verb also takes `--backplane <url>` (defaults to the URL from the
most recent `meho login`). Exit codes: `0`=ok, `1`=error/denied,
`2`=auth_expired, `3`=unreachable, `4`=unexpected.

### The generic dispatcher alternative

The same six ops are equally reachable through the generic `meho
operation` verbs — useful for scripting against any connector with one
code path, or when the op surface is newer than your CLI build's verb
tree. The connector id is `argocd-api-3.x`:

```console
$ meho operation call argocd-api-3.x argocd.app.list --target rdc-argocd
$ meho operation call argocd-api-3.x argocd.app.get --target rdc-argocd \
    --params '{"name": "guestbook"}'
$ meho operation call argocd-api-3.x argocd.app.diff --target rdc-argocd \
    --params '{"name": "guestbook"}'
$ meho operation call argocd-api-3.x argocd.repo.list --target rdc-argocd --json \
    | jq '.result.items[] | {repo, status: .connectionState.status}'
```

`--params` accepts inline JSON or `@<file>`; omit it for the no-param ops
(`app.list` with no filters, `appproject.list`, `repo.list`). Exit codes
mirror every dispatch verb: `0` ok, `1` error/denied, `2` auth_expired,
`3` unreachable, `4` unexpected response shape.

To discover ops without leaving the CLI, the hybrid search verb works
against the same connector id:

```console
$ meho operation search argocd-api-3.x "which apps are out of sync"
$ meho operation search argocd-api-3.x "diff drift" --group argocd-apps
```

## The agent meta-tool path

Agents never see the `meho operation …` verbs — those are operator CLI
ergonomics. Per [CLAUDE.md](../../CLAUDE.md) postulate 5, an agent reaches
every argocd op through the narrow-waist meta-tools — there are **no
per-op MCP tools** (one tool per op would fan eleven-plus duplicated
schemas onto the agent surface and re-implement the search-then-dispatch
flow the meta-tools already cover):

```text
search_connectors(query="argocd gitops")          → finds argocd-api-3.x
list_operation_groups(connector_id="argocd-api-3.x")
                                                   → argocd-apps / argocd-projects / argocd-repos
search_operations(
    connector_id="argocd-api-3.x",
    query="which apps are out of sync",
    group="argocd-apps",
)                                                  → top hit: argocd.app.list
call_operation(
    connector_id="argocd-api-3.x",
    op_id="argocd.app.diff",
    target={"name": "rdc-argocd"},
    params={"name": "guestbook"},
)
```

The agent's flow is always: pick connector → list operation groups →
search operations (optionally scoped to a group) → `call_operation`. The
CLI invocation and the `call_operation` params are 1:1 — the
`meho operation call argocd-api-3.x argocd.app.diff …` above and the
`call_operation` call dispatch the identical route, audit row, and
broadcast event. Each op's `llm_instructions` payload (registered at
`register_typed_operation()` time, reviewable in
[`backend/src/meho_backplane/connectors/argocd/ops.py`](../../backend/src/meho_backplane/connectors/argocd/ops.py))
is what `search_operations` surfaces to rank and guide the agent.

## Writes are deferred (read-only by design)

This connector ships **no write or mutating op**. The ArgoCD verbs that
change cluster state —

- **`app.sync`** — trigger a sync (reconcile an app to its Git-desired
  state),
- **`app.rollback`** — roll an app back to a previous synced revision,
- **`app.set`** / spec edits — change an Application's source, target
  revision, or sync policy —

are an **approval-gated follow-up** under Initiative
[#1387](https://github.com/evoila/meho/issues/1387), not part of this
op surface. They are deliberately out of scope here (Hard rule: no
speculative optionality — read parity first, writes land as a separate
Task behind the approval queue, following the
[github-write-ops-approval](./github-write-ops-approval.md) /
[g316-vmware-write-activation](./g316-vmware-write-activation.md)
precedent). Until they land, drive syncs/rollbacks through the `argocd`
CLI or the ArgoCD UI; use this connector for **visibility** —
"is this app in sync?", "what changed?", "which child resource is
unhealthy?", "can ArgoCD reach this repo?".

`argocd.app.diff` is the read-only counterpart of `argocd app diff
<app>`: it returns the managed-resources delta (each item's `liveState`
vs `targetState`) without touching the cluster — the right op to confirm
*what* a future `app.sync` would reconcile, before the write surface
exists.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `no backplane URL configured` (exit 2) | Never logged in / no `--backplane`. | `meho login <url>` or pass `--backplane <url>`. |
| `auth_expired` / stored token rejected | Keycloak operator token expired. | `meho login <url>` again. |
| `status=error … operation not found` | The typed-op registrar didn't run (lifespan crash) or op_id drift. | Verify the backplane started cleanly; `meho connector list` should show `argocd-api-3.x` with six enabled ops. Check logs for `argocd_operations_registered`. |
| `status=error … unknown_connector` | Connector id drift — typed `argocd-3.x` instead of `argocd-api-3.x`. | Use `argocd-api-3.x`; the `-api` impl_id discriminator is part of the connector_id grammar. |
| `RuntimeError … missing required key 'token'` | The Vault secret at `secret_ref` lacks the `token` field. | `meho vault kv get …` the path; the secret must carry a `token` key (no `username`). |
| op returns ArgoCD `401` / `403` | The stored token is invalid or its RBAC lacks `get`/`list`. | Re-mint with `argocd account generate-token`; grant read-only RBAC on `applications` / `projects` / `repositories`. |
| `probe` returns `ConnectError` | `argocd-server` not reachable from the backplane (port/VPN). | Confirm `:443` (or the configured port) is reachable; ArgoCD's `/api/version` is unauthenticated, so a 200 here isolates the failure to transport. |
| `app.get` returns ArgoCD `404` for an app you can see in the UI | The optional `project` param scoped it out (ArgoCD 404s rather than 403s an out-of-project app), or the token's project scope excludes it. | Drop `project` from `--params`, or confirm the token's AppProject scope includes the app. |

## References

- Initiative: [#1387 G3.12 ArgoCD connector](https://github.com/evoila/meho/issues/1387); Goal [#214](https://github.com/evoila/meho/issues/214) (connector parity / wrapper retirement).
- Tasks that shipped this surface: [#1390](https://github.com/evoila/meho/issues/1390) (skeleton — bearer auth + fingerprint + dual registration), [#1391](https://github.com/evoila/meho/issues/1391) (curated read core), [#1392](https://github.com/evoila/meho/issues/1392) (CLI/MCP review + recorded-fixture E2E + this doc).
- Engineering companion: [`docs/codebase/connectors-argocd.md`](../codebase/connectors-argocd.md).
- Op handlers: [`backend/src/meho_backplane/connectors/argocd/`](../../backend/src/meho_backplane/connectors/argocd/) (`connector.py`, `ops.py`, `session.py`).
- Recorded-fixture E2E: [`backend/tests/test_connectors_argocd_e2e.py`](../../backend/tests/test_connectors_argocd_e2e.py) (full `call_operation` + `search_operations` surface); read-core dispatch suite: [`backend/tests/test_connectors_argocd_reads.py`](../../backend/tests/test_connectors_argocd_reads.py).
- Generic dispatch CLI: [`cli/internal/cmd/operation/`](../../cli/internal/cmd/operation/) (`call.go`, `search.go`, `groups.go`).
- ArgoCD API docs: <https://argo-cd.readthedocs.io/en/stable/developer-guide/api-docs/>; `VersionMessage` proto: `argoproj/argo-cd` `server/version/version.proto`.
- Vault federation setup: [`vault-onboarding.md`](./vault-onboarding.md). Onboarding-doc precedents: [`bind9-onboarding.md`](./bind9-onboarding.md), [`kubernetes-onboarding.md`](./kubernetes-onboarding.md), [`harbor-onboarding.md`](./harbor-onboarding.md).
- Deferred write surface: tracked under Initiative [#1387](https://github.com/evoila/meho/issues/1387); approval-gated write precedents [`github-write-ops-approval.md`](./github-write-ops-approval.md), [`g316-vmware-write-activation.md`](./g316-vmware-write-activation.md).
