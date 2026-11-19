# Connectors overview

A connector is the part of MEHO that talks to one kind of system. This
page lists the systems MEHO works with.

## What it does

An agent or a person uses the same few steps for every system: pick a
connector, list its groups of operations, search for an operation and
call it. MEHO checks the policy, waits for an approval when a change
needs one, and writes an audit row. You do not need a different tool
for each vendor.

MEHO has two kinds of connectors. Both work the same for the caller:

- **Generic** connectors are built from a vendor's OpenAPI file (version
  3.0 or 3.1). The command line converts a Swagger 2.0 file to OpenAPI 3
  first.
- **Typed** connectors are written by hand against a vendor library or
  a protocol, where no good API file exists.

You can add your own connector as a package. See
[Write a connector package](../extend/connector-kit.md).

## Product versions

The tables below group the connectors by what they manage. **Built for**
is the range the code declares and CI tests. The exact ranges, and the
versions that a person ran against a real system, are on the
[connector inventory](../reference/connectors.md).

### In the public image

| Area | Systems | Built for |
|---|---|---|
| Kubernetes and delivery | Kubernetes, RKE2, Argo CD, Harbor | Kubernetes 1.x, Argo CD 2 to 3, Harbor 2.x |
| Secrets and login | Vault and OpenBao, Keycloak, FreeIPA, Microsoft Entra ID | Keycloak 26.x, FreeIPA 4.9 and later |
| Databases and queues | PostgreSQL, SQL Server, MongoDB, RabbitMQ | PostgreSQL 13 to 17, SQL Server 2016 to 2022, MongoDB 5 to 8, RabbitMQ 3.8 to 4.x |
| Monitoring and logs | Prometheus, Loki, Tempo | Prometheus 2.x, Loki 2.9 to 3.x, Tempo 2.x |
| Cloud | Azure, Google Cloud, Hetzner | Current cloud APIs |
| Windows | Windows Server, Active Directory, DNS, failover clusters, Hyper-V | Windows Server 2022 (DNS: 2016 and later) |
| Network and servers | pfSense, BIND 9, phpIPAM, Linux over SSH, Proxmox | Proxmox 7 to 8 |
| Code hosting | GitHub | GitHub REST API |

### In evoila's enterprise pack

The enterprise pack is a commercial add-on. It brings the VMware and
VCF connectors, and connectors for Fortinet, CrowdStrike and Dell NetWorker products,
as a signed package on top of the public image. To get the pack,
contact evoila.

<!-- live-tests:start -->
| Product | Built for | Tested live on |
| --- | --- | --- |
| VMware vCenter and vSphere | 8.0 Update 1 or newer, 9.x | `vmware-rest-9.0`: 9.1.0.0: changes (Oct 2026, last tested with MEHO v0.37.0). `vmware-rest-8.0`: 8.0.3: changes (Oct 2026, last tested with MEHO v0.37.0). |
| VMware NSX | 4.x, 9.x | `nsx-rest-9.0`: 9.1.0.0: reads (Nov 2026). `nsx-rest-4.2`: not yet tested live. |
| VCF SDDC Manager | 5.x, 9.x | `sddc-rest-9.1`: not yet recorded. `sddc-rest-9.0`: 9.1.0.0: reads (Nov 2026). `sddc-vcf5-5.0`: not yet tested live. |
| VCF Operations for Networks | 6.14, 9.x | `vrni-rest-9.0`: not yet recorded. `vrni-rest-6.14`: not yet recorded. |
| VCF Installer and Cloud Builder | 5.x, 9.x | `installer-rest-9.1`: 9.1.1.0: changes (Oct 2026, last tested with MEHO v0.37.0). `installer-cb-5.0`: not yet tested live. |
| VCF fleet management and Aria Suite Lifecycle | 8.18 to 9.x | `fleet-lcm-9.0`: not yet tested live. `fleet-rest-9.0`: not yet tested live. |
| VCF Automation and Aria Automation | 8.18 to 9.x | `vcfa-rest-9.0`: 9.1.0: changes (Oct 2026, last tested with MEHO v0.37.0); 9.1.1: reads (Nov 2026). `vcfa-vra8-8.0`: not yet tested live. |
| VCF Operations and Aria Operations | 8.18 to 9.x | `vrops-rest-9.0`: not yet tested live. `vrops-vrops8-8.0`: not yet tested live. |
| VCF Operations for logs and Aria Operations for Logs | 8.18 to 9.x | `vrli-rest-9.0`: not yet tested live. `vrli-vrli8-8.0`: not yet tested live. |
| VCF Operations orchestrator | 8.18 to 9.x | not yet tested live |
| VCF Identity Broker and Workspace ONE Access | 3.3, 9.x | `idb-rest-9.0`: not yet tested live. `idb-wsa-3.3`: not yet tested live. |
| VMware Cloud Director | 10.x | not yet tested live |
| VMware HCX | 4.11 to 9.x | not yet tested live |
| VMware Site Recovery Manager | 8.8 to 9.x | not yet tested live |
| VMware vSphere Replication | 8.8 to 9.x | not yet tested live |
| VMware Aria Automation Config (Salt) | 8.18 to 9.x | not yet tested live |
| VMware Holodeck | 9.0 | not yet tested live |
| Fortinet FortiGate | 7.2 to 7.x | not yet recorded |
| Fortinet FortiManager | 7.2 to 7.x | not yet recorded |
| Fortinet FortiAnalyzer | 7.2 to 7.x | not yet recorded |
| Fortinet FortiMail | 7.2 to 7.x | not yet recorded |
| Fortinet FortiSIEM | 7.4 to 7.x | `fortisiem-rest-7.5`: not yet recorded. `fortisiem-rest-7.4`: not yet recorded. |
| CrowdStrike Falcon | cloud service | not yet recorded |
| Dell NetWorker | 19.x | not yet recorded |
<!-- live-tests:end -->

## State

- The public connectors are **shipped**. The
  [maturity index](../reference/maturity.md) shows which features are
  beta.
- The VMware and VCF connectors are **shipped** as the enterprise
  pack. Since v0.44.0 they are not in the public image.
- A system that is not on the inventory is **not covered**. If it
  publishes an API file, you can add it by ingesting that file. Ask
  evoila if you need a hand-written connector.

## Operator guide

- [Connector inventory](../reference/connectors.md): every connector
  and its version range.
- [Run your first operations](../guides/first-operations.md): the steps
  to call an operation.
- [Register targets and secrets](../guides/targets-and-secrets.md): how
  to connect MEHO to your systems.
