# How MEHO works with the VCF 9.1 Configuration service (decision)

**Status:** proposed. It waits for the program owner to agree in the pull request.
**Date:** 2026-10-23
**Issue:** [#4510](https://github.com/evoila/meho/issues/4510)
**Initiative:** [#4306](https://github.com/evoila/meho/issues/4306) — VCF bring-up, fleet lifecycle, identity and platform services through MEHO

## The decision in short

- The Configuration service is one of the **VCF services** that ship with
  VCF 9.1. A VCF service is a package that VCF Automation installs and keeps
  running on one or more vSphere Supervisors. (A Supervisor is the Kubernetes
  control plane that vSphere runs inside a cluster.)
- The vendor does **not** say what the service does. No public page describes
  it. It only appears in the bill of materials and in a list of services that
  come pre-installed.
- The service has **no API of its own** in the vendor references. There is no
  file for it in `vmware/vcf-api-specs` (tags `9.1.0.0` and `9.1.1.0`), no
  reference on the Broadcom developer portal, and no TechDocs page.
- MEHO can still check **its health and its version**, through two APIs:
  1. **The VCF Automation Service Manager API** (`/api/extension/broadcom/service-manager/v2/...`).
     It lists every VCF service with its version, state and errors. These
     reads go on the existing VCF Automation connector (`vcf-automation`), as
     typed (hand-written) operations.
  2. **vCenter REST, Supervisor services part.** It lists the services on one
     Supervisor, with version and state. This read goes on the vSphere
     connector (`vmware-rest`).
- So the service needs **no connector of its own**.
- **We ask the program owner for a scope exception** for everything beyond
  health and version. We cannot read or change the configuration the service
  keeps: there is no public API and no vendor guidance for it.
- Some facts are **not confirmed** and are marked below. The connector Task
  checks them on a lab VCF Automation before the operations ship.

## What the service is

- The VCF 9.1.0 bill of materials lists "Configuration Service", version
  `9.1.0.0`, build `25303350`, in the "VCF Services" table, included in the
  VCF SKU
  ([BOM 9.1.0](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-1/release-notes/vmware-cloud-foundation-9-1-0-0-release-notes/vmware-cloud-foundation-bill-of-materials.html)).
  The same table has Secret Store, Migration service engine, VKS Cluster
  Management, DSM Data Services, Encryption Management, Metrics Aggregator,
  Protection and Recovery and Harbor.
- The VCF 9.1.1 bill of materials lists "Configuration Service (VCF service)",
  version `9.1.1.0`, build `25670523`
  ([BOM 9.1.1](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-1/release-notes/vmware-cloud-foundation-9-1-1-0-release-notes/vmware-cloud-foundation-9-1-1-0-bill-of-material.html)).
- The VCF 9.1 feature comparison lists it under "Services Framework
  (Pre-bundled Services: Harbor, DSM, Secret Store Service, VKSM Auto-Attach
  Service, Encryption Management (BYOK), Multi-tenant DR (VLR), Metrics
  Aggregator Service, Configuration Service, VCD Migration Assistant, Lumos
  In-Product Feedback Service)"
  ([feature comparison PDF](https://www.vmware.com/docs/vmware-cloud-foundation-9-1-feature-comparison-and-upgrade-paths)).
- A community setup guide lists "Configuration Service" as one of the extra
  Supervisor namespaces after VCF Automation is set up, next to Metrics
  Aggregator and VKS Cluster Management
  ([VCF 9.1 Automation All Apps Setup](https://amayacitta.co.uk/vcf-9-1-automation-all-apps-setup/)).
  This is not a vendor source.
- **Not found anywhere:** what the service does, who uses it, or which
  settings it keeps. The issue describes it as the service that "keeps the
  configuration of VCF 9.1 services". No vendor page confirms this.
- **Not like the Secret Store.** The Secret Store has a vendor guide and a
  package in the Broadcom package repository
  (`packages.broadcom.com/artifactory/vsphere-distro/vsphere/iaas/`). The
  Configuration service has neither. On 2026-10-23 that repository folder
  holds `contour`, `external-dns`, `harbor`, `kubernetes-service`,
  `lci-service`, `minio`, `nsx-management-proxy`, `secret-store`,
  `supervisor-management-proxy` and `velero-vsphere`. The 9.1 service guide
  has no chapter for it, and the VCF services page in the provider portal
  guide does not name it
  ([9.1 service guide](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-1.html),
  [Managing VCF Services](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-1/using-solution-add-ons.html)).
  The community catalog `vsphere-tmm/Supervisor-Services` does not list it
  either.

## Where the API reference is

| Source | What it has for the Configuration service |
|---|---|
| `vmware/vcf-api-specs`, tags `9.1.0.0` and `9.1.1.0` | Nothing. Both tags have the same 16 YAML and JSON spec files (VCF Operations and its parts, fleet lifecycle, SDDC Manager, SDDC lifecycle, VCF Installer, NSX, vSAN data protection, vCenter `vcenter.yaml` and `vi-json.yaml`). A search of all of them finds no "configuration service" text. The fleet lifecycle file lists the fleet component types (`SALT_RAAS`, `TELEMETRY_ACCEPTOR`, `VCFMS_METRICS_STORE`, `VCD_MIGRATOR` and others); the Configuration service is not one of them. The VCF Operations fleet paths (`/suite-api/api/fleet-management/...`) cover certificates, identity, passwords and tags only. There is no VCF Automation file in either tag. |
| `vmware/vcf-api-specs`, `vcenter.yaml` (both tags) | The Supervisor services API under `/api/vcenter/namespace-management/...`. It is the same API for every Supervisor service. |
| [Broadcom developer portal](https://developer.broadcom.com/xapis) | No reference for the Configuration service. No reference for the VCF Automation Service Manager either. The "configuration" APIs there belong to other products (for example Site Recovery). |
| [Service API Reference](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-1/vcf-services-development-guide/lifecycle-api.html), in the 9.1 VCF Services Development Guide | The VCF Automation Service Manager API. It installs, changes, checks and removes **any** VCF service. The same text is in the vendor repository [`vmware/vcf-services`](https://github.com/vmware/vcf-services), file `documentation/lifecycle/api.md`. This is the API MEHO uses for health and version. |

Not the same thing: SDDC Manager has `GET /v1/vcf-services` in
`sddc-manager-openapi.json`. Its summary is "Retrieve a list of SDDC Manager
services". It lists the services inside SDDC Manager, not VCF services on a
Supervisor.

## How to log in

- **VCF Automation Service Manager API.** It uses the provider login of VCF
  Automation: `POST /cloudapi/1.0.0/sessions/provider/` with HTTP Basic. The
  answer header `x-vmware-vcloud-access-token` carries the token. Later calls
  send `Authorization: Bearer <token>`
  ([Service API Reference](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-1/vcf-services-development-guide/lifecycle-api.html)).
  The `vcf-automation` connector already logs in this way (see
  `docs/codebase/connectors-vcf-automation.md`, part "Provider-plane session
  establishment"). Nothing new is needed for the login.
- **Not confirmed: the `Accept` header.** The vendor page gives
  `Accept: application/json;version=41.0.0-alpha` for this API. The connector
  sends `application/*+json;version=40.0` for paths under `/api/*` today. The
  word "alpha" may mean the API can still change. The connector Task checks on
  a lab VCF Automation which value works, and that the connector sends
  `/api/extension/...` paths to the provider login.
- **vCenter REST** (the per-Supervisor read): the vSphere connector logs in as
  it does today. The list call needs `System.Read` on the Supervisor
  (`vcenter.yaml`, operation
  `Vcenter.NamespaceManagement.Supervisors.SupervisorServices_list`).

## How to detect the version

- `GET /api/extension/broadcom/service-manager/v2/vcf-services?page=1&pageSize=25`
  returns each VCF service with `name`, `version`, `status` (for example
  `Healthy`), `lifecycleState` (`active`, `inactive`, `paused` or
  `decommissioned`) and `phase`. A FIQL filter (a small query language, for
  example `name==*arcturus*`) can pick one service
  ([Service API Reference](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-1/vcf-services-development-guide/lifecycle-api.html)).
- The expected versions are `9.1.0.0` on VCF 9.1.0 and `9.1.1.0` on VCF 9.1.1
  (bill of materials, see above).
- **Not confirmed:** the `name` of the Configuration service in this list.
  The vendor gives names in a reverse-domain form (`arcturus.galaxy.com` in
  the example; `svc-auto-attach.vksm.broadcom.com` in
  [KB 441498](https://knowledge.broadcom.com/external/article/441498)). The
  Configuration service name is not published. The connector Task reads it on
  a lab VCF Automation.
- **Not confirmed:** that the Configuration service also shows in the vCenter
  list `GET /api/vcenter/namespace-management/supervisors/{supervisor}/supervisor-services`.
  The vendor says a VCF service element of kind `SupervisorService` "registers
  the package with the vCenter Supervisor API"
  ([`supervisor-service.md`](https://github.com/vmware/vcf-services/blob/main/documentation/element-types/supervisor-service.md)).
  We do not know if the Configuration service uses that element.
- **Not confirmed:** that the VCF Automation 9.0 appliance serves this API.
  The vendor page is in the 9.1 guide. The new operations start with VCF
  Automation 9.1.
- The service has no version call of its own.

## What operators do with the service every week

The vendor does not describe any work with this service. The list below is
our reading of what an operator does with any pre-installed VCF service.

- Check that the service is healthy and active in each region.
- After a VCF upgrade, check that the service has the version of the new bill
  of materials.
- When the service is not healthy, see which part of it fails and the error
  message.

## The usual work

Risk levels: a **read** is `safe` and runs at once. All operations below are
reads. The ids are proposals. The connector Task picks the final names.

| Operation (proposed id) | Connector | What it does | Vendor call | Risk |
|---|---|---|---|---|
| `vcfa.provider.vcf_service.list` | `vcf-automation` | All VCF services: name, version, status, lifecycle state, phase. Can filter by name. | `GET /api/extension/broadcom/service-manager/v2/vcf-services` with `page`, `pageSize` and optional `filter` ([Service API Reference](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-1/vcf-services-development-guide/lifecycle-api.html)). | read |
| `vcfa.provider.vcf_service.get` | `vcf-automation` | One VCF service: version, status, lifecycle state, phase, and the regions it runs in. | `GET /api/extension/broadcom/service-manager/v2/vcf-services/{id}` (same page). | read |
| `vcfa.provider.vcf_service.state` | `vcf-automation` | The health of each part of one VCF service (`Healthy`, `Busy`, `Unhealthy` or `Maintenance`) and the error messages. | `GET /api/extension/broadcom/service-manager/v2/vcf-services/{id}/state` (same page). | read |
| `vmware.composite.supervisor_service.list` | `vmware-rest` | The services on one Supervisor: id, current and desired version, state. | `GET /api/vcenter/namespace-management/supervisors/{supervisor}/supervisor-services` (`vcenter.yaml`, 9.1 tags). Whether the Configuration service shows here is **not confirmed** (see above). | read |

**Notes**

1. **These operations work for every VCF service, not only this one.** The
   records for the other VCF 9.1 services (Encryption Management, Metrics
   Aggregator, Migration service, Telemetry) can use the same three
   `vcf-automation` reads. The Secret Store record already plans a status read
   on the same vCenter call. The connector Task can build one operation that
   serves all of them.
2. **The `{id}` is a long name** like
   `urn:vcloud:entity:broadcom:vcfService:<uuid>`. The agent gets it from the
   list first.
3. **The answers may be large.** With `package=true`, `metadata=true` or
   `inventory=true` the answer carries whole files. The reads ask for these
   only when the agent wants them. A long list goes back as a link to the
   stored result.

## Why not something else

- **Its own connector.** There is no API of its own to call. A new connector
  would only wrap the two APIs above, which MEHO already logs in to.
- **The Kubernetes connector (`k8s`).** It can log in to a Supervisor and list
  the pods in the service's namespace. That is fine for a deeper look, and
  MEHO's existing `k8s.pod.list` and `k8s.logs` cover it. But the vendor names
  no object kind for this service, so there is nothing service-specific to
  build there.
- **VCF Operations.** The VCF Operations 9.1 files, including the
  `/suite-api/api/fleet-management/...` paths, have no Configuration service
  path.
- **Changes through the Service Manager API** (`PATCH` or `PUT` on
  `.../vcf-services/{id}`, for example new settings or an upgrade). The
  service comes pre-installed and the vendor gives no settings for it. A
  wrong change could break other VCF services. These stay out until the
  vendor documents the service.

## Out of scope (scope exception asked)

- Reading or changing the configuration the service keeps. No public API.
- Installing, upgrading or removing the service. It comes pre-installed with
  VCF Automation, and VCF upgrades it.

**Request to the program owner:** please agree that, for the Configuration
service, MEHO covers only health and version (the four reads above). Please
agree a scope exception for the rest, until Broadcom documents the service.

## What happens next

1. The program owner agrees this record, and the scope exception, in the pull
   request.
2. A connector Task adds the three `vcf-automation` reads and the
   `vmware-rest` read. It first checks every **not confirmed** fact on a lab
   VCF Automation 9.1.
