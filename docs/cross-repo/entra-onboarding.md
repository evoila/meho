<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Entra ID onboarding: the login, the Graph permissions, admin consent, the target

This page tells an operator how to connect one Microsoft Entra ID tenant to
MEHO. When you are done, an agent can call `call_operation` with connector
`entra-graph-1.0` on your target, and MEHO saves an audit row for every call.

The most important step is step 2: the login needs Microsoft Graph
application permissions, and an admin must grant consent for them. Without
consent, every read fails with a clear error that names the missing
permission.

This page is about reading a directory. To let people sign in to MEHO
with their Entra ID account, see
[`docs-site/install/login/entra-id.md`](../../docs-site/install/login/entra-id.md).

The code is in
[`backend/src/meho_backplane/connectors/entra/`](../../backend/src/meho_backplane/connectors/entra/).
The engineering page is
[`docs/codebase/connectors-entra.md`](../codebase/connectors-entra.md).

## What you get

The Entra ID connector has typed (hand-written) operations only. There is
nothing to ingest and nothing to review: the operations are stored when MEHO
starts. All six are reads (`safe`) in the group `directory`. They need no
approval.

| Operation | What it does | Graph permission |
| --- | --- | --- |
| `entra.about` | Reads the tenant (ID, name, verified domains) and the permissions of the login | `Organization.Read.All` |
| `entra.user.list` | Lists users; filters for guests, members, enabled or disabled accounts | `User.Read.All` |
| `entra.group.list` | Lists groups with their type; member counts on request | `Group.Read.All` |
| `entra.role.assignment.list` | Lists who holds which Entra ID admin role | `RoleManagement.Read.Directory` |
| `entra.app.list` | Lists app registrations and when their secrets and certificates run out | `Application.Read.All` |
| `entra.service_principal.list` | Lists enterprise apps and when their secrets and certificates run out | `Application.Read.All` |

MEHO never shows a client secret, its hint or certificate data.

One target is one tenant.

## Step 1: pick the login

MEHO logs in as an app (a service principal). It can prove who it is in two
ways:

| Way | What MEHO keeps | When to use it |
| --- | --- | --- |
| **Certificate** (recommended) | a private key and the public certificate, in the secret store | a customer tenant, and any tenant that blocks new client secrets |
| Client secret | a client secret, in the secret store | a lab or a first test |

Microsoft advises a certificate, and no client secret in production. A tenant
admin can also block new client secrets for all apps, and then only the
certificate works. With a certificate, MEHO signs a short proof with the
private key. Microsoft checks it with the public certificate that you upload
to the app. The private key never leaves MEHO's secret store.

You can
reuse the login of an Azure target of the same tenant (see
[`azure-onboarding.md`](azure-onboarding.md), step 1): the secret has the
same shape. Or make a new app registration for Entra ID only. A login of its
own keeps the Graph permissions apart from the Azure roles.

You need the login's application (client) ID, its tenant ID and either a
certificate or a client secret.

## Step 2: add the Graph permissions and grant admin consent

These are **application permissions** (the app reads without a signed-in
person), not delegated permissions. Each one needs admin consent in the
tenant, once.

| Permission | Needed for |
| --- | --- |
| `Organization.Read.All` | `entra.about` |
| `User.Read.All` | `entra.user.list` |
| `Group.Read.All` (`GroupMember.Read.All` also works) | `entra.group.list` |
| `RoleManagement.Read.Directory` | `entra.role.assignment.list` |
| `Application.Read.All` | `entra.app.list`, `entra.service_principal.list` |

`Directory.Read.All` alone also allows all six reads, but it allows much
more. Prefer the five permissions above.

Only an admin with the **Privileged Role Administrator** role (or Global
Administrator) can grant consent for Microsoft Graph application
permissions. An Application Administrator or Cloud Application Administrator
cannot.

**In the Microsoft Entra admin center:**

1. Go to **Entra ID** > **App registrations** > **All applications** and
   open the login's app.
2. Open **API permissions** > **Add a permission** > **Microsoft Graph** >
   **Application permissions**.
