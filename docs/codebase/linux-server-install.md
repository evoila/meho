# Form-2 install script: `deploy/linux-server/install.sh` (#4733, #4735)

## Overview

Install form 2 puts MEHO on a plain Linux server that has no internet. The
customer copies one download onto the machine and runs one script as root:
`install.sh`. The design is in meho-internal
`docs/design/2026-10-10-install-forms/form-2-linux-server.md`. Section 4
gives the layout of the download, and section 5 gives the steps.

The script runs the six steps of the design:

1. **Check the machine.** Each check prints `[ok]` or `[WARN]`, or stops the
   script with exactly one `[FAIL]` line. The line says what is wrong and
   what to fix. Nothing on the machine changes in this step.
2. **Prepare the machine.** It creates `/var/lib/meho` and `/etc/meho`
   (both mode 0700), writes RKE2's settings, copies the join token when
   the join options are given, and copies the MEHO image archive and the
   storage manifest to the folders where RKE2 reads them.
3. **Install and start RKE2.** It runs RKE2's own installer in offline
   mode, starts `rke2-server`, and waits until the node is Ready, until
   `local-path` is the default storage class, and until the storage
   provisioner runs.
4. **Install the MEHO core.** It writes the answers of this place to
   `/etc/meho/values-place.yaml` and runs the download's Helm with the
   chart, `values-single-node.yaml` and that file. Helm waits until every
   pod and Job is done.
5. **Write the two one-time files.** The first-login passwords and the
   unseal key go from two Kubernetes Secrets into two files that only root
   can read.
6. **Install the `meho` command, write the install record and print the
   result:** the console address, the paths of the two files, and the
   first-start list (not in this version yet).

`--from-disk --hostname <fqdn>` runs only the steps that need the host name:
RKE2's settings, the start of RKE2, and steps 4 to 6. It is for a machine
where RKE2's own state was removed on purpose, but its files, the chart and
the image archives are still on the disk. The appliance's first start uses
it (form-1 design, section 2).

Exit codes: 0 when MEHO is installed, 1 when a check or a step failed, 2 for
wrong usage (an unknown option or a bad value).

