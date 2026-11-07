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

- **Generic** connectors are built from a vendor's API file (OpenAPI,
  GraphQL, WSDL or proto).
- **Typed** connectors are written by hand against a vendor library or
  a protocol, where no good API file exists.

You can add your own connector as a package. See
[Write a connector package](../extend/connector-kit.md).

## Product versions

The tables below group the connectors by what they manage. The exact
version ranges are on the [connector inventory](../reference/connectors.md).

### In the public image

| Area | Systems | Versions |
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
VCF connectors, and connectors for Fortinet and CrowdStrike products,
as a signed package on top of the public image. It covers, for example,
vCenter 8.0 and 9.x, NSX 4.x and 9.x, VCF 5.x and 9.x, HCX, Site
Recovery Manager and Cloud Director. To get the pack, contact evoila.

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
