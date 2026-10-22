# How MEHO talks to VCF Identity Broker and Workspace ONE Access (decision)

**Status:** proposed. It waits for the program owner to agree in the pull request.
**Date:** 2026-10-23
**Issue:** [#4507](https://github.com/evoila/meho/issues/4507)
**Initiative:** [#4306](https://github.com/evoila/meho/issues/4306) — VCF bring-up, fleet lifecycle, identity and platform services through MEHO
**Used by:** the connector Task [#4508](https://github.com/evoila/meho/issues/4508)

## The decision in short

- The two products are the same kind of service (single sign-on for VMware
  products), but their APIs are different. They share almost no paths.
- **Two versioned implementations of one product, `idb`:**
  - `idb-wsa-3.3` for Workspace ONE Access 3.3 (the version band `>=3.3,<4.0`).
  - `idb-rest-9.0` for VCF Identity Broker 9.x (the version band `>=9.0,<10.0`).
  - This follows [versioned-connector-dual-impl.md](versioned-connector-dual-impl.md):
    MEHO picks the implementation by the detected version of the target.
- **On VCF 9.x, the directory work is not in the Identity Broker API.** The
  public Identity Broker API has login, OAuth clients, API tokens, and user
  and group lookup. It has no call for identity providers, directories, the
  last sync, or "start a sync". In VCF 9.1 these calls are in the
  **VCF Operations** API, under `/suite-api/api/fleet-management/iam/`.
  So on 9.1 the directory and sync operations go on the VCF Operations
  connector (`vrops-rest-9.0`, 9.1 and newer only), next to the VCF role
  operations of [#4480](https://github.com/evoila/meho/issues/4480).
- Some Workspace ONE Access 3.3 calls have no vendor API reference. They are
  marked **not confirmed** below. The connector Task must check them on a
  lab appliance before they ship.

## Why two implementations

- The paths differ. Workspace ONE Access uses `/SAAS/...`. Identity Broker
  uses `/acs/t/{tenant}/...` and `/usergroup/t/{tenant}/scim/v2/...`.
- The login differs. Both use OAuth 2.0 client credentials (a client id and
  a secret). But the token path differs, and Identity Broker needs a tenant
  name in every path.
- The version numbers do not overlap (3.3.x and 9.x), so the two bands never
  fight.
- One product token (`idb`) keeps one set of operation ids for the agent. The
  agent calls `idb.user.find` on either product. MEHO runs the right call.

## Where the API reference is

| Product | Reference | What it covers |
|---|---|---|
| VCF Identity Broker 9.0, 9.1, 9.1.1 | [Identity Broker API on developer.broadcom.com](https://developer.broadcom.com/xapis/vmware-identity-broker/latest/) (operation lists: [9.0](https://developer.broadcom.com/xapis/vmware-identity-broker/9.0/operation-index/), [9.1](https://developer.broadcom.com/xapis/vmware-identity-broker/9.1/operation-index/), [9.1.1](https://developer.broadcom.com/xapis/vmware-identity-broker/latest/operation-index/)) | Login (OAuth 2.0 and OpenID Connect), OAuth clients, SCIM users and groups. 9.1 adds API tokens, emergency clients and `properties`. No downloadable OpenAPI file is linked. |
| VCF Operations 9.1 and 9.1.1 (identity part) | `specifications/vcf-operations/vcf-operations-openapi.json` in [`vmware/vcf-api-specs`](https://github.com/vmware/vcf-api-specs) (tags `9.1.0.0` and `9.1.1.0`) | Identity Broker instances, SSO realms, identity providers, LDAP directories with the last sync, sync, sync logs, user and group lookup. The `9.0.0.0` tag of the same file has none of these paths. |
| SDDC Manager 9.0 and 9.1 (identity part) | `specifications/sddc-manager/sddc-manager-openapi.json` in `vmware/vcf-api-specs` | `GET /v1/identity-providers`: the identity providers, and for a federated one, its directory list (no sync time). |
| vCenter 7.0 and newer (identity part) | `specifications/vsphere/openapi/automation/vcenter.yaml` in `vmware/vcf-api-specs` | `GET /api/vcenter/identity/providers`: the identity providers vCenter uses. |
| Workspace ONE Access 3.3 | No API reference for 3.3 found. The closest vendor files: [`apidocs/swagger.json`](https://github.com/omnissa-archive/idm/blob/master/apidocs/swagger.json) in `omnissa-archive/idm` (Identity Manager REST API, not tied to 3.3), and the example module [`VMware.WorkspaceOneAccess.psm1`](https://github.com/vmware/PowerCLI-Example-Scripts/blob/master/Modules/VMware.WorkspaceOneAccess/VMware.WorkspaceOneAccess.psm1) in `vmware/PowerCLI-Example-Scripts` (both repositories are archived) | The swagger file: login, SCIM users and groups, OAuth clients. The example module: directory list and identity provider list. Neither is an API reference for 3.3. |

The old VMware docs site now sends every page to the Broadcom TechDocs start
page, so the 3.3 product guides could not be read for this record.

## How to log in

**Identity Broker 9.x** ([token call](https://developer.broadcom.com/xapis/vmware-identity-broker/9.0/acs/t/tenant/token/post/),
[security schemes](https://developer.broadcom.com/xapis/vmware-identity-broker/9.0/api-security-schema/)):

- `POST /acs/t/{tenant}/token` with a form body (not JSON) and
  `grant_type=client_credentials`.
- The client sends `Authorization: Basic base64(client_id:client_secret)`.
- The answer has `access_token` (a JWT, a signed token), `token_type` and
  `expires_in`. MEHO sends `Authorization: Bearer <token>` on later calls.
- The OAuth client is made once by an admin (`POST /acs/t/{tenant}/broker/oauth2-clients`).
  MEHO keeps the client id and secret in Vault, like every other target secret.
- **Tenant name.** The default tenant is `CUSTOMER`. A Broadcom article shows
  the call `/acs/t/CUSTOMER/token` on VCF 9.1
  ([KB 443943](https://knowledge.broadcom.com/external/article/443943/after-upgrading-to-vcf-91-configuration.html)).
  The API reference itself does not name a default. MEHO stores the tenant on
  the target, with `CUSTOMER` as the default.

**VCF Operations 9.1** (for the directory operations): the VCF Operations
connector logs in as it does today (`POST /suite-api/api/auth/token/acquire`,
then `Authorization: OpsToken <token>`). See
`docs/codebase/connectors-vcf-operations.md`. Nothing new is needed.

**Workspace ONE Access 3.3:**

- `POST /SAAS/auth/oauthtoken` with `grant_type=client_credentials` and
  `Authorization: Basic base64(client_id:client_secret)`. Source: the
  `securityDefinitions` of the `omnissa-archive/idm` swagger file (it names
  `tokenUrl: /SAAS/auth/oauthtoken` and the client-credentials flow), and
  `Connect-WorkspaceOneAccess` in the example module.
- MEHO sends `Authorization: Bearer <token>` on later calls.
- The OAuth client is made once by an admin in the Workspace ONE Access console.

## How to detect the version

- **Identity Broker 9.1 and newer:** `GET /acs/t/{tenant}/properties` returns
  `version`, `deployment_type` and `instance_id`
  ([reference](https://developer.broadcom.com/xapis/vmware-identity-broker/latest/acs/t/tenant/properties/get/)).
  This call is in the 9.1 and 9.1.1 operation lists, not in the 9.0 list.
- **Identity Broker 9.0:** **not confirmed.** The 9.0 reference has no version
  call. If `properties` answers on a 9.0 lab appliance, MEHO uses it. If not,
  the operator sets the version on the target.
- **Also on 9.1:** VCF Operations `GET /suite-api/api/fleet-management/iam/vidbs`
  lists each Identity Broker with `version`, `healthy`, `vidbStatus` and
  `deploymentType` (`vcf-operations-openapi.json`, 9.1 tags).
- **Workspace ONE Access 3.3:** **not confirmed.** No vendor source found for
  a version call. The connector Task checks on a lab appliance whether the
  health answer (below) carries a version. If not, the operator sets the
  version on the target.
- **Which product is it?** Each product has its own OpenID Connect discovery
  path: `GET /SAAS/auth/.well-known/openid-configuration` (Workspace ONE
  Access, in the swagger file) and
  `GET /acs/t/{tenant}/.well-known/openid-configuration` (Identity Broker, in
  the [reference](https://developer.broadcom.com/xapis/vmware-identity-broker/9.0/operation-index/)).
  The one that answers tells MEHO the product. A lab check must confirm that
  both answer without a login.

## The usual work

Risk levels: a **read** is `safe` and runs at once. A **change** waits until a
second person approves.

| Operation (proposed id) | What it does | Workspace ONE Access 3.3 | Identity Broker 9.x | Risk |
|---|---|---|---|---|
| `idb.about` | Product, version, health. Also used for version detection. | `GET /SAAS/API/1.0/REST/system/health` — **not confirmed** (see note 1). | `GET /acs/t/{tenant}/properties` (9.1+). Health: VCF Operations `GET /suite-api/api/fleet-management/iam/vidbs` (9.1+). 9.0: **not confirmed**. | read |
| `idb.identity_provider.list` | Identity providers: name, type, state. | `GET /SAAS/jersey/manager/api/identityProviders?onlyEnabledAdapters=true`, `Accept: application/vnd.vmware.horizon.manager.identityprovider.summary.list+json` — example module only (note 2). | 9.1+: VCF Operations `GET /suite-api/api/fleet-management/iam/ssorealms` (gives `idpId`), then `GET /suite-api/api/fleet-management/iam/identity-providers/{idpConfigId}`. 9.0: SDDC Manager `GET /v1/identity-providers`. | read |
| `idb.directory.list` | Directories with last sync time and last sync state. | `GET /SAAS/jersey/manager/api/connectormanagement/directoryconfigs?includeJitDirectories=true`, `Accept: application/vnd.vmware.horizon.manager.connector.management.directory.list+json` — example module only (note 2). The last-sync fields are **not confirmed**. | 9.1+: VCF Operations `GET /suite-api/api/fleet-management/iam/identity-providers/{idpConfigId}/ldap-directories` (has `lastSyncDateTime`, `lastSyncStatus`, `numberOfUsers`, `numberOfGroups`). 9.0: SDDC Manager `GET /v1/identity-providers` lists the directories, with no sync time. | read |
| `idb.directory.sync_log` | The result of the last sync (or a page of past syncs). | **Not confirmed.** No source found. | 9.1+: VCF Operations `GET /suite-api/api/fleet-management/iam/identity-providers/{idpConfigId}/ldap-directories/{ldapDirectoryId}/sync-logs?last=true`. | read |
| `idb.directory.sync` | Start a directory sync now. | `POST /SAAS/jersey/manager/api/connectormanagement/directoryconfigs/{directoryId}/syncprofile/sync` — **not confirmed** (note 3). | 9.1+: VCF Operations `POST /suite-api/api/fleet-management/iam/identity-providers/{idpConfigId}/ldap-directories/{ldapDirectoryId}/sync` (answers `202` with `syncStatus`). LDAP directories only (note 4). | change (caution) |
| `idb.user.find` | Find users by name. | `POST /SAAS/jersey/manager/api/scim/Users/.search` (swagger file). | `POST /usergroup/t/{tenant}/scim/v2/Users/.search` (9.0+, reference). | read |
| `idb.group.find` | Find groups and their members. | `POST /SAAS/jersey/manager/api/scim/Groups/.search` (swagger file). | `POST /usergroup/t/{tenant}/scim/v2/Groups/.search` (9.0+, reference). | read |

On 9.x, the rows that name VCF Operations run on the VCF Operations
connector, not on `idb`. Proposed ids there: `vrops.fleet.identity_provider.get`,
`vrops.fleet.directory.list`, `vrops.fleet.directory.sync_log`,
`vrops.fleet.directory.sync`, and `vrops.fleet.identity_broker.list` (from
`/vidbs`). They need 9.1, like the role operations of #4480.

The SDDC Manager 9.0 rows go on the SDDC Manager connector as one read
(`sddc.identity_provider.list`), if the program owner wants 9.0 coverage.
Its answer can hold a sync token (`fedIdp.syncClientTokenInfo.token`). MEHO
must drop that field from the result.

**Notes**

1. The health path `/SAAS/API/1.0/REST/system/health` (and
   `/SAAS/API/1.0/REST/system/health/heartbeat` for load balancers) comes
   from Workspace ONE Access 20.10 install docs found by search. Those pages
   could not be opened, and no 3.3 page was found.
2. The example module is a VMware-owned GitHub file, but it is sample code,
   not an API reference. These calls use the admin "jersey" API that the
   console itself uses, with VMware media types in `Accept` and
   `Content-Type`.
3. The sync path comes only from a community forum thread. No vendor file
   names it. The body and media type are not known.
4. Identity Broker 9.x gets users either from AD or LDAP (MEHO can start that
   sync) or by SCIM push from an outside identity provider such as Okta or
   Entra ID. A SCIM-pushed directory is synced by the outside provider. MEHO
   cannot start that sync.

## Fixes for the connector Task #4508

The table in #4508 was written before this research. These calls in it do
not match a vendor source:

- `GET /vidb/health` — not in the Identity Broker reference. Use `properties`
  (9.1+) and VCF Operations `/vidbs`.
- `GET /usergroup/t/CUSTOMER/broker/directories` — not in the reference. On
  9.1 use the VCF Operations `ldap-directories` call.
- `GET /api/vcenter/identity/providers` — this is real (vCenter, 7.0+), but it
  shows the providers of vCenter, not of the Identity Broker. Keep it on the
  vCenter connector if it is wanted.
- The tenant should be a target setting with default `CUSTOMER`, not a fixed
  part of the path.

When this record is agreed, #4508 takes this table, and the 9.1 directory
rows move to a VCF Operations Task (or into #4480's connector work).

## Open points the lab must settle

- Workspace ONE Access 3.3: the health answer, a version call, the last-sync
  fields of a directory, and the sync call with its body.
- Identity Broker 9.0: whether `properties` answers, and how to see
  directories and the last sync without VCF Operations 9.1.
- Both products: whether the OpenID discovery path answers without a login.

## Out of scope

- Changing identity providers or directories (setup work). The APIs have
  these calls, but they are not the usual work.
- OAuth client and API token management.
