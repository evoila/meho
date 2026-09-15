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

A **publicly-trusted** certificate here keeps workstation trust simple
— operator machines, and the local MCP shims they run, trust it out of
the box with no OS-store import. An **internal CA** works just as well
and is the norm: every MEHO client connects from the internal network
/ VPN, so there is no cloud-brokered frontend in the picture (the
remote claude.ai / Claude Desktop Custom Connector is **not
applicable** — MEHO is never publicly exposed). The one extra step an
internal CA adds — trusting it on each operator workstation — and the
per-client trust picture are in
[Connect clients](../clients/index.md).

## Reverse-proxy trust (`forwardedAllowIps`)

The backplane sits behind your ingress controller, so it reads the real
client address from the `X-Forwarded-*` headers the proxy sets — but it
must only *trust* those headers from the proxy itself. That trust list
is `config.forwardedAllowIps`, and the **baseline is to narrow it to the
reverse proxy / ingress controller's own source address**, never the
whole pod CIDR:

```yaml
config:
  # Trust forwarded headers ONLY from the ingress controller's address.
  # Recover it from the controller's Service/Endpoints rather than
  # trusting the whole pod network:
  #   kubectl get endpoints <ingress-controller> -n <ns> \
  #     -o jsonpath='{.subsets[].addresses[].ip}'
  forwardedAllowIps: "10.0.4.11/32"   # the controller's address, not 10.0.0.0/16
```

uvicorn's default (`127.0.0.1`) fails closed in-cluster — the controller
pod is never loopback — so a value has to be set. The wrong fix is to
open it to the entire pod network: any workload that can reach the
backplane could then forge the client address the audit log and any
address-based policy record. Scope it to the controller's address (a
`/32`, or the tightest slice you have subnetted the controller onto).
The per-cluster recommended values and the diagnostic walk are in
[`docs/cross-repo/reverse-proxy-contract.md`](https://github.com/evoila/meho/blob/main/docs/cross-repo/reverse-proxy-contract.md).

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
credential backend's reading `unreachable: SSLError`), and an
`--atomic` install rolls itself back.

The fix is mounting a CA bundle and pointing **two** environment
variables at it — one is not enough, because the backplane's
dependencies do not agree on which one to read. The chart has
first-class hooks:

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
  # Read by Python's ssl module: httpx (Keycloak JWKS) and
  # asyncpg/SQLAlchemy (PostgreSQL).
  - name: SSL_CERT_FILE
    value: /etc/ssl/extra-certs/ca.crt
  # Vault is reached through hvac, which drives `requests` — and
  # requests ignores SSL_CERT_FILE entirely, reading only
  # REQUESTS_CA_BUNDLE / CURL_CA_BUNDLE. Omit this and the credential
  # backend's readiness check stays red with `unreachable: SSLError`
  # however correct SSL_CERT_FILE is.
  - name: REQUESTS_CA_BUNDLE
    value: /etc/ssl/extra-certs/ca.crt
```

These flow into **both** the backplane Deployment and the migration
Job (PostgreSQL over internal-CA TLS is exactly why the Job needs the
bundle too). The recommended way to produce and rotate the ConfigMap
is [trust-manager](https://cert-manager.io/docs/trust/trust-manager/);
a hand-created ConfigMap works if you own rotation.

!!! danger "The bundle must be a union — not just your CA"

    Both variables **replace** the default trust store, they do not
    extend it. A bundle containing *only* your internal CA breaks every
    public-CA connection the Pod also makes — via `SSL_CERT_FILE` for
    httpx and the database drivers, and identically via
    `REQUESTS_CA_BUNDLE` for anything reached through `requests`. Build
    the bundle as the union of the public roots **and** your CA —
    trust-manager's `Bundle` resource does exactly this with
    `useDefaultCAs: true` alongside your CA source.

Verify after install:

```bash
kubectl -n meho exec deploy/meho -- printenv SSL_CERT_FILE REQUESTS_CA_BUNDLE

# The image ships no curl or wget, so port-forward rather than exec.
kubectl -n meho port-forward deploy/meho 8000:8000 >/dev/null 2>&1 &
PF=$!

# Wait for the forward to bind before curling it — any HTTP answer counts.
for _ in $(seq 30); do
  curl -s -o /dev/null http://localhost:8000/ready && break
  sleep 1
done

curl -sS http://localhost:8000/ready | jq '.checks'
kill "$PF"
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

## Outbound peer authentication for credential-bearing connections

TLS on the ingress protects traffic *into* the backplane. The same
discipline applies to every credential-bearing connection the backplane
makes *outward* — and the **baseline is that none of these paths ships a
verification-disabled default**. Each fails closed and authenticates its
peer before a secret is offered:

- **SSH host-key verification.** The SSH connector verifies the target's
  host key fail-closed. A target whose resolved secret carries no
  `known_hosts` entry is refused before the connection opens — so a
  password can never be disclosed to an impostor host by default. Provision
  the target's host key as an OpenSSH `known_hosts`-format line on the
  target's secret (`<host-pattern> <keytype> <base64>`); an unexpected key
  then aborts key exchange before authentication. A per-target
  `known_hosts_insecure` escape hatch restores the old no-verification
  behaviour for one target, logging a loud warning on every connect — opt-in,
  per-target, never the default, and mirroring the `verify_tls: false`
  target posture above.
- **SMTP TLS verification.** The mail transport builds a validating TLS
  context (`CERT_REQUIRED` with hostname checking on) for both the
  implicit-TLS (port 465) and STARTTLS paths, so mail contents and SMTP
  credentials are not exposed to an active intermediary. Point it at your
  internal CA the same way the backplane's other outbound TLS is trusted
  (the CA-bundle section above).
- **Bootstrap CA trust.** Identity-bootstrap scripts that post an admin
  credential must verify the endpoint's certificate against a real CA
  bundle rather than running `curl -k` — provision the CA, and prefer a
  bounded provisioning identity over a master-admin credential.

Fail closed on the wrong name, an untrusted chain, or an unexpected SSH
key; treat the per-target opt-outs as audited, temporary exceptions.

## Back to the trail

Return to [the install trail, Step 6](index.md#step-6-write-your-values-file).
