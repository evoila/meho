# Form 1: the VMware appliance

!!! note "In testing, not released"

    The appliance has no public download yet. Ask evoila for a test
    appliance. Until the first release, some steps of this chapter can
    still change.

The appliance is one OVA file: a ready-made virtual machine for VMware
vSphere and VMware Cloud Foundation (VCF). You import it into vCenter,
fill in a few fields, and start it. MEHO is already inside, with its
database (PostgreSQL), its login server (Keycloak) and its password safe
(OpenBao). The appliance needs no internet.

You get one Linux login, `admin`, and one host command, `meho-host`. You
never manage the built-in Kubernetes yourself, and you need no Kubernetes
tools.

The appliance is [form 2, the Linux server](form-2-linux-server.md),
installed for you on a hardened Ubuntu. So some parts of this chapter
link to the form-2 chapter. Not sure this is your form? See
[Which install form is right for you?](which-form.md).

## Step 1: Check what you need

| | What |
|---|---|
| Platform | vSphere 8 or VCF 9. |
| Where you import | A vCenter. An ESXi host alone also works, see [Without the wizard](#without-the-wizard). |
| Room | 4 processors, 16 GB memory, and a 120 GB disk. The disk is thin: it takes real space only as it fills. |
| Virtual hardware | Version 20. Older hosts than ESXi 8 cannot run it. |

You also need:

- **A DNS name.** A full DNS name for the appliance, for example
  `meho.example.com`. Your DNS server must have an A record for it that
  points to the address of the appliance. Make the record before the
  import.
- **A time server.** One or more NTP servers that the appliance can
  reach. Without the right time, the first start waits and then stops.
- **An address.** A fixed IPv4 address with its prefix, the gateway and
  the DNS servers. Or DHCP, when your DHCP server always gives the same
  address.
- **The address range of your admin network.** Only this range may use
  SSH. For example `10.0.50.0/24`.
- **An SSH key pair.** SSH takes only keys, no passwords. You paste the
  public key into the import.
- **A key store.** A password manager or a safe, for two keys that the
  appliance makes at the first start.
- **A computer on the management network with a browser:** Chrome 111,
  Safari 16.4 or Firefox 128, or newer.

**Plan for two admins.** A change of rights (for example adding a person to a group or giving a role) needs a second admin to approve it. See [who approves a change of rights](../guides/approvals-and-break-glass.md#changes-of-rights-need-another-admin).

MEHO runs on your management network only, never on the internet. MEHO
holds the logins for your systems, so an address on the internet is a
risk you do not need. See
[MEHO stays on the management network](first-start-checks.md#meho-stays-on-the-management-network).

## Step 2: Download and check

A release of the appliance has four files:

- `meho-appliance-<version>.ova`: the appliance;
- `meho-appliance-<version>-os-packages.tar`: the operating system updates
  of this release. You need it only for an [update](#update);
- `SHA256SUMS`: the checksums of both files;
- `SHA256SUMS.cosign.bundle`: the signature of the checksum file.

The OVA is too big for a GitHub release. evoila tells you where the files
of a release are.

Check the signature of the checksum file first, then the files. You need
[cosign](https://docs.sigstore.dev/cosign/system_config/installation/)
version 3 or newer.

```bash
TAG=v<version>
cosign verify-blob \
  --certificate-identity "https://github.com/evoila/meho/.github/workflows/appliance-build.yml@refs/tags/$TAG" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --bundle SHA256SUMS.cosign.bundle SHA256SUMS
sha256sum -c SHA256SUMS
```

Both commands must succeed. cosign checks that the appliance build of
evoila/meho signed the checksum file for this tag. `sha256sum` prints
`OK` for each file.

## Step 3: Import

In vCenter, right-click the cluster or the folder, and choose **Deploy
OVF Template**. Select the OVA file. Choose the name, the place, the
storage and the network as for any other VM. Use the port group of your
management network.

Then the wizard asks for the settings of the appliance, in this order:

| Field | What to fill in |
|---|---|
| **Host name** | The full DNS name, for example `meho.example.com`. It must resolve in your DNS. |
| **Time servers** | One or more NTP servers, with commas between them. |
| **Network: static or DHCP** | `static` (the default) or `dhcp`. With `dhcp`, leave the next three fields empty. |
| **IP address with prefix** | For example `10.0.0.10/24`. |
| **Default gateway** | The address of the gateway. |
| **DNS servers** | One or more, with commas between them. |
| **Admin network** | The IPv4 range that may use SSH, for example `10.0.50.0/24`. From every other address, only the web console (ports 80 and 443) answers. |
| **Appliance admin password** | The first password of the Linux login `admin`. See the rules below. |
| **Admin SSH public key** | The public key that may log in as `admin` over SSH. One line that starts with, for example, `ssh-ed25519`. |
| **Join address**, **Join token**, **Virtual IP** | Leave all three empty. They are for more than one appliance, in a later version. With a join field filled in, the first start stops. |

**The password rules.** The password needs:

- at least 14 characters;
- an uppercase letter, a lowercase letter, a digit and another character
  such as `#` or `!`;
- no character more than 3 times in a row;
- no run of more than 3 characters such as `abcd` or `4321`;
- not the name `admin` anywhere in it;
- no dictionary word and no simple pattern.

The wizard checks only the length. The appliance checks the other rules
at the first start. You must change this password at the first login, so
it is used only once. A password expires after 365 days. The appliance
warns you 30 days before, on the VM console.

vCenter keeps the password of the wizard readable in the settings of the
VM. That is how OVA imports work. The change at the first login makes it
worthless.

Finish the wizard and power on the VM.

## Step 4: First start and first login

The first start takes some minutes. The appliance sets up the network and
the `admin` login, waits for the right time, and installs MEHO from its
own disk.

1. **Open the VM console in vCenter.** When the first start is done, the
   screen shows this:

    ```text
    MEHO appliance <version>

      Console:  https://meho.example.com/ui/
      Address:  10.0.0.10
      One-time file (root only; copy it to your key store, then delete it): /var/lib/meho/first-login-passwords
      One-time file (root only; copy it to your key store, then delete it): /var/lib/meho/unseal-key
      One-time file (root only; copy it to your key store, then delete it): /var/lib/meho/bootloader-password
    ```

    When a step failed, the screen shows `First start stopped:` and one
    `[FAIL]` line that says what to fix. A wrong field of the wizard is
    the usual reason. Power the VM off, change the field in the vApp
    options of the VM, and power it on again. The first start goes on
    from the step that failed.

2. **Log in as `admin` and change the password.** Use SSH with your key
   from the admin network, or the VM console. The appliance asks for the
   password of the wizard and then for a new one. The new one follows the
   same rules and must differ in at least 2 characters. After the change,
   SSH closes the session. Log in again.

3. **Read the first-login passwords:**

    ```bash
    sudo cat /var/lib/meho/first-login-passwords
    ```

    It has two lines, each as `name=value`: `admin_username` and
    `admin_password`. This is the login for the MEHO web console. It is
    not the Linux login.

4. **Copy the two keys to your key store:**

    ```bash
    sudo cat /var/lib/meho/unseal-key
    sudo cat /var/lib/meho/bootloader-password
    ```

    The keys show on your screen. Make sure that nobody else sees them,
    and clear the screen afterwards.

    - You need the unseal key to restore a backup and in an emergency.
      See [OpenBao unseal key](openbao-unseal-key.md).
    - The bootloader password is the only way back in after
      [a lost password or a locked account](#a-lost-password-or-a-locked-account).

5. **Log in to MEHO.** From a computer on the management network, open
   the console address, for example `https://meho.example.com/ui/`. Your
   browser warns about the certificate: the appliance made its own, and
   your browser does not know who signed it. Check the address and go on.
   Log in with the first-login password. The login page asks you to set a
   new password and to set up an authenticator app for one-time codes.
   Then it shows twelve recovery codes. Keep them in your key store.

6. **Delete the three files.** MEHO does not need them, and an update
   does not write them again:

    ```bash
    sudo rm /var/lib/meho/first-login-passwords /var/lib/meho/unseal-key /var/lib/meho/bootloader-password
    ```

7. **Check the appliance:**

    ```bash
    sudo meho-host check
    ```

    Every line must be `[ok]`. See
    [Check the server](form-2-linux-server.md#check-the-server).

8. **Open the first-start checks** in the console:
   `https://meho.example.com/ui/first-start`. The page is described in
   [The first-start checks](first-start-checks.md). With the appliance's
   own certificate, the `certificate` line is red. That is expected.

The `meho` command and the Claude tools on your laptops refuse the
appliance's own certificate until they trust its CA. The CA is in
`/var/lib/meho/console-ca.crt`, and it is not secret.
[Tools for your laptop](laptop-tools.md#step-1-trust-the-sites-own-certificate-authority)
says how a computer trusts it.

**You now have a running MEHO and a logged-in admin.** Go on with
[the setup page and the settings file](setup-page.md). The rest of this
chapter is for later.

## Without the wizard

An ESXi host without vCenter shows no wizard. There are two other ways.

### Answer the questions on the VM console

1. In the ESXi Host Client, choose **Create / Register VM**, then
   **Deploy a virtual machine from an OVF or OVA file**. Select the OVA
   and power on the VM.
2. Open the VM console. The appliance asks for the same fields as the
   wizard, in the same order. It checks each answer and asks again when
   one is wrong. The password is not shown while you type, and you type
   it twice.
3. The SSH key may stay empty, because a key is hard to type on a
   console. Then SSH stays closed until you add a key (see below).

### Give the fields with `ovftool`

`ovftool` 4.0 or later gives the fields to an ESXi host, with no person
at the console:

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
  --prop:admin.password='<a password that follows the rules>' \
  --prop:admin.ssh_key='ssh-ed25519 AAAA... admin@example.com' \
  meho-appliance-<version>.ova 'vi://root@esxi.example.com/'
```

`--X:injectOvfEnv` works only together with `--powerOn`. The password is
then in the shell history of the machine that runs `ovftool`. The change
at the first login makes it worthless.

### Add an SSH key later

Log in as `admin` on the VM console. Put the public key into the file of
allowed keys, one key per line:

```bash
mkdir -p -m 0700 ~/.ssh
nano ~/.ssh/authorized_keys
chmod 0600 ~/.ssh/authorized_keys
```

## The certificate and the unseal key

These two parts work as on the Linux server. The commands are the same:

- **Your own certificate.** For production, put a certificate from your
  own CA in place with `sudo meho-host certificate`. See
  [Use a certificate from your own CA](form-2-linux-server.md#use-a-certificate-from-your-own-ca).
- **The unseal key.** OpenBao, the password safe, locks itself at every
  restart. By default the appliance opens it again by itself, with a copy
  of the key that it keeps on its own disk, next to the data. So a
  restart needs no person, but a copy of the whole disk holds both the
  data and the key. In keyholder mode the appliance keeps no copy, and a
  person opens the safe after every restart with
  `sudo meho-host unseal`. See
  [The unseal key](form-2-linux-server.md#the-unseal-key).

## Update

A new MEHO version comes as two files: the Linux server download of the
release (`meho-linux-<version>-amd64.tar`, see
[form 2, Step 2](form-2-linux-server.md#step-2-download-and-check)) and
the OS-package file of the appliance. You do not import a new OVA.

1. **Take a VM snapshot.** When an update fails, you go back to it.
   There is no other way back.
2. Download and check both files. Copy them to the appliance with `scp`,
   and unpack the download:

    ```bash
    scp meho-linux-<version>-amd64.tar meho-appliance-<version>-os-packages.tar admin@meho.example.com:
    ssh admin@meho.example.com
    tar -xf meho-linux-<version>-amd64.tar
    ```

3. Run the install script of the download with the OS-package file:

    ```bash
    cd meho-linux-<version>-amd64
    sudo ./install.sh --os-packages ~/meho-appliance-<version>-os-packages.tar
    ```

    The script asks:
    `Did you take a VM snapshot or a backup of this machine? Type yes to go on:`.
    Only `yes` goes on. For a run with no person at the terminal, add
    `--confirm-backup`.

4. The script first installs the operating system updates from the file,
   with no network. It checks evoila's signature on them. Then it updates
   the built-in Kubernetes and MEHO.
5. When the script says that the new packages need a restart, restart
   the appliance with `sudo reboot`. MEHO starts again by itself.
6. Run `sudo meho-host check`. Every line must be `[ok]`. When a line is
   red and its fix does not help, go back to the snapshot.

**What the update keeps:** the fields of the import, the firewall with
your SSH range, your certificate, the data, the passwords and the keys.
It does not write the one-time files again.

**One minor version at a time.** The built-in Kubernetes (RKE2) moves
only one minor version at a time. When you skipped MEHO releases, the
script may stop and name the release that must come first. See
[Update in the form-2 chapter](form-2-linux-server.md#update).

**When an update cannot be repaired:** go back to the snapshot. Without a
snapshot, import the OVA of the new release as a new appliance and
restore your backup. See
[Back up, restore and update](backup-restore-update.md).

### Who keeps the operating system up to date

evoila does, through releases. Each release has an OS-package file with
the tested updates of Ubuntu. Fixes for the operating system alone come
as patch releases.

Do not update packages by hand, and do not point the appliance at another
package source. The appliance is tested as a whole, and a package from
somewhere else can break the hardening or MEHO. Also do not install other
software on it.

## Daily work

All commands run as `admin` with `sudo`.

| Command | What it does |
|---|---|
| `sudo meho-host check` | Prints a red/green list of the appliance. See [Check the server](form-2-linux-server.md#check-the-server). On the appliance the list has one more line, `log-server`. |
| `sudo meho-host support-bundle` | Writes one file for evoila support. Check it before you send it. See [Get help from support](form-2-linux-server.md#get-help-from-support). |
| `sudo meho-host admin-network <range>` | Changes the range that may use SSH. See [Change the SSH range](form-2-linux-server.md#change-the-ssh-range). |
| `sudo meho-host log-server <host>` | Sends the logs of the appliance to your log server. See below. |
| `sudo meho-host reset-second-factor <user>` | For a person who lost the one-time code app and the recovery codes. See [A person lost the one-time code app](form-2-linux-server.md#a-person-lost-the-one-time-code-app). |
| `sudo meho doctor` | Shows the same check list, after its own checks. |

**The password of `admin`** expires after 365 days. From 30 days before,
the VM console shows a warning. Set a new one in time with `passwd`. When
it has expired for more than 45 days, the account is disabled. Then see
[A lost password or a locked account](#a-lost-password-or-a-locked-account).

### Send the logs to your log server

```bash
sudo meho-host log-server logs.example.com
```

The appliance then sends every log line of the machine to your log
server over TCP, to port 514. The command also opens the way out to that
server in the firewall. Give another port as `logs.example.com:1514`.

With `--tls`, the logs go over TLS, to port 6514:

```bash
sudo meho-host log-server logs.example.com --tls ca.pem
```

`ca.pem` is the CA that signed the certificate of your log server. The
certificate must name the host name that you give.

The command looks up the host name once. When the address of your log
server changes, `meho-host check` warns, and you run the command again.
`sudo meho-host log-server none` stops sending and closes the way out.

## A lost password or a locked account

`admin` is the only Linux login. When its password is lost, or the
account is locked or disabled, SSH keys do not work either. The way back
in is the boot menu, with the bootloader password from your key store.

1. Restart the VM from vCenter or the ESXi Host Client, and open the VM
   console. The boot menu shows for 5 seconds. Press an arrow key to
   stop the countdown.
2. Select the first entry and press `e`. Log in as `root` with the
   bootloader password. The boot menu uses a US keyboard layout.
3. Find the line that starts with `linux`. Change `console=ttyS0` to
   `console=tty1`, and add `init=/bin/bash` at the end of the line.
   Press Ctrl+X to start.
4. A root shell opens. Run:

    ```bash
    mount -o remount,rw /
    passwd admin
    sync
    exec /sbin/init
    ```

    `passwd admin` sets a new password. The appliance then starts as
    usual.

5. When the VM console said that the account has expired, also run
   `chage -E -1 admin` before `sync`. Without it, the account still
   refuses every login.

Without the bootloader password there is no way back in. Then import a
new appliance and restore your backup.

## Security

### Closed by default

From another machine, only these ports answer:

| Port | From where |
|---|---|
| TCP 443 | Every address: the console, the API and the login. |
| TCP 80 | Every address. It only redirects to 443. |
| TCP 22 (SSH) and ping | Only the admin network. |
| Everything else | Nothing. The firewall drops it. |

SSH takes only keys, refuses `root`, and lets only `admin` in.

The firewall blocks in all three directions: traffic to the appliance,
traffic through it, and traffic that leaves it. The appliance's own
operating system may send only DNS, time (NTP), DHCP and ping. A log
server is reachable only after `meho-host log-server`. MEHO itself
connects to the systems it manages, as described in
[What MEHO connects to](form-2-linux-server.md#what-meho-connects-to).

IPv6 is off.

### CIS hardening

The operating system is hardened to the CIS Ubuntu Linux 24.04 LTS
Benchmark, Level 1 - Server. CIS, the Center for Internet Security,
writes the hardening checklists that security teams check against. Each
build is scanned, and a build with a failing rule that is not on a list
does not become a release.

Two lists come with each release, in its security sheet:

- **Rules that do not apply.** A machine that runs Kubernetes cannot
  follow a few rules. For example, IP forwarding stays on, and the
  appliance uses one of the three firewalls that CIS offers (nftables),
  so the rules for the other two do not apply.
- **Exceptions.** A short list of rules that apply and still fail, each
  with its reason. The release owner approves this list for each release.

The built-in Kubernetes also runs with its own CIS profile. See
[CIS hardening in the form-2 chapter](form-2-linux-server.md#cis-hardening).

### Files with secrets

- Only root can read the three one-time files. Delete them after the
  first login ([Step 4](#step-4-first-start-and-first-login)).
- vCenter keeps the password of the wizard in the settings of the VM.
  You change it at the first login.
- The support bundle can still hold secrets from the logs. Check it
  before you send it.
