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

### What works today

- Each [GitHub release](https://github.com/evoila/meho/releases) has
  the download `meho-linux-<version>-amd64.tar`, with the checksum file
  `SHA256SUMS-linux` and its signature `SHA256SUMS-linux.cosign.bundle`.
- The download holds everything the install needs: the install script,
  RKE2, the MEHO chart, every container image, `helm` and the `meho`
  command. The server needs no internet.
- One script, `install.sh`, checks the machine, sets the host firewall,
  installs RKE2 and installs MEHO with its database, login server and
  password safe. At the end it prints the address of the console and
  the two files with the first-login passwords and the unseal key.
- To update, run `install.sh` from the download of the newer version on
  the same machine. The update keeps your answers, the data, the
  passwords and the keys.
- `sudo meho doctor` checks the server: the node, the pods, the disk
  and the certificates (see [Troubleshooting](../clients/troubleshooting.md)).

### What is still in testing

- Tests on more Linux versions. Ubuntu 24.04 on x86-64 is tested.
  Another Linux with systemd gets a "not tested" warning, and the
  install goes on.
- A full guide in this documentation.
- Joining more servers into one cluster.

### How to try it

You need a server with:

- x86-64 and systemd (Ubuntu 24.04 is tested),
- at least 4 processors, 8 GB of memory, and 80 GB free under
  `/var/lib`,
- a full DNS name that your DNS server resolves to the server.

Then, on the server:

1. Download the three files of the release from the
   [releases page](https://github.com/evoila/meho/releases).
2. Check the signature of the checksum file first, then the download.
   You need [cosign](https://docs.sigstore.dev/cosign/system_config/installation/):

   ```bash
   TAG=v<version>
   cosign verify-blob \
     --certificate-identity "https://github.com/evoila/meho/.github/workflows/linux-download.yml@refs/tags/$TAG" \
     --certificate-oidc-issuer https://token.actions.githubusercontent.com \
     --bundle SHA256SUMS-linux.cosign.bundle SHA256SUMS-linux
   sha256sum -c SHA256SUMS-linux
   ```

   Both commands must succeed. `sha256sum` prints `OK` for the download.
3. Unpack the download and run the install script as root:

   ```bash
   tar -xf meho-linux-<version>-amd64.tar
   cd meho-linux-<version>-amd64
   sudo ./install.sh --hostname <meho.example.com> --admin-network <admin-range>
   ```

   `--admin-network` is the address range that may use SSH, for
   example `192.0.2.0/24`. From every other address, only ports 80 and
   443 answer. `sudo ./install.sh --help` lists every option.

Each check prints `[ok]` or `[WARN]`. When something is wrong, the
script stops with one `[FAIL]` line that says what to fix.

## Backup and the settings

Take a VM snapshot or a backup before each update of a Linux server.
The install script asks for it.

A tenant's policies, conventions and targets can be exported as one
file and imported again with a preview. The file holds no secret. The
file alone is not a full backup: a full restore also needs the backup
of the database, of the password safe, and of the Kubernetes Secrets
that the install made.
