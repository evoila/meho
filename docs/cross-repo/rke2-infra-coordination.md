<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# rke2-infra coordination — per-PR ephemeral smoke + `repository_dispatch`

> Cross-repo handshake between `evoila/meho` (this repo, producer) and
> [`evoila-bosnia/claude-rdc-hetzner-dc`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc)
> (private; consumer of MEHO and operator of the rke2-infra dogfooding
> cluster).
>
> This page is the upstream-side **tracker**. The actual provisioning
> work — kubeconfig minting, RBAC application, workflow authoring, secret
> storage — happens on the consumer side. What lives here is the spec
> the consumer reads to know exactly what `evoila/meho` will send into
> rke2-infra and exactly what RBAC surface it needs in return.

## Why this handshake exists

[Goal #11](https://github.com/evoila-bosnia/meho-internal/issues/11)
locks the v0.1 release on a single dogfooding consumer (RDC) operating
MEHO against the consumer's existing rke2-infra Kubernetes cluster.
Two CI/CD properties from `evoila/meho` cross that repo boundary into
the consumer:

1. **Per-PR ephemeral-cluster smoke** (G2.7-T2, [#50](https://github.com/evoila-bosnia/meho-internal/issues/50)).
   Every PR on `evoila/meho` deploys the chart into a fresh
   `meho-ci-<pr-number>` namespace on rke2-infra, runs the smoke
   script, and tears the namespace down. If the smoke fails the PR
   can't merge. This is the per-PR ephemeral-cluster discipline MEHO.X
   never had — every G2.0–G2.6 PR closes the real-target feedback loop
   before merge against a real (not mocked) Kubernetes API.
2. **`repository_dispatch` deploy trigger** (G2.7-T3, [#51](https://github.com/evoila-bosnia/meho-internal/issues/51)).
   Every merge to `main` on `evoila/meho` builds, signs, and pushes a
   new backplane image to GHCR, then dispatches a
   `repository_dispatch` event to `evoila-bosnia/claude-rdc-hetzner-dc`
   carrying the new image digest. The consumer side decides whether to
   roll the dogfooding instance forward (currently: yes, on every
   green merge).

Both edges cross the rke2-infra cluster boundary, and both need the
consumer side to provision auth + RBAC for the `evoila/meho` GitHub
Actions identity before Task #50 / #51 can land green. This tracker
documents exactly what to provision and how either side verifies the
contract.

## Consumer-side prerequisites

These items land **in `evoila-bosnia/claude-rdc-hetzner-dc`**, not in
this repo. The acceptance bullets close when the consumer-side PR(s)
land.

### 1. Cluster authentication for `evoila/meho` GitHub Actions

Two options. **Pick exactly one. Option A is preferred.**

#### Option A (preferred) — GitHub Actions OIDC trust

Configure rke2-infra's kube-apiserver to trust GitHub Actions' OIDC
identity provider directly. No long-lived kubeconfig secret stored
on `evoila/meho`.

| Field | Value |
| --- | --- |
| Issuer URL | `https://token.actions.githubusercontent.com` |
| JWKS endpoint | `https://token.actions.githubusercontent.com/.well-known/jwks` |
| Signing algorithm | `RS256` |
| Token audience | Operator-chosen; recommend `rke2-infra.evba.lab` (per-cluster, distinct) |

Sub-claim shape (the Kubernetes user identity rke2-infra will see):

```text
repo:evoila/meho:ref:refs/heads/main
repo:evoila/meho:pull_request
repo:evoila/meho:environment:rke2-ci
```

The exact shape depends on the workflow's `permissions:` block and
which trigger fired. The `evoila/meho` CI workflows that will use this
trust path set `permissions: id-token: write` at the workflow (or job)
level and request the OIDC ID token from GitHub Actions' own OIDC
provider — **not** from `actions/create-github-app-token`, which mints
GitHub App installation tokens via private-key auth and is unrelated
to the K8s OIDC trust path. Two equivalent retrieval mechanisms:

1. The Actions toolkit method `core.getIDToken(audience)` invoked from
   `actions/github-script` (or any custom JavaScript action).
2. A direct `curl` against the runner-local OIDC endpoint, using the
   two environment variables the runner injects when
   `id-token: write` is granted:
   `$ACTIONS_ID_TOKEN_REQUEST_URL` (the endpoint) and
   `$ACTIONS_ID_TOKEN_REQUEST_TOKEN` (the bearer token authorising
   the request).

Minimal worked example (the smoke workflow's authentication step):

```yaml
jobs:
  smoke:
    runs-on: ubuntu-latest
    environment: rke2-ci
    permissions:
      id-token: write
      contents: read
    steps:
      - name: Mint GitHub Actions OIDC ID token
        id: oidc
        uses: actions/github-script@v7
        with:
          script: |
            const token = await core.getIDToken('rke2-infra.evba.lab');
            core.setSecret(token);
            core.setOutput('token', token);
      # Equivalent curl form when not using actions/github-script:
      #   TOKEN=$(curl -sSf \
      #     -H "Authorization: bearer $ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
      #     "$ACTIONS_ID_TOKEN_REQUEST_URL&audience=rke2-infra.evba.lab" \
      #     | jq -r .value)
      - name: Configure kubectl to use the OIDC ID token
        run: |
          kubectl config set-credentials gha \
            --token="${{ steps.oidc.outputs.token }}"
          kubectl config set-cluster rke2-infra \
            --server=https://rke2-infra.evba.lab:6443 \
            --certificate-authority=/etc/ca/rke2-infra.crt
          kubectl config set-context gha \
            --cluster=rke2-infra --user=gha --namespace=meho-ci-${{ github.event.pull_request.number }}
          kubectl config use-context gha
```

The audience string (`rke2-infra.evba.lab` above) must match the
`audiences` entry in the kube-apiserver's OIDC configuration (see the
next subsection). For Kubernetes-side mapping, the recommended sub
template is `repo:evoila/meho:environment:rke2-ci` — gating workflow
access through a GitHub Environment named `rke2-ci` lets the consumer
side require manual approval, environment-scoped secrets, and the
audit trail the bare `pull_request` claim doesn't give.

**kube-apiserver configuration (pre-Kubernetes 1.29):**

```text
--oidc-issuer-url=https://token.actions.githubusercontent.com
--oidc-client-id=rke2-infra.evba.lab
--oidc-username-claim=sub
--oidc-username-prefix=actions:
```

The `actions:` username prefix keeps the GitHub identities clearly
separated from human and ServiceAccount identities in the apiserver's
audit log.

**Kubernetes 1.29+ Structured Authentication Configuration:**

Prefer `AuthenticationConfiguration` over the legacy flags when
rke2-infra runs 1.29 or newer. The config supports multiple
issuers, so the consumer can keep its existing OIDC IdP (Keycloak,
Google, etc.) alongside GitHub Actions. Worked example shape:

```yaml
apiVersion: apiserver.config.k8s.io/v1beta1
kind: AuthenticationConfiguration
jwt:
  - issuer:
      url: https://token.actions.githubusercontent.com
      audiences:
        - rke2-infra.evba.lab
    claimMappings:
      username:
        claim: sub
        prefix: "actions:"
    claimValidationRules:
      - claim: repository
        requiredValue: evoila/meho
```

The `claimValidationRules` block constrains the trust to the exact
repository — without it, **any** GitHub Actions workflow in **any**
public repo could mint a token against this audience.

#### Option B (fallback) — long-lived kubeconfig stored as a GHA secret

Mint a kubeconfig from a dedicated rke2-infra ServiceAccount, base64
the kubeconfig, and store it as the `RDC_KUBECONFIG` secret on
`evoila/meho` (or — better — as a repository-environment secret on the
`rke2-ci` GitHub Environment so it requires environment-scoped approval).

Tradeoffs vs Option A:

- **Worse:** long-lived static credential; rotation discipline
  required (consumer-side cron or manual quarterly rotation); shared
  secret blast radius if leaked.
- **Better:** zero kube-apiserver configuration changes; works on
  rke2-infra versions that pre-date Structured Authentication Config.

Only pick Option B when Option A is blocked on cluster-version or
operator-time budget. Document the rotation owner + cadence on the
consumer-side coordination ticket.

### 2. Namespace-scoped RBAC for `meho-ci-*`

Whichever auth path is picked, the identity that lands in rke2-infra
needs **exactly** these permissions and **no more**:

- **Cluster-scoped, `meho-ci-*` only:** create + delete namespaces
  whose names match the pattern `meho-ci-*`. There is no Kubernetes-
  native namespace name-pattern filter at the RBAC layer, so the
  consumer enforces this via one of:
  - Validating admission policy (Kyverno / Gatekeeper / native
    ValidatingAdmissionPolicy on K8s 1.30+) rejecting namespace
    create requests from `actions:repo:evoila/meho:*` users whose
    namespace name doesn't match `^meho-ci-[a-z0-9-]+$`.
  - A per-PR ServiceAccount minted by a consumer-side controller
    that owns only the one namespace.
- **Namespace-scoped (`meho-ci-*` only):** the verbs Helm needs for
  `helm upgrade --install`, `helm test`, and `helm uninstall` on the
  templates this chart actually ships
  ([`deploy/charts/meho/`](../../deploy/charts/meho/) — Deployment,
  Service, ConfigMap, Secret, ServiceAccount, NetworkPolicy, Ingress,
  Job, plus the broadcast subchart's resources).

Minimum verb set, per resource group:

| API group | Resources | Verbs |
| --- | --- | --- |
| `""` (core) | `services`, `configmaps`, `secrets`, `serviceaccounts`, `pods`, `pods/log` | `create`, `get`, `list`, `watch`, `patch`, `update`, `delete`, `deletecollection` |
| `apps` | `deployments`, `replicasets` | `create`, `get`, `list`, `watch`, `patch`, `update`, `delete` |
| `batch` | `jobs` | `create`, `get`, `list`, `watch`, `patch`, `update`, `delete` |
| `networking.k8s.io` | `networkpolicies`, `ingresses` | `create`, `get`, `list`, `patch`, `update`, `delete` |
| `rbac.authorization.k8s.io` | `roles`, `rolebindings` | `create`, `get`, `list`, `delete` (only if the chart later adds in-namespace RBAC; v0.1 chart does not) |

`pods/log` is the verb the smoke script will need to read backplane
startup logs when the readiness probe stays red. `replicasets` shows
up because Helm watches the `Deployment` rollout via the
`apps/v1/Deployment` status which references the replicasets.

**Verifiability of the scoping**: from the identity rke2-infra issues
to GitHub Actions, `kubectl auth can-i delete namespace default`
returns `no` and `kubectl auth can-i delete namespace meho-ci-pr-1`
returns `yes`. The Verification section below scripts both checks.

### 3. `repository_dispatch` consumer workflow

`evoila/meho`'s `main` push image workflow (G2.7-T3, #51) sends a
`repository_dispatch` event to
`evoila-bosnia/claude-rdc-hetzner-dc` with:

| Field | Value |
| --- | --- |
| `event_type` | `meho-image-pushed` (≤100 chars per GitHub API limit) |
| `client_payload.image` | `ghcr.io/evoila/meho` |
| `client_payload.digest` | `sha256:<64-hex>` of the pushed manifest |
| `client_payload.tag` | The calver-stamped tag (`0.1.YYYYMMDD-<short-sha>`) |
| `client_payload.commit` | The full 40-char git SHA of `main` |
| `client_payload.ref` | The git ref that was pushed (always `refs/heads/main` for this trigger) |

`client_payload` has a 10-top-level-key + 65535-character total ceiling
per GitHub's REST API contract — the five fields above sit comfortably
under both. Adding more fields later is non-breaking on the consumer
side as long as the listener doesn't assert key cardinality.

The consumer side authors a workflow at
`.github/workflows/meho-deploy.yml` (or equivalent) with:

```yaml
on:
  repository_dispatch:
    types: [meho-image-pushed]
```

The workflow's exact deploy logic — whether it edits `targets.yaml`,
runs `helm upgrade`, opens a follow-up PR for review, etc. — is the
consumer's call. This handshake only specifies the event shape.

### 4. Consumer-side `manifests/meho/` files

The consumer ships its environment-specific install plumbing at
`claude-rdc-hetzner-dc/manifests/meho/`:

- `values-rdc.yaml` — the actual values overlay for the dogfooding
  instance (copied from
  [`deploy/values-examples/values-rdc-example.yaml`](../../deploy/values-examples/values-rdc-example.yaml),
  with the `<REPLACE: ...>` placeholders substituted for the RDC
  lab's real Keycloak issuer, Vault address, network CIDRs, etc.).
- `install.sh` — wraps `helm upgrade --install` against the chart at
  `oci://ghcr.io/evoila/meho-chart` using `values-rdc.yaml`.
- `smoke.sh` — the post-install verification script the per-PR
  workflow runs (also used by the consumer's own production deploy).
- `README.md` — operator-facing notes.

These are explicitly **consumer-owned** per Goal #11's cross-repo
deps: the chart at `evoila/meho` ships the canonical *template*
([`deploy/values-examples/values-rdc-example.yaml`](../../deploy/values-examples/values-rdc-example.yaml));
the operator owns their per-environment substitution. The v0.2 work
on multi-operator OSS adoption will document the copy-and-substitute
pattern for new operators in `evoila/meho`'s top-level OSS docs.

### 5. Documentation update on the consumer

The consumer's `CLAUDE.md` carries a "MEHO event contract" section
documenting the `meho-image-pushed` event shape and the workflow that
consumes it. Keep that doc and this one in lock-step — when one
changes, the other changes in the same iteration.

## Verification

The end-to-end handshake passes when **all** of the following return
the documented exit codes from a workstation or CI context using the
GitHub-Actions-side identity (Option A token-exchange or Option B
kubeconfig):

```bash
# 1. Authentication round-trip works.
kubectl auth whoami
#   ⇒ Username: actions:repo:evoila/meho:environment:rke2-ci (Option A)
#     or:      system:serviceaccount:meho-ci-system:gh-actions (Option B)

# 2. The identity CAN create + delete a meho-ci-* namespace.
NS=meho-ci-verify-$(date +%s)
kubectl auth can-i create namespace                                # yes
kubectl create namespace "$NS"
kubectl auth can-i --namespace "$NS" list pods                     # yes
kubectl auth can-i --namespace "$NS" create deployments.apps       # yes
kubectl delete namespace "$NS"

# 3. The identity CANNOT touch non-meho-ci-* namespaces.
kubectl auth can-i --namespace default delete pods                 # no
kubectl auth can-i --namespace kube-system delete pods             # no
kubectl auth can-i --namespace meho delete deployments.apps        # no

# 4. The repository_dispatch listener fires on a manually-sent event.
gh api -X POST \
  -H "Accept: application/vnd.github+json" \
  repos/evoila-bosnia/claude-rdc-hetzner-dc/dispatches \
  -f event_type=meho-image-pushed \
  -f client_payload[image]=ghcr.io/evoila/meho \
  -f client_payload[digest]=sha256:0000000000000000000000000000000000000000000000000000000000000000 \
  -f client_payload[tag]=0.1.99991231-deadbee \
  -f client_payload[commit]=deadbeefdeadbeefdeadbeefdeadbeefdeadbeef \
  -f client_payload[ref]=refs/heads/main

gh run list --repo evoila-bosnia/claude-rdc-hetzner-dc \
  --event repository_dispatch --limit 1
#   ⇒ a run is queued / in_progress for the new event_type
```

The companion script
[`scripts/cross-repo/verify-rke2-access.sh`](../../scripts/cross-repo/verify-rke2-access.sh)
automates the kubectl portion (`KUBECONFIG=… ./verify-rke2-access.sh`),
prints a pass/fail line per check, and exits non-zero on any
unexpected outcome. Run it from either side of the handshake before
declaring the contract closed.

## Status

This is the live tracker for consumer-side acceptance. Bullets here
mirror the acceptance criteria on
[evoila-bosnia/meho-internal#53](https://github.com/evoila-bosnia/meho-internal/issues/53).

| Item | Status | Owner | Notes |
| --- | --- | --- | --- |
| Coordination ticket filed on `evoila-bosnia/claude-rdc-hetzner-dc` | pending | RDC maintainers | Linked to this doc + Initiative [#48](https://github.com/evoila-bosnia/meho-internal/issues/48) |
| kube-apiserver OIDC trust configured (Option A) **OR** `RDC_KUBECONFIG` secret stored (Option B) | pending | RDC maintainers | Prefer Option A; document choice on the coordination ticket |
| Namespace-scoped RBAC enforcing `meho-ci-*` | pending | RDC maintainers | Includes the admission-policy half of the scoping (see Section 2) |
| Consumer workflow listening for `meho-image-pushed` repository_dispatch | pending | RDC maintainers | Filename suggested `.github/workflows/meho-deploy.yml` |
| `manifests/meho/{values-rdc.yaml,install.sh,smoke.sh,README.md}` landed | pending | RDC maintainers | Also tracked in Goal [#11](https://github.com/evoila-bosnia/meho-internal/issues/11) cross-repo deps |
| `claude-rdc-hetzner-dc/CLAUDE.md` documents the MEHO event contract | pending | RDC maintainers | Mirror of this doc's Section 3 |

Move an item to `done` (with the linking PR/commit) only after the
consumer-side change has merged. When all six are done, close
[meho-internal#53](https://github.com/evoila-bosnia/meho-internal/issues/53).

## Out of scope

- Authoring `install.sh` / `smoke.sh` / `values-rdc.yaml` in this
  repo — Goal #11 explicitly rejects this in favour of the
  consumer-owned model.
- Production cluster provisioning — rke2-infra already exists.
- Multi-environment trust (staging vs prod) — v0.1 has only the
  dogfooding lab.
- Generalising this handshake to a multi-operator adoption pattern —
  v0.2; tracked separately.

## References

- [Goal #11](https://github.com/evoila-bosnia/meho-internal/issues/11) — Deployable v0.1, cross-repo dependencies section
- [Initiative #48](https://github.com/evoila-bosnia/meho-internal/issues/48) — G2.7 CI/CD + per-PR ephemeral smoke
- [Task #50](https://github.com/evoila-bosnia/meho-internal/issues/50) — Per-PR ephemeral cluster deploy
- [Task #51](https://github.com/evoila-bosnia/meho-internal/issues/51) — `repository_dispatch` trigger
- [Task #53](https://github.com/evoila-bosnia/meho-internal/issues/53) — This tracker
- GitHub Actions OIDC: <https://docs.github.com/en/actions/concepts/security/openid-connect>
- `repository_dispatch` API: <https://docs.github.com/en/rest/repos/repos#create-a-repository-dispatch-event>
- Kubernetes RBAC reference: <https://kubernetes.io/docs/reference/access-authn-authz/rbac/>
- Kubernetes Structured Authentication Configuration (1.29+):
  <https://kubernetes.io/docs/reference/access-authn-authz/authentication/#using-authentication-configuration>
