# How MEHO works with the VCF 9.1 Telemetry service (decision)

**Status:** proposed. It waits for the program owner to agree in the pull request.
**Date:** 2026-10-23
**Issue:** [#4514](https://github.com/evoila/meho/issues/4514)
**Initiative:** [#4306](https://github.com/evoila/meho/issues/4306) — VCF bring-up, fleet lifecycle, identity and platform services through MEHO

## The decision in short

- The Telemetry service sends usage and health data from a VCF 9.1 data
  center to Broadcom. It is one of the new **VCF management services**. These
  services run as containers on the **VCF services runtime**, a Kubernetes
  cluster that VCF 9.1 adds for its own management tools.
- It is **not** one of the VCF services that VCF Automation installs on a
  vSphere Supervisor (like the Configuration service or the Metrics
  Aggregator). The bill of materials lists it in the main table, not in the
  "VCF Services" table. So the VCF Automation Service Manager reads that the
  other records propose do **not** apply here.
- VCF Operations **fleet lifecycle** manages it. In the fleet lifecycle API it
  is a component of type `TELEMETRY_ACCEPTOR`.
- The service has **no API of its own** that MEHO can call. There is no file
  for it in `vmware/vcf-api-specs` (tags `9.1.0.0` and `9.1.1.0`), no
  reference on the Broadcom developer portal, and no TechDocs page with a path.
- MEHO can still check **its state, its version and the CEIP choice**, through
  connectors MEHO already has. (CEIP is the Customer Experience Improvement
  Program: the opt-in that lets a VMware product send data to Broadcom.)
  1. **Fleet lifecycle** (connector `fleet-lcm`): the Telemetry component, its
     version, whether it runs, and the CEIP flag of each fleet component.
  2. **SDDC Manager** (connector `sddc-manager`) and **VCF Installer**
     (connector `vcf-installer`): each has its own CEIP status read.
  3. **NSX** (connector `nsx`): NSX has its own telemetry settings, with a CEIP
     flag.
- So the service needs **no connector of its own**.
- **We ask the program owner for a scope exception** for the Telemetry service
  itself: what data it collects, where it sends it, and its settings. There is
  no public API for this.
- Some facts are **not confirmed** and are marked below. The connector Task
  checks them on a lab VCF 9.1 before the operations ship.

## What the service is

- **Bill of materials.** The VCF 9.1.0 bill of materials lists "Telemetry",
  version `9.1.0.0`, build `25181946`, in the main component table, as a VCF
  component
  ([BOM 9.1.0](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-1/release-notes/vmware-cloud-foundation-9-1-0-0-release-notes/vmware-cloud-foundation-bill-of-materials.html)).
  The VCF 9.1.1 bill of materials lists "Telemetry", version `9.1.1.0`, build
  `25671600`, in the same main table
  ([BOM 9.1.1](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-1/release-notes/vmware-cloud-foundation-9-1-1-0-release-notes/vmware-cloud-foundation-9-1-1-0-bill-of-material.html)).
  It is **not** in the "VCF Services" table of either page. So it is a
  separate part of VCF, and not a VCF service in the VCF Automation sense.
- **What it does.** The vendor page "Components in VCF and vSphere Foundation"
  says Telemetry "Sends telemetry data from the data center to Broadcom". It
  is part of both VCF and vSphere Foundation, with the scope "Every VCF
  Instance"
  ([Components in VCF and vSphere Foundation](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-1/deployment/vcf-management-appliances.html)).
  The same page says the VCF services runtime of each VCF instance hosts the
  VCF management services.
- **Not confirmed: one per instance or one per fleet.** A community upgrade
  guide says telemetry deploys only on the first VCF instance, next to fleet
  lifecycle, the software depot and the license server
  ([community upgrade guide](https://drpranayjha.com/deploy-vcf-management-services-software-depot/)).
  This does not match the vendor page above ("Every VCF Instance"). The
  connector Task checks this in a lab. The reads below work in both cases.
- **The link to `TELEMETRY_ACCEPTOR`.** A community API walk-through shows
  `GET /fleet-lcm/v1/release-versions/target-versions` returning an entry with
  `"type": "TELEMETRY_ACCEPTOR"`, `"publicName": "Telemetry"` and the version
  `9.1.0.0.25181946`
  ([community API walk-through](https://jadenetworksolutions.co.uk/post/vcf-91-apis/vcf-91-management-upgrade/)).
  The build number is the same as the "Telemetry" row of the 9.1.0 bill of
  materials. No vendor page says this in words, but the build number makes the
  link very likely.
- **How it runs.** A community lab write-up shows a pod named
  `telemetry-acceptor-...` in a Kubernetes namespace `telemetry` on the VCF
  management services cluster
  ([community lab write-up](https://blog.leaha.co.uk/2026/07/21/vcf-9-management-services-lab-downsize/)).
  This is not a vendor source. "Acceptor" suggests that the service takes in
  data from the other VCF parts and sends it on. **Not confirmed:** no vendor
  page says which VCF parts send data to it.

### How it relates to the CEIP settings

Each VCF part already has a CEIP switch. The Telemetry service is the part
that sends the data. The vendor does not say in words how the two connect.
This is what the vendor files show:

- **SDDC Manager and VCF Installer** each have `GET /v1/system/ceip` ("Retrieve
  the CEIP status") and `PATCH /v1/system/ceip` ("Configure CEIP to Opt-In or
  Opt-Out"). The read returns `status` (`ENABLED`, `DISABLED`, `ENABLING`,
  `DISABLING`, `ENABLING_FAILED` or `DISABLING_FAILED`) and `instanceId`
  ("Instance Id of VCF"). The change takes `status` = `ENABLE` or `DISABLE` and
  returns a task. Files: `sddc-manager-openapi.json` and
  `vcf-installer-openapi.json`, both tags.
- **Fleet lifecycle** keeps a `ceip` flag ("CEIP status") in the configuration
  of each component. It is in the schemas `OvaComponentConfig`,
  `VspComponentConfig` and `VspClusterConfigBase` of
  `fleet-lcm-openapi.yaml` (both tags). The read is
  `GET /v1/components/{componentId}/config`. The change is
  `PATCH /v1/components/{componentId}/config`. The SDDC lifecycle file
  `sddc-lcm-openapi.yaml` has the same schemas.
- **VCF Automation.** The vendor page for VCF Automation CEIP says CEIP is on by
  default. To change it, the operator sends
  `PATCH /fleet-lcm/v1/components/<componentId>/config` with
  `{"type": "VspClusterConfigSpec", "ceip": true}` for the VCF Automation
  platform cluster, after a Bearer login at `/api/v1/identity/token`. VCF
  Automation is down for a short time while its pods restart
  ([Manage CEIP in VCF Automation](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-1/provider-management/managing-system-settings/activate-or-deactivate-your-customer-experience-improvement-program-participation.html)).
- **VCF Operations.** The `suite-api` file `vcf-operations-openapi.json` (both
  tags) lists the global setting `TELEMETRY_ENABLED` ("Customer Experience
  Improvement Program") as "Deprecated from VMware Aria Operations 8.14", in the
  description of `GET /api/deployment/config/globalsettings/metadata`. No other
  CEIP path is in the file. **Not confirmed:** that the fleet `ceip` flag of
  the VCF Operations component is now the way to read it.
- **vCenter.** `vcenter.yaml` (both tags) has **no** path for CEIP or telemetry.
  There is no `/api/vcenter/system/telemetry` or similar. CEIP shows only as a
  field `ceip_enabled` in the install and upgrade specs, and as an error case
  on the ESX hardware compatibility paths ("not part of the Customer Experience
  Improvement Program"). **Not confirmed:** a community post from 2019 says
  vCenter keeps the CEIP choice in an advanced setting, read through the
  `OptionManager` of the vSphere SOAP API. No vendor page for 9.1 says this,
  so this record proposes no vCenter CEIP read.
- **vSAN** has its own telemetry calls in `vi-json.yaml` (for example
  `VsanVcClusterHealthSystem_VsanHealthSetVsanClusterTelemetryConfig`, which
  sets the vSAN cluster CEIP settings). These are vSAN health features, not the
  Telemetry service. Out of scope here.
- **NSX** has `GET /api/v1/telemetry/config` ("Returns the telemetry
  configuration", with `ceip_acceptance`, `proxy_enabled` and
  `schedule_enabled`) and `PUT` on the same path, in `nsx_api.yaml` (both
  tags). NSX also has its own node service called "Telemetry"
  (`GET /api/v1/node/services/telemetry/status`). This is a service inside each
  NSX Manager, not the VCF 9.1 Telemetry service.

**Not confirmed:** whether turning CEIP off in one part stops that part's data
at the Telemetry service, or whether the Telemetry service has its own switch.

## Where the API reference is

| Source | What it has for the Telemetry service |
|---|---|
| `vmware/vcf-api-specs`, tags `9.1.0.0` and `9.1.1.0` | No file for the service. Both tags have the same 16 YAML and JSON spec files. The only Telemetry text is in `specifications/fleet-lcm/fleet-lcm-openapi.yaml`: the value `TELEMETRY_ACCEPTOR` in the `ComponentType` list, and a schema `TelemetryAcceptorConfigSpec` ("Telemetry acceptor component config spec.") with one required field, `size`. The CEIP paths and fields of the other parts are listed above. |
| [Broadcom developer portal](https://developer.broadcom.com/xapis) | No reference for Telemetry or CEIP. It has references for the fleet lifecycle API ("VCF Fleet LCM Service APIs") and the SDDC lifecycle API ("VCF SDDC LCM Service APIs"). The usage APIs there ("VCF Usage Meter APIs", "Usage Management Public APIs") belong to other products. |
| Broadcom TechDocs, VCF 9.1 | The bill of materials and the components page (linked above). No page gives a path for the Telemetry service. |

## How to log in

- **Fleet lifecycle.** The vendor page for VCF Automation CEIP gets a Bearer
  token with a password grant at `POST /api/v1/identity/token`, then calls
  `/fleet-lcm/v1/...` with `Authorization: Bearer <token>`
  ([Manage CEIP in VCF Automation](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-1/provider-management/managing-system-settings/activate-or-deactivate-your-customer-experience-improvement-program-participation.html)).
  The spec file defines `bearerToken` and `basicAuth`. The `fleet-lcm`
  connector already sends Bearer, with Basic as a fallback (see
  `docs/codebase/connectors-fleet-lcm.md`, part "Auth"). Nothing new is needed.
- **SDDC Manager** and **VCF Installer:** the connectors log in as they do
  today (a token from `POST /v1/tokens`, see
  `docs/codebase/connectors-sddc-manager.md` and
  `docs/codebase/connectors-vcf-installer.md`).
- **NSX:** the `nsx` connector logs in as it does today (see
  `docs/codebase/connectors-nsx.md`).

## How to detect the version

- `GET /v1/components` on fleet lifecycle lists each managed component with
  `componentType`, `version` and `fqdn` (`fleet-lcm-openapi.yaml`, schema
  `Component`). The Telemetry service is the entry with `componentType`
  `TELEMETRY_ACCEPTOR`. **Not confirmed:** that this list shows the Telemetry
  component. We know it shows in the target versions list (community source
  above).
- `GET /v1/components/{componentId}/status` gives `Running`, `NotRunning` or
  `Unknown` (schema `ComponentStatus`).
- The expected versions are `9.1.0.0` on VCF 9.1.0 and `9.1.1.0` on VCF 9.1.1
  (bill of materials, see above).
- The service has no version call of its own.

## What operators do with the service every week

The vendor does not describe weekly work. Most operators never touch this
service. The list below is our reading of the vendor pages.

- Check that the Telemetry component runs.
- After a VCF upgrade, check that it has the version of the new bill of
  materials.
- Check that the CEIP choice is the one the company wants, on every part. Some
  companies must not send data out. The CEIP choice is made at bring-up and
  may drift, for example after an upgrade or a new VCF instance.

## The usual work

Risk levels: a **read** is `safe` and runs at once. All operations below are
reads. The ids marked "new" are proposals; the connector Task picks the final
names. The other ids exist in MEHO today, or are planned by another Task.

| Operation | Connector | What it does | Vendor call | Risk |
|---|---|---|---|---|
| `fleet-lcm.component.list` (exists) | `fleet-lcm` | All fleet components with type, version and FQDN. The agent picks the one of type `TELEMETRY_ACCEPTOR`. | `GET /fleet-lcm/v1/components` (`fleet-lcm-openapi.yaml`, operation `getComponents`). | read |
| `fleet-lcm.component.status` (exists) | `fleet-lcm` | Whether the Telemetry component runs. | `GET /fleet-lcm/v1/components/{componentId}/status` (operation `getComponentStatus`). | read |
| `fleet-lcm.release-version.list` (exists) | `fleet-lcm` | The versions known to the fleet. After an upgrade, compare with the Telemetry version. | `GET /fleet-lcm/v1/release-versions` (operation `getReleaseVersions`). | read |
| `fleet-lcm.component.config.get` (planned in [#4479](https://github.com/evoila/meho/issues/4479)) | `fleet-lcm` | The configuration of one fleet component, with its `ceip` flag. | `GET /fleet-lcm/v1/components/{componentId}/config` (operation `getComponentConfig`). | read |
| `sddc.ceip.get` (new) | `sddc-manager` | CEIP status and VCF instance id on SDDC Manager. | `GET /v1/system/ceip` (`sddc-manager-openapi.json`, operation `getCeipStatus`). | read |
| `installer.ceip.get` (new) | `vcf-installer` | CEIP status on the VCF Installer. | `GET /v1/system/ceip` (`vcf-installer-openapi.json`, operation `getCeipStatus`). | read |
| `nsx.telemetry.config` (new) | `nsx` | NSX telemetry settings: CEIP accepted, proxy, schedule. | `GET /api/v1/telemetry/config` (`nsx_api.yaml`, operation `GetTelemetryConfig`). | read |

**Notes**

1. **Most of this already exists.** Three reads exist, and one is planned in
   another Task of this initiative. Only three small CEIP reads are new. They
   are typed (hand-written) operations on the connectors that already log in.
2. **The `{componentId}` comes from the list.** The agent calls
   `fleet-lcm.component.list` first.
3. **One question, many parts.** "Is CEIP the same everywhere?" walks the reads
   above. It can be a runbook (a stored list of steps) later, not a new
   operation. That is a later choice, not part of this record.

## Why not something else

- **Its own connector.** There is no API of its own to call. A new connector
  would only wrap APIs MEHO already logs in to.
- **The VCF Automation Service Manager reads** (`vcfa.provider.vcf_service.*`,
  proposed by the Configuration service record). The Telemetry service is not
  in the "VCF Services" table of the bill of materials, and VCF Automation does
  not manage it. So these reads do not help here.
- **The Kubernetes connector (`k8s`).** The community lab write-up shows the
  service's pod in the namespace `telemetry`. MEHO's existing `k8s.pod.list`
  and `k8s.logs` could look at it, if MEHO can log in to the VCF services
  runtime. **Not confirmed:** how to log in there, and the vendor gives no
  object kind for this service. So this record proposes nothing there.
- **VCF Operations `suite-api`** (`/suite-api/api/...`). Its only telemetry
  setting is deprecated (see above).
- **vCenter REST** (`vmware-rest`). It has no CEIP or telemetry path in 9.1.
- **Changes to the CEIP choice.** The vendor files have them
  (`PATCH /v1/system/ceip` on SDDC Manager and VCF Installer,
  `PATCH /fleet-lcm/v1/components/{componentId}/config`, `PUT` on the NSX
  telemetry config). They are not weekly work. The fleet change restarts VCF
  Automation pods. They can be a later Task, with a second person to approve,
  if the program owner wants them.

## Out of scope (scope exception asked)

- What the Telemetry service collects, where it sends it, and its own settings
  (other than `size`). No public API.
- Installing, upgrading or removing the service. Fleet lifecycle does this as
  part of the VCF upgrade. The fleet lifecycle work of the initiative covers
  it.

**Request to the program owner:** please agree that, for the Telemetry
service, MEHO covers state, version and the CEIP choice (the reads in the
table above). Please agree a scope exception for the rest, until Broadcom
documents an API for the service.

## What happens next

1. The program owner agrees this record, and the scope exception, in the pull
   request.
2. A connector Task adds the three new CEIP reads (`sddc-manager`,
   `vcf-installer`, `nsx`). It first checks, on a lab VCF 9.1, every **not
   confirmed** fact here: that `GET /v1/components` shows the Telemetry
   component, one per instance or one per fleet, and how the fleet `ceip` flag
   relates to VCF Operations.
