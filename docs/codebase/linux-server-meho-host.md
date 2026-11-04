# Form-2 host command: `deploy/linux-server/meho-host` (#4737, #4638, #4738)

## Overview

`meho-host` is the customer's tool for the work on a form-2 machine after
the install. `install.sh` copies it to `/usr/local/sbin/meho-host` in
step 2, with the two files of `lib/` it sources in
`/usr/local/lib/meho-host/`, and every later run installs the version of
its download. The
customer never needs kubectl: that is ADR 0007 decision 2 (the support rule)
and the form-2 design, section 5, the `meho-host` table (meho-internal
`docs/design/2026-10-10-install-forms/form-2-linux-server.md`).

This version has eight commands:

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
- **`meho-host log-server <host>[:<port>] [--tls <ca.pem>]`** and
  **`meho-host log-server none`** (#5752) work only on the MEHO appliance
  (install form 1). They send the logs of the machine to the customer's log
  server, which CIS Ubuntu 24.04 rule 6.1.3.6 asks for. rsyslog sends them
  over TCP, to port 514 unless one is given. With `--tls` it sends over TLS,
  to port 6514 unless one is given, and only to a server whose certificate
  the given CA signed and that names the host. The command also adds one
  rule to the MEHO firewall, so the machine can reach that server and port.
  `none` removes both. On a form-2 machine the OS and its logging belong to
  the customer, so the command changes nothing there and says so.
- **`meho-host uninstall`** (#4739) removes MEHO and the built-in Kubernetes
  from the machine, with all their data. It asks twice. It also removes the
  host changes of RKE2's CIS profile (#5133): the kernel settings file, and
  the `etcd` user and group when the install created them. An `etcd` user
  or group that existed before the install stays. The MEHO firewall stays
  on, and a line names the command that removes it. The last line says
  that a restart of the machine brings back the operating system's own
  kernel values.
- **`meho-host certificate --cert <chain.pem> --key <key.pem>`** (#4738)
  puts the customer's certificate into the console's TLS Secret
  `meho-core-tls`, after it checks the chain, the key, the host name and
  the dates. When a check fails, it changes nothing and prints one
  `[FAIL]` line. At the end it prints the certificate line of `check`.
- **`meho-host keyholder on`** and **`meho-host keyholder off`** (#4738)
  switch OpenBao between "a keyholder unseals it after every restart" and
  "it unseals itself". `on` deletes the unseal Secret
  `meho-core-vault-unseal`, `off` creates it again, and both restart
  OpenBao and wait for the result.
- **`meho-host unseal`** (#4738) unseals OpenBao by hand in keyholder mode
  and says `unsealed` or `sealed`.

`keyholder` and `unseal` read the unseal key from a hidden prompt, or one
line of standard input when it is not a terminal. The key is never an
argument of a command, and `meho-host` never prints it (see
[The unseal key stays out of sight](#the-unseal-key-stays-out-of-sight)).

All need root. Exit codes: `check` exits 0 when every line is ok or a
warning, and 1 when a line is red; the other commands exit 0 when they are
done. Any command exits 1 with one `[FAIL]` line when it cannot do its job,
or with `Stopped. Nothing was changed.` when the admin answers a question
with anything but `yes`, and 2 for wrong usage.

`meho doctor` runs `check --json` as root on the server and shows
its lines (#4740, see below). The fourth hardening line of #4638, a scan of
the operating system against the CIS Linux benchmark, is not built yet
(see Known issues).

## Key files

| File | What it holds |
| --- | --- |
| `meho-host` | One bash script: the output helpers, the checks with the rule for the console certificate (`certificate_state`), the bundle and its scrub, the SSH rule rewrite, the log server (rsyslog's file and its firewall rule), the uninstall, the certificate install, and the keyholder switch and the unseal of OpenBao. It sources `lib/firewall.sh` and `lib/cis.sh` (see [How meho-host finds its lib files](#how-meho-host-finds-its-lib-files)). It carries its own copies of the IPv4 helpers of `lib/net.sh` and of the paths of `lib/common.sh` that it needs. |
| `lib/firewall.sh` | `firewall_state` (the firewall line) and the firewall paths `MEHO_FIREWALL_FILE`, `MEHO_FIREWALL_UNIT` and the others. |
| `lib/cis.sh` | `cis_state` (the CIS profile line), and for the uninstall the paths of the CIS host changes and the `etcd` look-ups (`etcd_user_exists`, `etcd_group_exists`, `etcd_account_recorded`). It reads `RKE2_CONFIG_FILE`, which `meho-host` defines as `lib/common.sh` does. |
| `lib/prepare.sh` | `install_meho_host`: the copy to `/usr/local/sbin`, mode 0755, and of the two lib files to `/usr/local/lib/meho-host`, mode 0644. |
| `lib/common.sh` | `MEHO_HOST_FILE`, `MEHO_HOST_PATH`, `MEHO_HOST_LIB_DIR`, `MEHO_HOST_LIB_FILES`, and `meho-host` in `DOWNLOAD_FILES`. |
| `packaging/parts.sh`, `packaging/common.sh` | The download ships `meho-host` next to `install.sh`, executable. |
| `tests/host_harness.py` | A fake installed machine: stub `kubectl`, `rke2`, `journalctl`, `df`, `free`, `ip`, `date`, `nft`, `systemctl`, `getent`, `rsyslogd`, `userdel`, `groupdel` and friends, the files of RKE2's CIS profile, and the real `openssl`. `console_certificate()` makes test certificates with fixed dates, and `certificate_with_key()` also gives the key of the server certificate. `make_appliance()` turns the form-2 machine into the appliance. `environment()` is the environment meho-host runs with, and `run()` takes the answers to the questions (`stdin`) and an SSH session (`ssh_client`). |
| `tests/test_meho_host.py` | The tests of `check`, `support-bundle`, `admin-network`, `log-server` and `uninstall`, of each check line, of the install by `install.sh`, and of the `etcd` record from the install through an update to `uninstall`. |
| `tests/fake_cluster.py`, `tests/cluster_harness.py` | A fake `kubectl` that keeps state: the Secrets of namespace `meho` and the one OpenBao pod, with its seal state, its uid and the chart's unseal helper. `ClusterHost` is the fake machine with it. |
| `tests/test_meho_host_certificate.py` | The tests of `certificate`. |
| `tests/test_meho_host_unseal.py` | The tests of `keyholder on`, `keyholder off` and `unseal`, and of the unseal key, which never appears in a command line, the output or a file. |
| `tests/fixtures/firewall-admin-network.nft` | The firewall file `install.sh` writes with `--admin-network 10.0.50.0/24`; the `admin-network`, `log-server` and `uninstall` tests start from it. |

The script has the same shape as `install.sh`: `set -Eeuo pipefail`, an ERR
trap that prints one `[FAIL]` line, `fail` and `usage_error`, and the
`MEHO_TEST_ROOT` seam, which puts every machine path below a test folder.

## Control flow

### `check`

Each check calls `result <name> <status> <detail> [fix]`. The seven results,
and on the appliance an eighth (`log-server`), are printed at the end, as
text or as JSON, in this order:

| Line | ok when | warn when | red when | Source of the facts |
| --- | --- | --- | --- | --- |
| `node` | every node has the condition Ready | never | a node is not Ready, or RKE2 does not answer | `kubectl get nodes -o jsonpath=...` |
| `pods` | every pod in `meho`, `kube-system` and `local-path-storage` is Running and Ready, or Succeeded | a pod the kubelet evicted (phase Failed, reason Evicted) while the other pods run | a pod is in another state, there is no pod in `meho`, or RKE2 does not answer | `kubectl get pods -A -o jsonpath=...` |
| `disk` | `/var/lib/rancher` and `/var/lib/meho` both have 20 % or more free | the lower of the two is below 20 % | below 15 %, or a path cannot be read | `df -P -k <path>` |
| `certificates` | every row of RKE2's list expires in 30 days or more | a row has less than 30 days left | a row is EXPIRED or its date has passed; a row is NOT YET VALID (the clock or the certificate is wrong; its EXPIRES column holds the start date); the command did not run; the list is empty | `/usr/local/bin/rke2 certificate check --output table` (by its path: sudo's `PATH` on RHEL has no `/usr/local/bin`) |
| `firewall` | `firewall_state` says `on`: the table `inet meho` is loaded, its chain `prerouting` filters the hook `prerouting` with `policy drop`, and `meho-firewall.service` is enabled | never | `firewall_state` says `off: <reason>`, also `off: nft not found` when `nft` is missing | `nft list table inet meho`, `nft list chain inet meho prerouting`, `systemctl is-enabled meho-firewall.service` |
| `cis-profile` | `cis_state` says `on`: `config.yaml` sets `profile: cis`, the pod security file exists, the `etcd` user and group exist, `/etc/sysctl.d/60-rke2-cis.conf` exists and the kernel has its values | never | `cis_state` says `off: <reason>` | the files of [RKE2's CIS profile](linux-server-install.md#rke2s-cis-profile) and `/proc/sys` |
| `console-certificate` | the rule below holds and more than 30 days are left | the rule holds, but 30 days or less are left | the certificate breaks the rule; the Secret cannot be read; the host name is not in `/etc/meho/install.json`; `openssl` is missing | the Secret `meho-core-tls` (`tls.crt`, `ca.crt`), `/etc/meho/install.json`, `openssl` |
| `log-server` (only on the appliance) | no log server is set, or one is set and its name resolves to the addresses in the firewall rule | only one of rsyslog's file and the firewall rule is there; the name does not resolve now; it resolves to other addresses now | never | the two lines of `log-server` in the chain `output` of `/etc/meho/firewall.nft`, `/etc/rsyslog.d/10-meho-log-server.conf`, `getent ahostsv4` |

**The rule for the console certificate.** `certificate_state` holds the
rule in one place. The certificate is the one the console, the API and the
login show: the TLS Secret of the Ingress, `meho-core-tls`
(`ingress.tls.secretName` in `values-single-node.yaml`). The install makes
it for the host name, and `meho-host certificate` (#4738) puts the
customer's own certificate into the same Secret. The check reads only
`tls.crt` (the server certificate, then the intermediate certificates) and
`ca.crt` (the CA the clients trust), with one `kubectl` call: one Go
template prints `tls.crt`, the line `#ca.crt`, then `ca.crt`. One call,
because `meho doctor` gives the whole check 60 seconds. It never reads
`tls.key`. The host name comes from `options.hostname` in
`/etc/meho/install.json`. The rule:

1. The server certificate is valid now: not before its start date, and not
   at or after its end date.
2. It names the host name. This is OpenSSL's host check
   (`openssl x509 -checkhost`): the DNS names of the subject alternative
   names, a wildcard only in the first label, and the common name only when
   there is no DNS name.
3. Its chain ends at `ca.crt`, and every certificate of the chain is valid
   now: `openssl verify -attime <now> -partial_chain -no-CApath -no-CAstore
   -CAfile <ca.crt> -untrusted <tls.crt> <tls.crt>`. `-partial_chain` lets
   `ca.crt` be an intermediate CA. `-no-CApath` and `-no-CAstore` stop
   OpenSSL from also trusting the machine's own CA folder and store, so a
   certificate from a public CA does not pass with an unrelated `ca.crt`.
   Without `ca.crt`, the trust store of the machine is the anchor.
4. More than 30 days are left.

Points 1 to 3 make a red line, which names every problem it finds. Point 4
alone makes a warning. Every time check uses one "now", `date -u +%s`, so
the tests can fix it. The first-start list (#4042) checks the console
certificate in the backplane, in Python. It must use the same rule, with
the same 30 days, so the two lists never disagree. `meho-host certificate`
(#4738) calls `certificate_state` to check a new certificate before it
writes the Secret.

**The fix lines of the hardening lines.** The firewall line names the two
`systemctl` commands that load the MEHO firewall and keep it on, and its
log. The CIS profile line asks to set back what it names, as `install.sh`
set it up, and to restart RKE2. The certificate line names the command that installs a
certificate: `sudo meho-host certificate --cert <chain.pem> --key <key.pem>`.

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

`meho doctor` reads this form (#4740). When `/usr/local/sbin/meho-host`
exists and the doctor runs as root, it runs `meho-host check --json` with
a 60-second limit and shows each line in its server layer, after its own
lines. It maps `ok`, `warn` and `fail` (and `skip`) to its own statuses.
A line with another status word becomes a red line in the doctor, so a
new status word needs a change in `cli/internal/cmd/doctor_server.go`
too. Output that is not this JSON gives one red doctor line. See
[cli.md](cli.md), section "Server layer".

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

### `log-server`

The design is ADR 0009's Work list (amended 2026-10-21 to 2026-10-23) and
the form-2 design, section 5, the `meho-host` table. The command changes
two things: rsyslog's settings, and one rule of the MEHO firewall. It
checks each new file before it takes the old one's place, so a value that
is wrong changes nothing.

1. Check the values, before root is checked. `<host>` is a host name or an
   IPv4 address; an IPv6 address is refused, because the appliance is IPv4
   only. `<port>` is 1 to 65535. Without a port, it is 514, or 6514 with
   `--tls`. `--tls` needs a host name: rsyslog checks that the server's
   certificate names the host, and a certificate rarely names an address.
   The checks also keep every value safe to write into rsyslog's file and
   the firewall file.
2. Stop with a `[FAIL]` line when `/opt/meho/appliance` is missing: then the
   machine is a form-2 machine, where the OS and its logging belong to the
   customer.
3. With `--tls`: take only the certificates from the CA file (the lines
   from `-----BEGIN CERTIFICATE-----` to `-----END CERTIFICATE-----`), so a
   private key in the same file never reaches the CA file, which every user
   may read. `openssl x509` must read them. rsyslog's TLS module
   (`lmnsd_gtls.so`, the package `rsyslog-gnutls`) must be on the machine:
   `rsyslogd -N1` does not notice when it is missing.
4. Resolve a host name once, with `getent ahostsv4`. Every IPv4 address of
   the name goes into the firewall rule, because rsyslog may connect to any
   of them. The name goes into rsyslog's file and into the note above the
   rule.
5. Rewrite the firewall file into a new file next to it (`mktemp`, root
   only). In the chain `output`, the old two lines of `log-server` and the
   empty line after them go, and the new ones come in front of the
   counter's comment line:

   ```
           # The log server, set with meho-host log-server: logs.example.com
           ip daddr { 192.0.2.10 } tcp dport 514 accept
   ```

   `none` only removes them. A second run replaces the first server. The
   new file must hold exactly these two lines in the chain `output`; after
   a hand edit that moved the counter's comment, it does not, and the
   command stops. Then `nft -c -f` checks the new file.
6. Write rsyslog's new file next to the old one in `/etc/rsyslog.d`, with a
   name that does not end in `.conf`, so rsyslog does not read it yet.
   `rsyslogd -N1 -f` checks it. The action follows rsyslog's guide to
   reliable forwarding: `omfwd` over TCP, with its own queue
   (`queue.type="LinkedList"`), so local logging goes on while the server is
   down, endless retries (`action.resumeRetryCount="-1"`), and the disk as a
   help (`queue.filename`, in Ubuntu's work folder `/var/spool/rsyslog`), at
   most 1 GB (`queue.maxDiskSpace`), so a server that stays down never
   fills the disk. With TLS: `StreamDriver="gtls"`, `StreamDriverMode="1"`,
   `StreamDriverAuthMode="x509/name"`, `StreamDriverPermittedPeers=<host>`
   and `StreamDriver.CAFile`. The CA file goes to
   `/etc/rsyslog.d/meho-log-server-ca.pem`, mode 0644: Ubuntu's AppArmor
   profile of `rsyslogd` lets it read no other folder of settings, and
   rsyslog reads the file after it has changed from root to the user
   `syslog`.
7. Move the firewall file into place and load it with `nft -f`, as
   `admin-network` does. Then move rsyslog's files into place, or remove
   them for `none`, and restart `rsyslog.service`. So the way out is open
   before rsyslog sends. For `none`, the queue files of the old server in
   `/var/spool/rsyslog` go too.

rsyslog's file is `/etc/rsyslog.d/10-meho-log-server.conf`. rsyslog reads
the files of that folder in name order, so `10-` comes before Ubuntu's
`50-default.conf` and before a `stop` in a later file.

### `uninstall`

1. Print what is removed, then ask twice. Only `yes` goes on.
2. Run `/usr/local/bin/rke2-uninstall.sh`, RKE2's own uninstall script of
   a tarball install. It stops RKE2 and removes its units, its binaries,
   the cluster data in `/var/lib/rancher` and itself. When it fails, the
   command stops before the MEHO files, and a second run goes on. When the
   script is not there, RKE2 is already gone, and the command says so and
   goes on.
3. Remove the host changes of RKE2's CIS profile (#4636, #5133). RKE2's
   script does not remove them. They go before `/etc/meho`, because the
   record of the `etcd` account is in that folder.
   - The kernel settings file `/etc/sysctl.d/60-rke2-cis.conf`. Removing
     the file does not change the running kernel: `systemd-sysctl` sets
     only the values that a file names, and it does not set others back.
     So the kernel keeps RKE2's values until the machine restarts, and the
     last line of the command says that a restart brings back the
     operating system's own kernel values. The file must not stay after
     MEHO is gone: with two of its values, `kernel.panic_on_oops=1` and
     `kernel.panic=10`, the machine restarts by itself 10 seconds after a
     kernel error.
   - The parts of the `etcd` account that the install created. The install
     writes them to `/etc/meho/etcd-account`, one per line: `user`,
     `group` or both ([linux-server-install.md](linux-server-install.md),
     "The host steps"). Only a part that the record names is removed:
     `userdel etcd` for the user, `groupdel etcd` for the group when it is
     still there. `userdel` also removes the user's group of the same name
     when `USERGROUPS_ENAB` is `yes` in `/etc/login.defs`, the default of
     Ubuntu and RHEL. So when the install created only the user, and the
     group existed before, both stay, and a `[note]` line says how to
     remove them.
   - A part that the record does not name stays: it existed before the
     install, or an install.sh from before #5133 created it and wrote no
     record. A `[note]` line names it and the command that removes it, for
     when nothing else uses it.
   - A failed `userdel` or `groupdel` is a `[WARN]` line with the command,
     and the uninstall goes on.
4. Remove `/var/lib/meho` (the volumes and the one-time files) and every
   entry of `/etc/meho` except `firewall.nft`, and the drop-in
   `/etc/systemd/system/rke2-server.service.d/meho-firewall.conf`, which
   would stop a later RKE2 install once the firewall is gone, with its
   folder when that is empty (RKE2's script removes only the
   `rke2-*.service` files). Then `systemctl daemon-reload`.
5. Remove the two commands `install.sh` put on the machine,
   `/usr/local/bin/meho` and `/usr/local/sbin/meho-host`, and the folder
   `/usr/local/lib/meho-host`. bash keeps the running script open and has
   read the two lib files at its start, so `meho-host` can remove itself
   and them and still print its last lines.
6. The firewall file, its boot unit and the loaded table stay, so the
   machine is never without a firewall (ADR 0009). A line names the
   command that removes them, for when the customer's own firewall is
   ready. The line about the kernel values comes after it, as the last
   line.

RKE2's script removes `/etc/rancher/rke2`, so the pod security file of the
CIS profile, `meho-pss.yaml`, goes with it.

### `certificate`

The install makes a self-signed certificate for the host name in
`meho-core-tls` (core.md section 7, sub-decision 3;
`backend/src/meho_backplane/core/tls.py`). `certificate` replaces it with
the customer's own. The steps, each of which stops with one `[FAIL]` line
and `Nothing was changed.` when it fails:

1. Check the options (`--cert` and `--key`, both needed; a usage error
   otherwise), root, `openssl`, the host name in `/etc/meho/install.json`,
   and that both files can be read.
2. Read the certificates of the chain file, in their order, and nothing
   else (`pem_certificates`): a private key in the same file, the
   `subject=` lines or `Bag Attributes` that some tools write, and Windows
   line ends are left out.
3. Check the chain (`check_new_certificate`):
   - at least two certificates, and the last one is not the server
     certificate again. Without this, the next check would pass with the
     server certificate as its own CA, because `-partial_chain` accepts
     any certificate in `-CAfile` as the anchor;
   - `openssl` can read the first certificate;
   - the rule of the console certificate, `certificate_state`, with the
     last certificate of the file as the CA: valid now, names the host
     name, and its chain ends at the CA. The problems are named in the
     words of the check line. "its chain to ca.crt" becomes "its chain to
     your CA, the last certificate of `<file>`".
4. Check the key (`check_new_key`): `openssl pkey -passin pass: -pubout`
   must read it. The empty pass phrase makes `openssl` refuse an encrypted
   key at once, and standard input is `/dev/null`, so it never asks for one.
   Traefik cannot ask for a pass phrase either. Its public key must be the
   one of the server certificate (`openssl x509 -pubkey`).
5. Write the Secret (`write_console_tls`) with one merge patch of its
   `data`: `tls.crt` is the certificates of step 2, `tls.key` is the key as
   `openssl pkey` prints it (PKCS#8, no pass phrase), and `ca.crt` is the
   last certificate. The patch goes to `kubectl patch --type=merge
   --patch-file=/dev/stdin` on standard input, so the key is never on a
   command line. A merge patch keeps the labels of the Secret. kubectl's
   messages are not shown, because an error text could quote the patch.
6. Print the certificate line of `check`, which reads the Secret again
   with the same rule. A certificate with 30 days or less left is
   installed, and the line is a `[WARN]`.

Why the Secret keeps the customer's certificate: the chart's secrets Job
makes `meho-core-tls` only when it is missing, and keeps a Secret that has
all three keys (`reconcile` in `backend/src/meho_backplane/core/secrets.py`).
So an update keeps it. Traefik watches Secrets and serves the new
certificate without a restart (`pkg/provider/kubernetes/ingress/client.go`
of Traefik v3: an informer on Secrets). Its filter leaves out only Helm's
own release Secrets.

### `keyholder on` and `keyholder off`

OpenBao's mode is whether the Secret `meho-core-vault-unseal` exists
(core.md section 4, sub-decision 9; [core-vault-init.md](core-vault-init.md)).
While it exists, the `unseal` helper container of the pod `meho-vault-0`
unseals OpenBao a few seconds after every start. Without it, OpenBao stays
sealed after a restart until a keyholder unseals it.

`keyholder on`:

1. Read the Secret with `kubectl get secret --ignore-not-found -o
   go-template=...`. The template prints the line `present` and the key,
   and with `--ignore-not-found` kubectl prints nothing when the Secret
   does not exist. Without the Secret, the mode is keyholder already: an
   `[ok]` line, and nothing changes.
2. Ask for the key, and compare it with the key in the Secret. A different
   key stops the command, and nothing changes. A "yes" would prove
   nothing: after the Secret is gone, nobody can open OpenBao without the
   key.
3. Delete the Secret. When `/var/lib/meho/unseal-key` still exists, a
   `[WARN]` line says to delete it: in keyholder mode the key belongs in
   the key store, not on the machine.
4. Restart OpenBao (`restart_openbao`, below) and wait for the first
   answer of the new pod. Sealed is the expected end: MEHO waits, and the
   last line names `sudo meho-host unseal`. The command does not unseal
   OpenBao itself, so the admin sees at once that the keyholder step works.

`keyholder off`:

1. Read the Secret. When it exists, the mode is self-unseal already: an
   `[ok]` line, and nothing changes.
2. Ask for the key.
3. When OpenBao is sealed now, check the key by unsealing with it. When it
   stays sealed, the key is wrong: the command stops, and nothing changes.
4. Create the Secret as the vault-init Job makes it: type `Opaque`, the key
   under `key`, the label `meho.evoila.com/managed-by: core-bootstrap`. The
   manifest goes to `kubectl create -f -` on standard input. `kubectl
   create` keeps no copy of the manifest in an annotation; `kubectl apply`
   would keep one, with the key in it. kubectl's messages are not shown.
5. Restart OpenBao and wait until the new pod is unsealed. That proves
   that the unseal helper unseals it with the key in the Secret.
6. When OpenBao is unsealed before the command, step 3 cannot check the
   key: only the restart can. When the new pod stays sealed for 5 minutes,
   the key is wrong. The command deletes the Secret again, so the mode
   stays what OpenBao really does, and the `[FAIL]` line says to unseal
   with the right key and run `keyholder off` again.

`restart_openbao` runs `kubectl rollout restart statefulset/meho-vault`
and then asks every 5 seconds, 60 times, for the uid of the pod and the
seal state. The uid tells the new pod from the old one, which still
answers while it shuts down. The StatefulSet also replaces a sealed pod,
because its `podManagementPolicy` is `Parallel` (see the comment in
`deploy/charts/meho/charts/openbao/templates/statefulset.yaml`).

The seal state comes from `bao status` in the container `bao` of the pod:
exit code 0 is unsealed, 2 is sealed, and 1 is an error. `kubectl exec`
exits with the exit code of the command in the pod. Any other failure, for
example a pod that does not run yet, counts as unknown, never as sealed or
unsealed.

### `unseal`

1. Ask OpenBao for its state. When it is unsealed, an `[ok] unsealed:`
   line, and the key is not asked for. When it does not answer, a `[FAIL]`
   line.
2. Ask for the key, and send it to OpenBao's unseal API (`/v1/sys/unseal`,
   the key in the request body) inside the pod: `kubectl exec -i
   meho-vault-0 -c bao -- bao write sys/unseal key=-`, with the key on
   standard input and no line break after it. `bao write` reads a value
   given as `-` from standard input. The chart's unseal helper uses the
   same call. Its output is thrown away, because an error text could quote
   the request.
3. Ask for the state again: `[ok] unsealed: ...`, or `[FAIL] sealed: ...`
   when the key is wrong.

`unseal` never creates the Secret, so it does not switch the mode.

### The unseal key stays out of sight

- `read -r -s -p` reads the key. On a terminal, read prints the prompt and
  turns the echo off. When standard input is not a terminal, read leaves
  out the prompt and reads one line. White space is removed, as the
  unseal helper removes it. A key must hold only letters, digits, `+` and
  `/`, with `=` at the end (base64, or hex). So it needs no escaping in
  JSON, and a wrong paste is refused without being printed.
- The key is a shell variable, never an argument of a command: `printf` is
  a bash builtin, so `printf '%s' "$UNSEAL_KEY" | kubectl ...` starts no
  process with the key in its arguments. So the key is not in `ps`, and
  `sudo` logs only `meho-host unseal` to the journal.
- The key that `keyholder on` reads from the Secret goes from kubectl
  straight into a variable. Both variables are cleared when the command is
  done with them.
- Any argument of `unseal`, and any value of `keyholder` other than `on`
  or `off`, is a usage error that does not print it, in case it is a key.
- kubectl's messages of the calls that carry the key or the certificate
  key (`create`, `patch`, `exec ... sys/unseal`) are not shown.

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
`install.sh` brings the versions of its download. It takes the first
place that holds both files. When neither does, `meho-host` stops at once
with a `[FAIL]` line that names both places and the folder to copy the
files to. Because `lib/firewall.sh` defines the
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
which removes RKE2's unit and data the way the real one does. The `userdel`
and `groupdel` stubs remove their lines from the fake `/etc/passwd` and
`/etc/group`; like the real `userdel` with `USERGROUPS_ENAB yes`, the
`userdel` stub also removes the group of the same name, and the answer
`usergroups_enab` turns that off. The tests cover each content of the
record (both parts, only the group, only the user, no record), a failed
`userdel` and `groupdel`, and the last line. `InstallUpdateUninstallTests`
runs `install.sh`, then the update, then the `meho-host` that the install
put on the machine, so it proves that the update keeps the record of the
first install and that `uninstall` then removes the account. It also
proves that an `etcd` user and group from before the install stay through
all three.

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

The `log-server` tests run on a fake appliance (`make_appliance()`: the
folder `/opt/meho/appliance`, rsyslog's folders and a stand-in for its TLS
module). The `getent` stub resolves every name to `192.0.2.10` unless a
test gives other addresses, and the `rsyslogd` stub accepts or refuses the
file it checks. The tests compare the whole firewall file and rsyslog's
whole file with the expected text, and read the calls log to prove the
order: both checks, then `nft -f`, then the restart of rsyslog. They cover
a name, an address with a port, a name with two addresses, TLS with a
private key in the CA file (only the certificate is copied), a second run,
`none` with queue files left on the disk, each wrong value, a name that
does not resolve, a CA file without a certificate, no TLS module, a file
that nft or rsyslog refuses, a chain `output` that is not in the form
`install.sh` writes, and a form-2 machine. Every failing case proves that
nothing changed. The check tests cover the line with and without a log
server, TCP and TLS, a name that resolves to another address or to none,
and a log server set only in part; on a form-2 machine the list keeps its
seven lines. `test_firewall_nft.py` runs `meho-host log-server` with the
real `nft` too, for an address and for `none`.

rsyslog's settings were also tried by hand on Ubuntu 24.04 with rsyslog
8.2312.0 and `rsyslog-gnutls`, with `openssl s_server` as the log server:
the line arrives at a server whose certificate the CA signed and that names
the host; a server signed by another CA, and one with another name, get
nothing.

The tests of `certificate`, `keyholder` and `unseal` use `ClusterHost`
(`tests/cluster_harness.py`). Its `kubectl` is `tests/fake_cluster.py`,
which keeps state in one JSON file: the Secrets `meho-core-tls` and
`meho-core-vault-unseal`, and the OpenBao pod with its uid, its seal state
and the key that opens it. A restart gives the pod a new uid and seals it.
The old pod still answers for two calls, as a pod that shuts down does, and
the new pod's first `bao status` fails, as a server that starts does. The
fake's unseal helper unseals a sealed pod when the Secret holds the right
key, as the chart's helper does. A failed write of the fake quotes its
request in the error text, so the tests prove that `meho-host` hides
kubectl's messages. The fake also records each call that got the unseal key
on standard input. `sleep` is a stub, so the 5-minute waits take no time,
and the tests count its calls.

The certificate tests make a customer chain with a root and an
intermediate CA with `certificate_with_key()`, which also returns the key
of the server certificate. They cover a valid chain, a chain of two, extra
text and Windows line ends, 20 days left, and one `[FAIL]` line with no
change for a wrong key, a wrong name, an expired certificate, a
certificate that is not yet valid, a missing intermediate, only one
certificate, the server certificate twice, no certificate, an encrypted key,
a certificate as the key, a file that cannot be read, no install record and
a failed write. The unseal tests cover each step of the three commands and
each way it fails. One test runs `keyholder on`, `unseal` and `keyholder
off` with a made-up key and proves that the key is in no line of
`calls.log` (the arguments of every command that ran), in no output and in
no file of the fake machine, and that only `exec -i ... sys/unseal` and
`create -f -` got it on standard input. One test runs `unseal` on a real
pseudo-terminal: it types the key only after `read -s` turned the echo off,
and proves that the terminal never shows it.

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

On the machine: bash, coreutils (`date`, `df`, `find`, `mktemp`, `base64`,
`sleep`), `sed`,
`awk`, `grep`, `tar`, `gzip`, `hostname`, `free` (procps), `ip` (iproute2),
`userdel` and `groupdel` (shadow-utils, the `passwd` package on Ubuntu),
`journalctl`, `systemctl` and `timedatectl` (systemd), `nft` (nftables,
which `install.sh` step 1 requires), `openssl` (Ubuntu's `ca-certificates`
package depends on it), RKE2's own `kubectl` at
`/var/lib/rancher/rke2/bin/kubectl` with `/etc/rancher/rke2/rke2.yaml`,
`rke2` at `/usr/local/bin/rke2` and `rke2-uninstall.sh` next to it. Ubuntu
24.04 has all of them. `log-server` also needs `getent` (libc-bin), rsyslog
(`rsyslogd`, `rsyslog.service`) and, for `--tls`, rsyslog's TLS module of
the package `rsyslog-gnutls`, which the appliance build installs
(`deploy/appliance/cis/harden.sh`), because the appliance cannot download
packages later.

## Known issues

- **The CIS Linux benchmark scan is not built.** #4638 asks for a fourth
  hardening line: a scan of the customer's operating system against the
  CIS benchmark, Level 1, with known exceptions such as IP forwarding. It
  needs a scanner and its rules in the download. The research and the
  recommendation are on #4638; the line comes with a later change.
- **The certificate line reads one Secret by name.** It reads
  `meho-core-tls`, the name of `values-single-node.yaml`. `meho-host
  certificate` (#4738) writes the same Secret. A change of
  `ingress.tls.secretName` needs a change of `CONSOLE_TLS_SECRET`.
- **`certificate` prints the check line, not the first-start list.** The
  first-start list of the backplane (#4042) is not built yet. Until it is,
  `certificate` prints the certificate line of `check`, which applies the
  same rule to the Secret it just wrote (a `shortcut:` comment in
  `command_certificate`). When #4042 lands, `certificate` prints that list.
- **The OpenBao names are fixed.** The commands use the StatefulSet
  `meho-vault`, its pod `meho-vault-0`, the container `bao` and the Secret
  `meho-core-vault-unseal`: the names of the release `meho` with the chart's
  default `coreVault.binary: bao`. A change there needs a change of the
  constants at the top of `meho-host`.
- **`keyholder off` checks the key only by the restart when OpenBao is
  unsealed.** OpenBao has no call that checks an unseal key while it is
  unsealed (rekey and generate-root need a token). So a wrong key shows
  only after the restart, and OpenBao stays sealed until a keyholder
  unseals it with the right key. The command then deletes the Secret again.
- **In keyholder mode, an update waits for a keyholder.** When an update
  restarts OpenBao, the vault-init Job waits up to 10 minutes for the
  unseal ([core-vault-init.md](core-vault-init.md)). The keyholder must run
  `sudo meho-host unseal` within that time.
- **One keyholder.** The commands work with the one key of the install.
  More keyholders (`bao operator rotate-keys`) and the backup of the key
  (#3960) are not part of them.
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
- **`log-server` resolves a host name once.** When the name moves to
  another address, the firewall still lets the logs out only to the old
  one. The check line warns, and running the command again fixes it.
- **`log-server` has not run on a real appliance yet.** The tests use
  stubs, and rsyslog's settings were tried in a container without the
  firewall. The lab test of the first appliance import (#4815 part 2) or
  the release test (#4816) proves that the logs reach a log server through
  the firewall.
- **`log-server --tls` needs a host name.** rsyslog checks that the
  server's certificate names the host (`x509/name`). An address is
  refused, because a certificate rarely names one.
- **`uninstall` keeps the log server.** It leaves rsyslog's file and the
  firewall rule, like the rest of the firewall. `log-server none` before
  `uninstall` removes them.
- **`uninstall` leaves the OS packages that RKE2's installer added** (out
  of scope in #4739).
- **`uninstall` does not set the kernel values back.** It removes RKE2's
  kernel settings file, but the running kernel keeps RKE2's values until
  the machine restarts (#5133 decision). The last line says so.
- **An `etcd` account without a record stays.** An install.sh from before
  #5133 wrote no `/etc/meho/etcd-account`, and an update does not write
  one later, because it cannot know who created the account. On such a
  machine `uninstall` keeps the `etcd` user and group and prints the
  commands that remove them.
- **A user that the install created can stay.** When the `etcd` group
  existed before the install and the install created only the user,
  `userdel` would remove the group too, so `uninstall` keeps both and says
  so.

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
- rsyslog's `omfwd` (`target`, `port`, `protocol`, the `StreamDriver*`
  parameters, `StreamDriver.CAFile` since 8.2108.0):
  https://docs.rsyslog.com/doc//configuration/modules/omfwd.html
- rsyslog's guide to reliable forwarding (`queue.type="LinkedList"`,
  `queue.filename`, `queue.saveOnShutdown`, `action.resumeRetryCount="-1"`):
  https://docs.rsyslog.com/doc/tutorials/reliable_forwarding.html
- rsyslog's gtls driver (`x509/name` checks the certificate and the
  name): https://www.rsyslog.com/doc/ns_gtls.html
- Ubuntu 24.04's rsyslog 8.2312.0: `rsyslog-gnutls` is in `main`,
  `rsyslog-openssl` only in `universe`; `/etc/rsyslog.conf` sets
  `$WorkDirectory /var/spool/rsyslog` and includes
  `/etc/rsyslog.d/*.conf`; the AppArmor profile
  `/etc/apparmor.d/usr.sbin.rsyslogd` allows reading `/etc/rsyslog.d/**`
  and writing `/var/spool/rsyslog/**` (read on 2026-11-04 in the
  `ubuntu:24.04` image).
- CIS Ubuntu Linux 24.04 LTS Benchmark v1.0.0, rule 6.1.3.6 (manual):
  remote logging. ComplianceAsCode `controls/cis_ubuntu2404.yml` (v0.1.82)
  lists it as manual, with the related rule `rsyslog_remote_loghost`.
- RKE2 uninstall (`/usr/local/bin/rke2-uninstall.sh` for a tarball install):
  https://docs.rke2.io/install/uninstall. The script of v1.37.1 removes
  `/etc/rancher/rke2` and RKE2's `share/rke2` folder, not `/etc/sysctl.d`
  and not the `etcd` user and group.
- `userdel(8)`: with `USERGROUPS_ENAB yes` it removes the group with the
  same name as the user, unless it is the primary group of another user;
  exit code 8 when a process of the user runs.
  https://man7.org/linux/man-pages/man8/userdel.8.html
- `groupdel(8)`: refuses the primary group of an existing user (exit code
  8). https://man7.org/linux/man-pages/man8/groupdel.8.html
- `systemd-sysctl.service(8)`: applies the settings of the `sysctl.d`
  files; it does not set unnamed values back.
  https://www.freedesktop.org/software/systemd/man/latest/systemd-sysctl.service.html
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
- OpenSSL 3.0 `openssl-pkey(1)`: `-pubout`, `-passin pass:` (an empty pass
  phrase). Checked with OpenSSL 3.0.13: an encrypted key fails with exit
  code 1 and no prompt.
- OpenBao `write`: "If the value for a key is "-", OpenBao will read the
  value from stdin" (openbao/openbao `website/content/docs/commands/write.mdx`,
  read 2026-11-04). `status`: exit code 0 unsealed, 1 error, 2 sealed
  (https://openbao.org/docs/commands/status/). The unseal API, `/sys/unseal`
  with `key` in the body (`website/content/docs/api/system/unseal.mdx`).
  `operator unseal` warns that a key given as an argument stays in the
  shell history (`website/content/docs/commands/operator/unseal.mdx`).
- kubectl (checked with v1.35; RKE2 v1.37.1 ships v1.37): `patch
  --patch-file` and `--type=merge`
  (https://kubernetes.io/docs/reference/kubectl/generated/kubectl_patch/);
  `get --ignore-not-found` prints nothing for a missing object with a
  template output (kubernetes/kubectl `pkg/cmd/get/get.go`, v0.35.0,
  `printGeneric`); `create -f -` reads standard input, and only
  `--save-config` stores the object in an annotation; `rollout restart`
  takes StatefulSets; `exec` exits with the exit code of the command in the
  container (kubernetes/kubectl `pkg/cmd/util/helpers.go`, v0.35.0).
- Traefik v3 watches Secrets with an informer (traefik/traefik
  `pkg/provider/kubernetes/ingress/client.go`, v3.5), so a changed TLS
  Secret is served without a restart.
- bash `read`: `-s` turns the echo off and `-p` prints the prompt, both
  only when the input comes from a terminal (bash(1), "read").
- core.md (meho-internal `docs/design/2026-10-10-install-forms/`), section 4,
  sub-decisions 9 to 11 (the modes, the switch, the unseal by hand) and
  section 7, sub-decision 3 (the host command for the certificate); the
  form-2 design, section 5, the `meho-host` table.