3. Select the five permissions from the table and add them.
4. Select **Grant admin consent** and confirm. Each permission now shows
   "Granted".

**With the Azure CLI** (use your own client ID). Adding the permissions needs
the right to change the app. For `admin-consent`, Microsoft's Azure CLI
reference asks you to sign in as a **Global Administrator**; with only
Privileged Role Administrator, grant the consent in the admin center instead.

```bash
az ad app permission add \
  --id 00000000-0000-0000-0000-000000000000 \
  --api 00000003-0000-0000-c000-000000000000 \
  --api-permissions \
    498476ce-e0fe-48b0-b801-37ba7e2685c6=Role \
    df021288-bdef-4463-88db-98f22de89214=Role \
    5b567255-7703-4780-807c-7be8301ae99b=Role \
    483bed4a-2ad3-4361-a73b-c83ccdbdc53c=Role \
    9a5d68dd-52b0-4cc2-bd40-abcf44ac3a30=Role
az ad app permission admin-consent --id 00000000-0000-0000-0000-000000000000
```

`00000003-0000-0000-c000-000000000000` is Microsoft Graph. The other IDs are
Microsoft's public IDs of the application permissions (from Microsoft's Graph
permissions reference):

| Permission ID | Permission |
| --- | --- |
| `498476ce-e0fe-48b0-b801-37ba7e2685c6` | `Organization.Read.All` |
| `df021288-bdef-4463-88db-98f22de89214` | `User.Read.All` |
| `5b567255-7703-4780-807c-7be8301ae99b` | `Group.Read.All` |
| `483bed4a-2ad3-4361-a73b-c83ccdbdc53c` | `RoleManagement.Read.Directory` |
| `9a5d68dd-52b0-4cc2-bd40-abcf44ac3a30` | `Application.Read.All` |

A token keeps the permissions it was made with. MEHO asks for a new token
after Graph refuses a call, so a permission granted later works on the next
call, without a restart.

## Step 3: put the secret in Vault

Skip this step when you reuse the secret of an Azure target. Then put that
Azure target's `secret_ref` into the Entra ID target in step 4, instead of
`entra/example-tenant`.

Otherwise the secret holds `tenant_id`, `client_id` and one of two ways (a
certificate, or a client secret). MEHO reads it with the caller's own
Vault identity, so the Vault policy must let your operators read this path
(see [`connector-vault-policy.md`](connector-vault-policy.md)).

**Certificate (recommended).**

1. Make a key pair. Use a certificate life of 12 months or less.

   ```bash
   openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
     -subj "/CN=meho-entra" -keyout key.pem -out cert.pem
   ```

2. Write the private key straight into the secret store, with the
   certificate. Do not paste the key into a chat or a ticket.

   ```bash
   vault kv put -mount=secret entra/example-tenant \
     tenant_id="<tenant id>" \
     client_id="<client id>" \
     private_key=@key.pem \
     certificate=@cert.pem
   ```

3. Delete `key.pem` from the disk. Upload only `cert.pem` (the public
   certificate) to the app: Microsoft Entra admin center, App registrations,
   your app, Certificates & secrets, Certificates, Upload certificate.
4. Test it (step 5). Then remove the old client secret from the app.
5. Put the end date of the certificate in your calendar. When it runs out,
   make a new pair and repeat steps 1 to 3. MEHO reads the new secret on the
   next call after a refused login, without a restart.

The key must be an RSA key without a password, in PEM form. MEHO works out
the certificate's thumbprint by itself. MEHO checks the key and the
certificate each time it reads the secret, before it calls Microsoft:

| Message says | What to fix |
| --- | --- |
| "not a readable PEM" | the key or the certificate is not PEM text, or the key has a password: write them again from `key.pem` and `cert.pem` |
| "must be RSA" | make the pair with `rsa:2048` as in step 1 |
| "does not belong to the certificate" | the key and the certificate come from two different pairs: write the matching pair |

After you fix the secret, the next call reads it, without a restart.

**Client secret.**

