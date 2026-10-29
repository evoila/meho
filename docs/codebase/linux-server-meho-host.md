# Form-2 host command: `deploy/linux-server/meho-host` (#4737, #4638)

## Overview

`meho-host` is the customer's tool for the work on a form-2 machine after
the install. `install.sh` copies it to `/usr/local/sbin/meho-host` in
step 2, with the two files of `lib/` it sources in
`/usr/local/lib/meho-host/`, and every later run installs the version of
its download. The
customer never needs kubectl: that is ADR 0007 decision 2 (the support rule)
and the form-2 design, section 5, the `meho-host` table (meho-internal
`docs/design/2026-10-10-install-forms/form-2-linux-server.md`).

This version has four commands:

- **`meho-host check [--json]`** prints a red/green list of the built-in
  Kubernetes (RKE2): the node, the pods, the disk and RKE2's certificates.
  Three more lines show the hardening of the machine (#4638): the MEHO
  host firewall, RKE2's CIS profile and the console certificate. Every
  line that is not ok ends with `Fix:` and what to do.
- **`meho-host support-bundle`** writes one file for evoila support to
  `/var/lib/meho/support/` and prints its path. Passwords, tokens and keys
  are removed from it. The customer sends the file to support and then
  deletes it; the command says so on its second line.
- **`meho-host admin-network <range>`** and **`meho-host admin-network none`**
  (#4739) set the addresses that may use SSH, in the MEHO firewall file
  `/etc/meho/firewall.nft` (#4635): one IPv4 range such as `10.0.50.0/24`,
  or one address. `none` closes SSH from every address, so only the console
  works. Only the SSH rule changes. Over SSH, the command asks first when
  the new range does not hold the admin's own address.
- **`meho-host uninstall`** (#4739) removes MEHO and the built-in Kubernetes
  from the machine, with all their data. It asks twice. The MEHO firewall
  stays on, and the last line names the command that removes it.

All need root. Exit codes: `check` exits 0 when every line is ok or a
warning, and 1 when a line is red; the other commands exit 0 when they are
done. Any command exits 1 with one `[FAIL]` line when it cannot do its job,
or with `Stopped. Nothing was changed.` when the admin answers a question
with anything but `yes`, and 2 for wrong usage.

A later Task adds the commands `certificate`, `keyholder` and `unseal`
(#4738). `meho doctor` runs `check --json` when it runs on the server
(#4740). The fourth hardening line of #4638, a scan of the operating
system against the CIS Linux benchmark, is not built yet (see Known
issues).

## Key files

| File | What it holds |
| --- | --- |
| `meho-host` | One bash script: the output helpers, the seven checks with the rule for the console certificate (`certificate_state`), the bundle and its scrub, the SSH rule rewrite and the uninstall. It sources `lib/firewall.sh` and `lib/cis.sh` (see [How meho-host finds its lib files](#how-meho-host-finds-its-lib-files)). It carries its own copies of the IPv4 helpers of `lib/net.sh` and of the paths of `lib/common.sh` that it needs. |
| `lib/firewall.sh` | `firewall_state` (the firewall line) and the firewall paths `MEHO_FIREWALL_FILE`, `MEHO_FIREWALL_UNIT` and the others. |
| `lib/cis.sh` | `cis_state` (the CIS profile line). It reads `RKE2_CONFIG_FILE`, which `meho-host` defines as `lib/common.sh` does. |
| `lib/prepare.sh` | `install_meho_host`: the copy to `/usr/local/sbin`, mode 0755, and of the two lib files to `/usr/local/lib/meho-host`, mode 0644. |
| `lib/common.sh` | `MEHO_HOST_FILE`, `MEHO_HOST_PATH`, `MEHO_HOST_LIB_DIR`, `MEHO_HOST_LIB_FILES`, and `meho-host` in `DOWNLOAD_FILES`. |
| `packaging/parts.sh`, `packaging/common.sh` | The download ships `meho-host` next to `install.sh`, executable. |
| `tests/host_harness.py` | A fake installed machine: stub `kubectl`, `rke2`, `journalctl`, `df`, `free`, `ip`, `date`, `nft`, `systemctl` and friends, the files of RKE2's CIS profile, and the real `openssl`. `console_certificate()` makes test certificates with fixed dates. `run()` takes the answers to the questions (`stdin`) and an SSH session (`ssh_client`). |
| `tests/test_meho_host.py` | The tests of the four commands and of each hardening line, and of the install by `install.sh`. |
| `tests/fixtures/firewall-admin-network.nft` | The firewall file `install.sh` writes with `--admin-network 10.0.50.0/24`; the `admin-network` and `uninstall` tests start from it. |

The script has the same shape as `install.sh`: `set -Eeuo pipefail`, an ERR
trap that prints one `[FAIL]` line, `fail` and `usage_error`, and the
`MEHO_TEST_ROOT` seam, which puts every machine path below a test folder.

## Control flow

### `check`

Each check calls `result <name> <status> <detail> [fix]`. The seven results
are printed at the end, as text or as JSON, in this order:

| Line | ok when | warn when | red when | Source of the facts |
| --- | --- | --- | --- | --- |
| `node` | every node has the condition Ready | never | a node is not Ready, or RKE2 does not answer | `kubectl get nodes -o jsonpath=...` |
| `pods` | every pod in `meho`, `kube-system` and `local-path-storage` is Running and Ready, or Succeeded | a pod the kubelet evicted (phase Failed, reason Evicted) while the other pods run | a pod is in another state, there is no pod in `meho`, or RKE2 does not answer | `kubectl get pods -A -o jsonpath=...` |
| `disk` | `/var/lib/rancher` and `/var/lib/meho` both have 20 % or more free | the lower of the two is below 20 % | below 15 %, or a path cannot be read | `df -P -k <path>` |
| `certificates` | every row of RKE2's list expires in 30 days or more | a row has less than 30 days left | a row is EXPIRED or its date has passed; a row is NOT YET VALID (the clock or the certificate is wrong; its EXPIRES column holds the start date); the command did not run; the list is empty | `/usr/local/bin/rke2 certificate check --output table` (by its path: sudo's `PATH` on RHEL has no `/usr/local/bin`) |
| `firewall` | `firewall_state` says `on`: the table `inet meho` is loaded, its chain `prerouting` filters the hook `prerouting` with `policy drop`, and `meho-firewall.service` is enabled | never | `firewall_state` says `off: <reason>`, also `off: nft not found` when `nft` is missing | `nft list table inet meho`, `nft list chain inet meho prerouting`, `systemctl is-enabled meho-firewall.service` |
| `cis-profile` | `cis_state` says `on`: `config.yaml` sets `profile: cis`, the pod security file exists, the `etcd` user and group exist, `/etc/sysctl.d/60-rke2-cis.conf` exists and the kernel has its values | never | `cis_state` says `off: <reason>` | the files of [RKE2's CIS profile](linux-server-install.md#rke2s-cis-profile) and `/proc/sys` |
| `console-certificate` | the rule below holds and more than 30 days are left | the rule holds, but 30 days or less are left | the certificate breaks the rule; the Secret cannot be read; the host name is not in `/etc/meho/install.json`; `openssl` is missing | the Secret `meho-core-tls` (`tls.crt`, `ca.crt`), `/etc/meho/install.json`, `openssl` |

**The rule for the console certificate.** `certificate_state` holds the
rule in one place. The certificate is the one the console, the API and the
login show: the TLS Secret of the Ingress, `meho-core-tls`
(`ingress.tls.secretName` in `values-single-node.yaml`). The install makes
it for the host name, and `meho-host certificate` (#4738) puts the
customer's own certificate into the same Secret. The check reads only
`tls.crt` (the server certificate, then the intermediate certificates) and
`ca.crt` (the CA the clients trust), each with its own Go template. It never
reads `tls.key`. The host name comes from `options.hostname` in
`/etc/meho/install.json`. The rule:

1. The server certificate is valid now: not before its start date, and not
   at or after its end date.
2. It names the host name. This is OpenSSL's host check
   (`openssl x509 -checkhost`): the DNS names of the subject alternative
   names, a wildcard only in the first label, and the common name only when
   there is no DNS name.
3. Its chain ends at `ca.crt`, and every certificate of the chain is valid
   now: `openssl verify -attime <now> -partial_chain -CAfile <ca.crt>
   -untrusted <tls.crt> <tls.crt>`. `-partial_chain` lets `ca.crt` be an
   intermediate CA. Without `ca.crt`, the trust store of the machine is the
   anchor.
4. More than 30 days are left.

Points 1 to 3 make a red line, which names every problem it finds. Point 4
alone makes a warning. Every time check uses one "now", `date -u +%s`, so
the tests can fix it. The first-start list (#4042) checks the console
certificate in the backplane, in Python. It must use the same rule, with
the same 30 days, so the two lists never disagree. `meho-host certificate`
(#4738) can call `certificate_state` to check a new certificate before it
writes the Secret.

**The fix lines of the hardening lines.** The firewall line names the two
`systemctl` commands that load the MEHO firewall and keep it on, and its
log. The CIS profile line asks to set back what it names, as `install.sh`
set it up, and to restart RKE2. The certificate line sends the customer to support until
`meho-host certificate` (#4738) exists; that Task changes the hint.

**Why Succeeded counts as fine.** RKE2's `helm-install-*` Jobs in
`kube-system` and the core's bootstrap Job leave finished pods behind. They
are in phase Succeeded and never Ready. A rule "every pod is Running and
Ready" would never be green.

**Why an evicted pod is a warning.** When the disk was full, the kubelet
evicts a pod and leaves it in the list as Failed with the reason Evicted.
After the disk is free again and the replacement runs, the entry stays
until someone removes it, and the customer has no kubectl. So it is a
warning with a fix, not a red line the customer cannot clear.

**Why 15 % is red.** The kubelet evicts pods when the free space of the file
system that holds the container images falls below 15 % (its default
`imagefs.available<15%`), and when the node's own file system falls below
10 % (`nodefs.available<10%`). On a form-2 machine both are
`/var/lib/rancher`, so 15 % is the line that matters.

**How the certificate table is read.** The table has the columns FILENAME,
SUBJECT, USAGES, EXPIRES, RESIDUAL TIME and STATUS. SUBJECT and USAGES may
hold spaces, so a row is not split into fields. A regular expression reads
the file name at the start, the EXPIRES date (the Go layout
`Jan 02, 2006 15:04 MST`, for example `Jun 12, 2026 11:12 UTC`) and the
STATUS at the end. GNU `date -d` turns the date into seconds. A STATUS of
EXPIRED is red even when the date is in the future. RKE2 itself renews a
certificate at its start when it has expired or has less than 120 days left,
so the fix on both lines is a restart of `rke2-server`.

The text form prints `[ok]   `, `[WARN] ` and `[FAIL] ` lines on standard
output, all of them, so the list stays in order when it is piped. The JSON
form is

```json
{"checks":[{"name":"node","status":"ok","detail":"Ready","hint":""}, ...]}
```

with `status` one of `ok`, `warn` and `fail`. `hint` is empty on an ok line.

### `support-bundle`

1. Make a work folder `/var/lib/meho/support/.build.XXXXXX`, and two files
   next to it for the Secret values and the sed rules (`.secrets.XXXXXX`,
   `.sed.XXXXXX`). `mktemp` makes all three for root only, and the support
   folder is 0700. An EXIT trap removes all three.
2. Collect, each part into its own file. `collect <file> <command>` keeps
   the command's output and error messages. When a command fails, the file
   says so at its end, and the bundle is still written: support needs it
   most when something is down.

   | File in the bundle | Content | Command |
   | --- | --- | --- |
   | `check.json` | the check list | the checks above, as JSON |
   | `pods/<namespace>.<pod>.log` | the logs of every pod in `meho`, and of the `kube-system` pods whose name starts with `rke2-traefik-` | `kubectl logs --all-containers --timestamps` |
   | `pods/<namespace>.<pod>.previous.log` | the logs of the previous run of the containers, only when there was one | the same with `--previous` |
   | `objects/<namespace>.txt` | the objects of `meho`, `kube-system` and `local-path-storage`, as a table | `kubectl get all,ingress,pvc,configmap,networkpolicy -o wide` |
   | `objects/<namespace>.secrets.txt` | the Secrets by name, type and number of keys | `kubectl get secrets` |
   | `events.txt` | the events of every namespace | `kubectl get events -A --sort-by=.metadata.creationTimestamp` |
   | `node.txt` | the node | `kubectl describe node` |
   | `rke2-server.journal.txt` | RKE2's log of the last 24 hours | `journalctl -u rke2-server --since -24h --no-pager -o short-iso` |
   | `os/*.txt` | facts about the operating system | `/etc/os-release`, `uname -a`, `free -m`, `df -h`, `ip addr`, `ip route`, `timedatectl` |
   | `install.json` | the install record, when it exists | a copy of `/etc/meho/install.json` |

3. Scrub every file in the work folder with one `sed` run. The rules go
   into the sed script file, never on `sed`'s command line: every user on
   the machine can read the arguments of a running process, and a form-2
   machine is the customer's server.
   - The value of any variable whose name holds PASSWORD, TOKEN, SECRET or
     KEY, in any case, written as `NAME=value`, `NAME: value` (with or
     without the space) or as the JSON pair `"name": "value"` (MEHO's own
     logs are JSON lines), becomes `[removed]`. `key` counts when it ends
     the name or when a digit, `_`, `.` or `-` follows it (`api_key`,
     `apiKey`, `unseal-key`, `KEY_ID`), so `keycloak` is not a key name and
     Keycloak's addresses and image names stay readable.
   - Every known secret value becomes `[removed]` wherever it appears, for
     example in a log line. The values come from the core's Secrets in
     namespace `meho`: `kubectl get secrets -o go-template=...` decodes
     them straight into the root-only values file, the way `install.sh`
     reads the first-login Secret. Helm's release records (Secrets of type
     `helm.sh/release.v1`, one per revision) are left out: each holds the
     whole gzipped release as one line of hundreds of KB, which never
     appears in a log and would cost `sed` hundreds of MB of memory as a
     rule. Keys with `user` in the name hold plain
     names such as `admin`, and values shorter than 8 characters are words,
     so both stay: removing "admin" everywhere would also hit
     `client-admin.crt` and `/admin/realms/`. A value on more than one line,
     such as a certificate, gives only its first line. The two one-time
     files (`/var/lib/meho/unseal-key` and the `*_password` lines of
     `/var/lib/meho/first-login-passwords`) are read too while they exist;
     the guide tells the customer to delete them after the first login, so
     the Secrets are the source that stays.

   Secret data never gets into the bundle: it lists Secrets by name only,
   and the values file is removed with the work folder. The fake kubectl of
   the tests gives Secret content only to that Go template, so the bundle
   cannot read it in another way.
4. Pack with `umask 077` into `meho-support-<host>-<UTC time>.tar.gz`
   (mode 0600; `<host>` is `hostname -f` in lower case, and the time is
   `date -u +%Y%m%dT%H%M%SZ`), print the path, and on a second line say to
   send the file to support and then delete it. Old bundles are not removed
   by the command.

### `admin-network`

1. Check the value: `none`, or an IPv4 address or range, with the rules of
   `install.sh --admin-network` (the patterns of `lib/net.sh`): a range must
   start at its first address, so `10.0.50.7/24` is refused. Anything else
   is a usage error, before root is checked. A `/0` range gets a `[WARN]`:
   it allows SSH from every address.
2. Find the set `admin_network` in `/etc/meho/firewall.nft`. Without it,
   the file is not the one `install.sh` wrote, and the command stops with
   a `[FAIL]` line.
3. Find the admin's own address: `SSH_CLIENT` or `SSH_CONNECTION`, and when
   sudo has removed both from the environment, the same values in the
   environment of the processes above this one, as `ssh_connection` in
   `lib/firewall.sh` does. On the console there is none. When there is one
   and the new range does not hold it (`none` holds no address; an IPv6
   session is outside every IPv4 range), the command warns that this
   session stays open but a new login would get no answer, and asks. Only
   `yes` goes on; a closed standard input counts as no.
4. Write the new file next to the old one with `mktemp` (root only), with
   only two lines changed: the `elements` line of the set, which `none`
   removes, and the note above the set, which becomes
   `# Set with meho-host admin-network.` Every other line stays, so a
   customer's own rules survive. `awk` does the rewrite.
5. Check the new file with `nft -c -f`. When nft refuses it, the new file
   is removed, nft's message is printed, and the old file stays, unchanged
   and still loaded.
6. Move the new file over the old one and load it with `nft -f`. The file
   replaces only the table `inet meho` (#4635), so this is what
   `systemctl reload meho-firewall.service` does, without depending on
   which unit loads the file: on the appliance that is `nftables.service`.
   Packets of connections that are already open always pass the rules, so
   the SSH session that runs the command stays open.

### `uninstall`

1. Print what is removed, then ask twice. Only `yes` goes on.
2. Run `/usr/local/bin/rke2-uninstall.sh`, RKE2's own uninstall script of
   a tarball install. It stops RKE2 and removes its units, its binaries,
   the cluster data in `/var/lib/rancher` and itself. When it fails, the
   command stops before the MEHO files, and a second run goes on. When the
   script is not there, RKE2 is already gone, and the command says so and
   goes on.
3. Remove `/var/lib/meho` (the volumes and the one-time files) and every
   entry of `/etc/meho` except `firewall.nft`, and the drop-in
   `/etc/systemd/system/rke2-server.service.d/meho-firewall.conf`, which
   would stop a later RKE2 install once the firewall is gone, with its
   folder when that is empty (RKE2's script removes only the
   `rke2-*.service` files). Then `systemctl daemon-reload`.
4. Remove the two commands `install.sh` put on the machine,
   `/usr/local/bin/meho` and `/usr/local/sbin/meho-host`, and the folder
   `/usr/local/lib/meho-host`. bash keeps the running script open and has
   read the two lib files at its start, so `meho-host` can remove itself
   and them and still print its last lines.
5. The firewall file, its boot unit and the loaded table stay, so the
   machine is never without a firewall (ADR 0009). The last line names the
   command that removes them, for when the customer's own firewall is
   ready.
6. The host steps of RKE2's CIS profile stay too (#4636): the kernel
   settings file `/etc/sysctl.d/60-rke2-cis.conf` and the `etcd` user and
   group. RKE2's script removes `/etc/rancher/rke2`, so the pod security
   file `meho-pss.yaml` goes with it. A later install uses the two that
   stay as they are.

### How meho-host finds its lib files

`meho-host` sources `lib/firewall.sh` and `lib/cis.sh`, the files that
`install.sh` uses too, so the firewall line and the CIS line have one
source of truth with the install. Both files only define constants and
functions when they are sourced. `meho-host` looks for them in two places,
both relative to its own folder (`cd -P` and `pwd -P` of
`dirname "${BASH_SOURCE[0]}"`):

1. `lib/` next to it: the download and the repository;
2. `../lib/meho-host/`: on the machine, `/usr/local/sbin/../lib/meho-host`,
   which is `/usr/local/lib/meho-host`.

Step 2 of `install.sh` (`install_meho_host`) copies the files of
`MEHO_HOST_LIB_FILES` there, so every install and every later run of
`install.sh` brings the versions of its download. When neither place
holds both files, `meho-host` stops at once with a `[FAIL]` line that
names the folder to copy them to. Because `lib/firewall.sh` defines the
firewall paths as `readonly`, `meho-host` takes them from there and keeps
no copy of its own. A function that `meho-host` defines after the
`source` lines would replace a function of the same name from `lib/`;
there is none today.

## Testing

`tests/host_harness.py` builds a fake installed machine for each test, the
way `tests/harness.py` does for `install.sh`: `MEHO_TEST_ROOT` is a temporary
root folder with the `kubectl` and `rke2` stubs at their real places, and `PATH` holds
only stubs and logging wrappers of a few real tools (`bash`, `awk`, `sed`,
`grep`, `find`, `tar`, `gzip` and some coreutils). The `date` stub answers "now"
(2026-10-23 12:00 UTC) and the bundle's time stamp from files, and passes
every other call on to the real `date`, which parses the certificate dates.
`certificate_table`, `pod_line` and `df_answer` build the stubs' answers.
Every real tool runs behind a wrapper that logs its arguments like a stub,
so the bundle test checks that no secret value was on any command line.
The `admin-network` tests start from the fixture file and read the calls
log to prove that `nft -c -f` ran on the new file before `nft -f` on the
real one; a stubbed `nft` that refuses the file proves the old file stays.
`test_firewall_nft.py` runs `meho-host admin-network` with the real `nft`
too, in a user and network namespace, on the file `install.sh` wrote, for
a range and for `none`. CI requires it (`MEHO_REQUIRE_NFT_CHECK=1`).
The `uninstall` tests put a fake `rke2-uninstall.sh` on the fake machine,
which removes RKE2's unit and data the way the real one does.

For the hardening lines, the fake machine has the files of RKE2's CIS
profile as `install.sh` writes them, the kernel values in `/proc/sys`, an
`nft` stub that answers `list table` and `list chain` from files, and a
`systemctl` stub that answers `is-enabled`. The check tests add the
install record. The console certificate is checked by the real `openssl`.
`console_certificate()` makes the test certificates with `openssl ca`,
which takes fixed start and end dates around the fixed "now" of the
`date` stub, so no test depends on the day it runs. The tests cover each
line on and off: the firewall without `nft`, without the table, with a
chain `prerouting` that does not drop, and with the unit disabled; the CIS
profile without `profile: cis`, without the `etcd` user and with another
kernel value; a valid certificate, one with an intermediate CA in
`tls.crt`, 20, 30 and 31 days left, another host name, an expired one, one
not yet valid, one of another CA, no `ca.crt`, no certificate, an
unreadable Secret, no install record and no `openssl`. They also prove
that no call reads `tls.key`. `test_firewall_nft.py` runs `firewall_state`
with the real `nft` on the loaded file, so its check of the chain
`prerouting` matches the real output of `nft`. `InstallTests` runs the
installed `meho-host --help` from the fake root, which proves that it
finds the files at `../lib/meho-host`.

The parent-process walk of `admin-network` reads `/proc` below the fake
root, so the test process must not be PID 1: in a container, run the tests
with `docker run --init`.

```bash
python3 -m unittest discover -s deploy/linux-server/tests -v
shellcheck deploy/linux-server/meho-host
```

The scripts need bash 4 or newer and GNU `date` and `sed`, so on macOS run
the tests in a Linux container, for example
`docker run --rm --init -v "$PWD/deploy/linux-server:/work/deploy/linux-server:ro" -w /work python:3.14-slim python -m unittest discover -s deploy/linux-server/tests`. CI (`.github/workflows/linux-server-test.yml`)
runs shellcheck on `meho-host` too: it finds every file with a shell shebang
in the folder.

## Dependencies

On the machine: bash, coreutils (`date`, `df`, `find`, `mktemp`), `sed`,
`awk`, `grep`, `tar`, `gzip`, `hostname`, `free` (procps), `ip` (iproute2),
`journalctl`, `systemctl` and `timedatectl` (systemd), `nft` (nftables,
which `install.sh` step 1 requires), `openssl` (Ubuntu's `ca-certificates`
package depends on it), RKE2's own `kubectl` at
`/var/lib/rancher/rke2/bin/kubectl` with `/etc/rancher/rke2/rke2.yaml`,
`rke2` at `/usr/local/bin/rke2` and `rke2-uninstall.sh` next to it. Ubuntu
24.04 has all of them.

## Known issues

- **The CIS Linux benchmark scan is not built.** #4638 asks for a fourth
  hardening line: a scan of the customer's operating system against the
  CIS benchmark, Level 1, with known exceptions such as IP forwarding. It
  needs a scanner and its rules in the download. The research and the
  recommendation are on #4638; the line comes with a later change.
- **The certificate line reads one Secret by name.** It reads
  `meho-core-tls`, the name of `values-single-node.yaml`. `meho-host
  certificate` (#4738) keeps that name. A change of
  `ingress.tls.secretName` needs a change of `CONSOLE_TLS_SECRET`.
- **The certificate line checks the Secret, not the connection.** It does
  not connect to port 443, so it does not prove that Traefik serves this
  Secret. The first-start list (#4042) fetches the certificate through the
  console address.

- **The Traefik pods are found by name.** The bundle takes the `kube-system`
  pods whose name starts with `rke2-traefik-`, which is how RKE2's
  `rke2-traefik` chart names them (the `helm-install-rke2-traefik` Job pod
  is left out). The week-4 lab install (#4741) proves it.
- **The scrub reads whole lines.** In the `NAME=value` form a value with a
  space in it is removed only up to the space, unless it is in quotes. The
  two one-time files hold no spaces, and their values are removed as a
  whole.
- **Text the scrub misses.** `Authorization: Bearer <token>`, a password
  inside a database address, written as `user:password@host`, and SQL
  `PASSWORD '<value>'` are not removed. They are rare in MEHO's logs; #5012
  adds them.
- **No size limit on the logs.** A pod with a very large log makes a large
  bundle. `kubectl logs --tail` can be added when that happens.
- **Not root is a `[FAIL]` line, not JSON.** `meho doctor` (#4740) runs
  `check --json` only as root.
- **`admin-network` finds the set by its exact lines.** It looks for
  `    set admin_network {` and `        flags interval` as `install.sh`
  writes them, and checks that the new set is exactly the one asked for.
  A set that is not in the form `install.sh` writes gives a `[FAIL]` line,
  and nothing changes. The rewrite is meant for the file as `install.sh`
  wrote it.
- **`uninstall` leaves the OS packages that RKE2's installer added** (out
  of scope in #4739).

## References

- RKE2 certificates, `rke2 certificate check` and the renewal at start:
  https://docs.rke2.io/security/certificates
- The table format, in RKE2's upstream k3s code, `pkg/cli/cert/cert.go`: the
  header `FILENAME SUBJECT USAGES EXPIRES RESIDUAL TIME STATUS`, the layout
  `Jan 02, 2006 15:04 MST`, the statuses OK, WARNING, EXPIRED and
  NOT YET VALID
- Traefik in RKE2 (the `rke2-traefik` chart):
  https://docs.rke2.io/networking/networking_services
- `kubectl logs` (`--previous`, `--all-containers`, `--timestamps`):
  https://kubernetes.io/docs/reference/kubectl/generated/kubectl_logs/
- `kubectl get` (`--sort-by`, `-o wide`, several types in one call):
  https://kubernetes.io/docs/reference/kubectl/generated/kubectl_get/
- kubectl JSONPath (`range`, filters):
  https://kubernetes.io/docs/reference/kubectl/jsonpath/
- `journalctl --since`, `--no-pager`, `-o short-iso`:
  https://www.freedesktop.org/software/systemd/man/latest/journalctl.html;
  relative times:
  https://www.freedesktop.org/software/systemd/man/latest/systemd.time.html
- The kubelet's eviction thresholds:
  https://kubernetes.io/docs/concepts/scheduling-eviction/node-pressure-eviction/
- ADR 0007 decision 2 (the support rule), meho-internal
  `docs/decisions/0007-kubernetes-inside-and-ha-path.md`.
- ADR 0009 (closed by default; "Work": how the customer sets the admin
  network after the install), meho-internal
  `docs/decisions/0009-closed-by-default.md`; the form-2 design, section 5
  (`admin-network`, `uninstall`) and section 11 (SSH and the admin network).
- The MEHO firewall file and its unit: `docs/codebase/linux-server-install.md`, "The host firewall"
  (#4635), `lib/firewall.sh`.
- RKE2 uninstall (`/usr/local/bin/rke2-uninstall.sh` for a tarball install):
  https://docs.rke2.io/install/uninstall
- `nft(8)`: `-c`, `--check` checks a rule file without applying it; `-f`
  reads the rules from a file; `list table` and `list chain` print a base
  chain with its `type`, `hook`, `priority` and `policy` on one line.
- OpenSSL 3.0 `openssl-verify(1)` and `openssl-verification-options(1)`:
  `-attime`, `-partial_chain`, `-CAfile`, `-untrusted`; the verify error
  codes 9 (not yet valid) and 10 (expired).
- OpenSSL 3.0 `openssl-x509(1)`: `-checkhost`, `-startdate`, `-enddate`;
  `X509_check_host(3)` for the host name rules.
- OpenSSL 3.0 `openssl-ca(1)`: `-startdate` and `-enddate`, which the tests
  use to make certificates with fixed dates.
