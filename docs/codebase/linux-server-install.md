# Form-2 install script: `deploy/linux-server/install.sh` (#4733, #4635)

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
   (both mode 0700). Then it turns on the MEHO host firewall and turns
   firewalld or ufw off (see [The host firewall](#the-host-firewall)).
   Then it writes RKE2's settings, copies the join token when the join
   options are given, copies the MEHO image archive and the storage
   manifest to the folders where RKE2 reads them, and writes the setting
   that makes RKE2's Traefik redirect port 80 to 443.
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
| `lib/firewall.sh` | The host firewall: the checks of step 1 (`nft`, the admin network), the files and the start in step 2, `firewall_state` and `firewall_port_list`. |
| `lib/prepare.sh` | Step 2: folders, `config.yaml`, join token, image archive, storage manifest, the Traefik redirect. |
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
| nftables | `nft` is on the machine and can read the kernel's tables | `command -v nft`, `nft list tables` |
| admin network | SSH stays possible for the admin after the install; see [The admin network](#the-admin-network) | `--admin-network`, `SSH_CONNECTION` |
| join and firewall | with `--join-address`, a `[WARN]` that the rules block a join until both machines accept the other node | `--join-address` |

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
| `/etc/rancher/rke2/config.yaml` | 0600 | `write-kubeconfig-mode: "0600"`, `tls-san`, `ingress-controller: traefik`, `cluster-cidr`, `service-cidr`; with the join options also `server` and `token-file` |
| `/etc/meho/join-token` | 0600 | a copy of `--join-token-file`, only with the join options |
| `/var/lib/rancher/rke2/agent/images/meho-images.linux-amd64.tar.zst` | 0600 | the MEHO image archive |
| `/var/lib/rancher/rke2/server/manifests/local-path-storage.yaml` | 0600 | the storage manifest |
| `/var/lib/rancher/rke2/server/manifests/rke2-traefik-config.yaml` | 0600 | a `HelmChartConfig` for `rke2-traefik`: port 80 redirects to 443 |

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

Until `meho-host admin-network` exists (#4739), the admin changes the set
`admin_network` in `/etc/meho/firewall.nft` and runs
`systemctl reload meho-firewall.service`. The comment above the set says
where its value came from: `--admin-network`, the SSH session, or `none`.

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
- The SSH rule reads the set `admin_network`. So `meho-host admin-network`
  (#4739) can change only that set, check the file with `nft -c -f`, and
  load it again.

### The redirect of port 80

`write_traefik_config` writes a `HelmChartConfig` named `rke2-traefik` in
`kube-system` into RKE2's manifests folder. It sets
`ports.web.http.redirections.entryPoint` to `websecure`, scheme `https`,
permanent. The chart turns this into the Traefik arguments
`--entryPoints.web.http.redirections.entryPoint.to=:443`, `scheme=https`
and `permanent=true` (checked with `helm template` against
`rke2-traefik` 41.2.003).

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
- For the firewall: `nft` from the `nftables` package (1.0.9 on Ubuntu
  24.04, in the server and cloud images), and a kernel with `nf_tables`,
  `nft_ct` and `nft_fib_inet` (modules of the Ubuntu 24.04 kernel; RHEL 9's
  firewalld uses the same parts). The download holds no OS packages, so a
  machine without `nft` stops in step 1.

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

Install:

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