The download itself, `meho-linux-<version>-amd64.tar`, is built by
`build-download.sh` (#4734); see
[linux-server-download.md](linux-server-download.md).

Later Tasks add their steps to `main()` in `install.sh`:

- #4635 adds the host firewall to step 2.
- #4636 adds the CIS host steps between installing and starting RKE2, and
  `profile: cis` with the storage exception in RKE2's settings.
- #4042 adds the first-start list to step 6, read with
  `meho status --first-start`.
- #4736 makes a second run an update, which reads `install.json` and
  `values-place.yaml`.
- #4737 adds `meho-host`, which `install.sh` installs.

## Key files

| File | What it holds |
| --- | --- |
| `install.sh` | Options, `--help`, the order of the steps, `--from-disk`, the error trap. |
| `lib/common.sh` | Machine paths, the lists of download files, the Helm release name, `ok`, `warn`, `fail` and `usage_error`. |
| `lib/net.sh` | IPv4 helpers: the format of an address or a range, and whether two ranges overlap. |
| `lib/checks.sh` | Step 1: one function per check. |
| `lib/prepare.sh` | Step 2: folders, `config.yaml`, join token, image archive, storage manifest. |
| `lib/rke2.sh` | Step 3: RKE2's installer, the start, and the wait with one time limit. |
| `lib/core.sh` | Steps 4 to 6: the versions of the download, the place values, the Helm call, the two one-time files, the `meho` command, the install record, the result lines. |
| `values-single-node.yaml` | The Helm values for one node. The download ships it as `chart/values-single-node.yaml`. |
| `storage/local-path-storage.yaml` | Rancher's local-path provisioner v0.0.37 manifest with three changes. |
| `tests/` | Python `unittest` suites that run `install.sh` against a fake machine. |
| `.shellcheckrc` | Lets shellcheck follow the `source` lines of `install.sh`. |

The files in `lib/` are sourced, not run. Functions get their inputs as
arguments. Global names are in upper case, which keeps shellcheck quiet
about names that another file defines.

## Control flow

### Step 1: the checks, in order

| Check | Passes when | Source of the facts |
| --- | --- | --- |
| root | `id -u` is 0 | `id` |
| operating system | x86-64 and systemd. Ubuntu 24.04 is `[ok]`; another Linux gets `[WARN] not tested` and the install goes on | `uname -m`, `/run/systemd/system`, `/etc/os-release` |
| download | every file of `DOWNLOAD_FILES` is next to `install.sh`, and the chart file that `VERSION` names exists | the download folder |
| processors | at least 4 online | `getconf _NPROCESSORS_ONLN` |
| memory | `MemTotal` at least 7 GiB, which a machine with 8 GB shows | `/proc/meminfo` |
| disk | at least 80 GB free for `/var/lib` | `df -P -k /var/lib` |
| other runtime | none of `k3s`, `rke2`, `kubelet`, `containerd`, `dockerd`, `crio` is on `PATH` or runs | `command -v`, `pgrep -x` |
| ports | TCP 80, 443, 6443, 9345, 10250, 2379, 2380, 2381, 9099 and UDP 8472 are free | `ss -H -l -n` |
| clock | the clock is synchronised | `timedatectl show --property=NTPSynchronized` |
| host name | a fully qualified name; default `hostname -f`, in lower case | `--hostname` |
| DNS | the name has an A record in DNS; `/etc/hosts` does not count | see below |
| address | the record points to an address of this machine, or to `--public-address` | `ip -4 -o addr show scope global` |
| ranges | the pod and service ranges do not overlap each other, the machine's addresses or its routes (the default route is left out) | `ip -4 -o addr show`, `ip -4 route show` |

The place answers are checked before any step runs, as wrong usage (exit
code 2): `--first-tenant-id` must be a UUID, `--admin-user` and
`--approver-user` take letters, digits and `. _ - @` and must differ. So
the values go into the Helm values and the install record as they are,
with no escaping.

**Why DNS needs care.** On Ubuntu, `systemd-resolved` answers from
`/etc/hosts` too, also through its stub at 127.0.0.53. So when
`systemd-resolved` runs, the check asks with
`resolvectl query --synthesize=no --cache=no -4`. Without it, the check asks
`getent -s dns ahostsv4`, which uses only the `dns` source of the name
service.

### Step 2: what is written

| Path | Mode | Content |
| --- | --- | --- |
| `/var/lib/meho`, `/etc/meho` | 0700 | folders |
| `/etc/rancher/rke2/config.yaml` | 0600 | `write-kubeconfig-mode: "0600"`, `tls-san`, `ingress-controller: traefik`, `cluster-cidr`, `service-cidr`; with the join options also `server` and `token-file` |
| `/etc/meho/join-token` | 0600 | a copy of `--join-token-file`, only with the join options |
| `/var/lib/rancher/rke2/agent/images/meho-images.linux-amd64.tar.zst` | 0600 | the MEHO image archive |
| `/var/lib/rancher/rke2/server/manifests/local-path-storage.yaml` | 0600 | the storage manifest |

The join token goes from file to file with `install`. It is never on a
command line, in the output or in `config.yaml`.

RKE2's installer puts RKE2's own image archive into the images folder
itself, after it checks the archive against `sha256sum-amd64.txt`. So the
script copies only the MEHO archive.

### Step 3: start and wait

`install_rke2` runs `INSTALL_RKE2_ARTIFACT_PATH=<download>/rke2 sh <download>/rke2/install.sh`.

`start_rke2` runs `systemctl enable --now --no-block rke2-server.service`.
The unit sets `TimeoutStartSec=0`, so a start that waits for the service
could wait forever. With `--no-block` the script sets the time limit itself.

`wait_for_rke2` polls RKE2's own kubectl
(`/var/lib/rancher/rke2/bin/kubectl --kubeconfig /etc/rancher/rke2/rke2.yaml`)
every 5 seconds, for 15 minutes in total, for three things in order: the
node is Ready, `local-path` carries the default-class annotation, and the
provisioner Deployment has an available replica. On the time limit the
script prints one `[FAIL]` line that names what did not happen and the
command that shows RKE2's log.

### Step 4: the core

`write_place_values` writes `/etc/meho/values-place.yaml` (form-2 design,
section 6): the host name as `ingress.host` and `coreKeycloak.hostname`,
the first tenant's id, the two user names, and the pod range as the two
network-policy ranges, so MEHO may reach the built-in PostgreSQL and
OpenBao. Without `--first-tenant-id`, the id comes from an existing
`values-place.yaml`, so a second run keeps the id Keycloak already uses;
on a fresh machine it is a new UUID read once from
`/proc/sys/kernel/random/uuid`. The two network-policy ranges are wiring
until the chart derives them from its built-in parts (#4047), not a place
answer; the update (#4736) removes them from existing place files. The
file holds no secret.

`install_core` runs the download's Helm:

```text
bin/helm upgrade --install meho chart/meho-<chart version>.tgz
    --kubeconfig /etc/rancher/rke2/rke2.yaml
    --namespace meho --create-namespace
    --wait --wait-for-jobs --timeout 15m
    --set image.tag=<MEHO_IMAGE_TAG>
    -f chart/values-single-node.yaml -f /etc/meho/values-place.yaml
```

The versions come from the download's `VERSION` file. `--install` installs
the release when it does not exist and upgrades it otherwise. `--wait`
waits until the pods are ready, `--wait-for-jobs` also until every Job is
done, within `--timeout` per operation. The chart's bootstrap Job is a
post-install hook, and Helm blocks until a Job hook completes, so the
first-login Secret exists when Helm returns. The later file wins where two
values files set the same key, so the place values come last. Helm's own
output is dropped: it names `kubectl` commands, and the customer never sees
Kubernetes. Its error messages stay on the screen. On a failure the script
prints one `[FAIL]` line with the hint `meho-host support-bundle`.

### Step 5: the two one-time files

`write_secret_file` reads one Secret with RKE2's kubectl and a Go template
that decodes the values (`-o go-template`, with kubectl's `base64decode`
function), and writes them to a temporary file next to the target, which
`mktemp` creates with mode 0600. Then it moves the file into place. The
values are never on a command line, in the output or in a log. kubectl's
own messages are dropped, so a failure shows only the script's line.

| Path | Mode | Content |
| --- | --- | --- |
| `/var/lib/meho/first-login-passwords` | 0600 | one `key=value` line per key of `meho-core-first-login`: `admin_username`, `admin_password`, `approver_username`, `approver_password` |
| `/var/lib/meho/unseal-key` | 0600 | the `key` of `meho-core-vault-unseal`, one line |

The first-login Secret must exist; the script stops otherwise. The unseal
Secret may be missing: without it OpenBao is in keyholder mode (a person
unseals it after every restart), so the script prints one `[WARN]` line,
writes no key file and goes on. This is a shortcut until the OpenBao setup
Job lands (#4044): the Secret then always exists after a fresh install,
and a missing one becomes a `[FAIL]`.

### Step 6: the `meho` command, the install record and the result

`install_cli` copies the download's `bin/meho` to `/usr/local/bin/meho`
(mode 0755).

`write_install_record` writes `/etc/meho/install.json`: the MEHO, chart
and RKE2 versions from `VERSION`, the date, and the options used. It holds
no secret. A later run reads it to find the install (#4736).

```json
{
  "meho_version": "0.36.0",
  "chart_version": "0.36.0",
  "rke2_version": "v1.37.1+rke2r1",
  "installed_at": "2026-10-23T12:00:00Z",
  "options": {
    "hostname": "meho.example.com",
    "public_address": "",
    "first_tenant_id": "3f1c2a7e-9b4d-4e2a-8c1f-5d6e7f8a9b0c",
    "admin_user": "admin",
    "approver_user": "approver",
    "cluster_cidr": "10.42.0.0/16",
    "service_cidr": "10.43.0.0/16",
    "from_disk": false
  }
}
```

`print_result` prints the console address `https://<hostname>/ui/`, the
paths of the two files with what to do with them, and the line that the
first-start list is not in this version yet.

### `--from-disk`

Step 1 then checks only root, the format of the host name, and that the
folder of `install.sh` holds the core files: `VERSION`,
`chart/values-single-node.yaml`, `bin/helm`, `bin/meho` and the chart file
that `VERSION` names. The RKE2 files and the image archive are not needed:
RKE2's installer already ran, and RKE2 imports the archives in its images
folder at every start. Step 2 writes only RKE2's settings. Step 3 starts
RKE2 and waits. Steps 4 to 6 run as above, and `install.json` records
`"from_disk": true`. The DNS check does not run, so `--public-address` is
refused as wrong usage. The firewall is not touched (form-2 design,
section 5).

## The values files

`values-single-node.yaml` turns the built-in parts on (`corePostgres`,
`coreKeycloak`, `coreVault`), sets `ingress.className: traefik`,
`networkPolicy.ingressControllerNamespace: kube-system` (RKE2's Traefik
runs there) and `ingress.tls.secretName: meho-core-tls`, the self-signed
certificate the install makes. Every storage class stays at the chart's
default, empty, so the volumes use the default class. Until the chart
derives them from its built-in parts (#4047), the file also names the
PostgreSQL Secret the install makes (`postgres.credentialsSecret`) and the
built-in OpenBao's address (`vault.address`, `config.vaultAddr`).

`/etc/meho/values-place.yaml` is written by step 4 (above). The download
build renders the chart with `packaging/render-values.yaml`, which holds
the same keys with placeholder values, and then `values-single-node.yaml`;
see [linux-server-download.md](linux-server-download.md).

## The storage manifest

`storage/local-path-storage.yaml` is the upstream v0.0.37 manifest with
three changes:

- the StorageClass `local-path` has `storageclass.kubernetes.io/is-default-class: "true"`;
- the volumes live in `/var/lib/meho/volumes`;
- both images are pinned by digest. Upstream names the helper image
  `busybox` with no tag, so it is pinned to `busybox:1.37`. The digests are
  the multi-platform index digests on Docker Hub.

`tests/fixtures/local-path-storage-v0.0.37.yaml` is the upstream file,
unchanged. A test compares the two files and accepts only these changes.
The CI job also compares the fixture with the file on GitHub.

To move to a newer provisioner: replace the fixture with the new upstream
file, apply the three changes again, and update the digests and the
`sha256` in `tests/test_storage_manifest.py`.

## Testing

The tests follow `scripts/tests/test_mint_review_app_token.py`: Python
`unittest`, stub commands on `PATH`. `tests/harness.py` builds a fake
machine for each test:

- the download (`install.sh`, `lib/`, `storage/`,
  `chart/values-single-node.yaml`, fake RKE2 files, a fake chart, a fake
  `bin/meho`, a `VERSION` file and a stub `bin/helm`) is copied into a
  temporary folder;
- `MEHO_TEST_ROOT` makes the script read and write below a temporary root
  folder instead of `/`. It is a test seam only. On a real machine it is not
  set;
- `PATH` holds only stub commands and links to a few real tools (`bash`,
  `awk`, `sed`, `grep` and some coreutils). Each stub answers from one file
  and logs its arguments, so a test changes one answer and reads the calls.
  The kubectl stub answers `get secret` with the decoded values the real
  kubectl would print for the script's templates, and a test greps every
  output and record for them.

`tests/test_core_install.py` also renders the chart with
`values-single-node.yaml` and the place values the script wrote. It skips
when `helm` is not on `PATH`, unless `MEHO_REQUIRE_HELM=1` is set; CI sets
it, with the Helm version the download ships.

Run the tests and shellcheck locally (the script needs bash 4 or newer,
which macOS does not ship; run the tests on Linux or in a container):

```bash
python3 -m unittest discover -s deploy/linux-server/tests -v
shellcheck deploy/linux-server/install.sh deploy/linux-server/lib/*.sh
```

CI: `.github/workflows/linux-server-test.yml` runs on every pull request
that changes `deploy/linux-server/` or the chart. It installs shellcheck
v0.11.0 and Helm v3.16.4 (both pinned with a checksum), runs shellcheck on
every `*.sh` file and every file with a shell shebang there, checks the
fixture against upstream, and runs the unit tests with
`MEHO_REQUIRE_HELM=1`. Scripts that later Tasks add in this folder are
checked by the same job.

## Dependencies

- RKE2 v1.37.1+rke2r1 and its offline files, Helm v3.16.4 and the `meho`
  CLI, from the download that `build-download.sh` builds (#4734).
- On the machine: bash 4 or newer, coreutils, `awk`, `sed`, `grep`, `ss`
  and `ip` (iproute2), `systemctl`, `timedatectl`, `getent`, and
  `resolvectl` when `systemd-resolved` runs. Ubuntu 24.04 has all of them.
- RKE2's own kubectl, for the waits and for reading the two Secrets.

## Known issues

- **Three parts of the core are not on `main` yet, so an install cannot be
  green on a real machine until they land:** the OpenBao init Job and the
  unseal Secret (#4044, #4046; the script warns and writes no key file),
  the chart's own wiring of the built-in parts (#4047; three keys in
  `values-single-node.yaml` do it by hand until then), and
  `meho status --first-start` (#4042; the result names the list as missing).
- **The Helm time limit is 15 minutes per operation:** the wait for the
  pods, and each hook Job, so the whole install may take longer. The
  week-4 install (#4741) shows whether a 4-processor machine needs more.
- **The disk check counts free space.** A machine with an 80 GB disk has
  less than 80 GB free after the OS install, so it fails. The design calls
  80 GB the smallest disk. The week-4 install (#4741) should decide whether
  the check compares the size of the file system or the free space.
- **The pinned digests must survive the download build.** The manifest
  names both images by digest, so the image archive must keep the registry
  manifests. The download build (#4734) writes an OCI image layout for this
  reason, and `check-download.sh` checks every digest; see
  [linux-server-download.md](linux-server-download.md). A `docker save`
  archive would get new digests, and RKE2 would not find the images offline.
- **No CIS profile yet.** Without a profile, RKE2 applies the `privileged`
  pod security level, so the local-path helper pods run. #4636 adds the
  profile and the exception for the `local-path-storage` namespace.
- **IPv4 only.** The DNS check reads A records, and the ranges are IPv4.

## References

- RKE2 offline install: https://docs.rke2.io/install/airgap
- RKE2 server options: https://docs.rke2.io/reference/server_config
- RKE2 configuration file: https://docs.rke2.io/install/configuration
- RKE2 requirements and ports: https://docs.rke2.io/install/requirements
- RKE2 installer and unit at the pinned version:
  https://github.com/rancher/rke2/blob/v1.37.1%2Brke2r1/install.sh,
  https://github.com/rancher/rke2/blob/v1.37.1%2Brke2r1/bundle/lib/systemd/system/rke2-server.service
- Helm `upgrade` flags at the pinned version (`--install`, `--wait`,
  `--wait-for-jobs`, `--create-namespace`, `--timeout`):
  https://github.com/helm/helm/blob/v3.16.4/cmd/helm/upgrade.go
- Helm hooks block until a Job hook completes:
  https://helm.sh/docs/topics/charts_hooks/
- kubectl's Go template printer, with `base64decode`:
  https://github.com/kubernetes/kubernetes/blob/release-1.31/staging/src/k8s.io/cli-runtime/pkg/printers/template.go
- `/proc/sys/kernel/random/uuid`, a new UUID at every read:
  https://www.kernel.org/doc/html/latest/admin-guide/sysctl/kernel.html
- local-path provisioner v0.0.37 manifest:
  https://github.com/rancher/local-path-provisioner/blob/v0.0.37/deploy/local-path-storage.yaml
- `resolvectl --synthesize`: https://www.freedesktop.org/software/systemd/man/latest/resolvectl.html
- `ReadEtcHosts=` of systemd-resolved: https://www.freedesktop.org/software/systemd/man/latest/resolved.conf.html
