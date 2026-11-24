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
- **The networks of the systems that MEHO manages**, for example of your
  vCenter and your switches. MEHO accepts a system on a private address
  only inside these networks. See [The target networks](#the-target-networks).
- **Nothing else that runs containers.** No Docker, containerd, k3s,
  RKE2 or other Kubernetes may be on the machine. The ports of the
  built-in Kubernetes must be free. The install checks both.

You also need a computer on the management network with a browser:
Chrome 111, Safari 16.4 or Firefox 128, or newer.

**Plan for two admins.** A change of rights (for example adding a person to a group or giving a role) needs a second admin to approve it. See [who approves a change of rights](../guides/approvals-and-break-glass.md#changes-of-rights-need-another-admin).

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
[cosign](https://docs.sigstore.dev/cosign/system_config/installation/)
version 3 or newer. Version 2 cannot read the signature file of the
release: it stops with `bundle does not contain cert for verification`.

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
| `--target-networks <list>` | The networks of the systems that MEHO manages: IPv4 ranges, addresses and host names, with commas between them. See [The target networks](#the-target-networks). | None: MEHO accepts no system on a private address. |
| `--public-address <ipv4>` | Only when address translation inside your network is in front of the machine, for example a load balancer: the address that the DNS name points to. Never an address on the internet. | None. |
| `--first-tenant-id <uuid>` | The id of the first tenant. Give the old id when you move MEHO to another machine. | A new random id. |
| `--admin-user <name>` | The user name of the one Admin login. | `admin` |
| `--cluster-cidr <cidr>` | The address range of the pods. | `10.42.0.0/16` |
| `--service-cidr <cidr>` | The address range of the services. | `10.43.0.0/16` |

An update keeps these answers and refuses these options. Only the SSH
range and the target networks can change after the install, with
`meho-host admin-network` and `meho-host target-networks` (see
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

### The target networks

MEHO checks the address of each system that you add as a target. It
accepts a system on a public address. It accepts a system on a private
address, for example a vCenter on `10.20.0.5`, only inside the target
networks. So MEHO cannot be used to reach the other systems of your
network. Give the target networks with `--target-networks`, or later
with `sudo meho-host target-networks` (see
[Change the target networks](#change-the-target-networks)).

The list has entries with commas and no spaces between them:

| Entry | Example | MEHO accepts |
|---|---|---|
| An IPv4 range | `10.20.0.0/16` | A system with an address in the range, on every port. |
| One IPv4 address | `10.30.0.5` | A system with this address, on every port. |
| A host name | `vcenter.example.com` | A system that you add with exactly this name, on every port. |
| An address or a host name with a port | `vcenter.example.com:443` | This system on this port only. |

For example:

```bash
sudo ./install.sh --hostname meho.example.com --admin-network 192.0.2.0/24 \
    --target-networks 10.20.0.0/16,vcenter.example.com:443
```

Name narrow ranges: the networks where your managed systems are, not
all of `10.0.0.0/8`. A system that you add by its name is accepted when
the name is in the list, or when the name points to an address inside a
listed range.

The install refuses a list with a wrong entry, before it changes
anything. A range must start at its first address, as for the admin
network. MEHO on this form reaches systems only over IPv4, so an IPv6
entry is refused. No entry may hold an address of MEHO itself:

- the pod range and the service range of the built-in Kubernetes (see
  the next section). So with the default ranges, `10.0.0.0/8` is
  refused;
- `127.0.0.0/8`, `0.0.0.0/8` and the link-local range `169.254.0.0/16`;
- the names `localhost` and `cluster.local`, and every name that ends
  with them.

Without the option, the list is empty, and MEHO accepts no system on a
private address. The result of the install then says so.

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
   `/etc/meho/install.json`. The record holds no secret. From v0.47.0, it
   also writes the CA of the console certificate to
   `/var/lib/meho/console-ca.crt` (see
   [The console certificate](#the-console-certificate)). From v0.49.0,
   the last line of the result names where to read the
   [first-start list](first-start-checks.md) after the first login. MEHO
   shows the list only to a signed-in person.

When a later step fails, its `[FAIL]` line says what to do. After RKE2
has started, a second run finds a machine that is only partly installed,
and it stops. Then run `sudo meho-host support-bundle` and send the file
to evoila support (see [Get help from support](#get-help-from-support)).

At the end, the script prints the result. From v0.49.0 it looks like
this:

```text
install.sh: MEHO <version> is installed on meho.example.com.
  Console:               https://meho.example.com/ui/
  Console CA:            /var/lib/meho/console-ca.crt (not secret; give it to the browsers and laptops that use MEHO)
  First-login passwords: /var/lib/meho/first-login-passwords (root only; delete it after the first login)
  Unseal key:            /var/lib/meho/unseal-key (root only; copy it to your key store, then delete it)
  Target networks:       10.20.0.0/16,vcenter.example.com:443
  First-start list:      after the first login, open https://meho.example.com/ui/first-start, or run "meho login https://meho.example.com" and then "meho status --first-start"
```

Read the list after the first login ([Step 4](#step-4-first-login)).

Before v0.49.0, the result has no `Target networks` line. In v0.47.0
and v0.48.0, the script reads the list itself and shows its lines under
the last line. Before v0.47.0, the result has no `Console CA`
line, and the last line says `First-start list: not in this version yet`.

## Step 4: First login

The install made one Admin login with a temporary password. It is in
`MEHO-Admins` and `MEHO-Platform-Admins`.

1. On the server, read the file with the passwords:

    ```bash
    sudo cat /var/lib/meho/first-login-passwords
    ```

    It has two lines, each as `name=value`: `admin_username` and
    `admin_password`.

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
5. Copy the unseal key to your key store, for example your password
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

From v0.47.0, the install writes this CA to
`/var/lib/meho/console-ca.crt`. The CA is not secret. Only root can open
the folder, so read the file with `sudo`:

```bash
sudo cat /var/lib/meho/console-ca.crt
```

Give the file to the computers that use MEHO. [Tools for your
laptop](laptop-tools.md#step-1-trust-the-sites-own-certificate-authority)
says how a computer trusts it. MEHO itself does not trust this CA, so the
`certificate` line of the first-start list stays red.

For production, use a certificate from your own CA.

### Use a certificate from your own CA

From v0.47.0, `meho-host certificate` puts a certificate from your own CA
in place.

1. Get a certificate for the DNS name of the install from your CA. It
   must be made for a TLS server. You need two files:
    - `chain.pem`: the server certificate first, then every intermediate
      CA certificate, and your CA certificate last;
    - `key.pem`: the private key of the server certificate, without a pass
      phrase.
2. Copy both files to the server, and run:

    ```bash
    sudo meho-host certificate --cert chain.pem --key key.pem
    ```

    The command checks that the key belongs to the certificate, that the
    certificate names the DNS name of the install and is valid now, and
    that the chain ends at your CA. When a check fails, one `[FAIL]` line
    says what is wrong, and nothing changes.

3. When every check passes, the console, the API and the login show the
   new certificate within a minute. The command writes your CA to
   `/var/lib/meho/console-ca.crt`. Its last line is the `console-certificate`
   line of `meho-host check`. With 30 days or less left, the line is a
   `[WARN]`.
4. From v0.49.0, the command also gives your CA to MEHO, and MEHO
   restarts. The console is away for about a minute. After that, the
   `certificate` line of the first-start list is green.
5. MEHO keeps the key. Delete your copy of `key.pem` from the server.

An update keeps your certificate, and MEHO keeps trusting your CA. To
replace the certificate, for example before it expires, run the command
again with the new files. If you installed your certificate with an
older version, run the command again after the update to v0.49.0, so the
`certificate` line turns green.

### The unseal key

OpenBao, the password safe, locks itself ("seals") at every restart. By
default it opens ("unseals") itself again within seconds, with a copy of
the key that the install keeps on the machine. So a restart of the
machine needs no person. The page
[OpenBao unseal key](openbao-unseal-key.md#what-self-unseal-protects-and-what-it-does-not)
says what this protects and what it does not.

In keyholder mode, OpenBao stays sealed after a restart until a person
unseals it with the key. From v0.47.0, `meho-host` switches the mode.

To switch to keyholder mode:

```bash
sudo meho-host keyholder on
```

The command asks for the unseal key from your key store. What you type
or paste is not shown. It checks that the key is the right one, deletes
the copy of the key that MEHO keeps on the machine, and restarts OpenBao.
OpenBao is then sealed, and MEHO waits. So you see at once that the
keyholder step works. Unseal OpenBao:

```bash
sudo meho-host unseal
```

From now on, after every restart of the machine, a keyholder runs
`sudo meho-host unseal`. Until then, MEHO waits. When an update restarts
OpenBao, it waits up to 10 minutes for a keyholder (see [Update](#update)).

To go back, so that OpenBao unseals itself again:

```bash
sudo meho-host keyholder off
```

It asks for the key, keeps a copy of it on the machine, and restarts
OpenBao.

Never give the key on the command line, because it would stay in the
shell history. A script gives the key on standard input instead.

!!! note "Before v0.47.0"

    `meho-host certificate`, `meho-host keyholder` and `meho-host unseal`
    come with v0.47.0. Ask evoila if you need your own certificate or
    keyholder mode on an older version.

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

    In keyholder mode, when the update restarts OpenBao, it waits up to
    10 minutes for the unseal. A keyholder then runs
    `sudo meho-host unseal` in a second SSH session.

4. When the script is done, run `sudo meho-host check`.

The update keeps the answers of the install with the target networks,
the firewall with your SSH range, your certificate, the data, the
passwords and the keys. It does not
write the two first-login files again. From v0.47.0, it writes the CA file
again and, as the install does, names where to read the first-start list
at the end.

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

### Change the target networks

```bash
sudo meho-host target-networks 10.20.0.0/16,vcenter.example.com:443
```

The command sets the networks of the systems that MEHO manages, with the
rule of [The target networks](#the-target-networks). It replaces the
whole list, so give every network again. `none` empties the list: MEHO
then accepts no system on a private address. When an entry is wrong,
nothing changes.

MEHO restarts for this: the console is away for about a minute. A target
that MEHO refused before can be added after the restart. An update keeps
the list. `sudo cat /etc/meho/install.json` shows it (`target_networks`).

### A person lost the one-time code app

Use this only in an emergency: when a person of a local account (the
first admin) lost the phone with the one-time code
app and also the recovery codes, so they cannot log in any more.

```bash
sudo meho-host reset-second-factor <user>
```

`<user>` is the person's user name in MEHO's login (realm `meho`). The
command:

- removes the person's one-time code and recovery codes;
- makes the next login of the person ask for a new password and a new
  one-time code app, and then show twelve new recovery codes;
- writes one row into the MEHO audit for each tenant of the person. It
  names your Linux user (the one behind `sudo`), this server, the person's
  user name and the time. An admin of each of these tenants finds it with
  `meho audit query --op-id meho-host.reset-second-factor`.

It prints what it removed. An unknown user name changes nothing, and the
command ends with exit code 1. Run as another user than root, it changes
nothing and ends with exit code 2. When the command cannot write the
audit row, it does not report success. When Keycloak does not answer a
change in time, the change may still have happened: the command says so,
writes the audit row with it, and ends with exit code 1. Run it again; a
second run is safe.

Give the person a new temporary password in a safe way, for example in
the Keycloak admin console (see [Local users](login/local-users.md)). It
does not apply to people who log in through your company login: their
second factor belongs to the company login.

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

**Check the file before you send it.** The command removes the
passwords and keys that the install made, and the value of each setting
whose name has PASSWORD, TOKEN, SECRET or KEY in it. It cannot find
every secret in the logs. For example, it keeps a login token after
`Authorization: Bearer`, and a password inside an address such as
`https://<user>:<password>@<host>`.

So unpack the file and search it. Use the path that the command
printed:

```bash
sudo install -d -m 0700 /root/support-check
sudo tar -xzf /var/lib/meho/support/<file>.tar.gz -C /root/support-check
sudo grep -r -n -i -E 'authorization:|bearer |://[^/@ ]+:[^/@ ]+@|--password' /root/support-check
```

The search shows the usual places where a secret can stay. It does not
find every secret. Read each line it shows. When a line holds a secret,
replace the secret in that file, for example with `[removed]`. Then
pack the folder again and send the new file to evoila support:

```bash
sudo tar -czf /root/meho-support-checked.tar.gz -C /root/support-check .
```

When support has the file, delete the bundle, the folder and the new
file:

```bash
sudo rm -r /var/lib/meho/support/<file>.tar.gz /root/support-check /root/meho-support-checked.tar.gz
```

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

### What MEHO connects to

MEHO connects to the systems it manages and to the AI service. From
v0.47.0, the network rule of MEHO lets it connect to every address
outside the built-in Kubernetes, on every port.

MEHO also checks each system before it connects to it. It accepts a
system on a private address only inside the target networks (see
[The target networks](#the-target-networks)). Most systems of a data
center have private addresses, so name their networks at the install or
with `sudo meho-host target-networks`. Before v0.49.0, the target
networks cannot be set on this form, so MEHO refuses every system on a
private address.

Two things stay closed to MEHO:

- the other parts of the built-in Kubernetes. There MEHO reaches only its
  own parts, such as its database and its password safe, and DNS;
- the ports of this machine itself, except the console on port 443.

To limit where MEHO may connect, name narrow target networks, and use a
firewall in your network.

Before v0.47.0, MEHO on this form can connect only to its own parts, so
it cannot reach the systems it manages. Update to v0.47.0 or later.

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
- `/var/lib/meho/console-ca.crt` holds only the CA certificate of the
  console. It is not secret.
- The support bundle can still hold secrets from the logs. Check it
  before you send it (see [Get help from support](#get-help-from-support)).

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

It also removes the route that the pod network of RKE2 leaves on the
machine. So you can run `install.sh` again right away, without a restart.

The MEHO firewall stays on, so the machine is never without a firewall.
The last lines of the command name the command that removes it. Turn on
your own firewall first, then remove the MEHO firewall.
