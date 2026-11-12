# Form 2: the Linux server

!!! note "Available, in testing"

    Use this form for tests and pilots. Talk to evoila before you run
    it in production.

This chapter takes you from a plain Linux server to a running MEHO and
a first login. Follow it from top to bottom.

One download holds everything: the install script, a small built-in
Kubernetes (RKE2), the MEHO chart, every container image, `helm` and the
`meho` command. One script, `install.sh`, installs MEHO with its
database (PostgreSQL), its login server (Keycloak) and its password safe
(OpenBao). The server needs no internet.

You never manage the built-in Kubernetes yourself, and you need no
Kubernetes tools. For the work after the install, the server has its
own command, `meho-host`.

Not sure this is your form? See
[Which install form is right for you?](which-form.md).

## Step 1: Check what the machine needs

| | Recommended | Minimum |
|---|---|---|
| Operating system | Ubuntu 24.04 LTS on x86-64. This is tested. | x86-64 with systemd. Another Linux, for example Red Hat Enterprise Linux 9, gets a "not tested" warning, and the install goes on. |
| Processors | 4 | 4 |
| Memory | 16 GB | 8 GB |
| Disk | 120 GB, on SSD | 80 GB free under `/var/lib` |

The install refuses a machine below the minimum.

The machine also needs:

- **Root access.** You run the install with `sudo`, over SSH or on the
  console of the machine.
- **A DNS name.** A full DNS name for the machine, for example
  `meho.example.com`. Your DNS server must have an A record for it that
  points to an address of the machine. An entry in `/etc/hosts` does not
  count. The install does not create the record.
- **A time server.** The clock of the machine must be synchronised, for
  example with `systemd-timesyncd` or `chrony`. `timedatectl` must show
  `System clock synchronized: yes`.
