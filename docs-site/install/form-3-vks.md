# Install MEHO on VKS by hand

This page takes you from an empty namespace on a VKS cluster to a
working `meho login`, one command at a time. VKS is vSphere Kubernetes
Service, the Kubernetes that VMware Cloud Foundation (VCF) runs. Each
step says what you should see when it worked.

- **Tested with:** MEHO v0.45.0 (chart 0.45.0), Kubernetes v1.35.5 on
  VCF 9.1, on 10 November 2026. The install itself took 2 minutes and
  17 seconds.
- **Also tested with:** the same MEHO version on Kubernetes v1.32.10 on
  vSphere 8.0 Update 3 (VCF 5.2), on 11 November 2026, with the same
  values file. The install itself took 2 minutes and 46 seconds.
- **Short version:** the [VKS notes](form-3-your-kubernetes.md#vks-vsphere-kubernetes-service)
  of the form 3 chapter. This page is the long version, with every
  command.

## Who does what

Two people take part. They can be the same person.

| Who | Rights | Steps |
|---|---|---|
| **The cluster admin** | Admin of the whole VKS cluster. `kubectl vsphere login` gives this. | [Step 1](#step-1-what-the-cluster-admin-does-first) and part of [Step 2](#step-2-with-no-internet-your-own-registry) |
| **You, the installer** | Admin of one namespace only | Steps 2 to 8 |

The page uses these example values. Use your own in their place.

| Example | What it is |
|---|---|
| `meho.example.com` | The DNS name of MEHO |
| `meho` | The namespace, and the name of the Helm release |
| `registry.example.com/meho` | Your own image registry, only with no internet |
| `192.0.2.20` | The address of Traefik |
| `192.0.2.10` | A vCenter that MEHO manages |

**Tools on your laptop:**

- `kubectl`, at most one minor version away from the cluster.
- [Helm](https://helm.sh/) 3.8 or newer.
- `curl` and `jq`.
- The `meho` command line. See
  [Tools for your laptop](laptop-tools.md#step-2-install-the-meho-command-line).

## Step 1: What the cluster admin does first

The cluster admin does this once, with a cluster admin login.

### 1.1 The namespace and its pod security rule

```bash
kubectl create namespace meho
kubectl label --overwrite namespace meho \
  pod-security.kubernetes.io/enforce=restricted \
  pod-security.kubernetes.io/enforce-version=latest
```

You should see `namespace/meho created` and `namespace/meho labeled`.
`restricted` is the strictest pod security rule of Kubernetes. Every pod
of MEHO runs under it, so you do not need to relax anything.

### 1.2 An account for the installer

The installer gets an account in the namespace, and the built-in role
`admin` for that namespace only. The role allows almost everything
inside the namespace, but nothing outside it.

```bash
kubectl -n meho create serviceaccount meho-installer
kubectl -n meho create rolebinding meho-installer-admin \
  --clusterrole=admin --serviceaccount=meho:meho-installer
kubectl -n meho get rolebinding meho-installer-admin -o wide
```

You should see:

```text
NAME                   ROLE                AGE   USERS   GROUPS   SERVICEACCOUNTS
meho-installer-admin   ClusterRole/admin   5s                     meho/meho-installer
```

To use a person's own login instead of an account, bind the role to
that user or group (`--user` or `--group` in place of
`--serviceaccount`).

### 1.3 A login file for the installer

This makes a kubeconfig file (a login file for `kubectl` and Helm) with a
token that lasts 8 hours. Give the file to the installer in a safe way.

```bash
umask 077
SERVER=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')
kubectl config view --minify --raw \
  -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' | base64 -d > cluster-ca.crt
TOKEN=$(kubectl -n meho create token meho-installer --duration=8h)
KC=meho-installer.kubeconfig
kubectl config --kubeconfig=$KC set-cluster vks --server="$SERVER" \
  --certificate-authority=cluster-ca.crt --embed-certs=true
kubectl config --kubeconfig=$KC set-credentials meho-installer --token="$TOKEN"
kubectl config --kubeconfig=$KC set-context meho --cluster=vks --user=meho-installer --namespace=meho
kubectl config --kubeconfig=$KC use-context meho
unset TOKEN; rm -f cluster-ca.crt
```

Check the file:

```bash
kubectl --kubeconfig=meho-installer.kubeconfig auth can-i create deployments
kubectl --kubeconfig=meho-installer.kubeconfig get nodes
```

You should see `yes`, and then
`Error from server (Forbidden): nodes is forbidden: User "system:serviceaccount:meho:meho-installer" cannot list resource "nodes"`.
That is right: the installer may do everything in `meho`, and nothing
outside it.

The token ends after 8 hours. Kubernetes recommends short tokens like
this one. A token that never ends is possible with a Secret of type
`kubernetes.io/service-account-token`, but Kubernetes calls that a
legacy way.

### 1.4 Traefik, the way in

VKS brings no ingress controller (the program that takes web traffic
from outside and sends it to MEHO). Install Traefik from its Helm chart,
in its own namespace. Write `traefik-values.yaml`:

```yaml
service:
  spec:
    type: LoadBalancer
    # Optional: a fixed address from the Supervisor's load balancer.
    loadBalancerIP: 192.0.2.20
# Only with no internet: the Traefik image from your registry. Copy it
# there first, as in Step 2.2. MEHO's image list does not include it.
# image:
#   registry: registry.example.com
#   repository: meho/traefik
#   tag: v3.7.17
# Only when your registry needs a login: the pull secret of Traefik.
# deployment:
#   imagePullSecrets:
#     - name: registry-pull
```

A pod can only use a pull secret of its own namespace. So when your
registry needs a login, Traefik needs its own pull secret in the
namespace `traefik`. The one of Step 2.4 is in `meho` and does not
count. Make it after the `kubectl label` line below and before
`helm install`, with the command of Step 2.4 and `-n traefik` in place
of `-n meho`. Without it, the Traefik pod stays in `ImagePullBackOff`
and `helm install` ends with an error.

Then install it:

```bash
helm repo add traefik https://traefik.github.io/charts
kubectl create namespace traefik
kubectl label namespace traefik \
  pod-security.kubernetes.io/enforce=restricted \
  pod-security.kubernetes.io/enforce-version=latest
helm install traefik traefik/traefik --version 42.3.1 -n traefik -f traefik-values.yaml --wait
kubectl -n traefik get service traefik
```

You should see the Service `traefik` of type `LoadBalancer` with an
address under `EXTERNAL-IP`. The Supervisor's load balancer gives that
address. In the first seconds, one event `SyncLoadBalancerFailed` can
show up. It goes away when the address comes.

### 1.5 A default storage class

VKS makes one storage class for each vSphere storage policy of the
Supervisor namespace, and marks none as the default. MEHO's disks need a
default, or a class named in your values file.

```bash
kubectl get storageclass
kubectl patch storageclass vsan-default-storage-policy \
  -p '{"metadata": {"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}'
kubectl get storageclass
```

Use the name of your class in place of `vsan-default-storage-policy`.
After the patch, the class shows `(default)` after its name.

### 1.6 The DNS name

Make a DNS record for `meho.example.com` that points to the
`EXTERNAL-IP` of Traefik. Check it from your laptop:

```bash
getent hosts meho.example.com
```

You should see `192.0.2.20       meho.example.com`.

### 1.7 The certificate

Put the certificate for `meho.example.com` and its key into the
namespace. The installer may do this too.

```bash
kubectl -n meho create secret tls meho-tls --cert=tls.crt --key=tls.key
```

You should see `secret/meho-tls created`. If your certificate comes
from an internal certificate authority, every laptop must trust it: see
[Tools for your laptop](laptop-tools.md#step-1-trust-the-sites-own-certificate-authority).

**Without a certificate**, the install makes a self-signed one, in the
Secret `meho-core-tls`. That is fine for a first test, but your laptop
must trust it before Step 5. Otherwise the `curl` lines of Step 5.2
print nothing, and the install check and `meho login` fail with a
certificate error. Right after Step 4, save the authority of that
certificate:

```bash
kubectl -n meho get secret meho-core-tls -o jsonpath='{.data.ca\.crt}' | base64 -d > meho-ca.crt
```

Then add `meho-ca.crt` to the trust store of your laptop, as in
[Tools for your laptop](laptop-tools.md#step-1-trust-the-sites-own-certificate-authority).
This file is a public certificate, not a secret.

## Step 2: With no internet: your own registry

Skip this step when the cluster can pull images from the internet
(`ghcr.io`, `docker.io` and `quay.io`).

### 2.1 The list of images

```bash
helm pull oci://ghcr.io/evoila/meho-chart --version 0.45.0 --untar
cat meho-chart/images.txt
```

You should see five lines. Each line has an image, fixed by its digest,
and its path in your registry:

```text
busybox@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e library/busybox:1.37
pgvector/pgvector@sha256:ac08538c6f8b9904c33c8224c5e5706dbe760aca29db1d096972b4052c22a75d pgvector/pgvector:0.8.7-pg17-bookworm
quay.io/keycloak/keycloak@sha256:79c623a22abacf3fe6c00a5fb30f5b7843d717b234033b80995891c06116d244 keycloak/keycloak:26.8.1
quay.io/openbao/openbao-ubi@sha256:33c4c3a7aeb8b812d570939bd7275f829901031e0a6b3e655207d8f0945bd248 openbao/openbao-ubi:2.7.1
valkey/valkey@sha256:2d04268dfa26ab31cfa3fdc538866e76438b2d00e41db41b8cb86d780ac5a416 valkey/valkey:9.0-alpine
```

The list has every image of the chart except MEHO's own. You copy that
one too.

### 2.2 Copy the images

This uses [crane](https://github.com/google/go-containerregistry/blob/main/cmd/crane/doc/crane.md),
with a registry login that may push. Run it on a machine that reaches
both the internet and your registry, close to the registry: the MEHO
image has one layer of about 240 MB, and a slow or cut connection stops
the upload.

```bash
read -rsp "Password of the push login: " PUSH_PASSWORD; echo
printf '%s' "$PUSH_PASSWORD" | crane auth login registry.example.com -u <push user> --password-stdin
unset PUSH_PASSWORD
while read -r source path; do
  crane copy "$source" "registry.example.com/meho/$path"
done < meho-chart/images.txt
crane copy ghcr.io/evoila/meho:v0.45.0 registry.example.com/meho/evoila/meho:v0.45.0
crane digest ghcr.io/evoila/meho:v0.45.0
crane digest registry.example.com/meho/evoila/meho:v0.45.0
```

The two `crane digest` lines must print the same `sha256:` value. The
pods pull the five images of the list by their digest, so for them the
tag in your registry is only a label. MEHO's own image is pulled by its
tag, `v0.45.0`.

### 2.3 The nodes must trust your registry

The cluster admin does this. If your registry has a certificate from an
internal authority, the nodes of the VKS cluster must trust it.
Otherwise every pull fails with `x509: certificate signed by unknown authority`.

- Add the authority to the cluster variable
  `osConfiguration.trust.additionalTrustedCAs` of the VKS cluster. See
  the [VKS variable reference](https://developer.broadcom.com/xapis/vmware-vsphere-kubernetes-service/latest/variable-docs.html).
- VKS then replaces or updates the nodes. **A node that VKS updates in
  place can keep failing with `x509`.** Its container runtime keeps the
  old list of trusted authorities. Have VKS replace that node.
- Check one pull from your registry on every worker node before you go
  on.

### 2.4 The pull secret

The installer makes this in the namespace, with a registry login that
may only pull. `read -rs` keeps the password out of the screen and out
of your shell history.

```bash
read -rsp "Password of the pull login: " PULL_PASSWORD; echo
kubectl -n meho create secret docker-registry registry-pull \
  --docker-server=registry.example.com \
  --docker-username=<pull user> \
  --docker-password="$PULL_PASSWORD"
unset PULL_PASSWORD
```

You should see `secret/registry-pull created`.

## Step 3: Your two values files

From here on, use the installer's login file:

```bash
export KUBECONFIG=$PWD/meho-installer.kubeconfig
```

### 3.1 `values-vks.yaml`, the same for every VKS cluster

```bash
curl -fsSLO https://raw.githubusercontent.com/evoila/meho/main/deploy/values-examples/values-vks.yaml
```

This file:

- sets Traefik as the way in, on HTTPS only (port 80 answers 404);
- turns on the web console;
- turns on the network rules of the built-in OpenBao and Keycloak. They
  are off by default. Without them, every pod in the cluster can reach
  OpenBao and Keycloak.

It expects Traefik in the namespace `traefik`. If yours is in another
namespace, change `traefik` in the file.

### 3.2 `values.yaml`, your own

Make a new tenant id (a UUID you pick once and keep for the life of the
install):

```bash
uuidgen | tr A-Z a-z
```

Then write `values.yaml`:

```yaml
image:
  tag: v0.45.0
global:
  # Only with no internet (Step 2). Leave out these lines otherwise.
  imageRegistry: registry.example.com/meho
  imagePullSecrets:
    - name: registry-pull
ingress:
  host: meho.example.com
  tls:
    secretName: meho-tls        # empty: the install makes a self-signed certificate
coreKeycloak:
  hostname: meho.example.com    # the same name as ingress.host
  firstTenantId: "<the UUID from uuidgen>"
networkPolicy:
  extraEgress:
    # The systems MEHO manages. One rule for each network and port.
    - to:
        - ipBlock:
            cidr: 192.0.2.10/32 # your vCenter
      ports:
        - port: 443
          protocol: TCP
```

MEHO may reach only its own parts and DNS. List here every system MEHO
must reach, for example the vCenter and NSX. Without a rule, MEHO cannot
reach the vCenter it runs on.

## Step 4: Install

```bash
helm install meho oci://ghcr.io/evoila/meho-chart --version 0.45.0 -n meho \
  -f values-vks.yaml -f values.yaml \
  --wait --wait-for-jobs --timeout 20m
```

Pass `values-vks.yaml` first, so your own file wins where both set a
value. Do not add `--create-namespace`: the namespace exists, and the
installer may not create one.

To watch it, run this in a second terminal:

```bash
kubectl -n meho get pods -w
```

What happens, with the times of our test:

| After | What happens |
|---|---|
| 6 s | A setup Job makes every password and key, and keeps them in Secrets named `meho-core-*`. |
| 17 s | PostgreSQL, Keycloak, OpenBao, Valkey (the store of the live activity) and MEHO start. vSphere makes the two disks in a few seconds. A second Job sets up OpenBao. |
| 43 s | MEHO's database migrations end, and MEHO starts. |
| 108 s | Keycloak has loaded the realm `meho` and is ready. |
| 113 s | MEHO is ready. |
| 123 s | A third Job makes the first two users in Keycloak. |
| 137 s | Helm ends. |

When Helm ends, you should see:

```text
NAME: meho
LAST DEPLOYED: ...
NAMESPACE: meho
STATUS: deployed
REVISION: 1
```

Then the install notes follow. On a slow cluster, the first start of
MEHO can take up to 15 minutes, because MEHO builds its search data on
an empty database. A pod that is `Running` but not `Ready` for some
minutes is normal then.

## Step 5: Check the install

### 5.1 Pods, disks and the way in

```bash
kubectl -n meho get pods
kubectl -n meho get pvc
kubectl -n meho get ingress
```

You should see:

```text
NAME                       READY   STATUS      RESTARTS   AGE
meho-<id>                  1/1     Running     0          2m23s
meho-broadcast-<id>        1/1     Running     0          2m23s
meho-keycloak-0            1/1     Running     0          2m23s
meho-postgres-0            2/2     Running     0          2m23s
meho-vault-0               2/2     Running     0          2m23s
meho-vault-init-<id>       0/1     Completed   0          2m23s
```

`<id>` is a random part of the name, different in every install.

Both disks, `data-meho-postgres-0` and `data-meho-vault-0`, are `Bound`
with 10Gi on the default storage class. The Ingress `meho` has the
class `traefik` and the address of Traefik.

### 5.2 Health, through Traefik

```bash
curl -s https://meho.example.com/healthz
curl -s https://meho.example.com/ready | jq -c '[.status, (.checks[] | "\(.name)=\(.ok)")]'
curl -s -o /dev/null -w '%{http_code}\n' http://meho.example.com/healthz
```

You should see:

```text
{"status":"ok"}
["ready","keycloak=true","vault=true","db=true","broadcast=true","docs_backends=true"]
404
```

The `404` is right: MEHO answers on HTTPS only.

### 5.3 The install check

`install-verify.sh` runs the install checks of a release against your
install. Take the script of your version:

```bash
curl -fsSLO https://raw.githubusercontent.com/evoila/meho/v0.45.0/scripts/acceptance/install-verify.sh
COMMIT=$(helm show chart oci://ghcr.io/evoila/meho-chart --version 0.45.0 | sed -n 's/^appVersion: //p')
bash install-verify.sh --host meho.example.com --namespace meho --release meho --expected-git-sha "$COMMIT"
```

You should see six lines that start with `[ok]`, and at the end:

```text
install-verify.sh: 6 passed, 0 failed, 0 warned
[ok]   MEHO is up
```

## Step 6: The first two users, `meho login`, `meho status`

The install made two users with temporary passwords: the first admin
(`admin`) and a second person who can approve the admin's changes
(`approver`). Write them into a file that only you can read:

```bash
umask 077
kubectl -n meho get secret meho-core-first-login \
  -o go-template='{{range $k,$v := .data}}{{$k}}: {{$v | base64decode}}{{"\n"}}{{end}}' > first-login.txt
```

`first-login.txt` has four lines: `admin_password`, `admin_username`,
`approver_password` and `approver_username`.

Log in with the command line:

```bash
meho login https://meho.example.com
```

The command prints an address and a code. Open the address in a
browser, enter the code and sign in as `admin` with its temporary
password. Keycloak asks for a new password, a name and an email. Then
you should see `Logged in to https://meho.example.com`.

!!! warning "`meho login` changes your default MEHO"

    The command line keeps one login. If you already use another MEHO,
    log in to this one with its own folder, for example
    `XDG_CONFIG_HOME=$HOME/.config/meho-second MEHO_KEYRING_DISABLE=1 meho login https://meho.example.com`,
    and use the same two settings for every later command.
    `MEHO_KEYRING_DISABLE=1` keeps the login in that folder, not in the
    keyring of your system.

Check the whole chain:

```bash
meho status
```

You should see:

```text
Logged in as <your email> (sub: ...)
  Tenants:
    main  tenant_admin  (current)
  Vault: reachable, read OK (version=1)
  DB:    migrated, revision 0137
  Readiness: ready (keycloak, vault, db, broadcast, docs_backends ok)
  Approvals: a second person must approve; decisions on someone else's instruction are refused; approval key configured
  Features: all configured
  Connector packages: none
```

Then open `https://meho.example.com/ui/` in a private browser window and
log in once as `approver`. Keycloak asks for a new password there too.
Keep both new passwords in your key store, and delete the file:

```bash
shred -u first-login.txt
```

## Step 7: The OpenBao unseal key

OpenBao, MEHO's built-in password safe, needs its unseal key after
every restart. The install keeps the key in the Secret
`meho-core-vault-unseal`, so OpenBao unseals itself. Copy the key to
your own key store now, for the day the Secret is lost:

```bash
umask 077
kubectl -n meho get secret meho-core-vault-unseal \
  -o jsonpath='{.data.key}' | base64 -d > unseal-key.txt
```

Copy the key from `unseal-key.txt` into your key store, then delete the
file with `shred -u unseal-key.txt`. Keep the Secret in the cluster. To
copy the key without a file, and to choose who unseals OpenBao, see
[OpenBao unseal key](openbao-unseal-key.md#save-the-key-now).

**MEHO now runs.** Go on with
[the setup page and the settings file](setup-page.md).

## Step 8: Remove the install fully

`helm uninstall` leaves three kinds of objects behind: the two disks,
the Secrets with the passwords and keys, and the objects of the setup
Jobs. Delete them together, or none of them. New passwords do not match
old data, and old passwords do not match a new database.

```bash
helm uninstall meho -n meho --wait
kubectl -n meho delete pvc -l app.kubernetes.io/instance=meho
kubectl -n meho delete secret -l meho.evoila.com/managed-by=core-bootstrap
kubectl -n meho delete configmap,serviceaccount,role,rolebinding -l app.kubernetes.io/instance=meho
kubectl -n meho get all,pvc,secrets,configmaps,serviceaccounts,roles,rolebindings
```

You should see `release "meho" uninstalled`, then the two disks, 15
Secrets and four other objects deleted. The last command shows only
what the cluster admin made in Step 1 and Step 2: the installer account
and its RoleBinding, the pull secret and the TLS Secret, plus the
account `default` and the ConfigMap `kube-root-ca.crt` that every
namespace has.

The cluster admin can check with `kubectl get pv` that no disk of the
install is left.

## Common problems

| What you see | Why, and what to do |
|---|---|
| "unbound immediate PersistentVolumeClaims" for some seconds | Normal on vSAN. It goes away when vSphere has made the disk. If it stays, the cluster has no default storage class (Step 1.5). |
| A pod stays in `ImagePullBackOff` | With your own registry: check the digest copy (Step 2.2), the pull secret (Step 2.4) and the trust of the nodes (Step 2.3). |
| `x509: certificate signed by unknown authority` in a pull | The nodes do not trust your registry (Step 2.3). |
| `https://meho.example.com/ui/` shows an error 503 | Your `values-vks.yaml` is an old copy without the web console. Take it again (Step 3.1) and run `helm upgrade` with the same command as Step 4. |
| `http://meho.example.com` answers with a page, not 404 | Your `values-vks.yaml` is an old copy without the HTTPS-only setting. Same fix. |
| The worker nodes are rebuilt every few minutes | A firewall rule, for example in NSX, blocks the traffic from the workers to the control plane. Remove that rule. |
| Your login to another MEHO is gone | You ran `meho login` without its own folder. Log in to that MEHO again. |
