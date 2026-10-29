# Appliance build: `deploy/appliance/build.sh` (#4811)

## Overview

Install form 1 ships MEHO as a VMware appliance, `meho-appliance-<version>.ova`.
The appliance is a Linux machine where the form-2 install
([linux-server-install.md](linux-server-install.md)) already ran, then
cleaned of everything that must differ between appliances, and exported.
The design is the internal design note `form-1-appliance.md`: section 1 (the
steps), section 5 (the size: 4 processors, 16 GB, a 120 GB thin disk) and
section 6 (hardware version 20).

`deploy/appliance/build.sh <version> <out-dir>` runs the steps against the
lab's vSphere 8 cluster with `govc`. `.github/workflows/appliance-build.yml`
runs it on the lab runner and keeps the OVA, `SHA256SUMS` and the build log
as workflow artifacts. Publishing is #4814.

The clean step is the security-relevant part. A file missed there would be
copied from one OVA into every appliance: the same RKE2 keys, the same SSH
host keys, the same passwords. So `clean.sh` follows a fixed list, checks
itself before the power-off, and the unit tests pin what it deletes and
what it keeps.

## Key files

| File | What it holds |
| --- | --- |
| `deploy/appliance/build.sh` | Options, settings, the five steps, `--from-step`. |
| `deploy/appliance/clean.sh` | Runs on the build VM as root, outside any SSH session: the deletions, the check (`--check` runs only that), the power-off. |
| `deploy/appliance/rewrite-ovf.sh` | Replaces the product section of an exported `.ovf` and declares the OVF transports. `awk` does the rewrite. |
| `deploy/appliance/product-section.xml` | The wizard fields of the form-1 design, section 2, with `@VERSION@`. |
| `deploy/appliance/tests/` | `test_clean.py` with `clean_harness.py` (a fake build VM), `test_rewrite_ovf.py`, and the Ubuntu cloud image's own `.ovf` as the fixture. |
| `.github/workflows/appliance-build.yml` | The build on the lab runner. |
| `.github/workflows/linux-server-test.yml` | shellcheck and the unit tests, on every pull request that touches `deploy/appliance/`. |

The scripts have the shape of `install.sh`: `set -Eeuo pipefail`, an ERR
trap that prints one `[FAIL]` line, `fail` and `usage_error`, and the exit
codes 0, 1 and 2. `clean.sh` reads and writes below `MEHO_TEST_ROOT` when
the tests set it, as `install.sh` does.

## Control flow

### `build.sh`, the five steps

