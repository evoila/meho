# Appliance first start: `deploy/appliance/firststart/` (#4812, #4813)

## Overview

The appliance OVA ([appliance-build.md](appliance-build.md)) leaves the
build without a host name, network, SSH host keys, login account or keys of
its own. At the customer's first boot, `firststart.sh` reads the fields of
the vCenter import wizard and makes the appliance its own: the network, the
`admin` account, the SSH rule, then the form-2 install from the disk
(`install.sh --from-disk`, [linux-server-install.md](linux-server-install.md)),
which starts RKE2 with new keys and installs the core. It runs once.

An OVA imported straight into an ESXi host has no wizard fields. Then
first start asks for the same fields on the VM console. `ovftool` can also
hand the fields to an ESXi host, and the boot menu, with each appliance's
own password, is the way back in after a lost password. See "Ways in
without the wizard" below.

`banner.sh` writes the console banner at every boot: the console address,
the appliance's address, the one-time files, and a warning before the
`admin` password expires. While first start is not done, the banner shows
the step that stopped and its fix.

The design is the internal design note `form-1-appliance.md`, section 2.

## Key files

| File | What it holds |
| --- | --- |
| `firststart/firststart.sh` | The seven steps, the done-markers, the [FAIL] line kept for the banner. |
| `firststart/banner.sh` | Writes `/etc/issue`. |
| `firststart/meho-firststart.service` | Runs `firststart.sh` once, before SSH and the console login. |
| `firststart/meho-banner.service` | Runs `banner.sh` at every boot, after first start. |
| `tests/firststart_harness.py`, `tests/test_firststart.py` | A fake appliance with stub commands, and the tests of both scripts. |

`build.sh` (step 3) installs the scripts under `/opt/meho/appliance/firststart/`
and the units under `/etc/systemd/system/`, and enables both. `clean.sh`
checks that they are still there and enabled before the power-off.

## Control flow

### The units

`meho-firststart.service` is a oneshot unit with
`ConditionPathExists=!/var/lib/meho/firststart.done`. It is ordered
`Before=ssh.service systemd-user-sessions.service getty-pre.target`: SSH
needs the host keys of step 1, and the console login waits until the
`admin` account exists. Ubuntu 24.04 starts SSH through `ssh.socket`, so a
connection that comes early waits in the socket. If first start fails, SSH
and the console login still start; ordering does not need success.

The unit connects the script's standard input to `/dev/tty1`
(`StandardInput=file:/dev/tty1`), for the console questions; the kernel's
`/dev/console` is the serial port on this image. A `file:` input leaves the
output in the journal (a `tty` input would send it to the terminal only).
Before the script, `plymouth --wait quit` stops the boot splash, which
would read the same keys; systemd's own `emergency.service` does the same.
The `-` before it ignores a Plymouth that does not run.

