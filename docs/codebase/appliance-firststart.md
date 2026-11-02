# Appliance first start: `deploy/appliance/firststart/` (#4812)

## Overview

The appliance OVA ([appliance-build.md](appliance-build.md)) leaves the
build without a host name, network, SSH host keys, login account or keys of
its own. At the customer's first boot, `firststart.sh` reads the fields of
the vCenter import wizard and makes the appliance its own: the network, the
`admin` account, the SSH rule, then the form-2 install from the disk
(`install.sh --from-disk`, [linux-server-install.md](linux-server-install.md)),
which starts RKE2 with new keys and installs the core. It runs once.

`banner.sh` writes the console banner at every boot: the console address,
the appliance's address, the one-time files, and a warning before the
`admin` password expires. While first start is not done, the banner shows
the step that stopped and its fix.

The design is the internal design note `form-1-appliance.md`, section 2.

## Key files

| File | What it holds |
| --- | --- |
| `firststart/firststart.sh` | The six steps, the done-markers, the [FAIL] line kept for the banner. |
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
   token, so RKE2 could not join. Then:
   - `hostnamectl hostname <name>`. The name does not go into `/etc/hosts`,
     so the first-start list asks the site's DNS.
   - `/etc/netplan/50-meho.yaml` (0600, as netplan wants), matching the
     interface by `en*`: a static address with gateway and DNS, or DHCP.
     Then `netplan apply`.
   - `/etc/systemd/timesyncd.conf.d/50-meho.conf` with the time servers.
     The cloud image has systemd-timesyncd, not chrony.
   - The `admin` account (groups `sudo`), the wizard password through
     `chpasswd` on stdin, `passwd --expire` so the first login must change
     it, and the SSH key in `~admin/.ssh/authorized_keys` (0600). The script
     checks the 14 characters itself, because `chpasswd` run by root skips
     the quality checks.
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
   menu's command line does. Then `update-grub`.

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

## Dependencies

On the appliance, all in the Ubuntu 24.04 cloud image: `vmtoolsd`
(open-vm-tools), `python3`, `netplan`, `systemd-timesyncd`,
`cloud-guest-utils` (`growpart`), `e2fsprogs`, `openssh-server`,
`grub2-common`. From the form-2 download: `install.sh --from-disk`,
`lib/net.sh`, `lib/checks.sh`, and `meho-host admin-network`.

## Known issues

- **Not run on a real appliance yet.** The tests use stub commands. The
  first import by hand (#4815) checks the real path: the OVF environment
  from vCenter, the console output, the boot menu.
- **No first-start list yet.** `meho status --first-start` comes with
  #4042, inside `install.sh`. The banner shows no list until then.
- **No wizard fields** stops step 3 with a message. The console setup that
  asks for the fields instead is #4813.
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
through the `vmtoolsd` stub or the CD-ROM (`mount` stub). The tests cover
every field and its check, static and DHCP, both transports, a missing
environment, the order of the steps, the markers, a run after a failed
step, the refused join fields, the bootloader password, and the banner.

On macOS run them in Docker, as `deploy/appliance/README.md` says.

## References

- Task evoila/meho#4812, Initiative evoila/meho#3958.
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
