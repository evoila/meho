# How MEHO works with the VCF 9.1 Metrics Aggregator service (decision)

**Status:** proposed. It waits for the program owner to agree in the pull request.
**Date:** 2026-10-23
**Issue:** [#4512](https://github.com/evoila/meho/issues/4512)
**Initiative:** [#4306](https://github.com/evoila/meho/issues/4306) — VCF bring-up, fleet lifecycle, identity and platform services through MEHO

## The decision in short

- The Metrics Aggregator is one of the **VCF services** that ship with VCF
  9.1. A VCF service is a package that VCF Automation installs and keeps
  running on a vSphere **Supervisor**. (A Supervisor is the Kubernetes control
  plane that vSphere runs inside a cluster.)
- It is a **pass-through for metrics**. Telegraf (a metrics agent) runs in
  each VKS cluster and sends metrics to the Metrics Aggregator on the
  Supervisor. The Metrics Aggregator sends them on to the VCF Operations
  cloud proxy, and so to VCF Operations. (A VKS cluster is a Kubernetes
  cluster that the Supervisor makes.)
- It is **not** the Real-Time Metrics API of VCF Operations. Real-Time Metrics
  is a different VCF Operations part, with its own OpenAPI file. It collects
  from vCenter, ESX, NSX and vSAN, not from VKS clusters.
- The service has **no API of its own** that MEHO can call. There is no file
  for it in `vmware/vcf-api-specs` (tags `9.1.0.0` and `9.1.1.0`), no
  reference on the Broadcom developer portal, and no TechDocs page gives a
  path for it.
- MEHO can still check **its health, its version and whether the metrics
  flow**, through connectors MEHO already has:
  1. **The VCF Automation Service Manager API**
     (`/api/extension/broadcom/service-manager/v2/vcf-services`), on the
     `vcf-automation` connector. These are the same reads the Configuration
     service record proposes. They work for every VCF service.
  2. **vCenter REST, Supervisor services part**, on the `vmware-rest`
     connector. Also the same read the other records propose.
  3. **The Kubernetes API of the Supervisor**, on the `k8s` connector. The
     existing reads show the service's load balancer, its pods and its logs,
     and which VKS clusters have monitoring turned on.
  4. **VCF Operations**, on the existing VCF Operations connector. The
     metrics that pass through the service end up there. MEHO reads them with
     the existing metric reads.
- So the service needs **no connector of its own**, and no new operation that
  only this service uses.
- **We ask the program owner for a scope exception** for everything beyond
  health, version and the checks above. The vendor gives no API to change
  the service's settings.
- Some facts are **not confirmed** and are marked below. The connector Task
  checks them in a lab before the operations ship.

## What the service is

- The VCF 9.1.0 bill of materials lists "Metrics Aggregator Package",
  version `9.1.0.0`, build `25317134`, in the "VCF Services" table, included
  in the VCF SKU
  ([BOM 9.1.0](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-1/release-notes/vmware-cloud-foundation-9-1-0-0-release-notes/vmware-cloud-foundation-bill-of-materials.html)).
- The VCF 9.1.1 bill of materials lists "Metrics Aggregator (VCF service)",
  version `9.1.1.0`, build `25665407`
  ([BOM 9.1.1](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-1/release-notes/vmware-cloud-foundation-9-1-1-0-release-notes/vmware-cloud-foundation-9-1-1-0-bill-of-material.html)).
- The VKS monitoring page says: "VCF 9.1 adds a metrics-aggregator Supervisor
  service that can be installed and managed as a VCF Service." It is
  "installed by default with VCF Automation", and the provider can check it
  in the VCF Automation provider portal
  ([Monitoring VKS clusters using VCF Operations](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-consumption/latest/managing-vsphere-kuberenetes-service-clusters-and-workloads/operating-tkg-service-clusters/monitoring-vks-clusters-using-vcf-operations.html),
  [Enabling Monitoring for VKS Clusters](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-1/infrastructure-operations/connect-to-data-sources/vsphere-supervisor-monitoring/steps-to-monitor-vsphere-supervisor-clusters-and-resources/prerequisites-for-vsphere-supervisor-monitoring.html)).
- What it does, from the same two pages:
  - It must run on the Supervisor before VCF Operations can monitor VKS
    clusters.
  - It "terminates the TLS connection from the VKS Cluster". (TLS is the
    encryption on the network link.) It uses certificates from the cloud
    proxy to talk to the cloud proxy.
  - Telegraf in a VKS cluster sends to a load balancer called
    `workload-metrics-loadbalancer` in the Metrics Aggregator service
    namespace. For older clusters the operator sets this address by hand
    (`endpointIP`, `endpointPort`).
  - VCF Automation installs Telegraf and Prometheus in VKS clusters through
    the VKS add-on system (`AddonRepository`, `Addon` and
    `AddonConfigDefinition` objects on the Supervisor). A cluster label
    `addons.kubernetes.vmware.com/automated-monitoring` (`Enabled` or
    `Disabled`) turns monitoring on or off per cluster.
- A vendor guide for offline (air-gapped) setups lists "Metrics Aggregator"
  as a Standard Supervisor service, version `0.1.0`, that can be downloaded
  from the Broadcom support portal
  ([air-gapped VCF 9.1 guide](https://github.com/vmware/vsphere-supervisor/blob/main/airgapped/air-gapped-vcf91.md)).
  This version is not the bill of materials version. We do not know how the
  two relate.
- The service has **no package** in the Broadcom package repository folder
  `packages.broadcom.com/artifactory/vsphere-distro/vsphere/iaas/` (checked
  on 2026-10-23). The page "Managing VCF Services" in the 9.1 service guide
  does not name it
  ([Managing VCF Services](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-1/using-solution-add-ons.html)).

### Not the same thing as Real-Time Metrics

- Real-Time Metrics and the real-time metrics store are listed in the
  **VCF Operations** table of the bill of materials (9.1.0: version
  `9.1.0.0`, build `25346020`), not in the VCF Services table
  ([BOM 9.1.0](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-1/release-notes/vmware-cloud-foundation-9-1-0-0-release-notes/vmware-cloud-foundation-bill-of-materials.html)).
- The operator adds Real-Time Metrics from VCF Operations (Build > Lifecycle >
  VCF Management > Add Components). It "collects performance data directly
  from" vCenter, ESX, NSX and vSAN
  ([Deploy Real-Time Metrics](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-1/deployment/deploying-a-new-vmware-cloud-foundation-or-vmware-vsphere-foundation-private-cloud-/manual-deployment-of-components-to-complete-your-vcf-platform/deploying-vcf-operations-for-real-time-data-service.html)).
  The page does not name the Metrics Aggregator.
- Its API is in `vmware/vcf-api-specs`, file
  `specifications/vcf-operations/realtime-metrics/realtime-metrics-openapi.yaml`
  (both tags). It has four paths: `GET /api/v1/query`,
  `GET /api/v1/query_range`, `GET /api/v1/metadata` (PromQL queries, a query
  language for metrics) and `PUT /api/v1/vcenters/{vcId}/metrics_config`.
  The file does not name the Metrics Aggregator.
- So Real-Time Metrics belongs to the VCF Operations connector, in its own
  record. It is out of scope here.

## Where the API reference is

| Source | What it has for the Metrics Aggregator |
|---|---|
| `vmware/vcf-api-specs`, tags `9.1.0.0` and `9.1.1.0` | Nothing. Both tags have the same spec files (VCF Operations and its parts, Real-Time Metrics, fleet lifecycle, SDDC Manager, SDDC lifecycle, VCF Installer, NSX, vSAN data protection, vCenter `vcenter.yaml` and `vi-json.yaml`). A search of all of them finds "aggregator" only once, in `vi-json.yaml`, about ESX hosts. The fleet lifecycle file has a component type `VCFMS_METRICS_STORE`. No vendor page links it to the Metrics Aggregator. There is no VCF Automation file in either tag. |
| `vmware/vcf-api-specs`, `vcenter.yaml` (both tags) | The Supervisor services API under `/api/vcenter/namespace-management/...`. It is the same API for every Supervisor service. |
| [Broadcom developer portal](https://developer.broadcom.com/xapis) | No reference for the Metrics Aggregator. |
| [Service API Reference](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-1/vcf-services-development-guide/lifecycle-api.html), in the 9.1 VCF Services Development Guide | The VCF Automation Service Manager API. It lists, checks, changes and removes **any** VCF service. This is the API MEHO uses for health and version. |
| Broadcom TechDocs, VKS monitoring pages (linked above) | What the service does, the load balancer name and the monitoring label. No REST path. |

## How to log in

- **VCF Automation Service Manager API.** It uses the provider login of VCF
  Automation: `POST /cloudapi/1.0.0/sessions/provider/` with HTTP Basic. The
  answer header `x-vmware-vcloud-access-token` carries the token. Later calls
  send `Authorization: Bearer <token>`
  ([Service API Reference](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-1/vcf-services-development-guide/lifecycle-api.html)).
  The `vcf-automation` connector already logs in this way (see
  `docs/codebase/connectors-vcf-automation.md`). Nothing new is needed.
- **Not confirmed: the `Accept` header.** The vendor page gives
  `Accept: application/json;version=41.0.0-alpha`. The connector Task checks
  which value works. This is the same open point as in the Configuration
  service record.
- **vCenter REST:** the vSphere connector logs in as it does today. The list
  call needs `System.Read` on the Supervisor (`vcenter.yaml`, operation
  `Vcenter.NamespaceManagement.Supervisors.SupervisorServices_list`).
- **Supervisor Kubernetes API:** the Kubernetes connector logs in to the
  Supervisor as it does today (see `docs/codebase/connectors-kubernetes.md`,
  part "vSphere Supervisor (WCP) SSO auth mode").
- **VCF Operations:** the VCF Operations connector logs in as it does today.

## How to detect the version

- `GET /api/extension/broadcom/service-manager/v2/vcf-services` returns each
  VCF service with `name`, `version`, `status`, `lifecycleState` and `phase`.
  `GET .../vcf-services/{id}/state` gives the health of each part (`Healthy`,
  `Busy`, `Unhealthy` or `Maintenance`)
  ([Service API Reference](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-1/vcf-services-development-guide/lifecycle-api.html)).
- The expected versions are `9.1.0.0` on VCF 9.1.0 and `9.1.1.0` on VCF 9.1.1
  (bill of materials, see above).
- **Not confirmed:** the `name` of the Metrics Aggregator in this list. The
  vendor uses names in a reverse-domain form (for example
  `arcturus.galaxy.com` in the guide). The name for this service is not
  published.
- **Not confirmed:** that the service shows in the vCenter list
  `GET /api/vcenter/namespace-management/supervisors/{supervisor}/supervisor-services`,
  and with which version (`0.1.0` from the offline guide, or `9.1.x.0`). The
  vendor calls it a "Supervisor service", so it likely shows there.
- The service has no version call of its own.

## What operators do with the service every week

The vendor does not describe weekly work. The list below is our reading of
the vendor pages.

- Check that the service is healthy on each Supervisor.
- After a VCF upgrade, check that it has the version of the new bill of
  materials.
- When VKS cluster metrics are missing in VCF Operations, find out where
  they stop:
  - Is the service healthy?
  - Does the load balancer `workload-metrics-loadbalancer` have an address?
  - Do the service's pods run, and what do their logs say?
  - Does the VKS cluster have monitoring turned on (the label)?
- Look at the VKS cluster metrics themselves, in VCF Operations.

## The usual work

Risk levels: a **read** is `safe` and runs at once. All operations below are
reads. The ids marked "new" are proposals; the connector Task picks the final
names. The other ids exist in MEHO today.

| Operation | Connector | What it does | Vendor call | Risk |
|---|---|---|---|---|
| `vcfa.provider.vcf_service.list` (new, shared with the other VCF service records) | `vcf-automation` | All VCF services: name, version, status, lifecycle state, phase. MEHO filters for the Metrics Aggregator. | `GET /api/extension/broadcom/service-manager/v2/vcf-services` ([Service API Reference](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-1/vcf-services-development-guide/lifecycle-api.html)). | read |
| `vcfa.provider.vcf_service.state` (new, shared) | `vcf-automation` | The health of each part of the service, and the error messages. | `GET /api/extension/broadcom/service-manager/v2/vcf-services/{id}/state` (same page). | read |
| `vmware.composite.supervisor_service.list` (new, shared) | `vmware-rest` | The services on one Supervisor: id, current and desired version, state. | `GET /api/vcenter/namespace-management/supervisors/{supervisor}/supervisor-services` (`vcenter.yaml`, 9.1 tags). Whether the Metrics Aggregator shows here is **not confirmed**. | read |
| `k8s.service.list` (exists) | `k8s` | The load balancer `workload-metrics-loadbalancer` in the service namespace, with its address and port. | Kubernetes list of `Service` objects in one namespace. The namespace name is **not confirmed**. | read |
| `k8s.pod.list`, `k8s.logs` (exist) | `k8s` | The service's pods and their logs. | Kubernetes list of `Pod` objects, and pod logs. | read |
| `k8s.cr.list` (exists) | `k8s` | Which VKS clusters have monitoring turned on: the `addons.kubernetes.vmware.com/automated-monitoring` label on each `Cluster` object (`cluster.x-k8s.io`). | Kubernetes list of `Cluster` objects. | read |
| `vrops.resource.stats` (exists) | VCF Operations | The VKS cluster metrics that came through the service. | The existing VCF Operations metric read under `/suite-api/api/...`. Which object kinds and metric keys VKS metrics use is **not confirmed**. | read |

**Notes**

1. **Most of this already exists.** Only the three shared reads are new. The
   Configuration and Encryption Management records already ask for them, and
   the Secret Store record plans a similar vCenter read. This record adds no
   operation of its own.
2. **The `{id}` is a long name** like
   `urn:vcloud:entity:broadcom:vcfService:<uuid>`. The agent gets it from the
   list first.
3. **A runbook may help later.** "Why are my VKS metrics missing" walks the
   reads above in order. It can be a runbook (a stored list of steps), not a
   new operation. That is a later choice, not part of this record.

## Why not something else

- **Its own connector.** There is no API of its own to call. A new connector
  would only wrap APIs MEHO already logs in to.
- **The Real-Time Metrics API.** It is a different VCF Operations part (see
  above). It does not read or manage the Metrics Aggregator.
- **Calling the load balancer directly.** It takes metrics from Telegraf. It
  is not an API for operators, and the vendor gives no paths for it.
- **Changes through the Service Manager API** (`PATCH` or `PUT` on
  `.../vcf-services/{id}`). The service comes pre-installed, and the vendor
  gives no settings for it. VKS monitoring depends on it.
- **Turning monitoring on or off for a VKS cluster.** The existing
  `k8s.label` operation can change the label. That is VKS cluster work, not
  Metrics Aggregator work, so it is not listed here.

## Out of scope (scope exception asked)

- Changing the service's settings. No public API and no vendor settings.
- Installing, upgrading or removing the service. It comes pre-installed with
  VCF Automation, and VCF upgrades it.

**Request to the program owner:** please agree that, for the Metrics
Aggregator, MEHO covers only health, version and the checks in the table
above. Please agree a scope exception for the rest, until Broadcom documents
an API for the service.

## What happens next

1. The program owner agrees this record, and the scope exception, in the pull
   request.
2. The connector Task for the shared VCF service reads (`vcf-automation` and
   `vmware-rest`) also checks, on a lab VCF Automation 9.1, every **not
   confirmed** fact here: the service name in the list, whether it shows in
   vCenter and with which version, its namespace, and how VKS metrics show
   in VCF Operations.
