<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Azure onboarding: the login, the roles, the Vault secret and the target

This page tells an operator how to connect one Azure subscription to MEHO.
When you are done, an agent can call `call_operation` with connector
`azure-arm-2026.10` on your target, and MEHO saves an audit row for every
call.

The code is in
[`backend/src/meho_backplane/connectors/azure/`](../../backend/src/meho_backplane/connectors/azure/).
The engineering page is
[`docs/codebase/connectors-azure.md`](../codebase/connectors-azure.md).

## What you get

The Azure connector has typed (hand-written) operations only. There is
nothing to ingest and nothing to review: the operations are stored when
MEHO starts.

The connector has 71 operations in 12 groups. These three in the group
`account` are the ones to try first:

| Group | Operation | What it does |
| --- | --- | --- |
| `account` | `azure.about` | Reads the subscription, its tenant and cloud, and the login MEHO uses |
| `account` | `azure.location.list` | Lists the Azure regions of the subscription (a link to the stored result) |
| `account` | `azure.operation.status` | Reads the state of a long-running Azure operation once |

The other groups are `inventory`, `compute`, `network`, `storage`, `aks`,
`arc`, `automation`, `avs`, `cost`, `migrate` and `security`. To see their
operations, run `meho operation groups azure-arm-2026.10` and
`meho operation search azure-arm-2026.10 "<what you need>"`. The engineering
page lists every operation.

69 operations are reads (`safe`) and need no approval. Two operations
change something in Azure. They are `caution`, and each one waits until a
second person approves it:

| Operation | What it does |
| --- | --- |
| `azure.tag.update` | Changes the tags of one resource |
| `azure.patching.assessment.start` | Checks one Azure VM or Arc machine for missing updates now (it installs nothing) |

One target is one subscription. For two subscriptions, make two targets.
They may share one login.

## Step 1: make the login

MEHO logs in to Microsoft Entra ID as a service principal (an app login)
with a client secret. Make one with the Azure CLI. You need the role
`User Access Administrator` (or higher) on the subscription.

```bash
az ad sp create-for-rbac \
  --name meho-reader \
  --role Reader \
  --scopes /subscriptions/00000000-0000-0000-0000-000000000000
```

Use your own subscription ID in `--scopes`. The answer looks like this:

```json
{
  "appId": "<client id>",
  "displayName": "meho-reader",
  "password": "<client secret>",
  "tenant": "<tenant id>"
}
```

Azure shows `password` only once. Put it in Vault right away (step 3).

Microsoft warns that `create-for-rbac` can change an existing app with the
same display name. Pick a name that is not used yet.

## Step 2: give the login its roles

| What MEHO does | Role | Scope |
| --- | --- | --- |
| most reads, also `azure.about`, `azure.location.list`, `azure.operation.status` | `Reader` | the subscription |
| cost reads (`azure.cost.query`, `azure.cost.forecast`, `azure.cost.budget.list`) | `Reader` or `Cost Management Reader`; the billing account must also let the subscription see costs (the error names the setting) | the subscription |
| blob list and file read (`azure.storage.blob.list`, `azure.storage.blob.read`) | `Storage Blob Data Reader` (`Reader` alone gives no access to files) | the storage account or the container |
| `azure.tag.update` (only if you use it) | a role that may write tags, for example `Tag Contributor` | the resources to tag |
| `azure.patching.assessment.start` (only if you use it) | a role that may check a machine for updates, for example `Virtual Machine Contributor` for a VM | the machines |

The command in step 1 already gives `Reader` on the subscription. Never
give the login `Contributor` or `Owner` for reads. The engineering page
names the role of each operation family.

## Step 3: put the secret in Vault

The secret holds three keys. MEHO reads it with the caller's own Vault
identity, so the Vault policy must let your operators read this path (see
[`connector-vault-policy.md`](connector-vault-policy.md)).

