# Install MEHO on OpenShift by hand

This page takes you from an empty OpenShift cluster to a working
`meho login`, one command at a time. Each step says what you should see
when it worked. It works with and without internet on the cluster.

You type no host names by hand. [Step 0](#step-0-the-variables) reads
them from the cluster and keeps them in shell variables, and every later
command uses those. Copy each block as it is.

- **Tested with:** MEHO v0.45.0 (chart 0.45.0) on a single-node
  OpenShift 4.22, online, in November 2026, with `REL=meho`. An admin
  of one project installed MEHO, every pod ran under OpenShift's
  default security rule `restricted-v2` with no extra rights, and
  `install-verify.sh` and `check-openbao-unseal.sh` passed. That run
  took the usual path: Steps 0, 1.1, 1.2 A, 1.3 (a default class
  existed), 3.1, 3.4, 4, 5.1 to 5.4, 6 and 7. Not run on OpenShift
  yet: Step 1.2 B, your own certificate (1.4), Step 2 (no internet),
  the files of 3.2 and 3.3, Step 5.5 and Step 8.
- **The general guide:** [Form 3: your Kubernetes](form-3-your-kubernetes.md)
  explains the same install for any Kubernetes. This page is the
  OpenShift version, with every command.

!!! warning "Use v0.45.0 on OpenShift for now"

    From v0.46.0 on, the chart uses the plain OpenBao image, and OpenShift
    needs the UBI one. With v0.46.0 or newer, also make the file of
    [Step 3.3](#33-only-for-v0460-or-newer-the-openbao-image).

## Who does what

Two people take part. They can be the same person.

| Who | Rights | Steps |
|---|---|---|
| **The cluster admin** | Admin of the whole OpenShift cluster | [Step 1](#step-1-what-the-cluster-admin-does-first) and part of [Step 2](#step-2-with-no-internet-your-own-registry) |
| **You, the installer** | Admin of one project only | Steps 2 to 8 |

**Tools on your laptop:**

- `oc`, the OpenShift command line, to log in. The web console has it
  under **?** → **Command line tools**.
- `kubectl`, at most one minor version away from the cluster. The check
  scripts of Steps 5 and 7 call `kubectl` and stop without it, so `oc`
  alone is not enough.
- [Helm](https://helm.sh/) 3.8 or newer.
- `curl`, `jq` and `uuidgen`.
- `skopeo`, only for Step 2 (no internet) and Step 3.3 (v0.46.0 or
  newer).
- The `meho` command line, the same version as the install. See
  [Tools for your laptop](laptop-tools.md#step-2-install-the-meho-command-line).

## Step 0: The variables

Run this in every terminal you use, after you have logged in to the
cluster (`oc login`). Change only the first line, and only if you want
another project name, release name or version.

```bash
export NS=meho REL=meho VER=0.45.0
export APPS=$(oc whoami --show-console | sed -E 's#^https://[^.]+\.##')
export HOST=meho.$APPS
export API=$(oc whoami --show-server)
export W=$HOME/meho-install-$NS
mkdir -p "$W" && cd "$W"
echo "project=$NS release=$REL version=$VER"; echo "apps=$APPS"; echo "host=$HOST"; echo "api=$API"
```

- `APPS` is the apps domain of the cluster. OpenShift's router answers
  every name under it, so `HOST` needs no DNS record of its own. The
  command takes it from the address of the web console, which every
  logged-in user may read.
- `W` is a folder for the files of this install: values files, scripts
  and the certificate. Every step works in it.

You should see four lines like:

```text
project=meho release=meho version=0.45.0
apps=apps.<your cluster>.<your domain>
host=meho.apps.<your cluster>.<your domain>
api=https://api.<your cluster>.<your domain>:6443
```

If `apps=` is empty, `oc` is not logged in. To use a name outside the
apps domain, set `HOST` by hand and make a DNS record for it that points
to the router.

## Step 1: What the cluster admin does first

The cluster admin does this once, logged in as a cluster admin.

```bash
oc whoami
kubectl auth can-i '*' '*' --all-namespaces
```

You should see your admin user name, then `yes`.

### 1.1 The project

```bash
kubectl create namespace $NS
sleep 5; kubectl get namespace $NS --show-labels
```

You should see `namespace/meho created`, then the project with labels
that OpenShift added, among them `pod-security.kubernetes.io/audit=restricted`.
Leave the labels as they are. Every pod of MEHO runs under the security
rule `restricted-v2` (OpenShift's security context constraint for normal
pods), so you do not need to relax anything. Do not give the project's
accounts the `anyuid` or `privileged` rule.

### 1.2 Rights for the installer

The installer gets the built-in role `admin` for the project only. The
role allows almost everything inside the project, but nothing outside
it.

Do **one** of the two ways below, A or B. Then check the rights at the
end of this step.

**A. A person with their own OpenShift login** (the usual way). See which
users exist:

```bash
kubectl get users.user.openshift.io
```

A user shows up here after their first login. Pick the installer, and
give them the role:

```bash
export INSTALLER=<a user name from the list>
kubectl -n $NS create rolebinding $NS-installer-admin --clusterrole=admin --user=$INSTALLER
```

You should see `rolebinding.rbac.authorization.k8s.io/meho-installer-admin created`.

The installer then logs in with their own login. `oc login` makes this
login the current one, so keep the name of the admin's login first:

```bash
export ADMIN_CONTEXT=$(kubectl config current-context)
oc login $API -u $INSTALLER
```

Back to the admin login later with `kubectl config use-context $ADMIN_CONTEXT`.

**B. An account, only when no person login fits.** Skip this when you did A.

```bash
kubectl -n $NS create serviceaccount $NS-installer
kubectl -n $NS create rolebinding $NS-installer-admin \
  --clusterrole=admin --serviceaccount=$NS:$NS-installer
umask 077
TOKEN=$(kubectl -n $NS create token $NS-installer --duration=8h)
KC=$W/installer.kubeconfig
kubectl config --kubeconfig=$KC set-cluster ocp --server="$API"
kubectl config view --minify --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' | base64 -d > $W/cluster-ca.crt
[ -s $W/cluster-ca.crt ] && kubectl config --kubeconfig=$KC set-cluster ocp --certificate-authority=$W/cluster-ca.crt --embed-certs=true
kubectl config --kubeconfig=$KC set-credentials installer --token="$TOKEN"
kubectl config --kubeconfig=$KC set-context installer --cluster=ocp --user=installer --namespace=$NS
kubectl config --kubeconfig=$KC use-context installer
unset TOKEN; rm -f $W/cluster-ca.crt
export KUBECONFIG=$KC
```

The token ends after 8 hours. If your admin login has no certificate
authority in its kubeconfig (the `[ -s ... ]` line then does nothing),
your laptop already trusts the API server's certificate, and the
installer's laptop must trust it too.

**Check the installer's rights** (after A or B), with the installer's login:

```bash
kubectl -n $NS auth can-i create statefulsets
kubectl auth can-i list nodes
```

You should see `yes`, then `no`. That is right: the installer may do
everything in the project, and nothing outside it.

### 1.3 A storage class

MEHO's two disks (PostgreSQL and OpenBao, 10 GiB each) need a default
storage class, or a class named in a values file.

```bash
kubectl get storageclass
```

If one class shows `(default)` after its name, go on. If none does,
either mark one as the default:

```bash
kubectl patch storageclass <name> \
  -p '{"metadata": {"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}'
```

or give its name to the installer for [Step 3.2](#32-the-files-you-need-only-in-some-cases).

### 1.4 The certificate

**For a test: skip this step.**

MEHO answers on HTTPS at `https://$HOST`, and HTTPS needs a certificate
for that name. There are two ways to get one:

- **Self-signed (a test).** You do nothing here. The install makes its
  own certificate for `$HOST`, in the Secret `$REL-core-tls`. No laptop
  trusts it by itself, so [Step 5.2](#52-trust-the-certificate) saves it
  to a file and makes `curl` and your laptop trust it.
- **Your company's certificate (a real install).** Your security or PKI
  team gives you a certificate for `$HOST` as two files: `tls.crt`
  (the certificate) and `tls.key` (its private key). Browsers that
  trust your company's authority then trust MEHO with no extra step.

Only for your company's certificate, load the two files into the
project:

```bash
kubectl -n $NS create secret tls meho-tls --cert=tls.crt --key=tls.key
```

You should see `secret/meho-tls created`. The installer may do this too.
If the certificate comes from an internal certificate authority, every
laptop must trust it: see
[Tools for your laptop](laptop-tools.md#step-1-trust-the-sites-own-certificate-authority).

## Step 2: With no internet: your own registry

Skip this step when the cluster's nodes can pull images from the
internet (`ghcr.io`, `docker.io` and `quay.io`). To find out, look at
any running pod: `kubectl get pods -A -o jsonpath='{.items[*].spec.containers[*].image}' | tr ' ' '\n' | grep -E 'quay.io|ghcr.io|docker.io' | head -3`
shows whether the cluster already runs public images. Your cluster
admin knows for sure.

Set the address of your registry, with the project path in it:

```bash
export REG=<registry host>/<project>
export REG_HOST=${REG%%/*}
```

### 2.1 The list of images

```bash
helm pull oci://ghcr.io/evoila/meho-chart --version $VER --untar -d $W
cat $W/meho-chart/images.txt
```

You should see five lines. Each line has an image, fixed by its digest,
and its path in your registry. For chart 0.45.0:

```text
busybox@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e library/busybox:1.37
pgvector/pgvector@sha256:ac08538c6f8b9904c33c8224c5e5706dbe760aca29db1d096972b4052c22a75d pgvector/pgvector:0.8.7-pg17-bookworm
quay.io/keycloak/keycloak@sha256:79c623a22abacf3fe6c00a5fb30f5b7843d717b234033b80995891c06116d244 keycloak/keycloak:26.8.1
quay.io/openbao/openbao-ubi@sha256:33c4c3a7aeb8b812d570939bd7275f829901031e0a6b3e655207d8f0945bd248 openbao/openbao-ubi:2.7.1
valkey/valkey@sha256:2d04268dfa26ab31cfa3fdc538866e76438b2d00e41db41b8cb86d780ac5a416 valkey/valkey:9.0-alpine
```

The list has every image of the chart except MEHO's own. You copy that
one too.

### 2.2 Check what your registry already has

Log in with any login that may pull, then check every image:

```bash
skopeo login $REG_HOST
while read -r source path; do
  skopeo inspect --raw "docker://$REG/${path%%:*}@${source##*@}" >/dev/null 2>&1 \
    && echo "OK      $path" || echo "MISSING $path"
done < $W/meho-chart/images.txt
skopeo inspect --raw docker://$REG/evoila/meho:v$VER >/dev/null 2>&1 \
  && echo "OK      evoila/meho:v$VER" || echo "MISSING evoila/meho:v$VER"
```

Every line should say `OK`. If all say `MISSING`, check the login first:
run one `skopeo inspect` without `>/dev/null 2>&1`. `unauthorized`
means the login is wrong.

### 2.3 Copy the missing images

This needs a registry login that may push. Run it on a machine that
reaches both the internet and your registry, close to the registry: the
MEHO image has one layer of about 240 MB, and a slow or cut connection
stops the upload. `--all` and `--preserve-digests` keep each image's
digest, which the chart pins.

```bash
skopeo login $REG_HOST
while read -r source path; do
  skopeo copy --all --preserve-digests --retry-times 5 "docker://$source" "docker://$REG/$path"
done < $W/meho-chart/images.txt
skopeo copy --all --preserve-digests --retry-times 5 docker://ghcr.io/evoila/meho:v$VER docker://$REG/evoila/meho:v$VER
```

Then run the check of Step 2.2 again. Every line should say `OK`. A
copy of an image that is already there only adds what is missing.

### 2.4 The nodes must trust your registry

The cluster admin does this. If your registry has a certificate from an
internal authority, the nodes must trust it. Otherwise every pull fails
with `x509: certificate signed by unknown authority`. See what the
cluster trusts today:

```bash
kubectl get image.config.openshift.io/cluster -o jsonpath='{.spec.additionalTrustedCA.name}{"\n"}'
```

**If it prints nothing**, put the registry's authority (`registry-ca.crt`)
into a new ConfigMap and name it there. The key is the registry's host
name. With a port, the port comes after two dots instead of a colon,
for example `harbor.example.com..5000`: the first line makes that key.

```bash
REG_KEY=${REG_HOST/:/..}; echo "key: $REG_KEY"
kubectl -n openshift-config create configmap registry-ca --from-file=$REG_KEY=registry-ca.crt
kubectl patch image.config.openshift.io/cluster --type=merge \
  -p '{"spec":{"additionalTrustedCA":{"name":"registry-ca"}}}'
```

**If it prints a name**, add your key (`$REG_KEY`, with the two dots) to
that ConfigMap instead:
`kubectl -n openshift-config edit configmap <that name>`.

The nodes take the change without a restart, within a few minutes.

### 2.5 The pull secret

The installer makes this in the project, with a registry login that may
only pull. `read` keeps the password out of the screen and out of your
shell history.

```bash
read -r 'PULL_USER?Pull user: ' 2>/dev/null || read -rp 'Pull user: ' PULL_USER
read -rs 'PULL_PASSWORD?Pull password: ' 2>/dev/null || read -rsp 'Pull password: ' PULL_PASSWORD; echo
kubectl -n $NS create secret docker-registry registry-pull \
  --docker-server=$REG_HOST --docker-username="$PULL_USER" --docker-password="$PULL_PASSWORD"
unset PULL_USER PULL_PASSWORD
```

The two `read` lines work in zsh and in bash. You should see
`secret/registry-pull created`.

## Step 3: The values files

From here on, use the installer's login. Check it, and that the
variables of Step 0 are set:

```bash
oc whoami; echo "$NS $HOST $VER"; cd "$W"
```

Every values file in this step goes into `$W`.

### 3.1 The two files everyone needs

`values-openshift.yaml` is the same for every OpenShift cluster. Take
the one of the version you install:

```bash
curl -fsSLO https://raw.githubusercontent.com/evoila/meho/v$VER/deploy/values-examples/values-openshift.yaml
cat values-openshift.yaml
```

This file:

- sets `global.openshift: true`. OpenShift gives every pod a user id
  from the project's range, and it refuses a pod that asks for a fixed
  one. With this setting, no pod of the chart asks for one;
- sets the ingress class `openshift-default`. OpenShift's router turns
  MEHO's Ingress into Routes, so you install no ingress controller;
- names the Secret `meho-core-tls` for the certificate, the one the
  install makes when you bring none. Your `values.yaml` below sets the
  same name with your release name, `$REL-core-tls`;
- turns the network rules off (#4050). In a data center the router runs
  on the host network, so the rules would block it. MEHO can then reach
  every network the cluster can reach.

`values.yaml` holds your install's own values. The command fills in the
host and makes a new tenant id, a UUID you keep for the life of the
install:

```bash
cat > values.yaml <<EOF
image:
  tag: v$VER
ingress:
  host: $HOST
  tls:
    secretName: $REL-core-tls
coreKeycloak:
  hostname: $HOST
  firstTenantId: "$(uuidgen | tr A-Z a-z)"
uiConsole:
  enabled: true
  secretName: $REL-core-ui-auth
config:
  uiKeycloakClientId: meho-web
EOF
cat values.yaml
```

You should see your host name twice, a UUID, and two Secret names that
start with your release name. The chart makes those two Secrets itself
only under these names. The `uiConsole` and `config` lines turn on the
web console at `/ui/`.

### 3.2 The files you need only in some cases

Run only the blocks that apply to you. Each one writes one file into
`$W`. Step 3.4 picks up the files that are there.

**Your own certificate** (Step 1.4):

```bash
printf 'ingress:\n  tls:\n    secretName: meho-tls\n' > values-tls.yaml
```

**No internet** (Step 2):

```bash
cat > values-registry.yaml <<EOF
global:
  imageRegistry: $REG
  imagePullSecrets:
    - name: registry-pull
EOF
```

**No default storage class** (Step 1.3):

```bash
export SC=<the storage class name>
cat > values-storage.yaml <<EOF
corePostgres:
  persistence:
    storageClass: $SC
coreVault:
  persistence:
    storageClass: $SC
EOF
```

**A test install** with the check of [Step 5.5](#55-the-install-check-with-a-login):
this makes a service login for the checks. Do not use it on a real
install.

```bash
cat > values-ci.yaml <<EOF
coreKeycloak:
  ci:
    enabled: true
    tenantId: "$(uuidgen | tr A-Z a-z)"
EOF
```

### 3.3 Only for v0.46.0 or newer: the OpenBao image

From v0.46.0 on, set the UBI OpenBao image. This takes the tag of the
chart's own OpenBao image and the digest of its UBI version:

```bash
[ -d $W/meho-chart ] || helm pull oci://ghcr.io/evoila/meho-chart --version $VER --untar -d $W
BAO_TAG=$(sed -n 's/^  tag: "\(.*\)"/\1/p' $W/meho-chart/charts/openbao/values.yaml | head -1)
echo "OpenBao tag: $BAO_TAG"
BAO_DIGEST=sha256:$(skopeo inspect --raw docker://quay.io/openbao/openbao-ubi:$BAO_TAG | shasum -a 256 | cut -d' ' -f1)
cat > values-openbao-ubi.yaml <<EOF
coreVault:
  image:
    repository: quay.io/openbao/openbao-ubi
    tag: "$BAO_TAG"
    digest: "$BAO_DIGEST"
EOF
cat values-openbao-ubi.yaml
```

`OpenBao tag:` must show a version, for example `2.7.1`. If it is
empty, look for `tag:` under `image:` in
`$W/meho-chart/charts/openbao/values.yaml`, and set `BAO_TAG` by hand.
The UBI image needs a CPU with x86-64-v3. See
[OpenBao unseal key](openbao-unseal-key.md#which-image-and-which-cpu).
With no internet, copy this image to your registry too (Step 2.3).

### 3.4 Check the files together

This collects the files of Step 3.2 and 3.3 that you made, in the list
`EXTRA`, and renders the install without changing anything:

```bash
EXTRA=()
for f in values-tls.yaml values-registry.yaml values-storage.yaml values-ci.yaml values-openbao-ubi.yaml; do
  [ -f "$f" ] && EXTRA+=(-f "$f")
done
echo "extra files: ${EXTRA[*]}"
helm template $REL oci://ghcr.io/evoila/meho-chart --version $VER -n $NS \
  -f values-openshift.yaml -f values.yaml "${EXTRA[@]}" | grep -E '^\s+image:' | sort -u
```

You should see the files you made after `extra files:` (or nothing),
then the image list of the install, and no error. With your own
registry, every image starts with `$REG`. Run Step 4 in this same
terminal: it uses `EXTRA`.

## Step 4: Install

```bash
export INSTALL_START_TS=$(date -u +%s)
helm install $REL oci://ghcr.io/evoila/meho-chart --version $VER -n $NS \
  -f values-openshift.yaml -f values.yaml "${EXTRA[@]}" \
  --wait --wait-for-jobs --timeout 20m
```

`values-openshift.yaml` comes first, so your own files win where they
set the same value. Do not add `--create-namespace`: the project exists,
and the installer may not create one.

To watch it, open a second terminal, run Step 0 there, and:

```bash
kubectl -n $NS get pods -w
```

What happens, in order:

1. A setup Job makes every password and key, and keeps them in Secrets
   named `$REL-core-*`. Without your own certificate, it also makes the
   self-signed one in `$REL-core-tls`.
2. PostgreSQL, Keycloak, OpenBao, Valkey (the store of the live
   activity) and MEHO start. A second Job sets up OpenBao.
3. MEHO's database migrations end, and MEHO starts.
4. Keycloak loads the realm `meho` and gets ready.
5. A third Job makes the first user in Keycloak.

When Helm ends, you should see `STATUS: deployed` and `REVISION: 1`,
then the install notes. The first start of MEHO can take up to 15
minutes, because MEHO builds its search data on an empty database. A
pod that is `Running` but not `Ready` for some minutes is normal then.

**If a pod does not appear at all**, OpenShift may have refused it. Look
for the reason:

```bash
kubectl -n $NS get events | grep -iE 'security context constraint|forbidden'
```

This should print nothing. See [Common problems](#common-problems) if it
does.

## Step 5: Check the install

### 5.1 Pods, disks and the way in

```bash
kubectl -n $NS get pods
kubectl -n $NS get pvc
kubectl -n $NS get ingress,route
```

You should see, with `REL=meho` (other release names change the start
of each name):

```text
NAME                       READY   STATUS      RESTARTS   AGE
meho-<id>                  1/1     Running     0          4m
meho-broadcast-<id>        1/1     Running     0          4m
meho-keycloak-0            1/1     Running     0          4m
meho-postgres-0            2/2     Running     0          4m
meho-vault-0               2/2     Running     0          4m
meho-vault-init-<id>       0/1     Completed   0          4m
```

`<id>` is a random part of the name, different in every install.

Both disks, `data-meho-postgres-0` and `data-meho-vault-0`, are `Bound`
with 10Gi. The Ingress `meho` has the class `openshift-default` and
your host. Next to it, the router made one Route for each path of the
Ingress: `/`, `/auth/realms/meho` and `/auth/resources`, each with
`edge/Redirect`.

Check that OpenShift gave every pod a user id from the project's range,
under `restricted-v2`:

```bash
kubectl -n $NS get pods -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.metadata.annotations.openshift\.io/scc}{"\t"}{.spec.containers[0].securityContext.runAsUser}{"\n"}{end}'
```

Every line shows `restricted-v2` and a large number, the same for every
pod (for example `1000750000`). It is never `999` or `1001`.

### 5.2 Trust the certificate

**With the self-signed certificate**, save its authority:

```bash
kubectl -n $NS get secret $REL-core-tls -o jsonpath='{.data.ca\.crt}' | base64 -d > $W/meho-ca.crt
CA=(--cacert $W/meho-ca.crt)
```

Then add `$W/meho-ca.crt` to the trust store of your laptop. `meho login`
in Step 6 needs that: `curl` gets `--cacert`, but `meho` uses the
system's trust store only. The file is a public certificate, not a
secret.

On a Mac (it asks for your Mac password):

```bash
sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain $W/meho-ca.crt
```

On Debian or Ubuntu:

```bash
sudo cp $W/meho-ca.crt /usr/local/share/ca-certificates/meho-ca.crt && sudo update-ca-certificates
```

Other systems: see
[Tools for your laptop](laptop-tools.md#step-1-trust-the-sites-own-certificate-authority).

**With a certificate your laptop already trusts:**

```bash
CA=()
```

### 5.3 Health, through the router

```bash
curl -s "${CA[@]}" https://$HOST/healthz; echo
curl -s "${CA[@]}" https://$HOST/ready | jq -c '[.status, (.checks[] | "\(.name)=\(.ok)")]'
```

You should see:

```text
{"status":"ok"}
["ready","keycloak=true","vault=true","db=true","broadcast=true","docs_backends=true"]
```

If both lines print nothing, `curl` does not trust the certificate:
check Step 5.2.

### 5.4 The install check

`install-verify.sh` runs the install checks of a release against your
install. Take the script of your version:

```bash
curl -fsSLO https://raw.githubusercontent.com/evoila/meho/v$VER/scripts/acceptance/install-verify.sh
COMMIT=$(helm show chart oci://ghcr.io/evoila/meho-chart --version $VER | sed -n 's/^appVersion: //p' | tr -d '"')
bash install-verify.sh --host $HOST --namespace $NS --release $REL --expected-git-sha "$COMMIT" "${CA[@]}"
```

You should see six lines that start with `[ok]`, and at the end:

```text
install-verify.sh: 6 passed, 0 failed, 1 warned
[ok]   MEHO is up
```

The warning `wall-clock ... > budget 300s` is fine: it measures the time
since Step 4, which is longer than 5 minutes when you install by hand. If the first two checks fail but the HTTP checks pass,
your `oc login` has ended: see [Common problems](#common-problems).

### 5.5 The install check with a login

Only with the file `values-ci.yaml` of Step 3.2. This gets a token for
the login `meho-ci` and runs the check again. The token lasts 5
minutes, so run the block in one go.

```bash
export MEHO_ACCESS_TOKEN=$(printf 'grant_type=client_credentials&client_id=meho-ci&client_secret=%s' \
  "$(kubectl -n $NS get secret $REL-core-ci-login -o jsonpath='{.data.client_secret}' | base64 -d)" \
  | curl -sS "${CA[@]}" -d @- https://$HOST/auth/realms/meho/protocol/openid-connect/token \
  | jq -r .access_token)
echo ${#MEHO_ACCESS_TOKEN}
bash install-verify.sh --host $HOST --namespace $NS --release $REL --expected-git-sha "$COMMIT" "${CA[@]}"
unset MEHO_ACCESS_TOKEN
```

The `echo` prints a number well over 500. If it prints `4`, the token is
`null`. Then you should see `7 passed, 0 failed`, with the line
`GET /api/v1/health (authenticated) returns 200`.

## Step 6: The first user, `meho login`, `meho status`

The install made one Admin login with a temporary password (`admin`, in
`MEHO-Admins` and `MEHO-Platform-Admins`).
Write the login into a file that only you can read:

```bash
umask 077
kubectl -n $NS get secret $REL-core-first-login \
  -o go-template='{{range $k,$v := .data}}{{$k}}: {{$v | base64decode}}{{"\n"}}{{end}}' > $W/first-login.txt
cat $W/first-login.txt
```

You should see two lines: `admin_password` and `admin_username`.

The first login asks for a new password, a name and an email.

### 6.1 Log in as `admin` on the command line

Keep this login apart from any other MEHO you use. The command line
then keeps it in its own folder:

```bash
export XDG_CONFIG_HOME=$W/cli MEHO_KEYRING_DISABLE=1
meho login https://$HOST
```

The command prints an address and a code. Open the address in your
**normal** browser window, enter the code and sign in as `admin` with
its temporary password. Then you should see
`Logged in to https://meho.apps...`.

Keep the two `export` settings in this terminal for every later `meho`
command. Check the whole chain:

```bash
meho status
```

You should see, among other lines:

```text
Logged in as <your email> (sub: ...)
  Vault: reachable, read OK (version=1)
  Readiness: ready (keycloak, vault, db, broadcast, docs_backends ok)
```

`Vault: read OK` means MEHO logged in to OpenBao with your login and read
a secret. A warning that the command line is newer or older than the
install is harmless, but keep both at the same version when you can.

### 6.2 Log in as `admin` in the web console

Go to this address and sign in as `admin` with the password you set in 6.1:

```bash
echo https://$HOST/ui/
```

The install notes also name a third login, the **console admin**: a
user of Keycloak's own realm `master`, for Keycloak's admin page (for
example to connect a company directory). MEHO itself refuses this
login. Its user name and temporary password are in the Secret
`$REL-core-console-admin`. You do not need it for this guide.

### 6.3 Delete the login file

Keep the new password in your key store, then delete the file:

```bash
rm -P $W/first-login.txt 2>/dev/null || shred -u $W/first-login.txt
```

## Step 7: The OpenBao unseal key

OpenBao, MEHO's built-in password safe, needs its unseal key after
every restart. The install keeps the key in the Secret
`$REL-core-vault-unseal`, so OpenBao unseals itself.

### 7.1 Save the key

Copy the key to your own key store now, for the day the Secret is lost:

```bash
umask 077
kubectl -n $NS get secret $REL-core-vault-unseal -o jsonpath='{.data.key}' | base64 -d > $W/unseal-key.txt
```

Copy the key from `$W/unseal-key.txt` into your key store, then delete
the file the same way as `first-login.txt`. Keep the Secret in the
cluster. To copy the key without a file, and to choose who unseals
OpenBao, see [OpenBao unseal key](openbao-unseal-key.md#save-the-key-now).

### 7.2 Check that OpenBao unseals itself

This deletes the OpenBao pod and asks MEHO's `/ready` every 2 seconds
until it is green again. It stops after 3 minutes at the latest.

```bash
T0=$(date +%s); kubectl -n $NS delete pod $REL-vault-0 --wait=false; red=
while :; do
  code=$(curl -s -o /dev/null -w '%{http_code}' "${CA[@]}" https://$HOST/ready)
  echo "$(( $(date +%s) - T0 ))s  /ready=$code"
  [ "$code" != 200 ] && red=1
  [ -n "$red" ] && [ "$code" = 200 ] && break
  [ $(( $(date +%s) - T0 )) -gt 180 ] && { echo "TIMEOUT"; break; }
  sleep 2
done
```

You should see some lines with `503`, then `/ready=200`, within 2
minutes.

### 7.3 The full unseal check

The check script restarts OpenBao several times and takes a few minutes.
MEHO's `/ready` is red now and then while it runs.

```bash
curl -fsSLO https://raw.githubusercontent.com/evoila/meho/v$VER/scripts/ci/check-openbao-unseal.sh
bash check-openbao-unseal.sh $NS $REL
```

It checks that OpenBao unseals itself while the Secret exists, stays
sealed while it is gone, keeps no copy of the key, and that the key
shows in no pod log and in no Helm output. It always puts the Secret
back. Every line starts with `ok:`, and the last line is `PASS`.

**MEHO now runs.** Go on with
[the setup page and the settings file](setup-page.md).

## Step 8: Remove the install fully

`helm uninstall` leaves three kinds of objects behind: the two disks,
the Secrets with the passwords and keys, and the objects of the setup
Jobs. Delete them together, or none of them. New passwords do not match
old data, and old passwords do not match a new database.

The Secrets go by the label of the setup Job, which every MEHO install
uses. So with two installs in one project, this also deletes the other
one's Secrets: remove the whole project instead (below).

```bash
helm uninstall $REL -n $NS --wait
kubectl -n $NS delete pvc -l app.kubernetes.io/instance=$REL
kubectl -n $NS delete secret -l meho.evoila.com/managed-by=core-bootstrap
kubectl -n $NS delete configmap,serviceaccount,role,rolebinding -l app.kubernetes.io/instance=$REL
kubectl -n $NS get all,pvc,secrets,configmaps,serviceaccounts,roles,rolebindings
```

You should see `release "meho" uninstalled` (with your release name),
then the two disks, the `$REL-core-*` Secrets and the objects of the
setup Jobs deleted. The
Routes go with the Ingress. The last command shows only:

- what Step 1 and Step 2 made: the installer's RoleBinding (and
  account), the pull secret and the TLS Secret;
- what OpenShift puts in every project: the accounts `builder`,
  `default` and `deployer` with their Secrets and RoleBindings, and the
  ConfigMaps `kube-root-ca.crt` and `openshift-service-ca.crt`.

Leave those. Remove `$W/meho-ca.crt` from your laptop's trust store
(on a Mac: `sudo security remove-trusted-cert -d $W/meho-ca.crt`), and
delete `$W` when you no longer need it.

**To start from an empty cluster again**, the cluster admin deletes the
whole project. That also removes what Step 1 and Step 2 made in it:

```bash
kubectl delete namespace $NS --wait
kubectl get pv | grep " $NS/" || echo "no disk left"
```

## Common problems

| What you see | Why, and what to do |
|---|---|
| `install-verify.sh` fails `no Deployment matching ...` and `helm release status='unknown'`, but the HTTP checks pass | Your `oc login` has ended: an OpenShift login lasts 24 hours by default. `oc whoami` then says `Unauthorized`. Run `oc login $API -u <your user>` again, then the check again. |
| `apps=` is empty in Step 0 | `oc` is not logged in. Run `oc login $API` (or the **Copy login command** of the web console), then Step 0 again. |
| A pod never appears; an event says `unable to validate against any security context constraint` | A pod asks for a fixed user id. Check that you passed `values-openshift.yaml` first in Step 4 (`global.openshift: true`). Do not give the project `anyuid`. |
| `$REL-vault-0` does not start, or restarts again and again | With v0.46.0 or newer: the plain OpenBao image does not run with OpenShift's user ids. Make the file of Step 3.3. `kubectl -n $NS logs $REL-vault-0 -c bao --tail=20` shows the reason. |
| `$REL-vault-0` stops with `CPU does not support x86-64-v3` | The UBI OpenBao image needs a newer CPU. See [OpenBao unseal key](openbao-unseal-key.md#which-image-and-which-cpu). |
| A pod stays in `ImagePullBackOff` | `kubectl -n $NS describe pod <pod> \| tail -5` shows the reason. `i/o timeout`: no internet, do Step 2. `unauthorized`: the pull login (Step 2.5). `not found`: an image is missing (Step 2.2). |
| `x509: certificate signed by unknown authority` in a pull | The nodes do not trust your registry (Step 2.4). |
| The copy of the MEHO image stops with `499` or `504` | A proxy in front of your registry cuts the long upload of its 240 MB layer. Run the copy close to the registry, or raise the proxy's timeout. |
| `curl` prints nothing, `meho login` fails with a certificate error | Your laptop does not trust the certificate (Step 5.2). |
| An OpenShift page "Application is not available" | The router has no Route for the name. Check `kubectl -n $NS get ingress,route`: the host must equal `$HOST`, and the class must be `openshift-default`. |
| `https://$HOST/ui/` shows an error 503 | The web console is off. Check the `uiConsole` lines of `values.yaml` (Step 3.1), then run `helm upgrade` with the same files as Step 4. |
| `helm install` stops with `pre-install hooks failed` and a `migrate` Job | The database update ran before PostgreSQL was ready. Run Step 4 again with `helm upgrade` in place of `helm install`. |
| `meho` talks to the wrong MEHO | `XDG_CONFIG_HOME` and `MEHO_KEYRING_DISABLE` of Step 6 are not set in this terminal. |
