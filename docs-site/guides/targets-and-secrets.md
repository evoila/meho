# Register targets and secrets

A **[target](../reference/words.md#target)** is one system MEHO operates against — a Kubernetes API
server, a vCenter, a Vault, a firewall. Registration gives the
backplane three things: the coordinates to reach the system (`host`,
`port`, TLS trust), the `product` token that selects which [connector](../reference/words.md#connector)
serves it, and a `secret_ref` — a *reference* into your credential
store. MEHO never stores the credential itself; it resolves the
reference at dispatch time, under an identity your store can audit.

Until at least one target is registered and its probe is green, an
agent session can discover [operations](../reference/words.md#operation) but cannot act on anything —
which is why this is the first guide.

!!! note "Prerequisites and roles"

    - A running backplane ([Install & operate](../install/index.md))
      and an authenticated CLI session
      ([Connect clients](../clients/index.md)):
      `meho login https://meho.example.com`.
    - Registration verbs (`import`, create, edit, delete) require the
      **tenant_admin** role. Read verbs (`list`, `describe`, `probe`,
      `discover`) need **operator**. If a step below answers
      `403 insufficient_role`, that is your JWT's role, not a broken
      deploy.

## Choose a registration path

| Path | What it is | When to use it |
|---|---|---|
| **CLI — `meho targets import`** | Bulk-import from a `targets.yaml` file you keep in version control. | The canonical path. Your target registry becomes a reviewable, re-runnable file. |
| **Operator console — "Targets" in the menu (`/ui/connectors`)** | Create/edit forms plus a paste-or-upload `targets.yaml` import with a preview table. | Console-first operators. Produces byte-identical writes to the CLI import. |
| **REST — `POST /api/v1/targets`** | The API the other two paths call. | Scripts that a person runs with their own login. |

Create, edit and delete on REST are for people only. A service login
gets `403 human_principal_required`, also with the **tenant_admin**
role. It registers a target with the `targets.register` operation,
changes one with `targets.update` and removes one with `targets.remove`
(connector `targets-registry-1.x`). Each new target and each change waits
for a person's [approval](../reference/words.md#approval): no standing
grant, run approval or agent grant covers it. The approval card shows the
target's values, also its `extras`.

**The CLI and REST wait for approval.** A change made with your login
token (`meho targets add`, `update`, `delete`, `import`, or a script on
REST) waits until a person approves it. The command prints the approval
id and exits 0; approving the request makes the change. Find it with
`meho approvals list`, or in the console under approvals. A change you
make in the operator console runs at once.

**Register targets on the operator surfaces above.** Registration is an
operator-trust decision. For agents, the default [MCP](../reference/words.md#mcp) tools only read
targets (`list_targets`). A session that asked for the `mcp:admin`
scope also has `meho_targets_register` and `meho_targets_update`
(tenant_admin only). Through `call_operation`, the connector
`targets-registry-1.x` offers `targets.register`, `targets.update`,
`targets.remove` and `targets.probe`; every register and every update
waits for a person's approval, and `targets.remove` always waits for one.

MEHO has no MCP tool to stage a secret, on purpose: a password must
never pass through an agent or a model. A person stores it with the
CLI or the web console (see
[Store the credential through MEHO](#store-the-credential-through-meho)).
This is also why the CLI is worth installing before your first agent
session.

## Write a `targets.yaml`

The file is a `targets:` list. A minimal, realistic pair — a
Kubernetes cluster and a VMware appliance that is reached by IP:

```yaml
targets:
  - name: lab-rke2
    product: k8s                    # registered product token — NOT "kubernetes"
    host: rke2-api.lab.example.com
    port: 6443
    auth_model: shared_service_account
    notes: RKE2 lab cluster, evaluation estate.

  - name: lab-vcenter
    product: vmware
    host: 192.0.2.15                # reached by IP...
    tls_server_name: vc01.lab.example.com   # ...but the cert's SAN is the FQDN
    tls_ca_pin: |
      -----BEGIN CERTIFICATE-----
      ...internal CA, PEM...
      -----END CERTIFICATE-----
    notes: vCenter reached by IP; SNI + cert verification pinned to the SAN name.
```

The `vmware` entry needs a VMware connector package. Without one, MEHO
refuses `product: vmware` with 422 `unknown_product`.

Mapping rules the importer applies:

- **Recognised top-level keys** map 1:1 to real columns: `name`,
  `aliases`, `product`, `host`, `port`, `fqdn`, `secret_ref`,
  `auth_model`, `vpn_required`, `notes`, `preferred_impl_id`,
  `verify_tls`, `tls_ca_pin`, `tls_server_name`, `secret_field_map`,
  `version` (a quoted string, for example `version: "9.0"`),
  `flight_recorder_capture`, `extras`.
- **`name`, `product`, and `host` are required** and validated
  locally before any HTTP request.
- **Unknown keys are not errors** — they spill into the target's
  `extras` JSON column, and the importer prints a warning for each one.
  To carry your own annotations without the warning, put them under an
  `extras:` block.
- **[`fingerprint`](../reference/words.md#detected-product-and-version) is server-managed** and dropped with a warning; the
  probe verb is its only writer.
- `verify_tls: false` and `tls_ca_pin` are mutually exclusive (422):
  pin the CA *or* disable verification, never both.
- Set **`tls_server_name`** whenever you reach an appliance by IP (or
  by an alias) while its certificate only carries the FQDN — without
  it, dispatch fails with `connector_tls_verify_failed`, carrying
  Python's own *"IP address mismatch"* text inside
  `extras.exception_message`.
- If a target has a `tls_ca_pin` and TLS fails, the error says the pin
  is stale. It does not tell you to set a pin. `extras.pin_stale` is
  `true`, and `extras.presented_certificate` shows the issuer, the
  subject and the SHA-256 fingerprint of the certificate the host
  presents now. This happens when the host certificate was re-issued.
  For an ESXi host or a vCenter whose certificates come from the VMCA
  (the vCenter's own certificate authority), pin the **VMCA root CA**,
  not the host certificate. Then a later re-issue does not break the
  target. Update the pin with `--update`.

You can leave `secret_ref` out entirely — see the next section.

### Add a whole VMware Cloud Foundation stack

VMware Cloud Foundation (VCF) is not one system. It is many systems
that run side by side. Each part has its own address and its own login,
so **each part is its own target**. Add one target for each part you
want MEHO to operate.

The `product` value of a target is the first part of the connector id
(the name before the first dash that is followed by a digit). For
example `vmware-rest-9.0` gives `vmware`, and `sddc-rest-9.0` gives
`sddc`. This table lists the value for each VCF part. The connector ids
are in the [connector reference](../reference/connectors.md#evoilas-enterprise-pack).

| VCF part | `product` value | Connector ids |
| --- | --- | --- |
| vCenter | `vmware` | `vmware-rest-9.0`, `vmware-rest-8.0` |
| NSX Manager | `nsx` | `nsx-rest-9.0`, `nsx-rest-4.2` |
| SDDC Manager | `sddc` | `sddc-rest-9.0`, `sddc-vcf5-5.0` |
| VCF Installer and Cloud Builder | `installer` | `installer-rest-9.1`, `installer-cb-5.0` |
| VCF fleet management | `fleet` | `fleet-lcm-9.0`, `fleet-rest-9.0` |
| VCF Operations | `vrops` | `vrops-rest-9.0`, `vrops-vrops8-8.0` |
| VCF Operations for logs | `vrli` | `vrli-rest-9.0`, `vrli-vrli8-8.0` |
| VCF Operations orchestrator | `vro` | `vro-rest-9.0` |
| VCF Automation | `vcfa` | `vcfa-rest-9.0`, `vcfa-vra8-8.0` |
| VCF Identity Broker | `idb` | `idb-rest-9.0`, `idb-wsa-3.3` |

The connectors for VCF come with evoila's enterprise pack. Without the
pack, MEHO refuses these `product` values with 422 `unknown_product`.
That answer lists the `valid_products` of your backplane. The
same list is in `meho connector list`. Use the value from that list if
it ever differs from this table.

A stack with the main parts looks like this. The names and the
addresses (`192.0.2.0/24`) are examples. Where a target has no
`secret_ref`, the server derives one secret path for each target (see
[Stage the secret](#stage-the-secret)).

```yaml
targets:
  - name: vcenter-lab
    product: vmware
    host: 192.0.2.21
    tls_server_name: vcenter-lab.example.com

  - name: nsx-lab
    product: nsx
    host: 192.0.2.22
    tls_server_name: nsx-lab.example.com

  - name: sddc-lab
    product: sddc
    host: 192.0.2.23
    tls_server_name: sddc-lab.example.com

  - name: vcfops-lab
    product: vrops
    host: 192.0.2.24
    tls_server_name: vcfops-lab.example.com

  - name: vcfa-lab
    product: vcfa
    host: 192.0.2.25
    fqdn: vcfa-lab.example.com      # the appliance picks the organisation from this name
    tls_server_name: vcfa-lab.example.com
    extras:
      domain: example-org           # the tenant organisation to log in to
      provider_username: provider-svc
      provider_secret_ref: tenants/<tenant-id>/vcfa-lab-provider
```

Add the other parts (`fleet`, `vrli`, `vro`, `idb`, `installer`) the same
way: a `name`, the `product` value from the table, and a `host`. Add
`tls_ca_pin` or `tls_server_name` as for any target. Then
[import and probe](#import-and-verify) each one. A probe that is green
for a part shows that MEHO reaches it and identified its product and version.

#### What VCF Automation reads

VCF Automation has two login planes on one appliance: the provider plane
and the tenant plane. The connector reads these values.

| Where | Name | What it does |
| --- | --- | --- |
| Target `fqdn` | `fqdn` | The appliance picks the organisation from the `Host` name. When you reach it by IP, set `fqdn` to its FQDN. |
| Target `extras` | `domain` | The tenant organisation to log in to. It is a target setting, **not** a field in the secret. |
| Target `extras` | `provider_username` | Optional. The user name for the provider plane, when the provider plane needs another login than the tenant plane. |
| Target `extras` | `provider_secret_ref` | Optional. The `secret_ref` of a second secret that holds the login of the provider user. It follows the same rules as `secret_ref` for your credential backend. |
| Secret | `username`, `password` | The tenant login. |
| Secret | `refresh_token` | Optional. A VCF Automation API token. For a VCF Automation 9.1 target whose appliance does not serve the usual login, add it to the secret of the target. |

Set these with `meho targets update`, for example
`meho targets update vcfa-lab --extra domain=example-org`. `--extra` on
`update` replaces the whole `extras` map, so pass every key you want to
keep.

The exact rules for each product, such as which login the provider plane
uses when you set no `provider_username`, are in the setup guide that
comes with the enterprise pack.

### The record schema

One entry of the `targets:` list has a published JSON Schema:
[`docs/schemas/target-record-v1.schema.json`](https://github.com/evoila/meho/blob/main/docs/schemas/target-record-v1.schema.json).
The toolkit repos and MEHO use the same description, so a file that
passes the schema imports without edits. The fields are the keys listed
above, plus `fingerprint`: the detected product and version (`vendor`,
`product`, `version`, `build`, `edition`, `reachable`, `probed_at`,
`probe_method` and `extras`). A file may carry `fingerprint`. The import
skips it with a warning, because only a probe writes it.

The schema allows keys it does not list. The import keeps them in
`extras` and warns. The CLI import and the console import map the same
keys.

The schema has the same limits as the API. A name or alias has no
spaces, line breaks or `/`. `secret_field_map` holds at most 50
entries. A CA pin (`tls_ca_pin`) does not go together with
`verify_tls: false`. `version` is text, for example `"8.0"`, or `null`.

### Use a secret with different field names

A connector reads fixed field names from the secret. For example, SSH
reads `ssh_private_key` and `username`. If your secret already holds the
right values under other names, you do not need a copy. Tell MEHO which
field to read instead with `secret_field_map`:

```yaml
targets:
  - name: web-01
    product: linux
    host: web-01.example.com
    secret_field_map:
      ssh_private_key: private_key   # the connector field: the field in your secret
      username: login
```

On the CLI, use `--secret-field ssh_private_key=private_key` with
`meho targets add`, `meho targets update` or `meho targets import`.
`meho targets probe` then checks that every field the map points to
exists in the secret. It names any missing field, never a value. For SSH,
a field named `private_key` also works with no map at all.

## Stage the secret

Every target that needs credentials carries a `secret_ref`. Which
store it points at is decided by your deploy's credential backend
(`vault`, the default, or `gsm` — see the
[install trail](../install/index.md)).

### Vault (default backend)

The `secret_ref` is the **logical KV-v2 path relative to the `secret`
mount** — never include the mount or the `/data/` segment. The
canonical, enforced layout is per-[tenant](../reference/words.md#tenant):

```
tenants/<tenant-id>/<target-name>
```

If you omit `secret_ref` at registration, **the server derives exactly
that path for you**. The reliable recipe is therefore: register first,
read the derived reference back, then store the secret at it through MEHO.

#### Store the credential through MEHO

MEHO stores the credential for you. The write uses the operation
`vault.kv.put`. It has the risk level `caution`, and **it always waits for
a person's approval.** No grant and no run approval covers it. While it
waits, MEHO keeps the value encrypted. The audit row keeps only a hash of
it, never the value. This recipe is for installs that use Vault or the
built-in OpenBao. It is not for Google Secret Manager (see below).

1. Read the derived reference:

    ```bash
    meho targets describe lab-vcenter
    #   ...
    #   secret_ref:        tenants/4f6f6f6f-.../lab-vcenter
    ```

2. Make a new, empty file that only you can read:

    ```bash
    CREDS="$(mktemp)"
    ```

    `mktemp` always makes a new file with a new name, readable only by
    you. It never reuses an old file. Then open the file `$CREDS` in an
    editor and write the credential. The password stays out of your shell
    history:

    ```json
    {"username": "svc-meho", "password": "<password>"}
    ```

    Save the file.

3. Send it. Use the mount `secret` and the `secret_ref` from step 1:

    ```bash
    meho vault kv put secret tenants/4f6f6f6f-.../lab-vcenter --data @"$CREDS"
    # → status: awaiting_approval
    #   approval id: 4b1c… — inspect with: meho approvals show 4b1c…
    ```

    You need no `--target` for this. MEHO logs in to Vault with your own
    token. The `--data` flag reads a file with `@<file>`. It does not read
    standard input, and you should never put the password in the command
    itself. If MEHO refuses the path, check that the mount is `secret` and
    that the path is the `secret_ref` from step 1.

4. A person approves the request. When they approve, MEHO runs the
   write with **the approver's** Vault rights, not yours. MEHO also
   checks your own write rights before the request waits. So both of
   you need the right to write the exact path from step 1.

    - **On the built-in OpenBao (the default install):** a tenant
      operator (`MEHO-Operators`) may write only under
      `tenants/<tenant-id>/rotation/`. They cannot write the target's
      path. So use a tenant admin (`MEHO-Admins`) for both steps: an
      admin sends the request, and an admin approves it. If you are the
      only admin, you may approve your own request with a click in the
      web console, or on the command line after a fresh sign-in. See
      [Approve on the command line](approvals-and-break-glass.md#approve-on-the-command-line).
    - **On your own Vault:** the Vault policies of both people must allow
      a write on the exact path. MEHO's own rules on who may approve
      still hold: see
      [Who may approve](approvals-and-break-glass.md#who-may-approve).

    On the command line, use the approval id from step 3:

    ```bash
    meho approvals approve 4b1c…
    ```

    Approving runs the write at once. No second step follows.

5. Delete the file after the write is done:

    ```bash
    rm -f "$CREDS"
    ```

    If the request was rejected, nothing was stored. Delete the file
    anyway. Fix the cause the person gave, make the file again (step 2)
    and send it again (step 3).

**The same in the web console.** Open the page **Vault** (`/ui/vault`),
press **Write secret**, and fill in the mount `secret`, the path from
step 1, and the JSON body. The form shows that the write waits for
approval. Approve it on the page **Approvals**.

!!! warning "An agent must not send a password"
    Never send a password through `call_operation` or any other agent
    tool. The value would pass through the model. Store the credential
    yourself, as above.

The two path notations trip everyone once, so:

| Context | Path shape | Example |
|---|---|---|
| MEHO `secret_ref` | logical, no mount | `tenants/<tenant-id>/lab-vcenter` |
| Vault CLI / API | mount-prefixed | `secret/tenants/<tenant-id>/lab-vcenter` |

Rules the backplane enforces:

- An explicitly supplied Vault `secret_ref` must stay **inside your
  tenant subtree** (`tenants/<tenant-id>/…`). Anything else is
  rejected at registration with a structured 422
  (`secret_ref_outside_tenant_scope`) that names the expected path —
  because your operator Vault identity could never read it at
  dispatch anyway.
- A mount- or API-shaped ref (`secret/data/…`, `kv/data/…`,
  `data/…`) is rejected at read time: Vault inserts the `/data/`
  segment itself, so a prefixed value double-resolves to a 404.
- Most connectors expect the fields **`username`** and
  **`password`**. Some want a different shape — the Kubernetes
  connector reads a single field named **`kubeconfig`** holding the
  kubeconfig YAML. The per-connector field contract is listed in each
  connector's onboarding notes
  ([`docs/cross-repo/`](https://github.com/evoila/meho/tree/main/docs/cross-repo)).

The `--data` file is a JSON object, so the kubeconfig text goes into
one JSON string. This one-liner builds the file from a kubeconfig
without printing it:

```bash
# Kubernetes example — one field, whole kubeconfig as the value:
# mktemp makes a new file that only you can read.
CREDS="$(mktemp)"
python3 -c 'import json,sys; print(json.dumps({"kubeconfig": open(sys.argv[1]).read()}))' \
  rke2-kubeconfig.yaml > "$CREDS"
meho vault kv put secret tenants/<tenant-id>/lab-rke2 --data @"$CREDS"
# After the request is approved and the write is done:
rm -f "$CREDS"
```

### Google Secret Manager (`credentialBackend: gsm`)

The ref grammar is:

```
gsm:<project-id>/<secret-name>[/versions/<version>][#<field>]
```

- `gsm:my-project/lab-vcenter-creds` — latest version, whole payload.
- `gsm:my-project/lab-vcenter-creds/versions/3` — pinned version.
- `gsm:my-project/lab-vcenter-creds#password` — one field only.

The secret's payload **must be a JSON object of named fields**, e.g.
`{"username": "svc-meho", "password": "..."}` — the same field names
the connector expects on Vault. A kubeconfig ref looks like
`gsm:my-project/lab-rke2-kubeconfig#kubeconfig`.

On a GSM deploy, set `secret_ref` explicitly in the YAML (the
server-derived default is the Vault layout).

MEHO reads a `gsm:` secret only from a Google Cloud project listed in
the setting `GSM_PARAM_ALLOWED_PROJECTS` (comma-separated project ids,
set through the chart's `extraEnv`). The list is empty by default, so
list your projects before you register targets. A target whose
`secret_ref` names a project that is not listed is refused at
registration (422 `secret_ref_project_not_allowed`), and a stored
target in such a project fails every dispatch with
`connector_error: GcpSecretManagerReadError` until the project is
listed. The same list applies to `gsm:` refs in operation parameters.

### Share one secret between targets, or not

MEHO lets two targets name the same `secret_ref`. Use one secret for
several targets only when one account is right for all of them. This is
what it means:

- **Rights.** Every call on every one of these targets runs with the
  rights of that one account. A read-only target and a target that
  changes things need two accounts, so give them two secrets.
- **Approvals.** The approval rules depend on the operation, not on the
  secret. A shared secret does not weaken them, and it does not make a
  change wait less. It does mean that an approved change on one target
  runs with an account that the other targets also use.
- **Audit.** MEHO writes one [audit row](../reference/words.md#audit-record) for each call. The row names the
  target and the person (or agent) who made the call, also when targets
  share a secret. The vendor sees only the one account, so the vendor's
  own log cannot tell the targets apart. Use one secret for each target
  when you need that in the vendor's log.
- **Logins.** Some vendors allow one session for each user. For many
  connectors, two targets of one tenant that use the same secret and the
  same login address share one login and one token, so they do not end
  each other's session. Targets with different secrets log in on their
  own.
- **Rotation.** When you change the shared secret, change it once. Then
  send `meho targets update <name> --secret-ref <same-ref>` for each
  target, to make MEHO read it again (see
  [Change a secret](#change-a-secret)).

For a VCF stack, a simple rule works: one secret for each target, named
after the target. Share a secret only for targets of the same product
that need the same rights, for example several vCenter targets that
use one read-only account.

### Targets that need no credential

Some products need no login, for example a Prometheus that you reach
through a port-forward. If you leave `secret_ref` out, the server still
derives the default path, and every call then fails on the Vault read.
To store the target with no credential, send an explicit null:

```bash
# Create it with no credential, over plain HTTP:
meho targets add lab-prom --product prometheus --host prom.example.com \
  --port 9090 --no-secret-ref --extra scheme=http

# Or fix a target that already exists:
meho targets update lab-prom --clear secret-ref --extra scheme=http
```

`--extra` on `update` replaces the whole `extras` map, so pass every key
you want to keep. `--clear extra` removes every extra, and `--clear alias`
removes every alias. In YAML, write `secret_ref: null`.

### Keycloak targets need a managed realm

A Keycloak target must name the realm MEHO manages, in
`extras.managed_realm`. MEHO has no built-in realm. Without it, MEHO
refuses to create or update the target, and refuses every operation on
it. The target works on that realm only: a call that names another realm
is refused.

```bash
meho targets add lab-sso --product keycloak --host sso.example.com \
  --extra managed_realm=example
```

In YAML, put it under `extras:` (`managed_realm: example`). The optional
`extras.admin_realm` is the realm the target logs in to; it is `master`
when you leave it out.

- A realm is one plain name: no `/`, `\`, `?`, `#` or `%`, no spaces at
  the start or the end, not only dots, and at most 255 characters. MEHO
  does not trim a value; it refuses it.
- MEHO allows a managed realm that is the admin realm, or the realm MEHO
  itself signs in with, but it warns you. When the warning shows depends
  on how you make the change:
  - You make it in the console, or with `meho targets add` or `update`
    after a person's login: the warning shows at once, after the save.
  - A request made with a token waits for a person to approve it (an AI
    agent's call, or a call with an API token). The warning is on the
    approval card, so the approver sees it before approving. It is in the
    result again after the approval.
  - A settings file import writes at once. Its preview and its result
    both list the warning, with the path of the target entry.
  - Each later write on that target also shows the warning on its
    approval card.
- `meho doctor` lists the Keycloak targets MEHO refuses. A platform
  admin sees the targets of every tenant. You can fix one with
  `meho targets update`, or in the console edit form ("Managed realm").

## Find targets whose secret is outside the tenant's folder

MEHO keeps each tenant's secrets under the tenant's own folder in the
secret store (Vault or OpenBao). When a target's `secret_ref` points
outside that folder, MEHO writes a warning to the server log. It writes the
warning at most once an hour for each target in each server process. After
a restart, or with more than one server process, the warning can come more
often. The warning never stops the read: the call still works. Move the
secret into the tenant's folder.

A target is listed, and a read of its secret warns, when its secret path
lies outside the tenant's folder, whoever built the path. A target of a
tenant with its own store role is listed too. A read of its secret is
refused, with no warning.

To list these targets, run this as an admin:

```bash
meho targets outside-folder
```

A tenant admin sees their own tenant. A platform admin sees every tenant,
or one tenant with `--tenant <tenant-id>`. To list one tenant you are a
member of, choose it first, so your own role there applies, and then name
its ID:

```bash
meho tenants use <name>
meho targets outside-folder --tenant <tenant-id>
```

Without `--tenant`, a platform admin still sees every tenant. The list of
every tenant leaves out each other tenant where you are a member without
the admin role. Each row has the target name, the
tenant, the `secret_ref` and why it is outside. The same list is at
`GET /api/v1/targets/checks/outside-tenant-folder`.

The folder is set by `VAULT_KV_TENANT_SCOPE_PREFIX`, with the tenant id in
place of `{tenant_id}`. By default it is `secret/tenants/<tenant-id>/`.
Ask the person who runs your MEHO install if your install uses another
folder.

To move a target into the folder:

1. Copy the secret into the tenant's folder with your own store tooling.
   With the default folder, that is `tenants/<tenant-id>/<target-name>`
   in the `secret` mount.
2. Run `meho targets update <name> --secret-ref <new path>`. With the
   default folder, the new path is `tenants/<tenant-id>/<target-name>`.
3. The update waits until a person approves it. The command prints the
   approval request. Wait for the approval.
4. Run `meho targets outside-folder` again. The target is gone from the list.

When the folder check is off on your install, the command says
"The folder check is off on this install" and lists nothing.

## Change a secret

MEHO keeps a login for each target for a while, so it does not have to
log in on every call. When you change a secret only in Vault (or your
secret store), MEHO keeps using the old login until it expires.

To make MEHO read the secret again right away, send the same
`secret_ref` again:

```bash
meho targets update lab-vcenter --secret-ref tenants/<tenant-id>/lab-vcenter
```

Any change to how MEHO reaches or logs in to a target (for example
`--host`, `--secret-ref`, or `--extra`) does the same: the next call
logs in with the new values.

The MEHO server that gets the update drops its login at once. Any
other backplane server, and any [satellite runner](../reference/words.md#satellite-runner), drops its login on
its next call to that target.

## Import and verify

```bash
# 1. Preview — classifies every entry CREATE / UPDATE, writes nothing:
meho targets import targets.yaml --dry-run

# 2. Apply. Default mode aborts before any write if a name already exists:
meho targets import targets.yaml
#   Applied: 0 created, 0 updated, 2 waiting for approval.
#   Each target is created when a person approves its request.

# 3. Iterate later with --update: PATCHes existing names, creates new ones.
#    The PATCH is sparse — fields your YAML omits are left untouched.
meho targets import targets.yaml --update
```

Each row of the file makes its own approval request, because the CLI
calls with a token. A file with 50 rows makes 50 requests. For a large
file, use one of these:

- Import the file in the operator console. A person in the console
  writes at once.
- Give the import a change ticket. Then a person goes through all its
  rows with one command. It shows each row and asks for a yes.

  ```bash
  meho --work-ref gh:acme/ops#42 targets import targets.yaml
  meho approvals approve --filter-work-ref gh:acme/ops#42 --in-order
  ```

Then probe. A target exists only after a person approved its request, so
approve the rows first. The probe invokes the connector matched to the
target's `product`, live, and persists the resulting fingerprint:

```bash
meho approvals approve <approval-id-of-the-row>
meho targets probe lab-rke2
```

```text
vendor:        kubernetes
product:       rke2
version:       v1.31.4+rke2r1
reachable:     true
probed_at:     2026-07-31T09:14:22Z
probe_method:  GET /version
```

**Probe-green means `reachable: true` with the product/version
identified.** (The fingerprint's `product` is what the connector
*observed* — here the RKE2 distribution — which can be more specific
than the `product` token you registered the target under.) The fingerprint is cached on the target row —
`meho targets describe lab-rke2` shows it from then on without
re-probing, and the connector resolver uses it to pick the right
implementation version at dispatch time.

Two useful companions:

```bash
meho targets list --product k8s     # everything registered, filterable
meho targets discover k8s           # candidate systems a connector can see
                                    # from an already-registered seed target
```

## Network ports and account rights: the VMware family

Open these ports from MEHO to the target, and give the target login at
least these rights. These are the vendor's own role names and standard
ports.

| Target system | Port MEHO opens | Rights for reads | Extra rights for changes |
| --- | --- | --- | --- |
| vCenter | HTTPS 443 | The built-in `Read-only` role, set on the top of the inventory with "propagate to children". | A role that holds the rights of each change, for example power operations or snapshots on virtual machines. Set it only on the folders or clusters MEHO may change. |
| ESXi host | HTTPS 443 | The built-in `Read-only` role on the host. | A role with the rights of each change. |
| NSX | HTTPS 443 to NSX Manager | The built-in `Auditor` role. | `Network Admin`, or `Enterprise Admin` for the whole estate. |
| SDDC Manager | HTTPS 443 | The `VIEWER` role. | `OPERATOR`, or `ADMIN` for lifecycle tasks. |
| VCF Operations | HTTPS 443 | The `ReadOnly` role. | A role that may edit the object a change touches. |

These products of the enterprise pack have no row here: VCF Automation,
VCF Operations for logs, VCF Installer and Cloud Builder, VCF fleet
management, VCF Operations orchestrator, VCF Identity Broker, VMware Cloud Director, HCX, Site Recovery Manager,
vSphere Replication, Aria Automation Config (Salt) and Holodeck. The
port and the role of each one are in the setup guide of its connector,
which comes with the enterprise pack. The role names are the vendor's
own, so they can change with the product version. To get the pack, contact evoila (see the
[connector reference](../reference/connectors.md#evoilas-enterprise-pack)).

Use a separate login for reads and for changes where you can. MEHO also
asks a person to approve a risky change before it runs
([Approvals and break-glass](approvals-and-break-glass.md)).

When a right is missing, the call fails with the vendor's own error.
MEHO passes that error on and changes nothing.

## What can go wrong here

Real failure modes, with the errors they actually produce:

| Symptom | What it means | Fix |
|---|---|---|
| 422 at registration: *"target destination is not a public address; refusing it as a server-side request forgery risk…"* | The SSRF guard rejects private/internal addresses by default. The number-one surprise on on-prem first installs. | Add your internal ranges or hostnames to the chart's `config.targetSsrfAllowlist` (env `MEHO_TARGET_SSRF_ALLOWLIST`) and upgrade the release. An entry can end in a port (`host.name:8443`); it then admits that host on that port only. |
| 422 `unknown_product`, listing `valid_products` | The `product` token isn't one a registered connector advertises. Product tokens are exact — `k8s`, not `kubernetes`. | Pick a token from the error's `valid_products` list. |
| 422 `secret_ref_outside_tenant_scope` | You supplied a Vault ref outside `tenants/<tenant-id>/…`. It would fail with "permission denied" at every dispatch. | Use the `expected_secret_ref` the error names — or omit `secret_ref` and take the server default. |
| 422 `secret_ref_project_not_allowed` | You supplied a `gsm:` ref in a Google Cloud project that `GSM_PARAM_ALLOWED_PROJECTS` does not list. MEHO would refuse every read of it. | List the project in `GSM_PARAM_ALLOWED_PROJECTS` and upgrade the release — or point `secret_ref` at a secret in a listed project. |
| Dispatch fails, `connector_error: GcpSecretManagerReadError` — *"…in the Google Cloud project '…', which GSM_PARAM_ALLOWED_PROJECTS does not list…"* | The target's `gsm:` login names a project that is not on the list (for example a target registered before the list was set). Nothing was read from Google. | List the project in `GSM_PARAM_ALLOWED_PROJECTS` and upgrade the release. |
| Probe exits with 501 — *"no connector registered for product=…"* | The target row is fine; no connector serves that product on this backplane version. Any cached fingerprint is left untouched. | Check the [connector catalog](https://github.com/evoila/meho/blob/main/docs/cross-repo/connector-catalog.md) for your version; for spec-ingested (generic) connectors, ingest one first. |
| Dispatch fails, `connector_error: VaultCredentialsReadError` — *"…has a KV-v2 API-path-shaped secret_ref…"* | Your ref embeds the mount or a `data/` segment (`secret/data/…`). | Store the logical path only: `tenants/<tenant-id>/<name>`. |
| Dispatch fails, `connector_error: VaultCredentialsReadError` — *"…is missing required field '…'"* | The secret exists but doesn't carry the field the connector needs (`username`/`password`, or `kubeconfig` for Kubernetes). | Re-stage the secret with the connector's field names. |
| Dispatch fails, `connector_vault_forbidden` — Vault answered *permission denied* | Nine times out of ten the secret is staged at the wrong path, **not** a missing grant: the error reads like a policy problem but the ref simply doesn't match where you wrote the secret. The error names the expected path. | Store the credential at the expected path with `meho vault kv put secret <path> --data @creds.json` ([recipe](#store-the-credential-through-meho)). If the error says the ref is the default path MEHO builds from the target name, this install does not let MEHO read that path: store the credential at a path MEHO may read, then run `meho targets update <name> --secret-ref <path>`. If the error says it is not the target's own `secret_ref`, check the path the operation names. Do **not** widen the backplane's Vault policy — it is deploy-owned and re-applied on upgrade. A probe answers the same error with a 422. |
| Dispatch fails, `connector_auth_failed` (HTTP 401 at session establish) | The credential resolved but the vendor rejected it — usually a password rotated upstream while the store copy lagged. | Re-stage the current credential at the same path, then retry. |
| Dispatch fails TLS: `extras.error_code` is `connector_tls_verify_failed` and `extras.exception_message` contains *"IP address mismatch"* (CPython `ssl` text relayed verbatim, not a MEHO string — MEHO's own summary says the chain "is not trusted") | You reach the appliance by IP but its certificate only names the FQDN. | Set `tls_server_name: <cert-fqdn>` on the target and re-import with `--update`. |
| Probe shows `reachable: false` / `auth_failed` on a GSM deploy with per-operator Workload Identity Federation | Probes run under a system placeholder identity that cannot complete the per-operator token exchange, so credentialed-target probes cannot read the secret on that configuration. Reachability of *uncredentialed* endpoints still probes fine. | Known limitation — documented in [`docs/deploying.md`](https://github.com/evoila/meho/blob/main/docs/deploying.md); verify credentialed dispatch with a real operation call instead. |
| Dispatch fails with `no_connector: no implementation for product='…' version='…'` | No connector implementation matches the target's product and version. | Check the target's `product` and `version` against the connector catalog. Probe the target, or set its `version`. |

!!! tip "Prove the secret is readable — without printing it"

    If Vault itself is registered as a MEHO target, dispatch the
    metadata-only `vault.kv.versions` operation against the
    credential's path instead of a read: it proves the path resolves
    and the policy allows access while returning version numbers,
    never values.

    To check that copies of one secret still match (for example after
    a password change), use `vault.kv.compare` for two or more Vault
    paths, or `k8s.secret.compare_to_ref` for a Kubernetes Secret key
    and its Vault copy. They answer only "same" or "different" per
    copy, never a value or a hash.

**Next:** [Run your first operations](first-operations.md).