```bash
vault kv put -mount=secret azure/example-subscription \
  tenant_id="<tenant id>" \
  client_id="<client id>" \
  client_secret="<client secret>"
```

| Key | Value |
| --- | --- |
| `tenant_id` | `tenant` from step 1, a GUID |
| `client_id` | `appId` from step 1, a GUID |
| `client_secret` | `password` from step 1 |

MEHO never writes the client secret or the access token into audit rows,
logs, traces or stored results.

## Step 4: add the target

```yaml
targets:
  - name: example-subscription
    product: azure
    host: management.azure.com
    port: 443
    secret_ref: azure/example-subscription
    auth_model: shared_service_account
    extras:
      subscription_id: 00000000-0000-0000-0000-000000000000
      tenant_id: 11111111-1111-1111-1111-111111111111
      cloud: AzureCloud
```

| Field | Value |
| --- | --- |
| `host` | the Resource Manager host of the cloud: `management.azure.com` for the public cloud |
| `extras.subscription_id` | required: the subscription ID |
| `extras.tenant_id` | optional: when set, it must equal `tenant_id` in the secret |
| `extras.cloud` | optional: `AzureCloud` (default), `AzureUSGovernment` or `AzureChinaCloud` |

No cloud is proven on a real tenant yet. The first live proof uses
`AzureCloud`. MEHO sends the token only to the Resource Manager host of the
target's cloud, over https on port 443 with the certificate checked. Any
other host or port fails before the login.

```bash
meho targets import example-subscription.yaml
meho targets probe example-subscription --json
```

The probe reads the subscription. A good answer has `reachable: true`,
vendor `microsoft`, product `azure`, and the subscription ID, tenant ID,
cloud and state in `extras`.

One login often sees several subscriptions. To make a target for each of
them, list the ones that have no target yet. The command uses the login of
an existing target:

```bash
meho targets discover azure --seed-target example-subscription --import-yaml > subscriptions.yaml
# check the file, then:
meho targets import subscriptions.yaml
```

## Step 5: check it end to end

From the CLI:

```bash
meho operation call azure-arm-2026.10 azure.about --target example-subscription
meho operation call azure-arm-2026.10 azure.location.list --target example-subscription
```

`azure.location.list` returns a link to the stored result (a result
handle), because Azure has more than 50 regions. Read the rows with:

```bash
meho operation result-query <handle_id> --where "geography = Germany"
```

From an agent, use the same path:

```text
call_operation(connector_id="azure-arm-2026.10", op_id="azure.about",
               target="example-subscription", params={})
```

`target` is the target **name**, not its ID. Then check the audit:

```bash
meho audit query --target example-subscription
```

Every call has an audit row, saved before the answer returns, with the
agent session ID. A caller without permission for the target is refused
before MEHO calls Azure, and the audit shows the refusal.

## When something fails

| Error | Meaning | What to do |
| --- | --- | --- |
| `connector_auth_failed` with `AADSTS7000215` | the client secret is wrong | write the right secret to Vault; no restart is needed |
| `connector_auth_failed` with `AADSTS7000222` | the client secret has run out | make a new secret (`az ad sp credential reset --id <client id>`) and write it to Vault |
| `connector_error`, `AzureForbiddenError` | the login has no role on the subscription | give it `Reader` on the subscription (step 2) |
| `AzureTargetConfigError` | the target is wrong (host, port, GUID or cloud) | fix the field the message names |
| `AzureSecretShapeError` | a secret key is missing or not a GUID | fix the key the message names |
| `connector_error`, `AzureThrottledError` | Azure is throttling | wait the time the message names |

## References

- Create Azure service principals with the Azure CLI:
  <https://learn.microsoft.com/en-us/cli/azure/azure-cli-sp-tutorial-1>
- Azure built-in roles:
  <https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles>
- [`targets-yaml.md`](targets-yaml.md), [`connector-vault-policy.md`](connector-vault-policy.md),
  [`mcp-client-setup.md`](mcp-client-setup.md)
