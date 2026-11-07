# Form 3: your Kubernetes

This chapter takes you from a Kubernetes cluster to a running MEHO and
a successful `meho login`. Follow it from top to bottom. The Helm chart
brings its own PostgreSQL (the database, with pgvector), Keycloak (the
login server) and OpenBao (the password safe, an open-source fork of
Vault). You bring none of them. The chart connects them and makes every
password and key.

Not sure this is your form? See
[Which install form is right for you?](which-form.md). Only looking
around on your laptop? Use the [local kind quickstart](kind-quickstart.md).

To connect your own PostgreSQL, Keycloak or Vault instead, use
[Your own parts](index.md). This chapter does not need it.

## Step 1: Check what you need

| You bring | Requirements |
|---|---|
| **Kubernetes cluster** | Kubernetes 1.28 or newer, with an ingress controller (the examples use ingress-nginx) and a default storage class. The built-in parts keep their data on disks the cluster makes. |
| **Helm** | [Helm](https://helm.sh/) 3.8 or newer on your workstation, so it can install from an OCI registry. Helm 4 is recommended. |
| **A DNS name** | A hostname for MEHO, for example `meho.example.com`, that points to your ingress controller. |

Everything else ships inside the Helm release: the activity-feed store
(Valkey), the database migrations and the operations catalog.

## Step 2: Decide the hostname and the certificate

MEHO serves one hostname, set as `ingress.host`. The chart derives the
MCP audience from it. The MCP audience is the name every agent token
must be made for.

You can leave the certificate empty. Then the install makes a
self-signed certificate. Or name your own TLS Secret. If your company
uses an internal certificate authority, read
[TLS and ingress](tls-ingress.md).

## Step 3: Write your values file

Create `values.yaml`:

```yaml
image:
  # Pin an immutable tag: v<x.y.z> for a release (see
  # https://github.com/evoila/meho/releases).
  tag: "v0.45.0"

ingress:
  enabled: true
  className: nginx
  host: meho.example.com
  tls:
    enabled: true
    # Empty: the install makes a self-signed certificate in the Secret
    # meho-core-tls. Or name your own TLS Secret here.
    secretName: ""

coreKeycloak:
  hostname: meho.example.com      # the same value as ingress.host
  # A UUID you pick once for your first tenant, and keep.
  # For example, make one with: uuidgen | tr 'A-Z' 'a-z'
  firstTenantId: "<REPLACE: a new UUID>"
```

That is all. Leave `postgres.credentialsSecret`, `vault.address`,
`config.vaultAddr`, `config.keycloakIssuerUrl` and `keycloak.issuer`
empty. The chart sets them, and Helm stops with an error that names the
key if you set one. You never type a database password.

### Let MEHO reach the systems it manages

The network rules of the chart are on by default. They let MEHO reach
its own parts and DNS, and nothing else. vCenter, NSX, ESXi, SSH hosts,
WinRM hosts and the AI service are blocked. MEHO still reports ready,
because its readiness check only tests its own parts. So list the
networks MEHO must reach in `networkPolicy.extraEgress`. Each item is a
normal Kubernetes egress rule. Open only the networks and ports MEHO
needs.

```yaml
networkPolicy:
  extraEgress:
    # The systems MEHO manages. Replace the range with yours, and list
    # only the ports your connectors use.
    - to:
        - ipBlock:
            cidr: 192.0.2.0/24
      ports:
        - port: 443     # vCenter, NSX
          protocol: TCP
        - port: 22      # SSH
          protocol: TCP
```

Every rule needs a `to` list. The chart refuses a rule without one.

**A target behind your ingress.** Kubernetes does not say whether a
network rule is checked before or after the cluster redirects a
connection. On Calico and Canal with kube-proxy (RKE2's default), it is
checked after. So when a target name points to your ingress load
balancer, the connection goes to an ingress Pod on that Pod's own port,
and a rule for the port you expect never matches. Write the rule for
the real Pod and its port, not for the load balancer address. For
ingress-nginx the port is usually 443. Check the `targetPort` of your
ingress Service.

### If a certificate comes from an internal authority

Skip this when every certificate you use is public. If a target or your
company login uses an internal certificate authority, MEHO must trust
it. Make a bundle that holds the public roots and your authority, put it
in a ConfigMap, and name it in the values. The bundle must hold both.
With only your authority, MEHO stops trusting every public site.

```bash
kubectl create namespace meho
# /etc/ssl/certs/ca-certificates.crt is the public roots on Debian and
# Ubuntu. On another system use any file of public roots.
cat /etc/ssl/certs/ca-certificates.crt my-internal-ca.pem > ca.crt
kubectl -n meho create configmap internal-ca-bundle --from-file=ca.crt=ca.crt
```

Then add the `extraVolumes`, `extraVolumeMounts` and `extraEnv` block
from
[TLS and ingress](tls-ingress.md#the-backplanes-own-trust-internal-ca-bundle)
to `values.yaml`.

## Step 4: Install the chart

```bash
helm upgrade --install meho oci://ghcr.io/evoila/meho-chart \
  --namespace meho --create-namespace \
  -f values.yaml
```

Pin `--version <chart-version>` in production. Find the versions with
`helm show chart oci://ghcr.io/evoila/meho-chart`. To deploy by digest
and verify the signature, see
[Step 7 of Your own parts](index.md#step-7-install-the-chart).

What happens, in order:

1. A setup Job makes every password and key once and keeps them in
   Secrets named `meho-core-*`. Every upgrade keeps them.
2. PostgreSQL, Keycloak and OpenBao start. Keycloak loads the realm
   `meho`. A second Job sets up OpenBao, and a third one makes the two
   first users in Keycloak.
3. MEHO runs its database migrations, then starts.

The **first start takes up to 15 minutes**. On an empty database, MEHO
first registers the full operations catalog and builds the search data,
before it starts listening. A Pod that is `Running` but not `Ready` for
some minutes is normal. Later starts take about 20 seconds.

The install notes (`helm status meho -n meho`) print the Secret with
OpenBao's unseal key, `meho-core-vault-unseal`. Copy the key to your own
key store now. See [OpenBao unseal key](openbao-unseal-key.md).

## Step 5: Check that it works

```bash
kubectl -n meho rollout status deploy/meho --timeout=960s

# The image has no curl, so use a port-forward.
kubectl -n meho port-forward deploy/meho 8000:8000 >/dev/null 2>&1 &
PF=$!
for _ in $(seq 30); do
  curl -s -o /dev/null http://localhost:8000/healthz && break
  sleep 1
done

curl -fsS http://localhost:8000/healthz
# /ready answers 503 until every check is green, so read the body.
curl -sS http://localhost:8000/ready | jq .
kill "$PF"

# From outside the cluster, through the ingress:
curl -fsS https://meho.example.com/healthz
curl -fsS https://meho.example.com/api/v1/auth-config
```

If `/healthz` is green but `/ready` answers 503, MEHO may not trust a
certificate. See
[TLS and ingress](tls-ingress.md#the-backplanes-own-trust-internal-ca-bundle).

## Step 6: Install the CLI and log in

Download the `meho` CLI for your platform from the
[releases page](https://github.com/evoila/meho/releases):

```bash
TAG=<the release tag of your backplane, for example v0.45.0>
TARBALL=meho_${TAG#v}_linux_amd64.tar.gz   # or darwin_arm64, etc.
curl -LO https://github.com/evoila/meho/releases/download/${TAG}/${TARBALL}
curl -LO https://github.com/evoila/meho/releases/download/${TAG}/SHA256SUMS
sha256sum -c SHA256SUMS --ignore-missing
tar xzf ${TARBALL}
sudo install -m 0755 meho /usr/local/bin/meho
```

With the self-signed certificate, install its authority in your
workstation's system trust store first. The CLI checks TLS against that
store. Get the authority from the Secret `meho-core-tls`:

```bash
kubectl -n meho get secret meho-core-tls -o jsonpath='{.data.ca\.crt}' | base64 -d > meho-ca.crt
```

How to add it to the store is in
[TLS and ingress](tls-ingress.md#your-workstation-os-trust-store).

Then log in:

```bash
meho login https://meho.example.com
```

!!! warning "`meho login` changes your default backplane"

    The CLI saves the address you log in to. If you already use another
    MEHO, later commands go to the new one. Run `meho status` after the
    login to see which backplane you talk to.

The CLI prints a verification URL and a code. You approve it in a
browser. The install made two users with temporary passwords: the first
admin (`admin`) and a second person who can approve the admin's changes
(`approver`). Both are in the Secret `meho-core-first-login`. Read the
admin's login with:

```bash
kubectl -n meho get secret meho-core-first-login \
  -o jsonpath='{.data.admin_username}' | base64 -d; echo
kubectl -n meho get secret meho-core-first-login \
  -o jsonpath='{.data.admin_password}' | base64 -d; echo
```

Keep these passwords in your own key store. The output goes to your
screen and not to a file, and no password goes into your shell history.

Prove the whole chain:

```bash
meho status
```

**You now have a running MEHO and a logged-in operator.** Go on with
[the setup page and the settings file](setup-page.md).
