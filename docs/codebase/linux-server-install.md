# Form-2 install script: `deploy/linux-server/install.sh` (#4733, #4635, #4636, #4735)

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
   (both mode 0700). Then it turns on the MEHO host firewall and turns
   firewalld or ufw off (see [The host firewall](#the-host-firewall)).
   Then it writes RKE2's settings, with RKE2's CIS profile and its pod
   security file (see [RKE2's CIS profile](#rke2s-cis-profile)), copies the
   join token when the join options are given, copies the MEHO image archive and the storage
   manifest to the folders where RKE2 reads them, and writes the setting
   that makes RKE2's Traefik redirect port 80 to 443. It also
   installs the host command `meho-host`
   ([linux-server-meho-host.md](linux-server-meho-host.md)).
3. **Install and start RKE2.** It runs RKE2's own installer in offline
   mode. Then it runs the host steps of RKE2's CIS profile: the `etcd` user
   and group, and RKE2's kernel settings, copied to `/etc/sysctl.d` and
   loaded. Then it starts `rke2-server`, and waits until the node is Ready,
   until `local-path` is the default storage class, and until the storage
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
RKE2's settings, the start of RKE2, and steps 4 to 6. Before the start it
also runs the host steps of the CIS profile, which change nothing where a
full install ran before. It is for a machine
where RKE2's own state was removed on purpose, but its files, the chart and
the image archives are still on the disk. The appliance's first start uses
it (form-1 design, section 2).

Exit codes: 0 when MEHO is installed, 1 when a check or a step failed, 2 for
wrong usage (an unknown option or a bad value).

The download itself, `meho-linux-<version>-amd64.tar`, is built by
`build-download.sh` (#4734); see
[linux-server-download.md](linux-server-download.md).

Later Tasks add their steps to `main()` in `install.sh`:

- #4042 adds the first-start list to step 6, read with
  `meho status --first-start`.
- #4736 makes a second run an update, which reads `install.json` and
  `values-place.yaml`.

## Key files

| File | What it holds |
| --- | --- |
| `install.sh` | Options, `--help`, the order of the steps, `--from-disk`, the error trap. |
| `lib/common.sh` | Machine paths, the lists of download files, the Helm release name, `ok`, `warn`, `fail` and `usage_error`. |
| `lib/net.sh` | IPv4 helpers: the format of an address or a range, and whether two ranges overlap. |
| `lib/checks.sh` | Step 1: one function per check. |
| `lib/firewall.sh` | The host firewall: the checks of step 1 (`nft`, the admin network), the files and the start in step 2, `firewall_state` and `firewall_port_list`. |
| `lib/prepare.sh` | Step 2: folders, `config.yaml`, join token, image archive, storage manifest, the Traefik redirect. |
| `lib/cis.sh` | RKE2's CIS profile: the pod security file that `config.yaml` names, the host steps of step 3 (the `etcd` user and group, the kernel settings), and `cis_state`. |
| `lib/rke2.sh` | Step 3: RKE2's installer, the start, and the wait with one time limit. |
| `lib/core.sh` | Steps 4 to 6: the versions of the download, the place values, the Helm call, the two one-time files, the `meho` command, the install record, the result lines. |
| `values-single-node.yaml` | The Helm values for one node. The download ships it as `chart/values-single-node.yaml`. |
| `meho-host` | The host command, `check` and `support-bundle`; see [linux-server-meho-host.md](linux-server-meho-host.md). |
| `storage/local-path-storage.yaml` | Rancher's local-path provisioner v0.0.37 manifest with five changes. |
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
| nftables | `nft` is on the machine and can read the kernel's tables | `command -v nft`, `nft list tables` |
| admin network | SSH stays possible for the admin after the install; see [The admin network](#the-admin-network) | `--admin-network`, `SSH_CONNECTION` |
| join and firewall | with `--join-address`, a `[WARN]` that the rules block a join until both machines accept the other node | `--join-address` |

The place answers are checked before any step runs, as wrong usage (exit
code 2): `--first-tenant-id` must be a UUID, `--admin-user` and
`--approver-user` take letters, digits and `. _ - @`, are put in lower
case first (Keycloak keeps user names in lower case) and must then
differ. So the values go into the Helm values and the install record as
they are, with no escaping.

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
| `/etc/meho/firewall.nft` | 0600 | the MEHO host firewall: the table `inet meho` |
| `/etc/systemd/system/meho-firewall.service` | 0644 | the boot unit that loads `firewall.nft` |
| `/etc/systemd/system/rke2-server.service.d/meho-firewall.conf` | 0644 | RKE2 starts only while the table `inet meho` is loaded |
| `/etc/rancher/rke2/meho-pss.yaml` | 0600 | the pod security file of RKE2's CIS profile, with one more exempt namespace (see [The pod security file](#the-pod-security-file)) |
| `/etc/rancher/rke2/config.yaml` | 0600 | `write-kubeconfig-mode: "0600"`, `tls-san`, `ingress-controller: traefik`, `cluster-cidr`, `service-cidr`, `profile: cis`, `pod-security-admission-config-file`; with the join options also `server` and `token-file` |
| `/etc/meho/join-token` | 0600 | a copy of `--join-token-file`, only with the join options |
| `/var/lib/rancher/rke2/agent/images/meho-images.linux-amd64.tar.zst` | 0600 | the MEHO image archive |
| `/var/lib/rancher/rke2/server/manifests/local-path-storage.yaml` | 0600 | the storage manifest |
| `/var/lib/rancher/rke2/server/manifests/rke2-traefik-config.yaml` | 0600 | a `HelmChartConfig` for `rke2-traefik`: port 80 redirects to 443 |
| `/usr/local/sbin/meho-host` | 0755 | the host command, a copy of `meho-host` from the download |

The join token goes from file to file with `install`. It is never on a
command line, in the output or in `config.yaml`.

RKE2's installer puts RKE2's own image archive into the images folder
itself, after it checks the archive against `sha256sum-amd64.txt`. So the
script copies only the MEHO archive.

### Step 3: start and wait

`install_rke2` runs `INSTALL_RKE2_ARTIFACT_PATH=<download>/rke2 sh <download>/rke2/install.sh`.

`prepare_cis_host` then makes the `etcd` user and group and loads RKE2's
kernel settings (see [The host steps](#the-host-steps)). RKE2 checks both
when it starts with the CIS profile and stops when one is missing, so they
must be in place before the start.

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
folder at every start. Step 2 writes only RKE2's settings and the pod
security file. Step 3 runs the host steps of the CIS profile, starts RKE2
and waits. RKE2's kernel settings file is still in RKE2's share folder,
because RKE2's own files stay on the disk. Steps 4 to 6 run as above, and `install.json` records
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

## The host firewall

ADR 0009 (closed by default) and the form-2 design, section 2, ask for one
host firewall that the install sets. From another machine only these
answer: TCP 80 and 443 from every address (80 only redirects to 443), and
SSH (TCP 22) from the admin network. Everything else that comes in on a
network card is dropped, RKE2's own ports too.

### The rules file

`/etc/meho/firewall.nft` holds every rule, with comments for the
customer's security team. `render_firewall_rules` writes it.
`tests/fixtures/firewall-admin-network.nft` is the whole file for
`--admin-network 10.0.50.0/24`.

- **Its own table.** The file defines only the table `inet meho`. It starts
  with `table inet meho` and `delete table inet meho`, then defines the
  table again. nft applies the whole file as one transaction, so a reload
  replaces the table in one step. The file never runs `flush ruleset`:
  kube-proxy and Canal (Calico and flannel) write their own rules into
  other tables of the same kernel firewall, and a flush would delete them.
- **Before the port forwarding.** RKE2's Traefik chart (`rke2-traefik`
  41.2.003 in RKE2 v1.37.1) sets `hostPort: 80` and `hostPort: 443`. The
  CNI `portmap` plugin turns a host port into a destination NAT in the
  prerouting hook, at priority `dstnat` (-100). After that NAT, a packet
  for port 443 of the machine is a packet for the Traefik pod, so a rule
  in the input hook would never see it. The chain that filters is the
  `filter` chain `prerouting`, in the prerouting hook at priority `mangle`
  (-150). It runs after connection tracking (-200) and before the NAT, so
  it sees the original port. Its policy is `drop`.
- **The machine's own traffic passes first.** Packets on `lo`, on Calico's
  pod interfaces (`cali*`) and on flannel's VXLAN interface (`flannel*`)
  come from this machine or its pods. The chain accepts them before any
  other rule. Then it accepts the packets of connections that are already
  open (`ct state established,related`), and drops packets that
  connection tracking marks invalid.
- **Pod and service ranges.** A packet from a network card with a source
  address in the pod or service range is forged, so the chain drops it.
  The ranges are the `--cluster-cidr` and `--service-cidr` values that
  `config.yaml` gets too. The chain does not accept packets by these
  ranges alone, because anyone on the network can forge a source address.
- **Only for this machine.** The accept rules for 80, 443, SSH and ping
  match `fib daddr type local`: the packet is for an address of this
  machine. RKE2 turns IP forwarding on, so without this match a packet
  for a pod or service address (for example `10.43.0.1:443`, the
  Kubernetes API service) could be passed on from the network.
- **What else passes.** Ping from the admin network; IPv6 neighbour
  discovery, router advertisements and multicast listener queries; the
  replies of DHCP servers (UDP 67 to 68, and 547 to 546). The last rule
  is a `counter` of the packets that the policy drops.
- **Outgoing traffic** is not filtered on a form-2 machine. The chain
  `output` has policy `accept` and no drop rule (see the next point).
- **Three chains for the appliance.** The table also has base chains on
  the `input`, `forward` and `output` hooks, at priority `filter` (0).
  They hold the rules of the MEHO appliance for traffic to the machine,
  through it and from it. On a form-2 machine their policy is `accept`
  and they have no drop rule, so they change nothing. The appliance build
  sets the three policies to `drop` and changes nothing else (form-2
  design, section 2, "One file for both forms"; ADR 0009 decision 1). The
  CIS Ubuntu 24.04 rules 4.3.5 to 4.3.8 look for a base chain with policy
  `drop` on each of these three hooks. They do not look at the prerouting
  hook. The design of 2026-10-22 named the `forward` and `output` chains.
  The `input` chain was added in the review of #4823, so that the
  appliance passes 4.3.5 and 4.3.8 too.
  - `input`: the same accept rules as the chain `prerouting`, without
    `fib daddr type local`, because the input hook sees only packets for
    this machine. The console on 80 and 443 does not normally come here:
    the host-port NAT sends it to the Traefik pod, through the `forward`
    hook. The kubelet, the API server and etcd reach each other over `lo`,
    also when they use the machine's own address, because Linux sends a
    packet for a local address through `lo`. Pods reach them over their
    `cali*` interfaces. The drop rules of `prerouting` (`ct state invalid`
    and the forged pod and service sources) stay in `prerouting` only.
  - `forward`: connections already set up, packets that the port
    forwarding rewrote (`ct status dnat`), and the pod and service ranges
    as source or destination.
  - `output`: `lo` and the cluster interfaces (for example the kubelet's
    health checks), connections already set up, the pod and service
    ranges, DNS (UDP and TCP 53), NTP (UDP 123), the requests of the DHCP
    clients, ping, IPv6 neighbour discovery, and the multicast listener
    reports of MLD versions 1 and 2. Linux sends version 2 by default
    (`force_mld_version` is 0), so the chain accepts
    `mld2-listener-report` next to the design's list. The rule for the
    customer's log server comes with `meho-host log-server` (#4739).
  - The loopback drop rules that CIS rule 4.3.6 also looks for (packets
    from `127.0.0.0/8` or `::1` on a network card) are not in the file.
    They can come with the appliance build.
- **The customer's own rules.** The header of the file says where they
  go: accept rules in the chain `prerouting`, before its last rule
  (`counter`), then a reload. The form-2 design (section 8) tells the
  customer to add rules to this file, not to replace it.

### The boot unit and RKE2

`meho-firewall.service` loads the file with `nft -f` at every start. It
follows the `nftables.service` of Debian and Ubuntu (`DefaultDependencies=no`,
`Before=network-pre.target`, `WantedBy=sysinit.target`), without its stop
command. It also runs `Before=rke2-server.service` and
`After=nftables.service`.

The distribution's `nftables.service` is not used. On Ubuntu 24.04 its stop
command is `nft flush ruleset`, and `/etc/nftables.conf` starts with
`flush ruleset`. On RHEL 9 its stop and reload commands run
`flush ruleset`. Each of them would delete the rules of kube-proxy and
Canal. If a customer enables `nftables.service` anyway, the MEHO unit loads
after it at boot, so its start does not remove the MEHO table.

`meho-firewall.service` has no `ExecStop`. Stopping the unit leaves the
rules loaded, and `systemctl restart` or `reload` replaces the table in one
step. To remove the rules: `nft delete table inet meho`.

The drop-in `rke2-server.service.d/meho-firewall.conf` adds
`Wants=` and `After=meho-firewall.service`, and an `ExecStartPre=` that runs
`nft list table inet meho`. So RKE2 does not start while the table is
missing. `rke2-server.service` restarts itself after a failed start, so
RKE2 starts as soon as the firewall is loaded again. A `Requires=` would
not do this: with it, `systemctl restart meho-firewall.service` would also
restart RKE2.

### The order in step 2

`set_up_firewall` runs right after the folders exist, before RKE2's files:

1. write the rules file, the unit and the drop-in;
2. `nft -c -f /etc/meho/firewall.nft`: the kernel checks the file and
   nothing is loaded. A refused file stops the install here;
3. `systemctl daemon-reload`, `systemctl enable` and
   `systemctl restart meho-firewall.service`;
4. `firewall_state` must say `on`;
5. only then: `systemctl disable --now firewalld.service` when firewalld is
   active or enabled, and `ufw disable` when `ufw status` says
   `Status: active`. RKE2's known-issues page says firewalld conflicts with
   Canal. So the machine is never without a firewall.

### The admin network

`--admin-network` takes an IPv4 range (`10.0.50.0/24`, which must start at
the first address of the range), one IPv4 address, or `none`. A wrong
value exits with code 2. Step 1 decides what goes into the set
`admin_network` and stops when that would lock the admin out. Packets of
open connections always pass, so the SSH session that runs `install.sh`
stays open. The check is about the next login.

| Option | In an SSH session | On the machine's console |
| --- | --- | --- |
| a range or an address | `[ok]` when the session's address is inside it; `[FAIL]` when it is outside | `[ok]` |
| none given | `[WARN]`: SSH only from the session's address, and how to change it | `[FAIL]`: give `--admin-network` (ADR 0009: on the console the script stops and asks) |
| `none` | `[WARN]`: no new SSH login works after this session | `[ok]` |

More rules of the check:

- A session over IPv6 with no option stops: the set holds IPv4 only. With
  a range, it gets a `[WARN]`. An IPv4-mapped address (`::ffff:10.0.0.9`)
  counts as IPv4.
- A session that reached sshd on a port other than 22 stops, unless the
  option is `none`, because the rules open SSH only on port 22.
- `--admin-network 0.0.0.0/0` gets a `[WARN]`.

**How the script finds the session.** The admin runs `sudo ./install.sh`.
sudo resets the environment (`env_reset`, on by default; Ubuntu's sudoers
keeps no `SSH_*` variable), so `install.sh` has no `SSH_CONNECTION` of its
own. `ssh_connection` reads `SSH_CONNECTION` from the environment first,
then from `/proc/<pid>/environ` of each process above the script (sudo,
the login shell), at most 32 levels up. The first value found wins.

After the install, `meho-host admin-network <range>` (or `none`) changes
the set, with the same input rules and the same lock-out question; see
[linux-server-meho-host.md](linux-server-meho-host.md#admin-network). The
comment above the set says where its value came from: `--admin-network`,
the SSH session, `none`, or `meho-host admin-network`.

### For later Tasks

- `firewall_state` (read-only) prints `on`, or `off: <reason>`, and returns
  0 or 1. On means the table `inet meho` is loaded and
  `meho-firewall.service` is enabled. `meho-host check` (#4737) and its
  hardening lines (#4638) can call it.
- `firewall_port_list` prints the ports as a port scan from another
  machine must see them: `80/tcp open`, `443/tcp open`,
  `22/tcp admin-network`, and RKE2's ports as `closed` (6443, 9345, 10250,
  2379, 2380, 2381, 9099, 30000-32767 and 8472/udp). The arrays are in
  `lib/firewall.sh`. The release test of form 2 (#4742) reads this list.
  A test checks that every port of the free-port check of step 1 is in it.
- The SSH rule reads the set `admin_network`. `meho-host admin-network`
  (#4739) changes only that set and its note, so a change to the rendering
  of the set must keep the lines it looks for (`set admin_network {`,
  `flags interval`, the note above the set).

### The redirect of port 80

`write_traefik_config` writes a `HelmChartConfig` named `rke2-traefik` in
`kube-system` into RKE2's manifests folder. It sets
`ports.web.http.redirections.entryPoint` to `websecure`, scheme `https`,
permanent. The chart turns this into the Traefik arguments
`--entryPoints.web.http.redirections.entryPoint.to=:443`, `scheme=https`
and `permanent=true` (checked with `helm template` against
`rke2-traefik` 41.2.003).

## RKE2's CIS profile

ADR 0009 (closed by default) asks for RKE2's CIS profile from the first
version. The profile is RKE2's built-in setting for the CIS Kubernetes
Benchmark, a common hardening standard. `config.yaml` sets `profile: cis`.
The facts below come from the source of RKE2 v1.37.1+rke2r1, the version
the download pins.

With `profile: cis`, RKE2:

- stops at its start when the host is not ready: the kernel must have the
  four values of RKE2's kernel settings file, and the `etcd` user and group
  must exist (`pkg/cli/cmds/profile_linux.go`). It also sets
  `protect-kernel-defaults` for the kubelet;
- runs etcd as the `etcd` user and writes its static pod files with mode
  0600 (`pkg/executor/staticpod/staticpod.go`);
- applies the "restricted" Pod Security Standard in every namespace,
  except the exempt namespaces of its pod security file (below);
- creates network policies in the namespaces `kube-system`, `kube-public`
  and `default` (below).

### The host steps

`prepare_cis_host` runs in step 3, after RKE2's installer and before RKE2
starts. Both steps change nothing when they ran before, because a second
run of `install.sh` is the update (#4736).

1. **The `etcd` user and group.** RKE2 looks them up in `/etc/passwd` and
   `/etc/group` only (hardening guide), so the step reads these two files,
   not `getent`. When both are missing, it runs the hardening guide's
   command, `useradd -r -c "etcd user" -s /sbin/nologin -M -U etcd`. When
   only the group exists, `useradd` gets `-g etcd` instead of `-U`. When
   only the user exists, `groupadd -r etcd` adds the group.
2. **The kernel settings.** RKE2 ships them as `rke2-cis-sysctl.conf`.
   RKE2's installer puts it in RKE2's share folder:
   `/usr/local/share/rke2/` for the tarball, `/opt/rke2/share/rke2/` for the
   tarball when `/usr/local` is read-only or a mount point, and
   `/usr/share/rke2/` for the RPM. `install.sh` always installs the tarball
   (`INSTALL_RKE2_ARTIFACT_PATH`), on Ubuntu and RHEL alike, but the step
   looks in all three places. It copies the file to
   `/etc/sysctl.d/60-rke2-cis.conf` (mode 0644), where `systemd-sysctl`
   loads it at every start of the machine. It copies only when the content
   differs, so a changed copy is replaced by RKE2's file. Then it reads
   every value of the file from `/proc/sys`. Only when one differs, it runs
   `systemctl restart systemd-sysctl.service`, as the hardening guide does,
   and reads the values again. A value that is still wrong means a file
   that `systemd-sysctl` reads later sets it again, for example
   `/etc/sysctl.conf`. The step then stops with one `[FAIL]` line that names
   the key, so RKE2 does not fail later with a less clear message. The
   values always come from RKE2's file; `install.sh` holds none of them.

### The pod security file

RKE2 writes its own pod security file, `/etc/rancher/rke2/rke2-pss.yaml`,
at every start, but only when its settings name no other file
(`pkg/rke2/rke2_linux.go`). With the CIS profile, that file enforces, audits
and warns at the "restricted" level, and exempts three namespaces:
`kube-system`, `compliance-operator-system` and `tigera-operator`
(`pkg/rke2/psa.go`).

`write_pod_security_config` writes
`/etc/rancher/rke2/meho-pss.yaml` (mode 0600), and `config.yaml`
names it with `pod-security-admission-config-file`. It is RKE2's own file
with one more exempt namespace: `local-path-storage`. RKE2 mounts the file
into the API server, because its path is an argument of the API server.
`write_rke2_config` writes it before `config.yaml`, so the settings never
name a missing file, also with `--from-disk`. The name is the one of the
form-2 design (section 2). The appliance's cleaning step keeps this file
by that name (form-1 design), so the name must not change.

Why the storage namespace needs the exception: the local-path provisioner
starts a helper pod in its own namespace for each volume. The helper pod
mounts a folder of the host (`/var/lib/meho/volumes`), which even the
"baseline" level refuses. The provisioner itself also lacks fields that
"restricted" asks for, such as `runAsNonRoot` and a seccomp profile. No
MEHO namespace is exempt, and RKE2's own three
stay as they are. `kube-system` is exempt by RKE2's default, so RKE2's
Traefik, Canal and CoreDNS run as before.

Why a file and not a namespace label: the RKE2 docs name
`pod-security-admission-config-file` as the way to change the default. The
exemption then lives in a root-only file on the host. A namespace label
(`pod-security.kubernetes.io/enforce: privileged`) would also work, but
anyone who may edit namespaces could change it, and it would be a sixth
change to the upstream storage manifest. One difference: an exempt
namespace is not checked at all, so the audit log and the warnings skip it
too (Kubernetes docs, "Exemptions"). With a label, the restricted level
would still write audit notes for the namespace.

### Network policies and Traefik

With the profile, RKE2 creates these network policies (`pkg/rke2/np.go`,
`pkg/controllers/cisnetworkpolicy`). All of them limit only incoming
traffic.

| Namespace | Policy | What it lets in |
| --- | --- | --- |
| `kube-system`, `kube-public`, `default` | `default-network-policy` | traffic from pods of the same namespace |
| `kube-system`, `kube-public`, `default` | `rke2-flannel-host-networking` | traffic from the flannel address of each node (the first address of its pod range) |
| `kube-system` | `default-network-dns-policy` | TCP and UDP 53 to CoreDNS, from every address |
| `kube-system` | `default-network-traefik-policy` | the ports named `web` and `websecure` of the pods labelled `app.kubernetes.io/name: rke2-traefik`, from every address |
| `kube-system` | `default-network-ingress-policy`, `default-network-ingress-webhook-policy` | the same for ingress-nginx, which form 2 does not run |
| `kube-system` | `default-network-snapshot-validation-webhook-policy`, `default-network-metrics-server-policy` | `https` to the snapshot webhook and the metrics server |

RKE2's Traefik gets the console traffic on ports 80 and 443 through a host
port. The CNI `portmap` plugin turns that into a destination NAT to the
Traefik pod, so Calico checks the policies against the pod's ports. In
`rke2-traefik` 41.2.003 (checked with `helm template`), the Traefik pod has
the label `app.kubernetes.io/name: rke2-traefik` and the ports `web`
(container port 8000, host port 80) and `websecure` (container port 8443,
host port 443). So `default-network-traefik-policy` lets traffic from
every address in to both ports, and the console loads. MEHO adds no
network policy for Traefik.

The policies limit only traffic into `kube-system`. Traffic from Traefik
to the MEHO namespace is outgoing traffic of `kube-system`, which no RKE2
policy limits. The chart's own policies let the namespace `kube-system`
reach MEHO (`networkPolicy.ingressControllerNamespace: kube-system` in
`values-single-node.yaml`). No RKE2 policy is in `meho` or
`local-path-storage`.

### For later Tasks

`cis_state` (read-only) prints `on`, or `off: <reason>`, and returns 0 or
1, like `firewall_state`. On means: `config.yaml` sets `profile: cis`, the
pod security file exists, the `etcd` user and group are in `/etc/passwd`
and `/etc/group`, `/etc/sysctl.d/60-rke2-cis.conf` exists, and the kernel
has its values. `meho-host check` (#4638) can call it. `meho-host` sources
nothing today, so #4638 decides how the function reaches the machine, as
for `firewall_state`.

### A newer RKE2

`tests/fixtures/rke2-pod-security-cis.yaml` is RKE2's pod security file of
`pkg/rke2/psa.go`, and `tests/fixtures/rke2-cis-sysctl.conf` is
`bundle/share/rke2/rke2-cis-sysctl.conf`. A CI step compares both with the
source of the RKE2 version in `packaging/common.sh`. It also checks that
this version still has `default-network-traefik-policy` for the ports
`web` and `websecure`, from every address. When RKE2 moves to a new
version and the step fails, copy RKE2's new files to the fixtures, update
their `sha256` in `tests/test_cis.py`, and write the new pod security file
in `render_pod_security_config` with `local-path-storage` added. Check the
labels and port names of the new `rke2-traefik` chart by hand.

## The storage manifest

`storage/local-path-storage.yaml` is the upstream v0.0.37 manifest with
five changes:

- the StorageClass `local-path` has `storageclass.kubernetes.io/is-default-class: "true"`;
- the volumes live in `/var/lib/meho/volumes`;
- both images are pinned by digest. Upstream names the helper image
  `busybox` with no tag, so it is pinned to `busybox:1.37`. The digests are
  the multi-platform index digests on Docker Hub;
- the provisioner requests and is limited to `100m` CPU and `128Mi` memory.
  These values come from the example in the
  [upstream v0.0.37 chart values](https://github.com/rancher/local-path-provisioner/blob/v0.0.37/deploy/chart/local-path-provisioner/values.yaml#L123-L133).
  They reserve room for the controller and bound its resource use. Upstream
  leaves resources unset by default. These are initial bounds, not measured
  MEHO usage. Check CPU throttling and memory use before changing them for
  a larger number of volumes;
- the provisioner container has `securityContext.allowPrivilegeEscalation: false`.
  So a process in it cannot gain more rights than it started with, for
  example through a program with the setuid bit. The provisioner only talks
  to the Kubernetes API and starts the helper pods, so it needs no extra
  rights. This matters because the strict pod security rule does not check
  the storage namespace (#4636): the container's own `securityContext` is
  the only guard there. Upstream v0.0.37 sets no `securityContext`.

`tests/fixtures/local-path-storage-v0.0.37.yaml` is the upstream file,
unchanged. A test compares the two files and accepts only these changes.
The CI job also compares the fixture with the file on GitHub.

To move to a newer provisioner: replace the fixture with the new upstream
file, apply the five changes again, and update the digests and the
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
  output and record for them. The RKE2 installer stub puts RKE2's kernel
  settings file in RKE2's share folder, the `systemctl` stub loads the
  files of `/etc/sysctl.d` into the fake `/proc/sys` on
  `restart systemd-sysctl.service`, and the `useradd` and `groupadd` stubs
  add their lines to the fake `/etc/passwd` and `/etc/group`.

`tests/test_firewall.py` covers the admin network check, the three
firewall files, the order of step 2, firewalld and ufw, the port list and
`firewall_state`. It also checks that the chains `input`, `forward` and
`output` have policy `accept` and no drop rule, and that the chain `input`
accepts the same traffic as the chain `prerouting`, so the two cannot
drift apart. `tests/test_firewall_nft.py` checks the written rules
file with the real `nft` and the real kernel: `nft -c -f`, and a load
twice next to stand-in tables of kube-proxy and Canal. It also loads the
appliance variant, with the three policies set to `drop`, and opens a TCP
connection over `lo` with it. It runs in a new
user and network namespace (`unshare --map-root-user --net`), so the
machine's own firewall stays as it is. It needs `nft`, user namespaces and
a kernel with `nft_ct` and `nft_fib_inet`, and it is skipped without them.
The CI job installs `nftables` and sets `MEHO_REQUIRE_NFT_CHECK=1`, which
turns that skip into a failure.

`tests/test_cis.py` covers RKE2's CIS profile: the two lines in
`config.yaml`, the pod security file against RKE2's own file (only
`local-path-storage` added, no MEHO namespace), the order of the host steps
(after the installer, before the start of RKE2), each place of RKE2's
kernel settings file, values that come from RKE2's file, a later file that
sets a value again, the existing `etcd` user or group, a second run that
changes nothing, `--from-disk`, and every answer of `cis_state`.

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
v0.11.0 and Helm v3.16.4 (both pinned with a checksum) and nftables, runs
shellcheck on every `*.sh` file and every file with a shell shebang there,
checks the local-path fixture against upstream, checks the two RKE2
fixtures and the Traefik policy against the source of the pinned RKE2
version (see [A newer RKE2](#a-newer-rke2)), and runs the unit tests with
`MEHO_REQUIRE_HELM=1` and `MEHO_REQUIRE_NFT_CHECK=1`. Scripts that later
Tasks add in this folder are checked by the same job.

## Dependencies

- RKE2 v1.37.1+rke2r1 and its offline files, Helm v3.16.4 and the `meho`
  CLI, from the download that `build-download.sh` builds (#4734).
- On the machine: bash 4 or newer, coreutils, `awk`, `sed`, `grep`, `ss`
  and `ip` (iproute2), `systemctl`, `timedatectl`, `getent`, and
  `resolvectl` when `systemd-resolved` runs. Ubuntu 24.04 has all of them.
- For the firewall: `nft` from the `nftables` package (1.0.9 on Ubuntu
  24.04, in the server and cloud images), and a kernel with `nf_tables`,
  `nft_ct` and `nft_fib_inet` (modules of the Ubuntu 24.04 kernel; RHEL 9's
  firewalld uses the same parts). The download holds no OS packages, so a
  machine without `nft` stops in step 1.
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
- **The CIS profile is not proven on a real machine yet.** The tests here
  prove the files and the order of the steps, not RKE2 itself. The week-4
  install (#4741) checks that every pod is Ready under the profile, that no
  MEHO namespace needs an exception, and that the console loads through
  Traefik. An offline check of the chart, rendered with
  `values-single-node.yaml`, found no pod of the `meho` namespace that
  breaks the "restricted" rules, but only the API server's own check
  proves it.
- **A later sysctl file can undo a kernel setting.** The install stops when
  a file that `systemd-sysctl` reads after `60-rke2-cis.conf` sets one of
  RKE2's values again. If someone adds such a line after the install, RKE2
  does not start at the next start of the machine, and `cis_state` says
  `off` with the key.
- **IPv4 only.** The DNS check reads A records, and the ranges are IPv4.
  The admin network is IPv4 too, so SSH over IPv6 gets no answer.
- **SSH on port 22 only.** The rules open SSH on port 22. A session on
  another port stops the install (see [The admin network](#the-admin-network)).
- **The live proof is still open.** No test here runs RKE2 with Canal
  behind the rules. The week-4 install (#4741) checks them on a real
  machine: `ss -tulpn`, a port scan from inside and outside the admin
  network, the console in a browser, the redirect of port 80, and every
  pod Ready. It records the real port list, and the closed list in
  `lib/firewall.sh` follows it. It also sets the policies of the chains
  `input`, `forward` and `output` to `drop`, as the appliance does, and
  checks the CIS rules 4.3.5 to 4.3.8 with Canal running. The kernel test
  here proves only that nft loads that variant and that traffic over `lo`
  passes it.
- **A customer's `nftables.service`.** Its stop command (and on RHEL its
  reload) runs `flush ruleset`, which removes the MEHO table and the rules
  of kube-proxy and Canal. RKE2 keeps running until its next start, which
  then waits for the MEHO table. `meho-host check` will show the firewall
  as off (#4638).
- **More nodes later.** The rules drop RKE2's ports from every other
  machine, the VXLAN port 8472/UDP too. A join (`--join-address`) needs
  rules that open them to the other nodes' addresses (ADR 0009). That comes
  with the high-availability work. Until then, step 1 prints a `[WARN]`
  when `--join-address` is given: the admin adds accept rules for the
  other node's address on both machines by hand.

## References

Firewall:

- `nft(8)`: priority names (`mangle` -150, `dstnat` -100), `-c`/`--check`,
  `fib`, `ct`: https://www.netfilter.org/projects/nftables/manpage.html
- nftables wiki, Netfilter hooks (conntrack at -200 before mangle; a
  packet for this machine passes prerouting and input, a forwarded packet
  prerouting, forward and postrouting, a local packet output and
  postrouting):
  https://wiki.nftables.org/wiki-nftables/index.php/Netfilter_hooks
- nftables wiki, base chains (an accept goes on to later base chains of
  the same hook; a drop is final):
  https://wiki.nftables.org/wiki-nftables/index.php/Configuring_chains
- nftables wiki, atomic rule replacement with `nft -f`:
  https://wiki.nftables.org/wiki-nftables/index.php/Atomic_rule_replacement
- Ubuntu 24.04 `nftables` 1.0.9-1ubuntu0.1: `debian/nftables.service`
  (`ExecStop=/usr/sbin/nft flush ruleset`, not enabled by default) and
  `debian/nftables.conf` (`flush ruleset`):
  https://git.launchpad.net/ubuntu/+source/nftables/tree/debian?h=ubuntu/noble-updates
- RHEL 9 `nftables.service` (`flush ruleset` in reload and stop):
  https://gitlab.com/redhat/centos-stream/rpms/nftables/-/blob/c9s/nftables.service
- CNI `portmap` (host ports as DNAT from `PREROUTING`):
  https://www.cni.dev/plugins/current/meta/portmap/
- RKE2 known issues (firewalld conflicts with Canal; Canal's interfaces):
  https://docs.rke2.io/known_issues
- RKE2 HelmChartConfig for packaged charts: https://docs.rke2.io/helm
- `rke2-traefik` 41.2.003 (`hostPort: 80`, `hostPort: 443`,
  `ports.web.http.redirections`):
  https://rke2-charts.rancher.io/assets/rke2-traefik/rke2-traefik-41.2.003.tgz
- sudo `env_reset` and its default list of kept variables (sudo 1.9.15p5,
  `plugins/sudoers/env.c`); Ubuntu 24.04's `/etc/sudoers`:
  https://git.launchpad.net/ubuntu/+source/sudo/tree/debian/etc/sudoers?h=ubuntu/noble-updates
- CIS rules 4.3.5 and 4.3.8 as the SCAP Security Guide checks them (base
  chains on the hooks input, forward and output at priority 0, and policy
  `drop` on each):
  https://docs.datadoghq.com/security/default_rules/xccdf-org-ssgproject-content-rule-set-nftables-base-chain,
  https://docs.datadoghq.com/security/default_rules/xccdf-org-ssgproject-content-rule-nftables-ensure-default-deny-policy
- Linux `force_mld_version` (default 0: MLD version 2, with a fallback to
  version 1): https://docs.kernel.org/networking/ip-sysctl.html

RKE2's CIS profile (RKE2 v1.37.1+rke2r1):

- RKE2 hardening guide (the `etcd` user, the kernel settings file and its
  places, `systemctl restart systemd-sysctl`, what the profile does):
  https://docs.rke2.io/security/hardening_guide
- RKE2 pod security standards (`/etc/rancher/rke2/rke2-pss.yaml`,
  `pod-security-admission-config-file`):
  https://docs.rke2.io/security/pod_security_standards
- RKE2 source at the pinned tag: `pkg/rke2/psa.go` (the default pod
  security file), `pkg/rke2/rke2_linux.go` (RKE2 writes its file only when
  none is named), `pkg/cli/cmds/profile_linux.go` (the checks at the
  start), `pkg/rke2/np.go` and `pkg/controllers/cisnetworkpolicy/` (the
  network policies), `pkg/executor/staticpod/staticpod.go` (etcd as the
  `etcd` user; the API server gets the file as an argument), `install.sh`
  (`INSTALL_RKE2_TAR_PREFIX`, `/opt/rke2`), and
  `bundle/share/rke2/rke2-cis-sysctl.conf`:
  https://github.com/rancher/rke2/tree/v1.37.1%2Brke2r1
- Kubernetes pod security admission, exemptions (an exempt namespace is
  not checked, audited or warned about):
  https://kubernetes.io/docs/concepts/security/pod-security-admission/#exemptions
- `rke2-traefik` 41.2.003 (the label `app.kubernetes.io/name: rke2-traefik`,
  the ports `web` and `websecure`):
  https://rke2-charts.rancher.io/assets/rke2-traefik/rke2-traefik-41.2.003.tgz

Install:

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
