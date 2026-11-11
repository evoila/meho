# Tools for your laptop

This page takes you from an empty laptop to Claude Code or Claude Desktop
connected to MEHO. It works on macOS, Windows and Linux. Follow the steps
in order. When you are done, you have asked MEHO one read-only question
and seen the answer.

Only the tools run on your laptop. The MEHO server runs somewhere else.

## What you need first

Ask the person who runs your MEHO (your admin) for these:

- **Network access.** MEHO is for your internal network. Connect to the
  VPN if your company uses one.
- **The server address.** In this page it is `https://meho.example.com`.
  Use your own address instead.
- **Your login.** A user name and password for MEHO.
- **Claude Code or Claude Desktop.** Install the one you want to use.
  Claude Desktop also needs [Node.js](https://nodejs.org/), and so does
  the Claude Code plugin.

## Step 1: trust the site's own certificate authority

A certificate authority (CA) signs the certificates that prove a server is
the right one. Many companies run their own CA. Your laptop does not know
it yet, so logins fail until you add it.

Check if you need this step. Use your own address.

- **macOS and Linux:**

    ```bash
    curl -sS -o /dev/null https://meho.example.com/healthz && echo trusted
    ```

- **Windows** (PowerShell; type `curl.exe`, not `curl`):

    ```powershell
    curl.exe -sS -o NUL https://meho.example.com/healthz; if ($LASTEXITCODE -eq 0) { "trusted" }
    ```

If it fails with `SSL certificate problem`, your site has its own CA.

If it prints `trusted`, your system already trusts the server. Ask your
admin if your site uses its own CA anyway. Company laptops often trust
it already. Then get `internal-ca.pem` (items 1 and 2 below) and skip
item 3: the Claude tools still need the file in step 4. If your site
uses a public CA, go to [step 2](#step-2-install-the-meho-command-line).

If your site has its own CA:

1. Ask your admin where the root CA file is. Save it as `internal-ca.pem`.
2. Check the file before you trust it. Compare its fingerprint with one
   your admin gave you in a place you already trust:

    ```bash
    openssl x509 -in internal-ca.pem -noout -subject -fingerprint -sha256
    ```

    On Windows, run this in Git Bash. If the fingerprints differ, stop and
    ask your admin.

3. Add the file to your system. Then open a new terminal.

    - **macOS:** `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain internal-ca.pem`
    - **Windows** (a terminal started as administrator): `certutil -addstore -f Root internal-ca.pem`
    - **Linux** (Debian, Ubuntu): `sudo cp internal-ca.pem /usr/local/share/ca-certificates/internal-ca.crt && sudo update-ca-certificates`

4. Run the check again. It must print `trusted`.

Keep `internal-ca.pem`. Step 4 needs it whenever your site has its own
CA, also when your system already trusts that CA. The long version, with the
reasons and more systems, is on
[TLS and ingress](tls-ingress.md#your-workstation-os-trust-store) and in
[Claude Code](../clients/claude-code.md#before-you-start-trust-your-deployments-ca).

## Step 2: install the meho command line

Use the same release as your server. Ask your admin for the version, or
read it from your MEHO. Set `TAG` to that version, for example `v0.27.0`.
Each file is on the
[releases page](https://github.com/evoila/meho/releases).

### macOS

Use `darwin_arm64` on an Apple silicon Mac, or `darwin_amd64` on an Intel
Mac.

```bash
TAG=v0.27.0
TARBALL=meho_${TAG#v}_darwin_arm64.tar.gz
BASE=https://github.com/evoila/meho/releases/download/${TAG}

curl -LO ${BASE}/${TARBALL}
curl -LO ${BASE}/SHA256SUMS
grep " ${TARBALL}\$" SHA256SUMS | shasum -a 256 -c -
tar xzf ${TARBALL}
sudo install -m 0755 meho /usr/local/bin/meho
meho version
```

The check line must print `<file>: OK`.

### Windows

Use PowerShell. Use `windows_amd64` on Intel and AMD, or `windows_arm64` on
ARM.

```powershell
$Tag = "v0.27.0"
$Zip = "meho_$($Tag.TrimStart('v'))_windows_amd64.zip"
$Base = "https://github.com/evoila/meho/releases/download/$Tag"

Invoke-WebRequest "$Base/$Zip" -OutFile $Zip
Invoke-WebRequest "$Base/SHA256SUMS" -OutFile SHA256SUMS

# These two lines must show the same value.
(Get-FileHash -Algorithm SHA256 $Zip).Hash.ToLower()
(Select-String -Path SHA256SUMS -Pattern ([regex]::Escape($Zip))).Line.Split(' ')[0]

$Dir = "$env:LOCALAPPDATA\Programs\meho"
Expand-Archive -Path $Zip -DestinationPath $Dir -Force
$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (($UserPath -split ';') -notcontains $Dir) {
  [Environment]::SetEnvironmentVariable("Path", "$UserPath;$Dir", "User")
}
```

Open a new terminal, then run `meho version`.

### Linux

Use `linux_amd64` or `linux_arm64`.

```bash
TAG=v0.27.0
TARBALL=meho_${TAG#v}_linux_amd64.tar.gz
BASE=https://github.com/evoila/meho/releases/download/${TAG}

curl -LO ${BASE}/${TARBALL}
curl -LO ${BASE}/SHA256SUMS
sha256sum -c SHA256SUMS --ignore-missing
tar xzf ${TARBALL}
sudo install -m 0755 meho /usr/local/bin/meho
meho version
```

The check line must print `<file>: OK`.

Every file is also signed with cosign. To check the signature too, see
[Install the signed binary](../clients/cli.md#install-the-signed-binary).

## Step 3: log in

```bash
meho login https://meho.example.com
```

The command prints a web address and a code. Open the address in your
browser, enter the code and log in. Then check the connection:

```bash
meho status
```

A clean answer means the command line, your login, the network and the
server all work together. If the command prints a warning about the
version, install the release it names.

`meho login` saves the address you logged in to. A later `meho login` to
another address replaces it. Run `meho status` after each login to see
which server you talk to.

If your login has several tenants (separate work areas), pick yours with
`meho tenants use <name>`. Without a name, the command lists your tenants.

Something fails? Run `meho doctor`. It checks your laptop and names the
step that fails. See also [Troubleshooting](../clients/troubleshooting.md).

## Step 4: connect Claude Code or Claude Desktop

Pick one.

### Claude Code (the MEHO plugin)

1. Add the MEHO plugin list. Run this in your terminal:

    ```bash
    claude plugin marketplace add evoila/meho
    ```

    Then start Claude Code and run this command inside Claude Code, not
    in the terminal:

    ```text
    /plugin install meho@meho
    ```

2. Tell the plugin your server address. Create the file
   `~/.config/meho/plugin.env` with this line:

    ```bash
    MEHO_MCP_URL="https://meho.example.com/mcp"
    ```

    On Windows, Claude Code runs the plugin in Git Bash. The `~` folder is
    your user folder, for example `C:\Users\<you>\.config\meho\plugin.env`.

3. If your site has its own CA, add the path of `internal-ca.pem` to the
   same file. Do this also when your system already trusts the CA. The
   plugin runs on Node, and Node does not read your system trust store:

    ```bash
    MEHO_CA_CERT="$HOME/internal-ca.pem"
    ```

4. If your login has several tenants, name the one to work in. Add this
   line to the same file, with your tenant name or ID:

    ```bash
    MEHO_TENANT="tenant-b"
    ```

    Claude does not read the tenant you chose with `meho tenants use` in
    step 3. Without this line, MEHO lets Claude call only `meho_status`,
    and step 5 cannot list the connectors. If your login has one tenant,
    skip this.

5. Start Claude Code again. In Claude Code, run `/mcp`, pick `meho` and
   log in in the browser.

More options (another tenant, how long the login lasts, a manual setup)
are on [Claude Code](../clients/claude-code.md#work-in-one-tenant).

### Claude Desktop (the `.mcpb` bundle)

1. On the [releases page](https://github.com/evoila/meho/releases), download
   `meho-claude-desktop-<version>.mcpb` from the same release as your
   command line.
2. Open the file. Claude Desktop shows an install dialog.
3. Fill in the dialog:
    - **MEHO MCP endpoint:** `https://meho.example.com/mcp`
    - **Internal CA bundle:** pick `internal-ca.pem` if your site has its
      own CA, also when your system already trusts it. Otherwise leave it
      empty.
    - **Tenant:** if your login has several tenants, enter the tenant name
      or ID. Claude does not read the tenant you chose with
      `meho tenants use`. Without it, MEHO allows only `meho_status`, and
      step 5 cannot list the connectors. If your login has one tenant,
      leave it empty.
4. Turn the extension on. Log in in the browser when it asks.

To check the file before you install it, see
[Claude Desktop](../clients/claude-desktop.md#the-one-command-path-the-mcpb-bundle).
The Windows setup of Claude Desktop is not field-tested yet. If it fails,
see [Troubleshooting](../clients/troubleshooting.md).

## Step 5: ask one read-only question

Ask Claude:

> Use MEHO to check its status and list the connectors.

Claude calls the MEHO tools `meho_status` and `meho_connector_list`. You
see MEHO's answer: it is healthy, and it lists the connectors it has.
Both tools only read. They change nothing.

You can ask the same on the command line:

```bash
meho connector list
```

If Claude says it has no MEHO tools, check that the plugin or the
extension is on, and run step 3 again. If Claude sees only `meho_status`,
set the tenant as shown in step 4.

## Step 6: what MEHO can do, and how approvals work

MEHO lets you and your agents work with many systems through a few
tools. The agent finds a connector, searches its operations, previews one
and calls it. MEHO checks the rules, hides secrets, and writes an audit
record for every call.

A change that needs approval does not run at once. MEHO waits until a
second person approves it. AI tools and agents never approve. A person does that in the web
interface or on the command line.

Read next:

- [Run your first operations](../guides/first-operations.md): the same
  steps, one by one, with a real connector.
- [Approvals and break-glass](../guides/approvals-and-break-glass.md):
  who may approve, and what to do in an emergency.

Then: [back up, restore and update](backup-restore-update.md).