- **The address range of your admin network.** After the install, only
  this range may use SSH. See [The admin network](#the-admin-network).
- **Nothing else that runs containers.** No Docker, containerd, k3s,
  RKE2 or other Kubernetes may be on the machine. The ports of the
  built-in Kubernetes must be free. The install checks both.

You also need a computer on the management network with a browser:
Chrome 111, Safari 16.4 or Firefox 128, or newer.

MEHO runs on your management network only, never on the internet. Users
reach it on ports 80 and 443 from the management network. MEHO holds the
logins for your systems, so an address on the internet is a risk you do
not need. See
[MEHO stays on the management network](first-start-checks.md#meho-stays-on-the-management-network).

## Step 2: Download and check

Each [GitHub release](https://github.com/evoila/meho/releases) has three
files for the Linux server:

- `meho-linux-<version>-amd64.tar`: the download;
- `SHA256SUMS-linux`: the checksum of the download;
- `SHA256SUMS-linux.cosign.bundle`: the signature of the checksum file.

The server needs no internet, so download the three files on another
machine. Check the signature of the checksum file there first, then the
download. You need
[cosign](https://docs.sigstore.dev/cosign/system_config/installation/):

```bash
TAG=v<version>
cosign verify-blob \
  --certificate-identity "https://github.com/evoila/meho/.github/workflows/linux-download.yml@refs/tags/$TAG" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --bundle SHA256SUMS-linux.cosign.bundle SHA256SUMS-linux
sha256sum -c SHA256SUMS-linux
```

Both commands must succeed. cosign checks that the release workflow of
evoila/meho signed the checksum file for this tag. `sha256sum` prints
`OK` for the download.

Copy the download and `SHA256SUMS-linux` to the server, for example with
`scp`. Then run `sha256sum -c SHA256SUMS-linux` on the server too. So you
know that the copy is complete.

## Step 3: Install

Unpack the download and run the install script as root:

```bash
tar -xf meho-linux-<version>-amd64.tar
cd meho-linux-<version>-amd64
sudo ./install.sh --hostname meho.example.com --admin-network 192.0.2.0/24
```

Use your own DNS name and your own admin range.
`sudo ./install.sh --help` lists every option.

### The options

| Option | What it is | Default |
|---|---|---|
| `--hostname <fqdn>` | The full DNS name of the machine. Users open the console at this name. | The full name of the machine (`hostname -f`). |
| `--admin-network <range>` | The IPv4 addresses that may use SSH: a range such as `192.0.2.0/24`, one address, or `none`. | Only the address of your SSH session. |
| `--public-address <ipv4>` | Only when address translation inside your network is in front of the machine, for example a load balancer: the address that the DNS name points to. Never an address on the internet. | None. |
| `--first-tenant-id <uuid>` | The id of the first tenant. Give the old id when you move MEHO to another machine. | A new random id. |
| `--admin-user <name>` | The user name of the first admin. | `admin` |
| `--approver-user <name>` | The user name of the second person, who approves the admin's changes. | `approver` |
| `--cluster-cidr <cidr>` | The address range of the pods. | `10.42.0.0/16` |
| `--service-cidr <cidr>` | The address range of the services. | `10.43.0.0/16` |

An update keeps these answers and refuses these options. Only the SSH
range can change after the install, with `meho-host admin-network` (see
[Daily work](#daily-work)). The options to join a second server are not
tested yet.

### The admin network

After the install, SSH answers only from the admin network. A range must
start at its first address: `192.0.2.0/24` is right, `192.0.2.7/24` is
refused.

| You give | You run the install over SSH | You run the install on the console of the machine |
|---|---|---|
| A range or an address | The install goes on when your own address is in it. It stops when your address is not in it, so you do not lock yourself out. | The install goes on. |
| Nothing | SSH is allowed only from your own address. The install warns and goes on. | The install stops and asks for `--admin-network`. |
| `none` | The install warns: no new SSH login works after this session. Only the console works. | The install goes on. |

The SSH session that runs the install stays open in every case.

### The pod and service ranges

The built-in Kubernetes gives its pods and services addresses from two
ranges: `10.42.0.0/16` and `10.43.0.0/16` by default. The install stops
when a range overlaps an address or a route of the machine.

The install cannot see the addresses of the systems that MEHO will
manage. When one of them, for example a vCenter, has an address in one
of these ranges, MEHO cannot reach it. The machine sends that traffic to
its own pods or services. Then choose two other ranges that no system of
your site uses, and give them with `--cluster-cidr` and
`--service-cidr`. Decide this before the install.

### What the script does

1. **It checks the machine.** Each check prints `[ok]` or `[WARN]`. When
   something is wrong, the script stops with one `[FAIL]` line that says
   what to fix. Nothing on the machine has changed yet, so fix the
   problem and run the script again.
2. **It prepares the machine.** It turns on the MEHO host firewall (see
   [Security](#security)). When firewalld or ufw is on, it turns them off
   after the MEHO firewall is on, so the machine is never without a
   firewall. It writes the settings of RKE2 and installs the command
   `meho-host`.
3. **It installs and starts RKE2**, with its CIS hardening profile.
4. **It installs MEHO** with its database, login server and password
   safe.
5. **It writes the first-login passwords and the unseal key** to two
   files that only root can read. Nothing secret shows on the screen.
6. **It installs the `meho` command** and writes the install record
   `/etc/meho/install.json`. The record holds no secret.

When a later step fails, its `[FAIL]` line says what to do. After RKE2
has started, a second run finds a machine that is only partly installed,
and it stops. Then run `sudo meho-host support-bundle` and send the file
to evoila support (see [Get help from support](#get-help-from-support)).

At the end, the script prints the result:

```text
install.sh: MEHO <version> is installed on meho.example.com.
  Console:               https://meho.example.com/ui/
  First-login passwords: /var/lib/meho/first-login-passwords (root only; delete it after the first login)
  Unseal key:            /var/lib/meho/unseal-key (root only; copy it to your key store, then delete it)
  First-start list:      not in this version yet. Open the console address to check that MEHO answers.
```

## Step 4: First login

The install made two users with temporary passwords: the first admin,
and a second person who approves the admin's changes.

1. On the server, read the file with the passwords:

    ```bash
    sudo cat /var/lib/meho/first-login-passwords
    ```

    It has four lines, each as `name=value`: `admin_username`,
    `admin_password`, `approver_username` and `approver_password`.

2. From a computer on the management network, open the console address,
   for example `https://meho.example.com/ui/`. Port 80 sends you on to
   port 443.
3. Your browser warns about the certificate. The install made its own
   certificate, and your browser does not know who signed it. Check that
   the address is the one you installed, and go on. See
   [The console certificate](#the-console-certificate).
4. Log in as the admin, with the temporary password. The login page
   asks you to set a new password and to set up an authenticator app for
   one-time codes. Then it shows recovery codes. Keep them in your key
   store.
5. Give the approver's user name and temporary password to the second
   person in a safe way. That person logs in the same way.
6. Copy the unseal key to your key store, for example your password
   manager or a safe:

    ```bash
    sudo cat /var/lib/meho/unseal-key
    ```

    The key shows on your screen. Make sure that nobody else sees it,
    and clear the screen afterwards. You need the key to restore a
    backup and in an emergency. See
    [OpenBao unseal key](openbao-unseal-key.md).

7. Delete both files. MEHO does not need them, and an update does not
   write them again:

    ```bash
    sudo rm /var/lib/meho/first-login-passwords /var/lib/meho/unseal-key
    ```

8. In the console, open the first-start checks:
   `https://meho.example.com/ui/first-start`. The page is described in
   [The first-start checks](first-start-checks.md). With the install's
   own certificate, the `certificate` line is red. That is expected (see
   the next step).
9. On the server, check the built-in Kubernetes and the hardening:

    ```bash
    sudo meho-host check
    ```

    Every line must be `[ok]`. See [Check the server](#check-the-server).

## Step 5: The certificate and the unseal key

### The console certificate

The install made a certificate for the DNS name. It is signed by the
install's own certificate authority (CA), "MEHO install CA". Your
browsers and laptops do not know this CA. So:

- browsers warn at the first visit;
- the `certificate` line of the first-start list is red;
- the `meho` command and the Claude tools on your laptops refuse the
  certificate (see [Tools for your laptop](laptop-tools.md)).

For production, use a certificate from your own CA.

### The unseal key

OpenBao, the password safe, locks itself ("seals") at every restart. By
default it opens ("unseals") itself again within seconds, with a copy of
the key that the install keeps on the machine. So a restart of the
machine needs no person. The page
[OpenBao unseal key](openbao-unseal-key.md#what-self-unseal-protects-and-what-it-does-not)
says what this protects and what it does not.

In keyholder mode, OpenBao stays sealed after a restart until a person
unseals it with the key.

!!! note "Not in this version yet"

    Two tasks need a command on the server that this version does not
    have yet: putting a certificate from your own CA in place, and
    switching OpenBao to keyholder mode. Ask evoila if you need either
    of them now.

**You now have a running MEHO and a logged-in admin.** Go on with
[the setup page and the settings file](setup-page.md). The rest of this
chapter is for later: updates, daily work, the operating system,
security and uninstall.

## Update

A new MEHO version comes as a new download. You run its `install.sh` on
the same machine. There is no other update command.

1. **Take a VM snapshot or a backup of the machine.** When an update
   fails, you go back to it. There is no other way back.
2. Download and check the new download as in
   [Step 2](#step-2-download-and-check). Copy it to the server and
   unpack it.
3. Run its install script, without the options of a first install:

    ```bash
    cd meho-linux-<new version>-amd64
    sudo ./install.sh
    ```

    The script finds the install and says which version it updates to.
    Then it asks:
    `Did you take a VM snapshot or a backup of this machine? Type yes to go on:`.
    Only `yes` goes on. For a run with no person at the terminal, add
    `--confirm-backup`.

4. When the script is done, run `sudo meho-host check`.

The update keeps the answers of the install, the firewall with your SSH
range, the data, the passwords and the keys. It does not write the two
first-login files again.

**One minor version at a time.** RKE2 moves only one minor version at a
time, for example from v1.37 to v1.38, but not from v1.37 to v1.39. When
you skipped MEHO releases, the script may stop and name the RKE2
version that must come first. Then install the MEHO release with that
RKE2 version first. The file `VERSION` in each unpacked download names
its RKE2 version (`RKE2_VERSION`). `sudo cat /etc/meho/install.json`
shows the installed versions.

The script also refuses a MEHO version that is older than the installed
one, because the database changes of an update cannot be undone.

When a step fails, the script stops with one `[FAIL]` line. The install
record still names the old version. So you can fix the problem and run
the update again, or go back to the snapshot.

## Daily work

All commands run as root on the server.

### Check the server

```bash
sudo meho-host check
```

The command prints a red/green list:

| Line | What it checks |
|---|---|
| `node` | The built-in Kubernetes is ready. |
| `pods` | Every part of MEHO and of the built-in Kubernetes runs. |
| `disk` | The free space in `/var/lib/rancher` and `/var/lib/meho`. A warning below 20 %, red below 15 %: the built-in Kubernetes starts to stop pods when less than 15 % is free. |
| `certificates` | The internal certificates of the built-in Kubernetes. A warning when one has less than 30 days left, red when one has expired. |
| `firewall` | The MEHO host firewall is loaded, and it is loaded again at every start of the machine. |
| `cis-profile` | The CIS hardening profile of RKE2 is on. |
| `console-certificate` | The certificate of the console is valid, names the DNS name, and has more than 30 days left. |

Each line is `[ok]`, `[WARN]` or `[FAIL]`. A line that is not ok says
what to do. The exit code is 0 when every line is ok or a warning, and 1
when a line is red. So your monitoring can run the check. `--json`
prints the same list as JSON.

`sudo meho doctor` on the server shows the same lines, after its own
checks. See
[Troubleshooting](../clients/troubleshooting.md#start-with-meho-doctor).

### Change the SSH range

```bash
sudo meho-host admin-network 192.0.2.0/24
```

The command changes only the SSH rule of the MEHO firewall. It takes one
IPv4 range or one address. `none` closes SSH from every address, so only
the console of the machine works. Over SSH, the command asks first when
the new range does not hold your own address.

### Get help from support

```bash
sudo meho-host support-bundle
```

The command writes one file to `/var/lib/meho/support/` and prints its
path. Only root can read the file. It holds:

- the check list;
- the logs of MEHO and of its web entry point (Traefik);
- the state of the built-in Kubernetes;
- the log of RKE2 of the last 24 hours;
- facts about the operating system.

Passwords, tokens and keys are removed from it. Send the file to evoila
support, then delete it.

## Who keeps the operating system up to date

You do. The machine and its Linux are yours, with your patch cycle and
your tools. For example: Ubuntu's unattended upgrades, Red Hat
Satellite, or updates by hand. The download holds no operating system
packages. A server with no internet gets its updates from your own
package mirror.

While MEHO is on the machine:

- Install operating system updates as usual. A restart is fine: RKE2
  and MEHO start again by themselves, and OpenBao unseals itself.
- Do not change or delete `/var/lib/rancher`, `/etc/rancher`,
  `/var/lib/meho` or `/etc/meho`. They hold the built-in Kubernetes, the
  data of MEHO, its settings and the firewall file. There are three
  exceptions, each described in this chapter: your own rules in
  `/etc/meho/firewall.nft`, the two first-login files, and the support
  bundles in `/var/lib/meho/support/`.
- Do not replace the MEHO firewall, and do not turn on firewalld or ufw
  again. Add your own rules to the MEHO firewall file instead (see
  [The firewall file](#the-firewall-file)).
- Do not install Docker, another container runtime or another
  Kubernetes on the machine.

## Security

### Closed by default

From another machine, only these ports answer:

| Port | From where |
|---|---|
| TCP 443 | Every address: the console, the API and the login. |
| TCP 80 | Every address. It only redirects to 443. |
| TCP 22 (SSH) and ping | Only the admin network. |
| Everything else | Nothing. The MEHO firewall drops it. |

The ports of the built-in Kubernetes are closed from outside too. A port
scan from another machine shows only the ports above. The traffic that
leaves the machine is not filtered, because the operating system and its
other software are yours.

### The firewall file

The rules are in one file, `/etc/meho/firewall.nft`, with comments for
your security team. The file is for nftables, the firewall of the Linux
kernel. It holds only its own table, `inet meho`, and leaves the rules
of other tables in place.

The service `meho-firewall.service` loads the file at every start of the
machine, before the network and before RKE2. RKE2 does not start while
the rules are missing.

To let more traffic in, add your own accept rules to the chain
`prerouting`, before its last rule (`counter`). Then check the file and
load it again:

```bash
sudo nft -c -f /etc/meho/firewall.nft
sudo systemctl reload meho-firewall.service
```

The first command only checks the file. It changes nothing. Never add
`flush ruleset` to the file: the built-in Kubernetes keeps its own rules
in other tables, and that command deletes them.

### CIS hardening

The built-in Kubernetes runs with RKE2's CIS profile from the first
install. CIS, the Center for Internet Security, writes the hardening
checklists that security teams check against. The profile follows the
CIS Kubernetes Benchmark. With it:

- the strictest built-in pod rule of Kubernetes ("restricted") applies
  in every namespace, the namespace of MEHO too;
- the install creates the `etcd` user and group when they are missing,
  and sets the kernel settings of RKE2 in
  `/etc/sysctl.d/60-rke2-cis.conf`.

MEHO adds one exception: the namespace `local-path-storage`. Its storage
helper keeps the data of MEHO in folders of the machine
(`/var/lib/meho/volumes`), and the strictest rule refuses access to
folders of the machine. The exceptions of RKE2 itself stay as they are:
`kube-system`, `compliance-operator-system` and `tigera-operator`.

The profile hardens the built-in Kubernetes, not the operating system.
`meho-host check` shows whether the firewall and the profile are on.

### Files with secrets

- Only root can read `/var/lib/meho/first-login-passwords` and
  `/var/lib/meho/unseal-key`. Delete both after the first login
  ([Step 4](#step-4-first-login)).
- Only root can open the folders `/etc/meho` and `/var/lib/meho`.
- The support bundle holds no password, token or key.

## Uninstall

```bash
sudo meho-host uninstall
```

The command removes MEHO and the built-in Kubernetes from the machine,
with all their data. It asks twice, and only `yes` goes on. Take a
backup first if you need the data.

It also removes the kernel settings file of RKE2, and the `etcd` user and
group when the install created them. A restart of the machine brings
back the kernel values of the operating system.

The MEHO firewall stays on, so the machine is never without a firewall.
The last lines of the command name the command that removes it. Turn on
your own firewall first, then remove the MEHO firewall.
