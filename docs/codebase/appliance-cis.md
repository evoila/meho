# Appliance OS hardening: `deploy/appliance/cis/` (#4640)

## Overview

The appliance build hardens the appliance's Ubuntu 24.04 to the CIS Ubuntu
Linux 24.04 LTS Benchmark v1.0.0, Level 1 - Server (ADR 0009 decision 2),
and scans it. `build.sh` runs `cis/harden.sh` on the build VM in step 3,
after the form-2 install and the appliance pieces, before the clean
([appliance-build.md](appliance-build.md)).

- **The tool:** ComplianceAsCode 0.1.82 with OpenSCAP 1.3.9 from Ubuntu's
  own packages. It is free, so the customer needs no Ubuntu Pro. The same
  content both fixes and checks the rules.
- **The tailoring:** `tailoring.xml` leaves out the rules that do not apply
  to a machine that runs Kubernetes. `not-applicable.md` gives each reason.
- **The result:** a list with one line per rule, next to the OVA as
  `meho-appliance-<version>-cis.txt` and kept by the build workflow. A
  failing rule must be in `exceptions.md`; a rule that fails and is in
  neither list stops the build.

The design is the internal design note `form-1-appliance.md`, section 1,
step 3, "What the build sets beyond the image's defaults" and the CIS
decisions.

## Key files

| File | What it holds |
| --- | --- |
| `cis/harden.sh` | Runs on the build VM as root: the packages, the fixes, the design's settings, the firewall's default block, the scan. |
| `cis/tailoring.xml` | Two profiles: `appliance` (the scan) and `appliance_remediate` (the fixes). |
| `cis/not-applicable.md` | Each rule the scan leaves out, with its reason; also the manual rules that do not apply. |
| `cis/oscap-run-sce-script` | OpenSCAP's runner for script checks it cannot execute, as from the `noexec` `/tmp`. |
| `cis/exceptions.md` | Each rule that applies but fails on the build or on an appliance after first start, with where it fails and one sentence on why. The list is approved once per release (`docs/RELEASING.md`, pre-flight). |
| `tests/test_harden.py` | `harden.sh` against a fake build VM. |
| `tests/test_cis_lists.py` | The tailoring and the two lists agree. |

## Control flow

`build.sh` downloads `scap-security-guide-0.1.82.tar.gz` from the
ComplianceAsCode release (pinned with its sha512, kept in the build cache),
takes `ssg-ubuntu2404-ds.xml` out of it, and copies that file and `cis/` to
the build VM. Then:

```
sudo bash meho-cis/harden.sh --content meho-cis/ssg-ubuntu2404-ds.xml --results /run/meho-cis-results.txt
```

`/run` is a RAM disk, so the list, the fix log and the raw scan are not in
the OVA. `build.sh` copies the list back. It holds rule names and results
only, nothing of the lab, so it can be a public workflow artifact.

`harden.sh` sources `lib/firewall.sh` from the copy of the form-2 download
under `/opt/meho/appliance/download`, for the firewall file's path, its
unit and `firewall_state`. In order:

