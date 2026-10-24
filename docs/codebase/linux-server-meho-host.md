# Form-2 host command: `deploy/linux-server/meho-host` (#4737)

## Overview

`meho-host` is the customer's tool for the work on a form-2 machine after
the install. `install.sh` copies it to `/usr/local/sbin/meho-host` in
step 2, and every later run installs the version of its download. The
customer never needs kubectl: that is ADR 0007 decision 2 (the support rule)
and the form-2 design, section 5, the `meho-host` table (meho-internal
`docs/design/2026-10-10-install-forms/form-2-linux-server.md`).

This version has two commands:

- **`meho-host check [--json]`** prints a red/green list of the built-in
  Kubernetes (RKE2): the node, the pods, the disk and RKE2's certificates.
  Every line that is not ok ends with `Fix:` and what to do.
- **`meho-host support-bundle`** writes one file for evoila support to
  `/var/lib/meho/support/` and prints its path. Passwords, tokens and keys
  are removed from it.

Both need root. Exit codes: `check` exits 0 when every line is ok or a
warning, and 1 when a line is red; `support-bundle` exits 0 when the file is
written. Any command exits 1 with one `[FAIL]` line when it cannot do its
job, and 2 for wrong usage.

Later Tasks add to this file: the hardening lines of `check` (#4638), the
commands `certificate`, `keyholder` and `unseal` (#4738), and
`admin-network` and `uninstall` (#4739). `meho doctor` runs `check --json`
when it runs on the server (#4740).

## Key files

| File | What it holds |
| --- | --- |
| `meho-host` | One self-contained bash script: the output helpers, the four checks, the bundle and its scrub. It sources nothing, because `lib/` is not on the machine after the install. |
| `lib/prepare.sh` | `install_meho_host`: the copy to `/usr/local/sbin`, mode 0755. |
| `lib/common.sh` | `MEHO_HOST_FILE`, `MEHO_HOST_PATH`, and `meho-host` in `DOWNLOAD_FILES`. |
| `packaging/parts.sh`, `packaging/common.sh` | The download ships `meho-host` next to `install.sh`, executable. |
| `tests/host_harness.py` | A fake installed machine: stub `kubectl`, `rke2`, `journalctl`, `df`, `free`, `ip`, `date` and friends. |
| `tests/test_meho_host.py` | The tests of both commands, and of the install by `install.sh`. |

The script has the same shape as `install.sh`: `set -Eeuo pipefail`, an ERR
trap that prints one `[FAIL]` line, `fail` and `usage_error`, and the
`MEHO_TEST_ROOT` seam, which puts every machine path below a test folder.

## Control flow

### `check`

Each check calls `result <name> <status> <detail> [fix]`. The four results
are printed at the end, as text or as JSON, in this order:

| Line | ok when | warn when | red when | Source of the facts |
| --- | --- | --- | --- | --- |
| `node` | every node has the condition Ready | never | a node is not Ready, or RKE2 does not answer | `kubectl get nodes -o jsonpath=...` |
| `pods` | every pod in `meho`, `kube-system` and `local-path-storage` is Running and Ready, or Succeeded | never | a pod is in another state, there is no pod in `meho`, or RKE2 does not answer | `kubectl get pods -A -o jsonpath=...` |
| `disk` | `/var/lib/rancher` and `/var/lib/meho` both have 20 % or more free | the lower of the two is below 20 % | below 10 %, or a path cannot be read | `df -P -k <path>` |
| `certificates` | every row of RKE2's list expires in 30 days or more | a row has less than 30 days left | a row is EXPIRED or its date has passed; the command did not run; the list is empty | `rke2 certificate check --output table` |

**Why Succeeded counts as fine.** RKE2's `helm-install-*` Jobs in
`kube-system` and the core's bootstrap Job leave finished pods behind. They
are in phase Succeeded and never Ready. A rule "every pod is Running and
Ready" would never be green.

**Why 10 % is red.** The kubelet starts to evict pods when the free space of
the node's file system falls below 10 %, its default
`nodefs.available<10%` threshold.

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

1. Make a work folder `/var/lib/meho/support/.build.XXXXXX` (the support
   folder is 0700). An EXIT trap removes it.
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

3. Scrub every file in the work folder with one `sed` run:
   - the value of any variable whose name holds PASSWORD, TOKEN, SECRET or
     KEY, in any case, written as `NAME=value` or `NAME: value`, becomes
     `[removed]`;
   - every value of the two one-time files (`/var/lib/meho/unseal-key`, and
     the part after `=` of each line of
     `/var/lib/meho/first-login-passwords`) becomes `[removed]` wherever it
     appears, for example in a log line.

   Secret data never gets in: the bundle asks for Secrets as a table, which
   shows names, types and the number of keys, never a value. A test fills
   the fake kubectl's answer for `get secret ... -o` with a known password
   and checks that no call asks for it.
4. Pack with `umask 077` into `meho-support-<host>-<UTC time>.tar.gz`
   (mode 0600; `<host>` is `hostname -f` in lower case, and the time is
   `date -u +%Y%m%dT%H%M%SZ`), and print the path.

## Testing

`tests/host_harness.py` builds a fake installed machine for each test, the
way `tests/harness.py` does for `install.sh`: `MEHO_TEST_ROOT` is a temporary
root folder with RKE2's kubectl stub at its real place, and `PATH` holds
only stubs and links to a few real tools (`bash`, `awk`, `sed`, `grep`,
`find`, `tar`, `gzip` and some coreutils). The `date` stub answers "now"
(2026-10-23 12:00 UTC) and the bundle's time stamp from files, and passes
every other call on to the real `date`, which parses the certificate dates.
`certificate_table` and `df_answer` build the stubs' answers.

```bash
python3 -m unittest discover -s deploy/linux-server/tests -v
shellcheck deploy/linux-server/meho-host
```

The scripts need bash 4 or newer and GNU `date` and `sed`, so on macOS run
the tests in a Linux container. CI (`.github/workflows/linux-server-test.yml`)
runs shellcheck on `meho-host` too: it finds every file with a shell shebang
in the folder.

## Dependencies

On the machine: bash, coreutils (`date`, `df`, `find`, `mktemp`), `sed`,
`awk`, `grep`, `tar`, `gzip`, `hostname`, `free` (procps), `ip` (iproute2),
`journalctl` and `timedatectl` (systemd), RKE2's own `kubectl` at
`/var/lib/rancher/rke2/bin/kubectl` with `/etc/rancher/rke2/rke2.yaml`, and
`rke2` at `/usr/local/bin/rke2`. Ubuntu 24.04 has all of them.

## Known issues

- **The Traefik pods are found by name.** The bundle takes the `kube-system`
  pods whose name starts with `rke2-traefik-`, which is how RKE2's
  `rke2-traefik` chart names them (the `helm-install-rke2-traefik` Job pod
  is left out). The week-4 lab install (#4741) proves it.
- **The scrub reads whole lines.** In the `NAME=value` form a value with a
  space in it is removed only up to the space. The two one-time files hold
  no spaces, and their values are removed as a whole.
- **No size limit on the logs.** A pod with a very large log makes a large
  bundle. `kubectl logs --tail` can be added when that happens.
- **Not root is a `[FAIL]` line, not JSON.** `meho doctor` (#4740) runs
  `check --json` only as root.

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