1. **The base image.** The Ubuntu 24.04 cloud image OVA of one pinned
   serial, downloaded once into a cache folder and checked against the
   pinned sha256. `govc import.ova` deploys it with an options file that
   fills the image's own properties: `hostname`, `public-keys` and
   `user-data` (base64). The user-data makes only the build user
   `meho-build` with the build key and sudo (no `default` in the `users`
   list, so cloud-init makes no `ubuntu` user), sets a static address so
   the lab's DNS record for the placeholder name fits, and removes the
   image's DHCP file. Then `govc vm.change` sets 4 processors and 16 GB,
   `govc vm.disk.change` grows the one disk to 120 GB before the first
   power-on (cloud-init's growpart then grows the root file system),
   `govc vm.upgrade` sets hardware version 20 (the image ships `vmx-10`,
   and the in-guest trim of the clean step reaches the thin disk only from
   virtual hardware 13 on), and the VM starts. The build waits until SSH answers.
2. **The form-2 install.** The download goes to the VM with `scp`, and
   `sudo ./install.sh --hostname <placeholder>` runs. No `--admin-network`,
   so the host firewall lets in SSH only from the build's own address,
   which the clean step removes. Every build also proves the form-2
   install.
3. **The appliance pieces** under `/opt/meho/appliance`: a copy of the
   unpacked download without `rke2/` and `images/` (those files are already
   where RKE2 reads them), so `install.sh --from-disk` can run from
   `/opt/meho/appliance/download` at first start; and
   `/etc/cloud/cloud-init.disabled`, so cloud-init never runs on the
   appliance. The first-start service (#4812), the console banner and the
   OS hardening (#4640) are not built yet; `add_appliance_pieces` marks
   where their lines go.
4. **Clean for copying.** `clean.sh` goes to `/run/meho-clean.sh` (a RAM
   disk, so it is not in the OVA) and starts with
   `systemd-run --unit=meho-clean --collect -p StandardOutput=journal+console`,
   outside the SSH session, because it deletes the build user and closes
   SSH. Its lines go to the VM console as well as the journal. The build
   reads the VM's power state (`govc object.collect`) until it is off. When it is still on
   after 20 minutes, the script stopped or its check found something, and
   the build fails with the advice to read the console. `--from-step 4`
   works only when `clean.sh` stopped in its step 1, while RKE2 still
   runs; after a later failure the VM is destroyed and the build starts
   again.
5. **Export.** `govc vm.upgrade -version 20` (already set in step 1; the
   call accepts a VM at that version), then `govc export.ovf`. The
   build checks that the descriptor says `vmx-20`. `rewrite-ovf.sh` puts
   our product section in and declares the transports. The manifest
   (`SHA256(file)= hash`, one line per file) is written again, and
   `tar --format=ustar` packs the descriptor first, then the manifest, then
   the disk: that is an OVA. `SHA256SUMS` goes next to it, and the build
   VM is destroyed. On a failure the VM stays, and `--from-step` goes on
   from the failed step.

### `clean.sh`, in order

1. **The Secrets of the core.** `kubectl delete secret -l meho.evoila.com/managed-by=core-bootstrap`
   in namespace `meho`, with RKE2's own kubectl and kubeconfig, then any
   `meho-core-*` Secret that is still there, by name. When one is still
   there after that, or when kubectl cannot list the Secrets at all, the
   script stops before it removes anything from the machine, so the VM is
   still reachable to find out why.
2. **RKE2, its pods and its own state.** `systemctl disable --now rke2-server`,
   so the customer's first boot does not start RKE2 with the build's
   placeholder name; first start turns it on again. A disable alone does
   not last: the clean resets the machine-id, systemd treats the next boot
   as a first boot and enables every unit without a preset rule. So the
   clean writes `/etc/systemd/system-preset/10-meho-appliance.preset` with
   `disable` rules for `rke2-server` and `rke2-agent`. A preset rule stops
   only the automatic enable; first start's `systemctl enable --now
   rke2-server` still works. The service stops only
   `rke2`, kubelet and containerd (`KillMode=process`); the pods keep
   running. So `/usr/local/bin/rke2-killall.sh` (RKE2's tarball installer
   puts it there) stops them and unmounts their volumes. Then `server/db`,
   `server/tls`, `server/cred`, `server/token`, `server/agent-token` and
   `server/node-token`; everything under `agent/` except `agent/images/`;
   the node password `/etc/rancher/node/password`; the kubeconfig
   `/etc/rancher/rke2/rke2.yaml`; all of `/var/lib/kubelet` (the pods'
   folders, with the on-disk `emptyDir` volumes of the core's Jobs), as
   RKE2's own uninstall script does. **Kept:** `server/manifests/` (the
   storage manifest of the form-2 install), `agent/images/` (the image
   archives), `/etc/rancher/rke2/config.yaml` and the pod security file
   `/etc/rancher/rke2/meho-pss.yaml` of the CIS profile. RKE2 makes new
   certificate authorities, a new Secrets key and a new token at first
   start, and imports the archives again.
3. **MEHO's data**, now that no pod writes to it. Everything under
   `/var/lib/meho/`: the data volumes (PostgreSQL, OpenBao with its init,
   Valkey), the two one-time files, support bundles. The folder itself
   stays. Also the install's answers under `/etc/meho/`:
   `values-place.yaml` holds the first tenant id, which `install.sh` reuses
   when the file exists, and `install.json` holds the build VM's address.
   First start runs `install.sh`, which writes both again with a new
   tenant id.
4. **The machine.** The build user's sessions end (`loginctl`, `pkill`,
   then a wait of up to 30 seconds until no process of the user is left,
   because `userdel` refuses a user with a running process); `userdel -r`
   removes the user, its home and its key; the copy of the
   key that cloud-init writes into `/root/.ssh/authorized_keys` (with a
   "please log in as" command, because of `disable_root`), cloud-init's sudo
   rule and the build's network file `/etc/netplan/60-meho-build.yaml` go.
   `cloud-init clean --logs --machine-id` removes cloud-init's state and
   logs and writes `uninitialized` to `/etc/machine-id`. The SSH host keys
   under `/etc/ssh/ssh_host_*` go; first start makes new ones.
   `meho-host admin-network none` closes SSH for every address
   ([linux-server-meho-host.md](linux-server-meho-host.md)); the script
   runs in no SSH session, so the command does not ask. The journal is
   rotated and emptied, every file under `/var/log` except the journal
   files (journald has them open) is truncated, root's shell history goes,
   and `fstrim -av` lets the disk export small.
5. **The check**, also `clean.sh --check` on any machine. One `[LEFT]` line
   per finding, then one `[FAIL]` line with the count; the VM is not
   powered off, so the console shows the list. It looks for: a
   `meho-core-*` Secret (when `server/db` is gone there can be none; when
   it exists, kubectl must answer and list none); a running
   `containerd-shim` (a pod that still runs); a file under `/var/lib/meho/`;
   each RKE2 path of step 2; anything under `agent/` but `images/`; the
   node password; the kubeconfig; `/var/lib/kubelet`; the two files under
   `/etc/meho/`; root's `authorized_keys`; the build's network file and
   sudo rule; `rke2-server` enabled; an SSH host key; the build user
   and its home; a machine-id other than `uninitialized`. It also checks
   that what must stay is there: `agent/images/`, `server/manifests/`,
   `config.yaml`, `meho-pss.yaml`, the preset file,
   `/opt/meho/appliance/download/install.sh` and
   `/etc/cloud/cloud-init.disabled`.
6. **Power off:** `sync`, `systemctl poweroff`.

### `rewrite-ovf.sh <file.ovf> <product-section.xml> <version>`

Removes every `<ProductSection>` block (the cloud image's cloud-init
properties: `instance-id`, `hostname`, `seedfrom`, `public-keys`,
`user-data`, `password`), prints ours before the one
`<VirtualHardwareSection>`, and sets `ovf:transport="com.vmware.guestInfo iso"`
on it, in place of the image's `iso`. With both transports, the appliance
reads the wizard values through VMware Tools
(`vmtoolsd --cmd 'info-get guestinfo.ovfEnv'`) or from the `ovf-env.xml`
CD-ROM. `@VERSION@` becomes the version. A descriptor with no hardware
section, or more than one, is refused unchanged.

### The product section

The properties of the form-1 design, section 2, in the wizard's order:
`hostname`, `net.ntp`, `net.mode` (`static` or `dhcp`, default `static`),
`net.ip`, `net.gateway`, `net.dns`, `net.admin_network`, `admin.password`
(a password field, `MinLen(14)`), `admin.ssh_key`, `cluster.join.address`,
`cluster.join.token` (a password field) and `cluster.vip`. Their meaning
is the first-start service's (#4812); until it lands, an appliance does
nothing with them.

## The workflow

`appliance-build.yml` runs on `workflow_dispatch` with a release tag. It
installs a pinned `govc`, fetches the tag's form-2 download from the
GitHub release and checks it the way `docs/RELEASING.md` says (the cosign
signature of `SHA256SUMS-linux`, then the checksum), makes a new ed25519
key for the build, runs `build.sh` and keeps the OVA, `SHA256SUMS` and
`build.log` as one artifact for 3 days. The vCenter login is in the
repository secrets `GOVC_URL`, `GOVC_USERNAME` and `GOVC_PASSWORD`; the
place of the build VM and its network are the repository variables
`MEHO_LAB_DATASTORE`, `MEHO_LAB_FOLDER`, `MEHO_LAB_RESOURCE_POOL`,
`MEHO_LAB_NETWORK`, `MEHO_BUILD_HOSTNAME`, `MEHO_BUILD_ADDRESS`,
`MEHO_BUILD_GATEWAY`, `MEHO_BUILD_DNS` and `MEHO_LAB_GOVC_INSECURE`. One build at a
time (`concurrency`). A run on every release tag comes with the release
test (#4816).

## Dependencies

- `govc` v0.56.2 (vmware/govmomi), pinned with its sha256 in the workflow.
  Commands used: `find`, `import.ova`, `vm.change`, `device.ls`,
  `vm.disk.change`, `vm.power`, `object.collect`, `vm.upgrade`,
  `export.ovf`, `vm.destroy`. No `vm.info`: it also reads the VM's host,
  so it fails for a login that has rights only on the folder, the pool,
  the datastore and the network, and none on the host.
- The Ubuntu 24.04 cloud image OVA, serial `release-20261017`, pinned with
  its sha256 in `build.sh`. Its properties and its cloud-init are used
  only at the build; the appliance has cloud-init off.
- The form-2 download of the same version, v0.38.0 or later: the first
  release with `meho-host admin-network none` (#4739) and the pod security
  file of the CIS profile (#4636). The clean of an older download fails
  after SSH is closed. `install.sh --hostname` and `--from-disk` are used.
- On the runner: `ssh`, `scp`, `ssh-keygen`, `curl`, `tar`, `sha256sum`,
  GNU `base64`. No `ovftool`: `govc export.ovf` plus our own manifest and
  `tar` make the OVA.

## Known issues

- **Not run against vCenter yet.** The lab's test place is ready, but
  steps 1, 2 and 5 have not run. They are written from the `govc`
  reference and the lab's own cloud-image deploys, and wait for the first
  build by hand. The first run will measure the times and may need the
  waits changed.
- **The first-start service (#4812)**, the console banner and the
  hardening (#4640) are not in the OVA yet. `add_appliance_pieces` in
  `build.sh` is where their install lines go. Until #4812 lands, the
  appliance has no network configuration after the clean (the build's
  network file is removed) and no SSH host keys; first start writes both.
- **The cloud image's `ubuntu` user** is not made (no `default` in the
  user-data). If a later cloud-init version changes that rule, the check
  finds no such user; add it to `check_clean` if the first build shows one.

## Testing

`deploy/appliance/tests/` follows `deploy/linux-server/tests/`: Python
`unittest`, stub commands on `PATH`, a temporary root folder that
`MEHO_TEST_ROOT` points the script to. `clean_harness.py` builds a fake
build VM after the form-2 install: MEHO's data, RKE2's state, the SSH host
keys, the build user (a fake `passwd` file that the `getent` and `userdel`
stubs read and write), a machine-id, logs and the appliance pieces. The
`kubectl` stub at RKE2's own path keeps the cluster's Secrets in one file
with their `managed-by` label. The `rke2-killall.sh` stub stops the fake
pods, which the `pgrep` stub reports until then; it fails when MEHO's data
is already gone, so the pods must stop first.

`test_clean.py` pins what goes and what stays, the order of the steps,
that a Secret that survives the delete stops the script before anything
else is removed, that a failed `admin-network none` leaves the VM on, and
that the check names each kind of leftover and each missing keep.
`test_rewrite_ovf.py` runs the rewrite on the Ubuntu cloud image's own
descriptor and parses the result.

The tests need Linux bash 5; on macOS run them in Docker
(`deploy/appliance/README.md`). CI runs them with shellcheck in
`linux-server-test.yml`.

## References

- Task evoila/meho#4811, Initiative evoila/meho#3958.
- Design: the internal design note `form-1-appliance.md`,
  sections 1, 2, 5 and 6, and "What form 1 needs from form 2", needs 2, 3
  and 10.
- govc: `govc/USAGE.md` in vmware/govmomi at v0.56.2; the import options
  file shape from `ovf/importer/options.go`.
- cloud-init docs (read 2026-10-26): `cloud-init clean --logs --machine-id`
  (reference/cli), `/etc/cloud/cloud-init.disabled`
  (howto/disable_cloud_init), the `users` list without `default`
  (reference/yaml_examples/user_groups), growpart defaults
  (reference/modules).
- systemd: `StandardOutput=journal+console` (systemd.exec), `systemd-run`.
- OVF: the envelope as vmware/govmomi `ovf/envelope.go` parses it
  (`ProductSection`, `Property`, `VirtualHardwareSection` `transport`).