```bash
vault kv put -mount=secret entra/example-tenant \
  tenant_id="<tenant id>" \
  client_id="<client id>" \
  client_secret="<client secret>"
```

Keep one way only. A secret that holds both `client_secret` and the
certificate keys, or neither, is refused.

MEHO never writes the client secret, the private key or the access token into
audit rows, logs, traces or stored results.

## Step 4: add the target

```yaml
targets:
  - name: example-tenant
    product: entra
    host: graph.microsoft.com
    port: 443
    secret_ref: entra/example-tenant
    auth_model: shared_service_account
    extras:
      tenant_id: 11111111-1111-1111-1111-111111111111
      cloud: AzureCloud
```

| Field | Value |
| --- | --- |
| `host` | the Microsoft Graph host of the cloud: `graph.microsoft.com` for the public cloud |
| `extras.tenant_id` | optional: when set, it must equal `tenant_id` in the secret |
| `extras.cloud` | optional: `AzureCloud` (default), `AzureUSGovernment` (host `graph.microsoft.us`) or `AzureChinaCloud` (host `microsoftgraph.chinacloudapi.cn`) |

No cloud is proven on a real tenant yet; the first live proof uses
`AzureCloud`. MEHO sends the token only to the Graph host of the target's
cloud, over https on port 443 with the certificate checked.

```bash
meho targets import example-tenant.yaml
meho targets probe example-tenant --json
```

A good probe has `reachable: true`, vendor `microsoft`, product `entra`, and
the tenant ID, cloud and default domain in `extras`.

## Step 5: check it end to end

```bash
meho operation call entra-graph-1.0 entra.about --target example-tenant
```

Check `granted_permissions` in the answer: it should list the five
permissions from step 2. Then try a list:

```bash
meho operation call entra-graph-1.0 entra.app.list --target example-tenant \
  --params '{"expiring_within_days": 60}'
```

A list with more than 50 rows comes back as a link to the stored result (a
result handle). Read the rows with `meho operation result-query <handle_id>`.

From an agent, use the same path:

```text
call_operation(connector_id="entra-graph-1.0", op_id="entra.about",
               target="example-tenant", params={})
```

`target` is the target **name**, not its ID. Every call has an audit row,
saved before the answer returns.

## When something fails

| Error | Meaning | What to do |
| --- | --- | --- |
| `connector_error`, `EntraPermissionError` | the login lacks the Graph permission the message names, or consent is missing | step 2; the remediation lists the permissions the token carries |
| `connector_auth_failed` with `AADSTS7000215` | the client secret is wrong | write the right secret to Vault; no restart is needed |
| `connector_auth_failed` with `AADSTS7000222` | the client secret has run out | make a new secret and write it to Vault |
| `connector_auth_failed`, the message says "with the certificate" | Microsoft refused the proof | check that the certificate is uploaded to the app, has not run out, and that the `private_key` in the secret belongs to it |
| `EntraTargetConfigError` | the target is wrong (host, port, GUID or cloud) | fix the field the message names |
| `EntraSecretShapeError` | a secret key is missing or not a GUID | fix the key the message names |
| `connector_error`, `EntraThrottledError` | Graph is limiting the calls of this login | wait the time the message names |

## References

- Certificate credentials (the proof MEHO signs):
  <https://learn.microsoft.com/en-us/entra/identity-platform/certificate-credentials>
- Security best practices for app registrations:
  <https://learn.microsoft.com/en-us/entra/identity-platform/security-best-practices-for-app-registration>
- Grant tenant-wide admin consent:
  <https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/grant-admin-consent>
- Microsoft Graph permissions reference:
  <https://learn.microsoft.com/en-us/graph/permissions-reference>
- `az ad app permission`:
  <https://learn.microsoft.com/en-us/cli/azure/ad/app/permission>
- [`azure-onboarding.md`](azure-onboarding.md), [`targets-yaml.md`](targets-yaml.md),
  [`connector-vault-policy.md`](connector-vault-policy.md),
  [`mcp-client-setup.md`](mcp-client-setup.md)
