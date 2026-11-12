# Ways to install MEHO

Every way installs MEHO with its own PostgreSQL (the database),
Keycloak (the login server) and OpenBao (the password safe). You do not
need to bring any of them. Bringing your own is an expert path,
described separately (for the Helm chart:
[Bring your own parts](index.md#part-b-bring-your-own-parts-expert-path)).
After the install, the steps are the same.

| Way | For whom | State |
|---|---|---|
| **Helm chart on your Kubernetes** | Teams that run their own Kubernetes or OpenShift | **Available.** Follow the [standard install](index.md#part-a-standard-install). |
| **A plain Linux server** | Hyper-V, cloud VMs, physical servers, sites with no internet | **Available, in testing.** One download with everything inside and one install script. It sets up a small built-in Kubernetes (RKE2) that you never manage yourself. See [The Linux server install](#the-linux-server-install) below. |
| **A VMware appliance (OVA)** | VMware and VCF teams | **In progress, not released.** You import one OVA into vCenter, fill in name, address, DNS and time server, and start it. Everything is inside. No internet needed. |

**Which one is right for you?** If you run Kubernetes, use the Helm
chart. If you do not, try the Linux server install, and talk to evoila
before you use it in production. For the appliance, talk to evoila.

## The Linux server install

The Linux server install is available and still in testing. Use it
for tests and pilots. Talk to evoila before you run it in production.

- Each [GitHub release](https://github.com/evoila/meho/releases) has
  one download for it. The download holds everything the install needs:
  the install script, RKE2, the MEHO chart, every container image,
  `helm` and the `meho` command. The server needs no internet.
- One script, `install.sh`, checks the machine, sets the host firewall,
  installs RKE2 and installs MEHO with its database, login server and
  password safe.
- To update, run `install.sh` from the download of the newer version on
  the same machine.
- Ubuntu 24.04 on x86-64 is tested. Another Linux with systemd gets a
  "not tested" warning, and the install goes on. Joining more servers
  into one cluster is not tested yet.

The steps are in the chapter
[Form 2: the Linux server](form-2-linux-server.md): what the machine
needs, how to check the download, the install, the first login, updates
and the daily work.

## Backup and the settings

Take a VM snapshot or a backup before each update of a Linux server.
The install script asks for it.

A tenant's policies, conventions and targets can be exported as one
file and imported again with a preview. The file holds no secret. The
file alone is not a full backup: a full restore also needs the backup
of the database, of the password safe, and of the Kubernetes Secrets
that the install made.
