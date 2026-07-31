# TLS and ingress

One hostname and a handful of trust decisions shape the whole
deployment. This page covers the four distinct places TLS matters —
they fail independently, with different symptoms, and conflating them
costs hours.

## The hostname drives the MCP audience

The backplane serves everything on one host — set as `ingress.host` in
the chart values. That hostname does more than routing: the chart
**derives the MCP resource URI from it** (`https://<host>/mcp`), and
every agent token must be issued *for* that URI as its audience. This
is why [Keycloak realm setup](keycloak-realm.md) has you paste the
exact backplane URL (no trailing slash) into the `meho-mcp-audience`
mapper.

Three values can supply the URI, in override order:
`config.mcpResourceUri`, `config.backplaneUrl`, or derivation from
`ingress.host`. If **none** of the three resolves — say, ingress is
disabled and no URL is set — the chart **fails at render time**
(`helm template` / `helm install`) with a message naming all three
knobs. It will not deploy an MCP endpoint that silently rejects every
token. To see what a values file resolves to before installing:

```bash
helm template meho oci://ghcr.io/evoila/meho-chart -f values.yaml \
  | grep -A1 MCP_RESOURCE_URI
```

If you change the hostname later, remember the audience mapper in the
realm must change with it.

## The certificate on the ingress

The backplane needs to be served over HTTPS — the OAuth flows require
it, and agents will refuse plaintext. Any certificate source works;
with [cert-manager](https://cert-manager.io/) it is one annotation:

```yaml
ingress:
  enabled: true
  className: nginx
  host: meho.example.com
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
  tls:
    enabled: true
    secretName: meho-tls
```

A **publicly-trusted** certificate here keeps everything downstream
simple (workstations and hosted MCP clients trust it out of the box).
An **internal CA** works for the CLI and self-hosted clients — but
note for later that hosted agent frontends (e.g. connecting claude.ai
directly to your backplane) can only reach endpoints whose
certificates chain to a public CA. The client-by-client picture lives
in [Connect clients](../clients/index.md).

## Your workstation: OS trust store

Skip this section if the backplane and Keycloak present
publicly-trusted certificates.

The `meho` CLI is a Go binary: it verifies TLS against your
**operating system's trust store** — on macOS it reads the system
keychain and **ignores the `SSL_CERT_FILE` environment variable
entirely**. So an internal CA must be installed OS-level on every
operator workstation:

- **Linux** — drop the CA into `/usr/local/share/ca-certificates/`
  and run `update-ca-certificates`.
- **macOS** — `security add-trusted-cert -d -r trustRoot -k
  /Library/Keychains/System.keychain <ca>.pem` (or import via
  Keychain Access).
- **Windows** — `certutil -addstore -f Root <ca>.pem`.

Verify from a fresh shell before attempting a login:

```bash
curl -sf https://meho.example.com/healthz
```

The failure this prevents: `meho login` dying at its discovery probe
with `x509: certificate signed by unknown authority`.

## The backplane's own trust: internal-CA bundle

Skip this section if your Keycloak, Vault, and PostgreSQL present
publicly-trusted certificates.

The backplane *makes* TLS connections too — to Keycloak (token
validation), the credential backend, and PostgreSQL. Its Python
runtime trusts only public CAs by default, so internal-CA-signed
dependencies fail their probes and the symptom is distinctive:
**`/healthz` is green but `/ready` returns 503**, with the `keycloak`
entry in its `checks` reading `jwks_fetch_failed: ConnectError` (or the
credential backend's reading `unreachable: ConnectError`), and an
`--atomic` install rolls itself back.

The fix is mounting a CA bundle and pointing `SSL_CERT_FILE` at it —
the chart has first-class hooks:

```yaml
extraVolumes:
  - name: trust-bundle
    configMap:
      name: internal-ca-bundle   # rendered by trust-manager (recommended)
      optional: false

extraVolumeMounts:
  - name: trust-bundle
    mountPath: /etc/ssl/extra-certs
    readOnly: true

extraEnv:
  - name: SSL_CERT_FILE
    value: /etc/ssl/extra-certs/ca.crt
```

These flow into **both** the backplane Deployment and the migration
Job (PostgreSQL over internal-CA TLS is exactly why the Job needs the
bundle too). The recommended way to produce and rotate the ConfigMap
is [trust-manager](https://cert-manager.io/docs/trust/trust-manager/);
a hand-created ConfigMap works if you own rotation.

!!! danger "The bundle must be a union — not just your CA"

    `SSL_CERT_FILE` **replaces** Python's default trust store, it does
    not extend it. A bundle containing *only* your internal CA breaks
    every public-CA connection the Pod also makes. Build the bundle as
    the union of the public roots **and** your CA — trust-manager's
    `Bundle` resource does exactly this with `useDefaultCAs: true`
    alongside your CA source.

Verify after install:

```bash
kubectl -n meho exec deploy/meho -- printenv SSL_CERT_FILE
kubectl -n meho exec deploy/meho -- wget -qO- http://localhost:8000/ready
```

## One layer further out: targets with private certificates

Everything above covers MEHO's own plumbing. The same problem returns
when MEHO *dispatches* to your infrastructure: appliances routinely
present self-signed or internal-CA certificates, and a dispatch
against an untrusted chain fails with a structured
`connector_tls_verify_failed` error naming the host and both
remediations.

In preference order:

1. **Add the appliance's CA to the global bundle** above — verification
   stays fully on.
2. **Pin the CA on the target** (`tls_ca_pin`) — trust *this*
   appliance's chain specifically, keeping chain and hostname
   verification on. The right fix when the CA does not belong in the
   global bundle.
3. **`verify_tls: false` on the target** — the audited, per-target
   last resort. Verification is off for that one target, so the
   forwarded credential is exposed to interception on that path; every
   flip writes an audit row. Never global, and mutually exclusive with
   a pin.

The API and `targets.yaml` recipes for pinning and the opt-out live
with the targets documentation in the
[values-examples deep-dive](https://github.com/evoila/meho/blob/main/deploy/values-examples/README.md#connector-dispatch-against-self-signed--internal-ca-targets),
and will be promoted into the *Do real work* section's target guide.

## Back to the trail

Return to [the install trail, Step 6](index.md#step-6-write-your-values-file).