systemd opens the `file:` input with `O_NOCTTY`, but bash opens the
terminal of its standard input again at its start (`check_dev_tty` in
bash's `shell.c`). The service's process leads its own session, so tty1
becomes its controlling terminal anyway, and a Ctrl+C typed on the console
would stop first start in the middle of a step. So `firststart.sh` ignores
`SIGINT`, `SIGQUIT` and `SIGTSTP` (`trap ''`), and the commands it starts
inherit that. A Ctrl+C at a question only clears the line typed so far.

`meho-banner.service` runs after it, also before the console login, at
every boot.

### `firststart.sh`, in order

The script sources `lib/net.sh` and `lib/checks.sh` from the copy of the
download under `/opt/meho/appliance/download/`, for the same host name and
address checks the form-2 install uses. Its output goes to the journal and,
through `tee`, to `/dev/tty1`, the screen the VM console in vCenter shows.
The unit's `journal+console` would go to the serial port, because the
cloud image's kernel line ends with `console=ttyS0`.

1. **SSH host keys.** `ssh-keygen -A` makes the keys the clean removed.
   Ubuntu does not make them again at boot, and sshd does not start without
   them. A second run keeps them.
2. **The disk.** `growpart` on the root partition, then `resize2fs`, so a
   disk made bigger at the import is used. growpart exits 1 when the
   partition already fills the disk; that counts as done.
3. **The wizard fields.** `vmtoolsd --cmd 'info-get guestinfo.ovfEnv'` gives
   the OVF environment; without VMware Tools the script mounts each CD-ROM
   and reads `ovf-env.xml`. Python's XML parser reads the
   `<Property oe:key oe:value>` elements, so escaped characters come out
   right. All fields are checked before anything changes; a wrong one stops
   the step with the fix "change the field in the VM's vApp options and
   power the VM on again". The join fields must be empty: this version runs
   as a single appliance, and `install.sh --from-disk` does not copy a join
   token, so RKE2 could not join.

   **Without an OVF environment** (no `vmtoolsd` value and no CD-ROM with
   the file), the script asks on the console, in the wizard's order: host
   name, time servers, network (`static` or `dhcp`; empty means static, as
   in the wizard), for static the address, gateway and DNS servers, the
   admin network, the password twice (not shown while typed), and the SSH
   key. Each answer goes through the wizard's own check of that field
   (`field_problem`), and a wrong answer is asked again, so a password of 13
   characters is refused. The SSH key may stay empty, because a key is hard
   to type on a VM console: then no `authorized_keys` is written, and SSH
   stays closed until the admin adds a key. If the input ends before the
   last answer, the step stops with "power the VM off and on". The join
   fields are not asked. Then:
   - `hostnamectl hostname <name>`. The name does not go into `/etc/hosts`,
     so the first-start list asks the site's DNS.
   - `/etc/netplan/50-meho.yaml` (0600, as netplan wants), matching the
     interface by `en*`: a static address with gateway and DNS, or DHCP.
     Then `netplan apply`.
   - `/etc/systemd/timesyncd.conf.d/50-meho.conf` with the time servers.
     The cloud image has systemd-timesyncd, not chrony.
   - The `admin` account (groups `sudo`; it joins the empty group `admin`
     that the cloud image already has, because `useradd` refuses to make a
     second one, exit code 9, #5739), the wizard password through
     `chpasswd` on stdin, `passwd --expire` so the first login must change
     it, and the SSH key in `~admin/.ssh/authorized_keys` (0600). Without a
     key the file is removed, in case an earlier try wrote one. The script
     checks the 14 characters itself, before anything changes. `chpasswd`
     then runs the CIS password checks too, also for root
     (`enforce_for_root`), and refuses a dictionary word
     ([appliance-cis.md](appliance-cis.md), #5744).
   - `meho-host admin-network <range>`: SSH answers only from the admin
     network.
4. **The clock.** Waits up to 10 minutes until `timedatectl` says the clock
   is synchronised, the check `install.sh` makes too.
5. **The core.** `install.sh --from-disk --hostname <name>`, with the host
   name the machine has now (so a fix made on the console after a failed
   step counts). `install.sh` starts RKE2 (and enables it at boot),
   installs the core and writes the two one-time files.
6. **The bootloader password.** 24 random letters and digits (no `y` or
   `z`, which swap places between a US and a German keyboard; the boot
   menu has a US one) go to `/var/lib/meho/bootloader-password` (0600)
   first, then `/etc/grub.d/01_meho-password` (0700) sets
   `superusers="root"` and its `password_pbkdf2` hash. Ubuntu's
   `/etc/grub.d/10_linux` gets `--unrestricted` on its `CLASS` line, so
   the normal boot needs no password; only editing an entry or the boot
   menu's command line does. The cloud image hides the boot menu and waits
   0 seconds (`/etc/default/grub.d/50-cloudimg-settings.cfg`), so only
   Shift held at the right moment would open it. So
   `/etc/default/grub.d/90-meho-boot-menu.cfg`, read after that file, sets
   `GRUB_TIMEOUT_STYLE=menu`, `GRUB_TIMEOUT=5` and
   `GRUB_RECORDFAIL_TIMEOUT=5` (the wait after a boot that did not finish):
   the menu shows for 5 seconds at every boot. Then `update-grub`.
7. **The AIDE database.** `aideinit --yes --force` builds the database of
   this appliance's files (CIS 6.3.1), after the core is installed. The
   build hardened the appliance but left the database out, because it
   must hold each appliance's own files. AIDE's daily check
   (`dailyaidecheck.timer`) compares the files with it. Which folders AIDE
   skips is in [appliance-cis.md](appliance-cis.md).

Each step writes `/var/lib/meho/firststart/<n>-<name>.done`, and
`/var/lib/meho/firststart.done` follows the last one. A failed step writes
its `[FAIL]` line to `/var/lib/meho/firststart/failed`; the next run removes
it and goes on from that step. Every step is safe to run twice.

The design's step 1 also makes a new machine-id. systemd does that by
itself: the clean wrote `uninitialized`, so the first boot makes a new one.
The design's step 9 also enables RKE2; `install.sh` already does.

### Secrets

The wizard values stay in the script's memory. The password reaches
`chpasswd` and the bootloader password reaches `grub-mkpasswd-pbkdf2`
through a pipe from `printf`, a shell builtin, so they are in no command
line. No [FAIL] line holds a secret value. The tests check the output, the
console and every command line for the passwords.

The wizard password is still readable in the VM's vApp options in vCenter
and through `vmtoolsd` inside the guest; the OVF standard passes password
fields in clear text. The forced change at the first login makes it a
one-time value.

A password typed on the console is read with `read -s`, so the console
does not show it, and it is kept only in the script's memory. The
questions go to the journal too, but the answers do not: the console
itself shows what is typed. So in the journal the questions run together
on one line.

### `banner.sh`

Writes `/etc/issue` (0644) in one step:

- the version from the download's `VERSION` file;
- with `firststart/failed`: that line (backslashes doubled, because agetty
  reads `\` as an escape) and where to read more. Before step 3 makes the
  `admin` account nobody can log in, so the banner then says to follow the
  fix from vCenter, and the fix of a failed step 1 or 2 says to power the VM
  off and on there instead of `sudo reboot`;
- before `firststart.done`: that first start has not finished;
- after it: `https://<hostname>/ui/`, `\4` (agetty prints the IPv4 address,
  so a DHCP address shows), each one-time file that still exists, and a
  `[WARN]` line when the `admin` password expires in less than 30 days or
  has expired. The days come from `getent shadow admin` (last change plus
  maximum age); a password that must be changed at the next login gives no
  warning.

## Ways in without the wizard

The design decided three ways (internal design note `form-1-appliance.md`,
section 2, "No wizard, and a lost password or a locked account"). The
week-4 import by hand (#4815) tests all three; the customer chapter
(#4817) takes the steps from here.

### The console questions

Import the OVA into the ESXi host (Host Client: Create / Register VM,
Deploy a virtual machine from an OVF or OVA file) and power it on. Open the
VM console and answer the questions (step 3 above). With an empty SSH key,
log in as `admin` on the console afterwards and put the key into
`~/.ssh/authorized_keys` (mode 0600). `meho-host ssh-key add <file>` will
do this, but form 2 has not built it yet.

### `ovftool`

`ovftool` 4.0 or later hands the fields to an ESXi host without a person at
the console. `--X:injectOvfEnv` passes the OVF environment to the VM, and
works only together with `--powerOn`. The guest then reads it through
VMware Tools, the same way as after a vCenter import, so first start needs
no change. Give every field with `--prop:<key>=<value>`; the keys are the
`ovf:key` names in `product-section.xml`:

```bash
ovftool --acceptAllEulas --name=meho --datastore=datastore1 \
  --network='VM Network' --diskMode=thin \
  --X:injectOvfEnv --powerOn \
  --prop:hostname=meho.example.com \
  --prop:net.ntp=10.0.0.4 \
  --prop:net.mode=static \
  --prop:net.ip=10.0.0.10/24 \
  --prop:net.gateway=10.0.0.1 \
  --prop:net.dns=10.0.0.2 \
  --prop:net.admin_network=10.0.50.0/24 \
  --prop:admin.password='at-least-14-characters' \
  --prop:admin.ssh_key='ssh-ed25519 AAAA... admin@example.com' \
  meho-appliance-<version>.ova 'vi://root@esxi.example.com/'
```

The password is then in the shell history and the process list of the
machine that runs `ovftool`, and in the VM's settings, as after a vCenter
import. The forced change at the first login makes it a one-time value.

### The boot menu, after a lost password or a locked `admin`

`admin` is the only Linux login, and a disabled account also refuses SSH
keys. The way back in is the boot menu, with the bootloader password from
the customer's key store (the third one-time file of step 6):

1. Restart the VM from the vSphere or ESXi console. The boot menu shows for
   5 seconds; press an arrow key to stop the countdown.
2. Select the first entry and press `e`. Log in as `root` with the
   bootloader password.
3. On the line that starts with `linux`, change `console=ttyS0` to
   `console=tty1`, and add `init=/bin/bash` at the end of the line. Press
   Ctrl+X to start it. Without the change, the root shell opens on the
   serial port, the last `console=` of the line, and not on the VM screen.
4. In the root shell:

   ```bash
   mount -o remount,rw /
   passwd admin
   sync
   exec /sbin/init
   ```

   `passwd admin` sets a new password. That also ends a lock with
   `passwd -l` and an expired password, so the 45-day disable after the
   expiry no longer applies. `exec /sbin/init` goes on with the normal
   boot.

A lock after failed logins (`pam_faillock`) needs no step: the count is
kept under `/var/run/faillock`, which is empty after every boot. If the
hardening (#4640) moves it to the disk, add
`faillock --user admin --reset` before `sync`.

## Dependencies

On the appliance, all in the Ubuntu 24.04 cloud image: `vmtoolsd`
(open-vm-tools), `python3`, `netplan`, `systemd-timesyncd`,
`cloud-guest-utils` (`growpart`), `e2fsprogs`, `openssh-server`,
`grub2-common`, `plymouth`. From the form-2 download: `install.sh --from-disk`,
`lib/net.sh`, `lib/checks.sh`, and `meho-host admin-network`.

## Known issues

- **Not run on a real appliance yet.** The tests use stub commands. The
  first import by hand (#4815) checks the real path: the OVF environment
  from vCenter, the console output and questions, `ovftool`, the boot menu.
- **No first-start list yet.** `meho status --first-start` comes with
  #4042, inside `install.sh`. The banner shows no list until then.
- **`meho-host ssh-key add` does not exist yet.** After a console setup
  with an empty SSH key, the admin writes `~/.ssh/authorized_keys` by hand.
- **An OVF environment with empty fields** gets the wizard's checks and
  their vApp-options fix, not the console questions. If an ESXi host's own
  import passes the properties empty, the week-4 import shows it.
- **The join fields** are refused at step 3. `install.sh --from-disk` does
  not copy a join token, and joining is not part of the single-node form.
  The fields stay in the product section for the HA work.
- **10_linux** is a conffile of Ubuntu's GRUB package. A package update that
  replaces it drops `--unrestricted`, and then the boot menu asks for the
  password at every boot. The update path (#4814) must keep the line.

## Testing

`tests/test_firststart.py` follows `test_clean.py`: the real scripts run
with `MEHO_TEST_ROOT` pointing at a temporary root folder, and only stub
commands and a few real tools on `PATH`. The harness writes the OVF
environment the way vCenter does, with escaped values, and serves it
through the `vmtoolsd` stub or the CD-ROM (`mount` stub). The console
answers come on the script's standard input. The tests cover every field
and its check, static and DHCP, both transports, the order of the steps,
the markers, a run after a failed step, the refused join fields, the
bootloader password and the boot menu file, and the banner. Without an OVF
environment they check that the console setup ends with the same files and
commands as a wizard import, that each wrong answer and a 13-character or
mistyped password are asked again, the empty SSH key, input that ends
early, and that no password reaches the output or a command line.

On macOS run them in Docker, as `deploy/appliance/README.md` says.

## References

- Tasks evoila/meho#4812 and #4813, Initiative evoila/meho#3958.
- systemd 255 `systemd.exec(5)` (`StandardInput=file:`, and the `inherit`
  default of `StandardOutput=` that only `tty`, `socket` and `fd:` inputs
  get); systemd v255 `src/core/exec-invoke.c` (`acquire_path` opens a
  `file:` input with `O_NOCTTY`); `units/emergency.service.in`
  (`plymouth --wait quit`).
- Bash 5.2 manual, `read -s` and `trap`; bash 5.2 `shell.c`
  (`check_dev_tty` opens `ttyname(0)` without `O_NOCTTY`; seen with
  `strace` on a pseudo-terminal, and Ctrl+C then stopped the script).
- OVF Tool 4.0 and later: `--X:injectOvfEnv` with `--powerOn`, and
  `--prop:` (William Lam, "How to finally inject OVF properties into VCSA
  when deploying directly onto ESXi", 2014; COT 1.4.1 `deploy_esxi`).
- GRUB manual, "Simple configuration" (`GRUB_TIMEOUT_STYLE`,
  `GRUB_TIMEOUT`).
- `faillock(8)` on Ubuntu 24.04 (the default folder `/var/run/faillock`).
- OVF environment: DMTF DSP0243 2.1.1 (the `Environment` and
  `PropertySection` elements; password values in clear text); open-vm-tools
  13.0.10 `services/vmtoolsd/cmdLine.c` (`--cmd` exits 1 when no value is set).
- systemd 255: `systemd.special` (`getty-pre.target`), `timesyncd.conf`,
  `machine-id(5)`.
- netplan 1.1.2: file permissions (`parse.c`), `netplan apply`.
- GRUB 2.12 manual (`superusers`, `password_pbkdf2`, `--unrestricted`);
  Ubuntu's `10_linux.in`.
- cloud-guest-utils 0.33 `growpart` exit codes.
- util-linux 2.39 `agetty(8)` (`\4`).
