# Form-2 install script: `deploy/linux-server/install.sh` (#4733)

## Overview

Install form 2 puts MEHO on a plain Linux server that has no internet. The
customer copies one download onto the machine and runs one script as root:
`install.sh`. The design is in meho-internal
`docs/design/2026-10-10-install-forms/form-2-linux-server.md`. Section 4
gives the layout of the download, and section 5 gives the steps.

This first version runs steps 1 to 3 of the design:

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

Exit codes: 0 when RKE2 and its storage are ready, 1 when a check or a step
failed, 2 for wrong usage (an unknown option or a bad value).

The download itself, `meho-linux-<version>-amd64.tar`, is built by
`build-download.sh` (#4734); see
[linux-server-download.md](linux-server-download.md).

Later Tasks add their steps to `main()` in `install.sh`:

- #4635 adds the host firewall to step 2.
- #4636 adds the CIS host steps between installing and starting RKE2, and
  `profile: cis` with the storage exception in RKE2's settings.
- #4735 installs the core after step 3.
- #4737 adds `meho-host`, which `install.sh` installs.

## Key files

| File | What it holds |
| --- | --- |
| `install.sh` | Options, `--help`, the order of the steps, the error trap. |
| `lib/common.sh` | Machine paths, the list of download files, `ok`, `warn`, `fail` and `usage_error`. |
| `lib/net.sh` | IPv4 helpers: the format of an address or a range, and whether two ranges overlap. |
| `lib/checks.sh` | Step 1: one function per check. |
| `lib/prepare.sh` | Step 2: folders, `config.yaml`, join token, image archive, storage manifest. |
| `lib/rke2.sh` | Step 3: RKE2's installer, the start, and the wait with one time limit. |
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
| download | every file of `DOWNLOAD_FILES` is next to `install.sh` | the download folder |
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

- the download (`install.sh`, `lib/`, `storage/`, and fake RKE2 files) is
  copied into a temporary folder;
- `MEHO_TEST_ROOT` makes the script read and write below a temporary root
  folder instead of `/`. It is a test seam only. On a real machine it is not
  set;
- `PATH` holds only stub commands and links to a few real tools (`bash`,
  `awk`, `sed`, `grep` and some coreutils). Each stub answers from one file
  and logs its arguments, so a test changes one answer and reads the calls.

Run the tests and shellcheck locally:

```bash
python3 -m unittest discover -s deploy/linux-server/tests -v
shellcheck deploy/linux-server/install.sh deploy/linux-server/lib/*.sh
```

CI: `.github/workflows/linux-server-test.yml` runs on every pull request
that changes `deploy/linux-server/`. It runs shellcheck v0.11.0 on every
`*.sh` file and every file with a shell shebang there, checks the fixture
against upstream, and runs the unit tests. Scripts that later Tasks add in
this folder are checked by the same job.

## Dependencies

- RKE2 v1.37.1+rke2r1 and its offline files, from the download that
  `build-download.sh` builds (#4734).
- On the machine: bash, coreutils, `awk`, `sed`, `grep`, `ss` and `ip`
  (iproute2), `systemctl`, `timedatectl`, `getent`, and `resolvectl` when
  `systemd-resolved` runs. Ubuntu 24.04 has all of them.

## Known issues

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
- local-path provisioner v0.0.37 manifest:
  https://github.com/rancher/local-path-provisioner/blob/v0.0.37/deploy/local-path-storage.yaml
- `resolvectl --synthesize`: https://www.freedesktop.org/software/systemd/man/latest/resolvectl.html
- `ReadEtcHosts=` of systemd-resolved: https://www.freedesktop.org/software/systemd/man/latest/resolved.conf.html
