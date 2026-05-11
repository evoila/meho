<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# values-examples — sanitized chart-values templates

This directory ships **sanitized example values files** for the MEHO Helm
chart at [`deploy/charts/meho/`](../charts/meho/). Each file targets a
specific deployment shape (today: the RDC Hetzner dogfooding lab) and is
designed to be copied into a private deploy repo, the placeholders
substituted, and applied via `helm install` / `helm upgrade`.

The files here ship **no real secrets, no real CIDRs**. Every
site-specific field uses a `<REPLACE: ...>` placeholder that fails
`values.schema.json` validation at install time, so an operator who
forgets to substitute one fails-loud at `helm install` rather than
silently connecting to the wrong system or CrashLoopBackOff'ing at first
request.

## Files

| File | Targets | Backed by |
| --- | --- | --- |
| [`values-rdc-example.yaml`](./values-rdc-example.yaml) | The RDC Hetzner lab (`*.evba.lab` hosts, rke2-infra ingress-nginx, cluster-internal Postgres + Vault + Keycloak). | The actual, private file lives in [`evoila-bosnia/claude-rdc-hetzner-dc`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc)'s `manifests/meho/values-rdc.yaml`; this file is the sanitized template for other Vault-+-Keycloak-+-Postgres-shaped labs. |

## Using `values-rdc-example.yaml`

1. **Copy** the file into your private deploy repo. Do not put real
   CIDRs / hostnames / image tags into a public repo.

2. **Substitute** the `<REPLACE: ...>` placeholders. The minimum required
   set:

    | Placeholder | What to put |
    | --- | --- |
    | `image.tag` | An immutable tag from the G2.4 image pipeline. Production: `sha-<40-char-git-sha>` from a green CI run. Pre-prod / lab: `v0.1.0` once the release tag exists. **`:latest` and `:main` are forbidden by Goal #11 deploy discipline.** |
    | `config.keycloakIssuerUrl` / `keycloak.issuer` realm | Your Keycloak realm name. Both fields must agree — the ConfigMap-sourced env mirrors the values block (`config.keycloakIssuerUrl` is what the backplane process reads at startup). |
    | `networkPolicy.postgresCIDR` | The IPv4 CIDR of your Postgres Service. Recover via `kubectl get endpoints <pg-svc> -n <ns> -o jsonpath='{.subsets[].addresses[].ip}'` and widen to the controlling subnet. |
    | `networkPolicy.vaultCIDR` | Same, for Vault. |
    | `networkPolicy.keycloakCIDR` | Same, for Keycloak. |

