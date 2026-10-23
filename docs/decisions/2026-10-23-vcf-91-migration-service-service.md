# How MEHO works with the VCF 9.1 Migration Service (decision)

**Status:** proposed. It waits for the program owner to agree in the pull request.
**Date:** 2026-10-23
**Issue:** [#4513](https://github.com/evoila/meho/issues/4513)
**Initiative:** [#4306](https://github.com/evoila/meho/issues/4306) — VCF bring-up, fleet lifecycle, identity and platform services through MEHO

## The decision in short

- The Migration Service moves a **VMware Cloud Director** (VCD) estate into
  **VCF Automation** 9.1: its workloads and its configuration. The vendor also
  calls it "VCD Migrator", and calls its main part the "Migration service
  engine".
- It is **not** HCX (the VMware tool that moves single VMs between sites).
  HCX is its own row in the bill of materials ("VCF Operations HCX").
- It is **not** the upgrade from Aria Automation 8 to VCF Automation 9.1.
  That upgrade is a workflow of VCF Operations fleet lifecycle (see below).
- The service has **no public API** for the migration itself. There is no file
  for it in `vmware/vcf-api-specs` (tags `9.1.0.0` and `9.1.1.0`), no
  reference on the Broadcom developer portal, and no TechDocs page with a
  path. The vendor says to run the migration in the VCF Automation provider
  UI. The step-by-step guide is not public: Broadcom sends it on request.
- MEHO can still check **its health, its version and its upgrade tasks**,
  through three connectors MEHO already has. All are typed (hand-written)
  reads:
  1. **VCF Operations fleet lifecycle** (connector `fleet-lcm`). The engine is
     a managed component of type `VCD_MIGRATOR`. This gives version, state and
     upgrade tasks.
  2. **The VCF Automation Service Manager API** (connector `vcf-automation`).
     This gives the health of the Migration Service and its error messages.
  3. **VMware Cloud Director** (connector `vcd`). This reads the source estate
     before a migration. These reads exist today.
- So the service needs **no connector of its own**.
- **We ask the program owner for a scope exception** for the migration itself
  (connect to VCD, check the estate, run the steps). There is no public API
  and no public guide for it.
- Some facts are **not confirmed** and are marked below. The connector Task
  checks them on a lab VCF 9.1 before the operations ship.

## What the service is

- **Bill of materials.** The VCF 9.1.0 bill of materials lists "Migration
  service engine", version `9.1.0.0`, build `25370929`, in the "VCF Services"
  table, included in the VCF SKU
  ([BOM 9.1.0](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-1/release-notes/vmware-cloud-foundation-9-1-0-0-release-notes/vmware-cloud-foundation-bill-of-materials.html)).
  The VCF 9.1.1 bill of materials lists two rows with the same build: "Migration
  service engine (VCF service)" and "Migration Service (VCF service)", both
  version `9.1.1.0`, build `25714559`
  ([BOM 9.1.1](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-1/release-notes/vmware-cloud-foundation-9-1-1-0-release-notes/vmware-cloud-foundation-9-1-1-0-bill-of-material.html)).
  The issue title says "Migration service service". This record treats both
  rows as one product: the engine is the part that VCF Operations installs
  and upgrades, and "Migration Service" is the name in the VCF Automation
  provider UI. **Not confirmed:** the vendor does not explain why 9.1.1 has
  two rows.
- **What it moves.** Broadcom says the Migration Service "automates the
  transfer of VMware Cloud Director (VCD) workloads and configurations into
  VCF 9.1 with minimal downtime"
  ([VCF 9.1 for cloud providers blog](https://blogs.vmware.com/cloud-foundation/2026/05/12/introducing-vmware-cloud-foundation-9-1-to-vcsps/)).
  The VCF Automation 9.1 release notes call it the "Migration Tool" for "an
  in-place migration from VMware Cloud Director to VCF Automation"
  ([What's new in VCF Automation 9.1](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-1/release-notes/vmware-cloud-foundation-9-1-0-0-release-notes/what-s-new/whats-new-vcf-automation.html)).
- **What it needs.** The source must be VCD 10.6.2. The operator first runs the
  VCD Environment Assessment Tool, asks Broadcom for the migration guide by
  email, then connects the VCF Automation Migration Service to VCD
  ([VCD 10.6.2 blog](https://blogs.vmware.com/cloudprovider/2026/09/announcing-vmware-cloud-director-10-6-2-enabling-your-migration-to-vmware-cloud-foundation-9-1.html),
  [KB 455238](https://knowledge.broadcom.com/external/article/455238/how-can-we-migrate-to-vcf-91-from-vmware.html)).
- **Where it shows.** It has a tile in the VCF Automation provider UI. A
  Broadcom article describes that tile as "unhealthy" after an upgrade, with a
  health error of the VCF Automation service controller (`service-rde-v2`).
  The fix runs on a VCF Automation node
  ([KB 453702](https://knowledge.broadcom.com/external/article/453702/911error-servicesvcfabroadcomcomvcfaserv.html)).
  So the service runs with VCF Automation, not on a vSphere Supervisor. (A
  Supervisor is the Kubernetes control plane that vSphere runs inside a
  cluster.)
- **How it is installed and upgraded.** The engine binary comes from the
  software depot. VCF Operations installs and upgrades it, as a management
  component. The VCF Automation 9.1.1 release notes say: upgrade VCF
  Automation to 9.1.1 **before** the engine, or both upgrades block
  ([VCF Automation 9.1.1 release notes](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-1/release-notes/vmware-cloud-foundation-9-1-1-0-release-notes/vcfautomation-9-1-1-0-release-notes.html),
  [KB 455559](https://knowledge.broadcom.com/external/article/455559)).
  KB 455559 names the upgrade step `VCD_MIGRATOR`. A community walk-through
  shows the engine in VCF Operations under Build, Lifecycle, VCF Management,
  Components, with state "Running" and a version
  ([community upgrade guide](https://cosmin.us/upgrading-vcf-migration-service-engine-to-9-1-1-step-by-step/)).
  That page is not a vendor source.
- **Known limit.** VCF Automation 9.1 cannot move provider VDCs (pools of
  vCenter capacity in VCD) on one NSX Manager when they use more than one
  vCenter. The error code is `CANNOT_MIGRATE_PVDCS_ACROSS_MULTIPLE_VCENTERS`
  ([KB 451182](https://knowledge.broadcom.com/external/article/451182/cloud-director-to-vcf-automation-migrati.html)).

### Why it is not one of the other migration tools

- **HCX.** HCX is a separate product line: "VCF Operations HCX", version
  `9.1.0.0`, build `25318520`, in the main table of the bill of materials, and
  not in the VCF SKU (BOM 9.1.0 above). The Migration Service pages do not
  name HCX. HCX has its own API reference on the developer portal ("HCX
  Workload Migration APIs"). It is out of scope for this record.
- **Aria Automation 8 to VCF Automation 9.1.** Broadcom describes this as an
  upgrade that VCF Operations fleet lifecycle runs: export the data, attach
  the old disk to the new node, restore
  ([Aria Automation 8.18.1 upgrade blog](https://blogs.vmware.com/cloud-foundation/2026/07/27/modernizing-infrastructure-vmware-aria-automation-8-18-1-to-vmware-cloud-foundation-automation-9-1-upgrade-technical-deep-dive/)).
  The same blog lists the Migration Service Engine binary among the files to
  put in the depot first. It does not say the engine does any of the upgrade
  work. **Not confirmed:** whether the engine has any role in this upgrade.
- **VCD Migration Assistant.** The VCF 9.1 feature comparison lists "VCD
  Migration Assistant" among the pre-bundled services
  ([feature comparison PDF](https://www.vmware.com/docs/vmware-cloud-foundation-9-1-feature-comparison-and-upgrade-paths)).
  **Not confirmed:** that this is the same service. The name and the purpose
  match, but no vendor page links the two names.

## Where the API reference is

| Source | What it has for the Migration Service |
|---|---|
| `vmware/vcf-api-specs`, tags `9.1.0.0` and `9.1.1.0` | No file for the service. Both tags have the same 16 YAML and JSON spec files. A search for "migration service", "migration engine", "Cloud Director" and "VCD migr" finds nothing. The only related text is in the fleet lifecycle file `specifications/fleet-lcm/fleet-lcm-openapi.yaml`: the component type `VCD_MIGRATOR` in the `ComponentType` list, and the query flag `includeVcdMigrator` ("Whether to include the VCD Migrator component.") on `GET /v1/components` (operation `getComponents`). Both tags have them. |
| [Broadcom developer portal](https://developer.broadcom.com/xapis) | No reference for the Migration Service or "Migrator". The migration APIs there belong to other products (HCX, a vRealize Automation migration assessment). |
| Broadcom TechDocs, VCF 9.1 | Release notes and the bill of materials only. No page gives a path. The migration guide is sent by Broadcom on request (VCD 10.6.2 blog above). |
| [Service API Reference](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-1/vcf-services-development-guide/lifecycle-api.html), in the 9.1 VCF Services Development Guide | The VCF Automation Service Manager API. It lists, checks and changes **any** VCF service. The same text is in the vendor repository [`vmware/vcf-services`](https://github.com/vmware/vcf-services), file `documentation/lifecycle/api.md`. That repository has no file about the Migration Service. |

## How to log in

- **Fleet lifecycle** (`fleet-lcm`). The spec gives HTTP Bearer (a JWT token)
  as the main login, and HTTP Basic as the other choice
  (`fleet-lcm-openapi.yaml`, `securitySchemes`, both tags). The `fleet-lcm`
  connector already does both (see `docs/codebase/connectors-fleet-lcm.md`,
  part "Auth"). Nothing new is needed.
- **VCF Automation Service Manager API** (`vcf-automation`). It uses the
  provider login of VCF Automation: `POST /cloudapi/1.0.0/sessions/provider/`
  with HTTP Basic. Later calls send the token as `Authorization: Bearer
  <token>` ([Service API Reference](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-1/vcf-services-development-guide/lifecycle-api.html)).
  The `vcf-automation` connector already logs in this way (see
  `docs/codebase/connectors-vcf-automation.md`, part "Provider-plane session
  establishment").
- **Not confirmed: the `Accept` header.** The vendor page gives
  `Accept: application/json;version=41.0.0-alpha` for this API. The word
  "alpha" may mean the API can still change. The Configuration service
  record (issue #4510) has the same open point. The connector Task checks it
  once for all VCF services.
- **VCD** (`vcd`). The connector logs in as it does today.

## How to detect the version

- `GET /v1/components?includeVcdMigrator=true` on the fleet lifecycle API
  lists the managed components with `componentType`, `version` and `fqdn`.
  The engine is the entry with `componentType` `VCD_MIGRATOR`
  (`fleet-lcm-openapi.yaml`, both tags).
- **Not confirmed:** that the engine is left out of the list without
  `includeVcdMigrator=true`. The flag name suggests it. MEHO's
  `fleet-lcm.component.list` sends no query flags today, so it may not show
  the engine.
- `GET /api/extension/broadcom/service-manager/v2/vcf-services` lists each VCF
  service with `name`, `version`, `status` and `lifecycleState`
  ([Service API Reference](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-1/vcf-services-development-guide/lifecycle-api.html)).
- **Not confirmed:** that the Migration Service shows in this list, and its
  `name` there. KB 453702 shows that its health is checked by the VCF
  Automation service controller. That suggests it is a VCF service like the
  others, but no page shows it in the list.
- The expected versions are `9.1.0.0` on VCF 9.1.0 and `9.1.1.0` on VCF 9.1.1
  (bill of materials). Patch builds also exist, for example `9.1.0.0200` and
  `9.1.1.0100` (KB 455559, community upgrade guide).
- The service has no version call of its own.

## What operators do with the service every week

A migration is a project, not weekly work. Outside a migration project there
is nothing to do with the service. During a project, and after each VCF
upgrade, an operator does this. The list is our reading of the vendor pages,
not a vendor statement.

- Check that the engine runs and has the version of the bill of materials.
- Check that the Migration Service is healthy in VCF Automation. If not, read
  the error.
- Watch the engine upgrade task, and upgrade VCF Automation first.
- Before a migration, list the VCD source estate: organizations, VDCs, vApps,
  VMs, edge gateways. MEHO's `vcd` connector does this today.
- Run the migration steps in the VCF Automation provider UI. MEHO cannot help
  here: there is no public API (scope exception asked below).

## The usual work

Risk levels: a **read** is `safe` and runs at once. All operations below are
reads. New ids are proposals. The connector Task picks the final names.

| Operation (id) | Connector | What it does | Vendor call | Risk |
|---|---|---|---|---|
| `fleet-lcm.component.list` (exists; add one optional flag) | `fleet-lcm` | All managed components, now with the engine: type `VCD_MIGRATOR`, version, FQDN, nodes. | `GET /v1/components?includeVcdMigrator=true` (`fleet-lcm-openapi.yaml`, operation `getComponents`, both tags). | read |
| `fleet-lcm.component.status` (exists) | `fleet-lcm` | Does the engine run? `Unknown`, `NotRunning` or `Running`. | `GET /v1/components/{componentId}/status` (operation `getComponentStatus`, both tags). | read |
| `fleet-lcm.task.list` (exists) | `fleet-lcm` | The fleet tasks, with the engine upgrade task and its state. | `GET /v1/tasks` (both tags). | read |
| `vcfa.provider.vcf_service.list` (proposed in the Configuration service record) | `vcf-automation` | All VCF services with name, version, status and lifecycle state. A filter can pick the Migration Service. | `GET /api/extension/broadcom/service-manager/v2/vcf-services` ([Service API Reference](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-1/vcf-services-development-guide/lifecycle-api.html)). That the service shows here is **not confirmed**. | read |
| `vcfa.provider.vcf_service.state` (proposed in the Configuration service record) | `vcf-automation` | The health of each part of the Migration Service and the error messages. This is where an error like the one in KB 453702 shows. | `GET /api/extension/broadcom/service-manager/v2/vcf-services/{id}/state` (same page). | read |
| `vcd.org.list`, `.vdc.list`, `.vapp.list`, `.vm.list`, `.edge-gateway.list` (exist) | `vcd` | The VCD source estate, before a migration. | Already shipped. See `docs/codebase/connectors-vcd.md`. | read |

**Notes**

1. **Only one small change for this service.** Three operations already
   exist. The only new piece is the optional `includeVcdMigrator` flag on
   `fleet-lcm.component.list`. The two `vcf-automation` reads are the same
   shared reads that the other VCF 9.1 service records propose. They serve
   every VCF service.
2. **Upgrade order.** The agent can warn when the engine is newer than VCF
   Automation, or when both upgrade at once. KB 455559 shows that this blocks
   both upgrades. Both versions come from `fleet-lcm.component.list`.
3. **The answers may be large.** A long list goes back as a link to the
   stored result.

## Why not something else

- **Its own connector.** There is no public API of the service to call. A new
  connector would only wrap the APIs above, which MEHO already logs in to.
- **The Kubernetes connector (`k8s`).** The service runs with VCF Automation,
  not on a Supervisor (KB 453702), and the vendor names no object kind for it.
  There is nothing to build there.
- **The vCenter connector (`vmware-rest`).** The Supervisor services list
  in `vcenter.yaml` is for Supervisor services. Nothing shows that the
  Migration Service is one.
- **VCF Operations suite API** (`/suite-api/api/...`). The VCF Operations 9.1
  files have no Migration Service path. The engine is managed through the
  fleet lifecycle API instead.
- **Changes through the fleet lifecycle API** (install, upgrade, remove the
  engine). These are rare, and a wrong order blocks both upgrades
  (KB 455559). They stay out of the usual work. The `fleet-lcm` component
  writes are a separate, planned follow-up of that connector.

## Out of scope (scope exception asked)

- Connecting the Migration Service to VCD, checking the VCD estate for a
  migration, and running or stopping a migration. No public API, and the
  guide is only sent on request.
- The Environment Assessment Tool. It is a VCD tool, not part of VCF 9.1.
- HCX workload moves and the Aria Automation 8 upgrade. Other products (see
  above).

**Request to the program owner:** please agree that, for the Migration
Service, MEHO covers only health, version and upgrade tasks (the reads
above), plus the existing reads of the VCD source. Please agree a scope
exception for the migration itself, until Broadcom publishes an API for it.

## What happens next

1. The program owner agrees this record, and the scope exception, in the pull
   request.
2. A connector Task adds the `includeVcdMigrator` flag to
   `fleet-lcm.component.list`. The shared `vcf-automation` reads come from
   the Task that serves all VCF services. Both first check every **not
   confirmed** fact on a lab VCF 9.1 with the Migration Service installed.