1. **Packages.** `apt-get install` of `aide`, `aide-common`,
   `openscap-scanner`, `libpam-pwquality`, `libpwquality-tools`,
   `cracklib-runtime` and `apparmor-utils`, which the cloud image does not
   have, and `rsyslog-gnutls`, rsyslog's TLS module for
   `meho-host log-server --tls` (#5752; CIS 6.1.3.6, a remote log server),
   because the appliance cannot download packages later; `apt-get purge ufw`
   (one firewall tool, CIS 4.1.1). The build VM must reach the Ubuntu
   archive. `cracklib-runtime` builds the dictionary of the password check
   that the CIS fixes turn on (`dictcheck`). Without it `chpasswd` refuses
   every password, and first start stops at step 3 (#5744). The install
   skips recommended packages, so it must be named. `libpwquality-tools`
   has `pwscore`, with which first start checks the admin password against
   the same settings before `chpasswd` (#5749).
2. **The scanner files** go to `/opt/meho/appliance/cis/`: the data stream,
   the tailoring and the two lists. They stay on the appliance, because
   `meho-host check` and the update scan with them later (#4737).
   `oscap-run-sce-script` goes to `/usr/local/bin`. OpenSCAP writes the
   script checks of the content under `/tmp` (built in; it ignores
   `TMPDIR`). When it cannot execute them there, because `/tmp` is
   `noexec`, it runs them through that file on `PATH`. Ubuntu ships the
   file only in `openscap-utils`, which also pulls in `rpm`; without it,
   every script check ends in `error`.
3. **The fixes of ComplianceAsCode:** `oscap xccdf eval --remediate` with
   the profile `appliance_remediate`. It sets the bulk of Level 1: the
   password and lock rules in PAM, the SSH settings, sysctl values, file
   permissions, kernel modules, packages to remove. oscap exits 2 when a
   rule still fails after its fix, which is not an error; 1 is. The build
   user keeps sudo without a password, which the clean step needs: the
   rule that takes it away, `sudo_require_authentication` (CIS 5.2.4), is
   Level 2 only.
4. **The design's settings**, after the fixes so they win:
   - `/dev/shm`: one `/etc/fstab` line with `nodev,nosuid,noexec`, and a
     remount (CIS 1.1.2.2).
   - `/tmp`: a RAM disk through systemd's `tmp.mount`, copied from Ubuntu's
     example `/usr/share/systemd/tmp.mount` with `noexec` added, enabled
     and started (CIS 1.1.2.1). Any `/tmp` line in `/etc/fstab` goes.
   - AppArmor: Ubuntu ships about 90 profiles in "unconfined" mode, for
     desktop programs that need user namespaces. They are turned off
     (a link in `/etc/apparmor.d/disable/`, `apparmor_parser -R`), so every
     loaded profile is in enforce or complain mode (CIS 1.3.1.3).
   - IPv6 off with `net.ipv6.conf.{all,default}.disable_ipv6 = 1` in
     `/etc/sysctl.d/60-meho-ipv6-off.conf`. The IPv6 stack stays, so a
     program that opens an IPv6 socket still works; the kernel parameter
     `ipv6.disable=1` would not.
   - SSH with keys only, no root, and only for `admin` (ADR 0009
     decision 4; CIS 5.1.4):
     `/etc/ssh/sshd_config.d/05-meho-appliance.conf` with
     `PasswordAuthentication no`, `KbdInteractiveAuthentication no`,
     `PermitRootLogin no` and `AllowUsers admin`, checked with `sshd -t`.
     CIS Level 1 alone does not turn password login off, and
     ComplianceAsCode has no fix for 5.1.4. sshd keeps the first value it
     reads, and Ubuntu reads `sshd_config.d/*.conf` first, in name order:
     after the fixes' `00-complianceascode-hardening.conf` and
     `01-complianceascode-reinforce-os-defaults.conf` (neither sets these
     four), before cloud-init's `50-cloud-init.conf`. The running sshd is
     not reloaded, so the build's own login stays; the appliance starts
     with these settings. After a restart of sshd on the build VM, the
     build user could no longer log in.
   - root's `PATH` without `/snap/bin` (CIS 5.4.2.5): the check reads the
     scanner's own `PATH`, which is sudo's `secure_path` or the one in
     `/etc/environment`. Both name `/snap/bin`, which the appliance has
     not. `harden.sh` takes it out of `/etc/environment` and writes
     `/etc/sudoers.d/meho-appliance` with its own `secure_path` (sudo uses
     the last one it reads), checked with `visudo -c` before it goes in
     place. `/etc/sudoers` itself stays as the package ships it. The scan
     of step 6 runs in the same process, which got its `PATH` from sudo
     before the change, so `harden.sh` takes `/snap/bin` out of its own
     `PATH` too.
   - `/etc/cron.allow` in the group `crontab` (CIS 2.4.1.8): after the
     fixes it is in the group `root`.
   - AIDE skips `/var/lib/rancher`, `/var/lib/kubelet`,
     `/var/lib/meho/volumes`, `/var/log` and `/run`
     (`/etc/aide/aide.conf.d/90_meho_appliance`). First start builds the
     database (step 7 of [appliance-firststart.md](appliance-firststart.md)).
   - `nftables.service` loads the MEHO firewall too: `/etc/nftables.conf`
     has only `include "/etc/meho/firewall.nft"`, without `flush ruleset`,
     and a drop-in empties `ExecStop=`, which on Ubuntu is
     `nft flush ruleset`. CIS 4.3.9 and 4.3.10 look for exactly this.
     `meho-firewall.service` stays: `meho-host check` and RKE2's start
     depend on it. Both load the same file; it replaces its own table in
     one step, so loading it twice is safe.
5. **The firewall blocks by default.** The form-2 file has the chains
   `input`, `forward` and `output` with `policy accept`; `harden.sh` sets
   all three to `drop` (CIS 4.3.5 to 4.3.8; ADR 0009 decision 1). Each line
   must be found, so a changed form-2 file stops the build. `nft -c` checks
   the new file before it replaces the old one, and `firewall_state` must
   say "on" after the load. This comes after the fixes, because they
   install packages from the internet; the SSH session of the build stays,
   because the chains accept established connections and SSH from the
   build's address. The form-2 update and `meho-host admin-network` change
   only the set `admin_network`, so the policies stay.
6. **The scan:** `oscap xccdf eval` with the profile `appliance`. oscap
   prints `Rule<CR><TAB><id>` and `Result<CR><TAB><result>` for each rule;
   the list keeps `<result> <rule>` without the `xccdf_org...content_rule_`
   prefix. A rule that is not `pass`, `notapplicable` or `informational`
   and not in `exceptions.md` gets a `[WARN]` line in the build log, and
   `harden.sh` stops with exit 1 after the list is written. An exception
   that passes on the build gets a note: when it passes on an appliance
   too, it can leave the list.

Every step is safe to run twice, so `build.sh --from-step 3` runs it again.

### The two profiles of `tailoring.xml`

- **`appliance`** extends `cis_level1_server` and leaves out 18 rules:
  IPv4 forwarding (RKE2), the whole ufw and iptables sections (CIS offers
  three firewall choices, and the appliance uses nftables; the iptables
  section would even demand `nftables.service` off), and the two rules on
  files without an owner (container layers). 390 of 408 rules remain.
- **`appliance_remediate`** extends `appliance` and leaves out 9 more
  rules from the fixes only, which the scan still checks: the nftables
  rules and `firewall_single_service_active` (their fixes would add an
  `inet filter` table to `/etc/nftables.conf`; `harden.sh` sets the
  firewall itself), `package_ufw_removed` (step 1 does it), and
  `aide_build_database` (first start builds the database of each
  appliance).

### What the checks of the firewall rules look for

Read in ComplianceAsCode 0.1.82; they are small scripts (SCE), which
Ubuntu's OpenSCAP runs:

- 4.3.4 `set_nftables_table`: any `inet` table (ours is `inet meho`).
- 4.3.5 `set_nftables_base_chain` and 4.3.8
  `nftables_ensure_default_deny_policy`: in `nft list ruleset`, a chain on
  each of the hooks `input`, `forward` and `output` with `policy drop`.
  One chain per hook is enough, so Canal's own chains with `accept` do not
  matter.
- 4.3.6 `set_nftables_loopback_traffic`: in the input chain, before the
  first line with a `}`, the text `iif "lo" accept` and an `ip saddr`
  rule. The form-2 file has both at the top of its input chain
  (`iif "lo" accept`, `ip saddr 127.0.0.0/8 drop`), so the rule passes on
  the build and on the appliance.
- 4.3.10 `nftables_rules_permanent`: `/etc/nftables.conf` has an
  `include "<file>"` line, and that file exists and is not empty.
- 4.1.1 `firewall_single_service_active`: exactly one of `ufw.service`,
  `iptables.service` and `nftables.service` is active.
  `meho-firewall.service` is not on the list.

## Dependencies

- ComplianceAsCode 0.1.82 (2026-09-01): `scap-security-guide-0.1.82.tar.gz`
  from its GitHub release, pinned with the sha512 in `build.sh`. The data
  stream `ssg-ubuntu2404-ds.xml` holds the profile `cis_level1_server`
  (CIS Ubuntu 24.04 v1.0.0, 408 rules; most have a fix in bash, a few,
  such as the bootloader password and `sshd_limit_user_access`, have none). The
  same file is in the `.zip`; the `.tar.gz` needs only `tar` on the runner.
- On the appliance, from Ubuntu 24.04: `openscap-scanner` 1.3.9 (with SCE),
  `aide` 0.18.6 (`aideinit`, `dailyaidecheck.timer`), `libpam-pwquality`,
  `libpwquality-tools` 1.4.5 (`pwscore`),
  `cracklib-runtime` (the dictionary in `/var/cache/cracklib/`),
  `apparmor-utils`, `nftables` 1.0.9, `rsyslog-gnutls` 8.2312.0 (in
  `main`; `rsyslog-openssl` is only in `universe`).
- From the form-2 download: `lib/firewall.sh` (the firewall file, its unit,
  `firewall_state`).

## Known issues

- **The first real build** (week 4, #4815) ended with pass 343, not
  applicable 41, fail 6 on the build, and fail 7 on an appliance after
  first start. The ten rules that only a real build shows are recorded on
  #4640. Five of the failing rules have a fix now: three in `harden.sh`
  (root's `PATH`, `/etc/cron.allow`, `AllowUsers`) and two in first start
  (the `admin` start-up files, and the log files under `/var/log/aide` and
  `/var/log/apt`). The others are in `exceptions.md`. A new lab build has
  not checked the fixes yet.
- **Rules first start sets fail on the build:** the bootloader password
  (1.4.1, `grub2_password` and `grub2_uefi_password`). They are in
  `exceptions.md` with that reason, and pass on an appliance.
- **`aide_build_database` (6.3.1) never passes:** the check looks for a
  `database=` line in `aide.conf`, and AIDE 0.18 calls it `database_in=`.
- **The scan runs on the build VM only.** Rules that fail only on an
  appliance after first start (the world-writable kubelet files, 7.1.11)
  do not stop the build. `meho-host check` (#4737) and the release test
  (#4816) scan an appliance.
- **AIDE and the one-time files.** First start builds the database after
  it wrote the three one-time files under `/var/lib/meho/`. When the admin
  deletes them, as the guide says, AIDE's daily check reports them as
  removed until the database is built again (the update does that). The
  guide (#4817) should say to run `aideinit --yes --force` after the
  delete, or AIDE should skip those three files.

## Testing

`tests/test_harden.py` runs the real `harden.sh` with `MEHO_TEST_ROOT`,
stub commands on `PATH`, and the real `lib/firewall.sh`. The firewall file
of the fake VM is the one `render_firewall_rules` of the form-2 install
writes, so the policy change is tested on the real format. It checks the
packages, the scanner files, the order (fixes, settings, firewall, scan),
the fstab and `tmp.mount` lines, the AppArmor links, IPv6 and AIDE,
`nftables.conf` and its drop-in, root's `PATH` and the sudo file (and a
sudo file `visudo` refuses), the group of `/etc/cron.allow`, the three
`drop` policies with nothing else changed, a changed or refused firewall
file, no archive, the oscap exit codes, the result list and its warnings,
that a failing rule in neither list stops the build, and that a second run
changes nothing more. `tests/test_cis_lists.py` checks that the tailoring
and `not-applicable.md` name the same rules and that no exception is also
"not applicable".

The tailoring was also checked with the real data stream in an
`ubuntu:24.04` container: both profiles load, the scan runs 390 rules and
the fixes 381. A second container run had `/tmp` mounted `noexec` and a
build user with a locked password and sudo without a password, as
cloud-init makes it. After the fixes, that user's `sudo -n` still worked.
With `nftables` installed, five script checks apply in the container.
With `/tmp` `noexec` and no runner, all five ended in `error`. With the
runner, every rule had the same result as in a scan with an executable
`/tmp`.

## References

- Task evoila/meho#4640, Initiative evoila/meho#3958.
- ComplianceAsCode 0.1.82: `products/ubuntu2404/profiles/cis_level1_server.profile`,
  `controls/cis_ubuntu2404.yml`, and the SCE and OVAL checks under
  `linux_os/guide/system/network/network-nftables/` and
  `linux_os/guide/system/network/firewall_single_service_active/`.
- OpenSCAP 1.3.9: `oscap xccdf eval --remediate`, `--tailoring-file`, the
  exit codes 0, 1 and 2.
- XCCDF 1.2 (NIST IR 7275 rev 4): `Tailoring`, `Profile extends`, `select`.
- Ubuntu 24.04: `nftables.service` (`ExecStop=/usr/sbin/nft flush ruleset`),
  `/usr/share/systemd/tmp.mount`, `aide-common` (`aideinit`,
  `/etc/aide/aide.conf.d`).
- systemd 255 `systemd.service(5)`: an empty `ExecStop=` removes the
  command.