3. **Provision** the Kubernetes Secrets named in the values file. The
   chart references `postgres.credentialsSecret` by name; the Pod will
   not start until that Secret exists with a `url` key holding the
   `DATABASE_URL`. The recommended sync mechanism is **External Secrets
   Operator (ESO)** — see [§ ESO sync patterns](#eso-sync-patterns) below.

4. **Install** (after ESO has populated the target Secret — see install
   flow below):

    ```bash
    helm upgrade --install meho ./deploy/charts/meho/ \
      --namespace meho --create-namespace \
      -f values-rdc.yaml
    ```

5. **Verify** the release came up clean:

    ```bash
    kubectl -n meho rollout status deploy/meho
    kubectl -n meho get pods -l app.kubernetes.io/name=meho
    kubectl -n meho logs deploy/meho --tail=50
    ```

## ESO sync patterns

The MEHO chart references operator-provisioned Kubernetes Secrets *by
name* — it does not embed credentials in `values.yaml`, and it does not
ship a `Secret` template that consumers `--set` values into. Instead, the
chart expects the consumer to sync secrets from a backing store (Vault,
1Password Connect, AWS Secrets Manager, …) into Kubernetes via
[**External Secrets Operator (ESO)**](https://external-secrets.io/).

The RDC lab uses ESO with **HashiCorp Vault** as the backend
([provider docs](https://external-secrets.io/latest/provider/hashicorp-vault/)).
Two resources combine to materialise a Secret the chart can consume:

1. **`ClusterSecretStore`** — cluster-scoped pointer at the upstream
   store. Carries the Vault address, auth method, and (for JWT/Kubernetes
   auth) the SA token. Created once, by the platform team. **Lives
   outside this chart by design**: it outlives any given release and
   carries the cluster's Vault credentials, so it belongs in the
   consumer's GitOps repo, not the application chart.

    ```yaml
    apiVersion: external-secrets.io/v1beta1
    kind: ClusterSecretStore
    metadata:
      name: vault-store
    spec:
      provider:
        vault:
          server: https://vault.evba.lab
          path: secret          # KV v2 mount
          version: v2
          auth:
            kubernetes:
              mountPath: kubernetes
              role: external-secrets
              serviceAccountRef:
                name: external-secrets
                namespace: external-secrets
    ```

2. **`ExternalSecret`** — namespaced resource that pulls one or more
   keys out of the upstream store and projects them into a Kubernetes
   Secret in the same namespace. **Two options for who owns this**:

    - **Default (consumer-managed):** the consumer's GitOps repo applies
      ExternalSecret manifests alongside (or before) the chart. The
      chart references the resulting Secret by name. This is the RDC
      lab's convention and stays out of the chart entirely.

      ```yaml
      apiVersion: external-secrets.io/v1beta1
      kind: ExternalSecret
      metadata:
        name: meho-postgres
        namespace: meho
      spec:
        refreshInterval: 1h
        secretStoreRef:
          name: vault-store
          kind: ClusterSecretStore
        target:
          # MUST match `.Values.postgres.credentialsSecret` in the chart.
          name: meho-postgres
          creationPolicy: Owner
        data:
          - secretKey: url       # the Secret's data key — MUST be `url`
            remoteRef:
              key: secret/meho/postgres   # Vault KV path
              property: url               # the JSON property holding DATABASE_URL
      ```

      The chart's Deployment env reads `DATABASE_URL` from this Secret's
      `url` key — see [`deploy/charts/meho/templates/deployment.yaml`](../charts/meho/templates/deployment.yaml).

    - **Opt-in (chart-managed):** set `eso.enabled: true` in your
      values file and the chart renders the ExternalSecret itself. Use
      this when you'd rather keep one source of truth (`helm template`
      shows everything) and you're not running a sibling GitOps
      operator that already owns the ExternalSecrets.

      ```yaml
      eso:
        enabled: true
        secretStore:
          name: vault-store
          kind: ClusterSecretStore
        refreshInterval: 1h
        postgres:
          remoteKey: secret/meho/postgres
          remoteProperty: url
        keycloak:
          enabled: false        # v0.1 does not yet consume this in env
      ```

      The schema enforces `secretStore.name` + `secretStore.kind` when
      `eso.enabled: true`, so a misconfigured opt-in fails at install
      time. With `eso.enabled: false` (the default) the chart never
      renders ExternalSecret resources — verify with
      `helm template ... | grep -c ExternalSecret` → `0`.

### Vault paths the chart expects

| Vault KV path | What's stored | Consumed by |
| --- | --- | --- |
| `secret/meho/postgres` (property `url`) | The full `DATABASE_URL`: `postgresql+asyncpg://<user>:<pass>@<host>:<port>/<db>` | The Deployment env `DATABASE_URL` via `postgres.credentialsSecret` |
| `secret/meho/keycloak/client_secret` (property `client_secret`) | The Keycloak OAuth client secret backing `keycloak.audience` | v0.2 federation wiring (rendered optionally today for end-to-end sync verification) |

The `secret/meho` base is configurable via `vault.paths.kv` — adjust the
KV paths above accordingly if you remount Vault elsewhere.

## End-to-end install flow

The order matters because the chart's migration Job runs as a
`pre-install,pre-upgrade` hook and refuses to start without
`DATABASE_URL`.

```bash
# 0. Once per cluster: install External Secrets Operator.
helm repo add external-secrets https://charts.external-secrets.io
helm install external-secrets external-secrets/external-secrets \
  -n external-secrets --create-namespace

# 1. Once per cluster: create the ClusterSecretStore pointing at Vault.
#    (Owned by your GitOps repo, not this chart — see above.)
kubectl apply -f cluster-secret-store-vault.yaml

# 2. Per release: provision the ExternalSecret(s) the chart references.
#    Skip this step if you set `eso.enabled: true` in values — the chart
#    renders them itself.
kubectl apply -n meho -f externalsecret-meho-postgres.yaml

# 3. Wait for the target Secret to materialise. The chart's pre-install
#    migration Job mounts it; if it isn't ready, the Job fails fast.
kubectl -n meho wait --for=create secret/meho-postgres --timeout=60s
kubectl -n meho get externalsecret meho-postgres -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}'
# → "True"

# 4. Apply the chart.
helm upgrade --install meho ./deploy/charts/meho/ \
  --namespace meho --create-namespace \
  -f values-rdc.yaml
```

## Out of scope for v0.1

- The actual `values-rdc.yaml` (private, lives in the consumer's repo).
- The `ClusterSecretStore` / `SecretStore` manifest itself — consumer-owned.
- Multi-environment overlays (staging, prod) — v0.1 ships only the lab shape.
- Helm `--values=secret://` plugin integration — v0.2 if the dual-source
  ExternalSecret pattern proves friction-heavy.
- ApplicationSet (ArgoCD) — v0.2.

## References

- Parent Goal: [#11 — Deployable v0.1](https://github.com/evoila-bosnia/meho-internal/issues/11)
- Parent Initiative: [#36 — G2.5 Helm chart](https://github.com/evoila-bosnia/meho-internal/issues/36)
- This task: [#40 — values-rdc-example.yaml + ESO sync patterns documented](https://github.com/evoila-bosnia/meho-internal/issues/40)
- External Secrets Operator: <https://external-secrets.io/>
- ESO Vault provider: <https://external-secrets.io/latest/provider/hashicorp-vault/>
- ESO ExternalSecret API: <https://external-secrets.io/latest/api/externalsecret/>
- Chart documentation: [`docs/codebase/devops.md`](../../docs/codebase/devops.md)
