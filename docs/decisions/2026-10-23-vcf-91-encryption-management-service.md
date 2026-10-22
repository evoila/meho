# How MEHO works with the VCF 9.1 Encryption Management service (decision)

**Status:** proposed. It waits for the program owner to agree in the pull request.
**Date:** 2026-10-23
**Issue:** [#4511](https://github.com/evoila/meho/issues/4511)
**Initiative:** [#4306](https://github.com/evoila/meho/issues/4306) — VCF bring-up, fleet lifecycle, identity and platform services through MEHO

## The decision in short

- The Encryption Management service lets an organization (a tenant in VCF
  Automation) encrypt its virtual machines and disks with keys from **its own
  key provider**. A key provider is an outside key server (a KMS) that
  vCenter talks to over KMIP, the standard protocol for key servers. This is
  often called "bring your own key" (BYOK).
- It is a **VCF service** that comes pre-installed and turned on in VCF
  Automation 9.1. A VCF service is a package that VCF Automation installs and
  keeps running.
- The service has **no public API of its own**. There is no file for it in
  `vmware/vcf-api-specs` (tags `9.1.0.0` and `9.1.1.0`), no reference on the
  Broadcom developer portal, and the 9.1 TechDocs pages describe only the
  provider portal (the web page for the cloud provider).
- MEHO can still check **the health of the service and the key providers it
  set up**, through APIs MEHO already logs in to:
  1. **The VCF Automation Service Manager API**
     (`/api/extension/broadcom/service-manager/v2/vcf-services`). It gives
     the version, state and errors of every VCF service. These reads go on
     the VCF Automation connector, as typed (hand-written) operations. They
     are the same reads the Configuration service record proposes.
  2. **The vCenter API for key providers.** The service links each key
     provider to a vCenter. vCenter then holds the key provider and checks
     that it can reach the key servers. These reads go on the vSphere
     connector (`vmware-rest`), as typed operations.
- So the service needs **no connector of its own**.
- **We ask the program owner for a scope exception** for the changes
  (register, publish, unpublish, edit and remove a key provider, and the
  organization's own key settings). The vendor gives no API for them. MEHO
  must not make these changes behind the service's back in vCenter.
- The vCenter paths under `/api/vcenter/crypto-manager/...` and
  `/api/vcenter/trusted-infrastructure/...` are **not** this service. They
  are vCenter's own key provider API and vSphere Trust Authority (see below).
- Some facts are **not confirmed** and are marked below. The connector Task
  checks them in a lab before the operations ship.

## What the service is

- The provider guide says the service "grants organization administrators
  the ability to use encryption keys from their own key providers" to encrypt
  virtual machines (with or without a vTPM, a virtual security chip) and
  disks. It also says the service "is pre-installed and enabled" in VCF
  Automation
  ([provider guide](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-1/cloud-provider-guide.html)).
- The work is split in two steps
  ([provider guide](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-1/cloud-provider-guide.html)):
  1. The provider **registers** a key provider and links it to one vCenter.
  2. The provider **publishes** it to one or more organizations. For each
     organization the provider types a "Name in vCenter". It must be unique
     and cannot change later
     ([publish](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-1/cloud-provider-guide/publish-key-provider-to-a-tenant.html)).
- What the service needs before it works
  ([before you begin](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-1/cloud-provider-guide/before-you-begin.html)):
  - vCenter has a default standard key provider, and storage policies with
    encryption.
  - VCF Automation 9.1 or later, with storage classes that have encryption on.
  - vCenter can reach the key provider. The key provider is on the VMware
    compatibility list for key servers. It **cannot** be the vSphere Native
    Key Provider (the key provider built into vCenter).
- The key servers listen on port 5696 by default. More servers can be added
  for high availability. They must share the same keys
  ([register](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-1/cloud-provider-guide/register-key-provider-with-vmware-cloud-director-encryption-management.html)).
- The bill of materials lists "Encryption Management" in the "VCF Services"
  table, included in the VCF SKU:
  - VCF 9.1.0: version `9.1.0.0`, build `25370929`
    ([BOM 9.1.0](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-1/release-notes/vmware-cloud-foundation-9-1-0-0-release-notes/vmware-cloud-foundation-bill-of-materials.html)).
  - VCF 9.1.1: "Encryption Management (VCF service)", version `9.1.1.0`,
    build `25714559`
    ([BOM 9.1.1](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-1/release-notes/vmware-cloud-foundation-9-1-1-0-release-notes/vmware-cloud-foundation-9-1-1-0-bill-of-material.html)).
- It comes from the VMware Cloud Director Encryption Management add-on
  (versions 1.x). VCF Automation is built on Cloud Director code. The page
  names still say so: the 9.1 "register" page file is
  `register-key-provider-with-vmware-cloud-director-encryption-management.html`.
  The older add-on is described in a
  [VMware blog](https://blogs.vmware.com/cloudprovider/2023/12/vmware-cloud-director-encryption-management-service-byok-byokms.html)
  and the
  [1.1 tenant guide](https://techdocs.broadcom.com/us/en/vmware-cis/cloud-director/encryption-management/1-1-0/vmware-cloud-director-encryption-management-documentation/tenant-guide/tenant-guide.html).
- Where it runs: a Broadcom article calls it a "VCF Automation service" for
  VCF Automation 9.1.0.0 and 9.1.1.0. It keeps its data as VCF Automation
  defined entities (records with a type that a service adds to VCF
  Automation) and adds a global role
  ([KB 453499](https://knowledge.broadcom.com/external/article/453499)). A
  community post shows an `encryption-manager` pod in the `prelude`
  namespace of the VCF Automation appliance
  ([VCF Automation 9.1 anatomy](https://adrian.heissler.at/2026/08/vcf-automation-9-1-anatomy/)).
  That post is not a vendor source.
- **Not like the Secret Store.** The Secret Store runs on a Supervisor and
  has a package and Kubernetes objects. The Encryption Management service runs
  inside VCF Automation. The vendor names no Kubernetes object for it, so the
  Kubernetes connector (`k8s`) has nothing service-specific to do.

## Where the API reference is

| Source | What it has for Encryption Management |
|---|---|
| `vmware/vcf-api-specs`, tags `9.1.0.0` and `9.1.1.0` | No file for the service. A search of every spec file in both tags finds no "encryption management", "encryption-manager" or "BYOK" text. There is no VCF Automation file in either tag. |
| `vmware/vcf-api-specs`, `specifications/vsphere/openapi/automation/vcenter.yaml` (both tags) | vCenter's own key provider API under `/api/vcenter/crypto-manager/kms/providers` (details below). Not this service. |
| `vmware/vcf-api-specs`, `specifications/vsphere/openapi/vi-json/vi-json.yaml` (both tags) | The vCenter object `CryptoManagerKmip`, which holds the standard (KMIP) key providers. This is where the service's key providers live in vCenter. |
| `vmware/vcf-api-specs`, `specifications/vcf-operations/vcf-operations-openapi.json` | No Encryption Management path. In `9.1.1.0` only, the fleet certificate search can filter on the categories `KMS_CLIENT_CERT` and `KMS_SERVER_CERT` (see "Notes"). |
| [Broadcom developer portal](https://developer.broadcom.com/xapis) | No reference for Encryption Management. The "CryptoManager" pages there are the vSphere key manager objects, not this service. |
| Broadcom TechDocs, [9.1 provider guide](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-1/cloud-provider-guide.html) | Six pages: before you begin, register, publish, unpublish, edit and remove a key provider. All are steps in the provider portal (**VCF Services** > **Encryption**). None names an API. There is no 9.1 page for the organization side. |
| [Service API Reference](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-1/vcf-services-development-guide/lifecycle-api.html), in the 9.1 VCF Services Development Guide | The VCF Automation Service Manager API. It lists and checks **any** VCF service. This is the API MEHO uses for the service's health and version. |

## The vCenter key paths: what they are

The issue asks if the vCenter paths below are this service. They are not.
They are older vCenter features. Encryption Management uses the KMIP part of
vCenter, not these paths.

- `/api/vcenter/crypto-manager/kms/providers` (list, get, create, update,
  delete, export, import) and `/api/vcenter/crypto-manager/hosts/{host}/kms/providers`.
  Added in vSphere API 7.0.2. The provider `type` has one value only:
  `NATIVE`, the vSphere Native Key Provider (`vcenter.yaml`, both tags). The
  service **cannot** use a Native Key Provider (see "before you begin"
  above). The list also gives a `health` value (`NONE`, `OK`, `WARNING`,
  `ERROR`).
- `/api/vcenter/trusted-infrastructure/...`: vSphere Trust Authority. It has
  key paths for trusted clusters
  (`/api/vcenter/trusted-infrastructure/trust-authority-clusters/{cluster}/kms/providers`)
  and attestation. It is a separate feature for trusted hosts.
- The standard (KMIP) key providers are in `vi-json.yaml`, under
  `/sdk/vim25/{release}/CryptoManagerKmip/{moId}/...`. The calls MEHO needs
  are `ListKmsClusters`, `RetrieveKmipServersStatus_Task` and
  `GetDefaultKmsCluster`. They need the `Cryptographer.ManageKeyServers`
  privilege.
- Both `vcenter.yaml` and `vi-json.yaml` paths can already be ingested as
  generic operations on `vmware-rest`. By the VMware rule (typed operations
  for the usual work, no new generic ones), the reads below are typed.

## How to log in

- **VCF Automation Service Manager API.** It uses the provider login of VCF
  Automation: `POST /cloudapi/1.0.0/sessions/provider/` with HTTP Basic. The
  answer header `x-vmware-vcloud-access-token` carries the token. Later calls
  send `Authorization: Bearer <token>`
  ([Service API Reference](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-1/vcf-services-development-guide/lifecycle-api.html)).
  The VCF Automation connector (implementation `vcfa-rest`) already logs in
  this way. See `docs/codebase/connectors-vcf-automation.md`, part
  "Provider-plane session establishment".
- **Not confirmed: the `Accept` header.** The vendor page gives
  `Accept: application/json;version=41.0.0-alpha`. The connector sends a
  different version today. The Configuration service record has the same
  open point. One check in the lab answers both.
- **vCenter.** The vSphere connector logs in as it does today. The key
  provider reads need `Cryptographer.ManageKeyServers` (KMIP calls in
  `vi-json.yaml`). Nothing new is needed for the login.

## How to detect the version

- `GET /api/extension/broadcom/service-manager/v2/vcf-services` returns each
  VCF service with `name`, `version`, `status`, `lifecycleState` and `phase`.
  A FIQL filter (a small query language, for example
  `lifecycleState==active`) can pick services
  ([Service API Reference](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-1/vcf-services-development-guide/lifecycle-api.html)).
- The expected versions are `9.1.0.0` on VCF 9.1.0 and `9.1.1.0` on VCF 9.1.1
  (bill of materials, see above).
- **Not confirmed:** that the Encryption Management service shows in this
  list. It is pre-installed and is shown in the provider portal under
  **VCF Services**, so we expect it there. Its `name` in the list is not
  published either. The connector Task reads both in a lab.
- The service has no version call of its own.

## What operators do with the service every week

The vendor pages list these tasks. That they happen weekly is our reading,
not a vendor statement.

- Check that the service is healthy after an upgrade. A Broadcom article
  describes the service going unhealthy after an upgrade or a second install
  ([KB 453499](https://knowledge.broadcom.com/external/article/453499)).
- Check that vCenter can still reach each key provider. If it cannot, new
  encryption and work on encrypted objects can fail.
- See which key providers exist, which vCenter each one is linked to, and
  which is the vCenter default.
- Check the key server certificates before they expire.
- Rare: register a new key provider, publish it to a new organization,
  change its servers, unpublish or remove it. After a change of the server
  details, every organization must log in to the key provider again
  ([edit](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-1/cloud-provider-guide/edit-key-provider.html)).

## The usual work

Risk levels: a **read** is `safe` and runs at once. All operations below are
reads. The ids are proposals. The connector Task picks the final names.

| Operation (proposed id) | Connector | What it does | Vendor call | Risk |
|---|---|---|---|---|
| `vcfa.provider.vcf_service.list` | VCF Automation (`vcfa-rest`) | All VCF services, with name, version, status, lifecycle state and phase. Finds the Encryption Management service. Shared with the Configuration service record. | `GET /api/extension/broadcom/service-manager/v2/vcf-services` ([Service API Reference](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-1/vcf-services-development-guide/lifecycle-api.html)) | read |
| `vcfa.provider.vcf_service.state` | VCF Automation (`vcfa-rest`) | The health of each part of the service (`Healthy`, `Busy`, `Unhealthy` or `Maintenance`) and its error messages. Shared with the Configuration service record. | `GET /api/extension/broadcom/service-manager/v2/vcf-services/{id}/state` (same page) | read |
| `vmware.key_provider.list` | `vmware-rest` | The standard key providers in one vCenter: id, servers (address and port), management type, and which one is the default. | `POST /sdk/vim25/{release}/CryptoManagerKmip/{moId}/ListKmsClusters` and `.../GetDefaultKmsCluster` (`vi-json.yaml`, 9.1 tags) | read |
| `vmware.key_provider.status` | `vmware-rest` | For each key provider: the overall status, and for each key server whether vCenter can connect and whether both sides trust each other's certificate. | `POST /sdk/vim25/{release}/CryptoManagerKmip/{moId}/RetrieveKmipServersStatus_Task`, with the key providers from `ListKmsClusters`. It starts a vCenter task. The read waits for the task and returns its result (`vi-json.yaml`, 9.1 tags). | read |

**Notes**

1. **Not confirmed: that "Name in vCenter" is the key provider id in
   vCenter.** The publish page asks for one unique "Name in vCenter" for each
   organization. We expect that the service makes one vCenter key provider
   (KMIP cluster) with that name for each organization. Then
   `vmware.key_provider.list` shows which organization each key provider
   serves. The connector Task checks this in a lab.
2. **Not confirmed: the `moId` of `CryptoManagerKmip`.** The read takes it
   from the vCenter service content (`cryptoManager`). The connector Task
   checks this on vCenter 9.1.
3. **`RetrieveKmipServersStatus_Task` starts a task but changes nothing.**
   It only asks the key servers for their status. That is why it is a read.
   **Not confirmed:** the shape of the task result. The spec only says the
   call returns a task. We expect a list of `CryptoManagerKmipClusterStatus`
   (fields `overallStatus`, `servers` with `connectionStatus`,
   `clientTrustServer`, `serverTrustClient` and `certInfo`). The connector
   Task checks this on vCenter 9.1.
4. **Key server certificates.** On VCF 9.1.1, the VCF Operations fleet
   certificate search
   (`POST /suite-api/api/fleet-management/certificate-management/certificates/query`,
   `vcf-operations-openapi.json`, tag `9.1.1.0`) takes the categories
   `KMS_CLIENT_CERT` and `KMS_SERVER_CERT`. The 9.1.0 file does not have these
   categories. This read belongs to the Task that brings fleet certificate
   reads to the VCF Operations connector. This record does not add it.
5. Long lists go back as a link to the stored result, like every list in
   MEHO.

## Why not something else

- **Its own connector.** There is no API of its own to call. A new connector
  would only wrap the APIs above, which MEHO already logs in to.
- **The service's own data in VCF Automation.** The service keeps its data
  as VCF Automation defined entities
  ([KB 453499](https://knowledge.broadcom.com/external/article/453499)). The
  vendor does not publish their type or fields, and no page tells operators
  to use them. Using them would tie MEHO to an inside detail that can change
  in any release.
- **Making the changes straight in vCenter.** vCenter has calls to add,
  change and remove KMIP key providers (for example `RegisterKmsCluster` and
  `RemoveKmipServer` in `vi-json.yaml`). But the service owns these key
  providers. A change behind its back would make the service and vCenter
  disagree. Unpublish also re-encrypts the organization's objects with the
  vCenter default key provider
  ([unpublish](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-1/cloud-provider-guide/unpublish-key-provider.html)).
  Only the service does that. So MEHO only reads in vCenter.
- **The Native Key Provider paths** (`/api/vcenter/crypto-manager/...`). The
  service cannot use a Native Key Provider, so these paths do not show its
  work.
- **VCF Operations.** The VCF Operations 9.1 files, including the
  `/suite-api/api/fleet-management/...` paths, have no Encryption Management
  path. Only the certificate categories in note 4 touch this area.
- **The Kubernetes connector (`k8s`).** The service runs inside VCF
  Automation, not on a Supervisor, and has no vendor Kubernetes object.

## Out of scope (scope exception asked)

- Register, publish, unpublish, edit and remove a key provider. No public
  API. These are also rare.
- The organization side: log in to the key provider, pick keys, set key
  rotation, and pick which storage uses the key. No public API, and no 9.1
  page for these steps.

**Request to the program owner:** please agree that, for the Encryption
Management service, MEHO covers only the four reads above (service health
and version, and the key providers in vCenter). Please agree a scope
exception for the changes, until Broadcom publishes an API for the service.

## What happens next

1. The program owner agrees this record, and the scope exception, in the pull
   request.
2. A connector Task adds the two `vmware-rest` key provider reads. The two
   VCF Automation reads come from the same Task as the Configuration service
   reads, so they are built once. The Task first checks every **not
   confirmed** fact in a lab with VCF Automation 9.1 and a key provider.
