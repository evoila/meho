<!--
  GENERATED FILE — do not edit by hand.
  Regenerate from backend/ with: uv run python scripts/generate_reference_docs.py
  The freshness gate in backend/tests/test_reference_docs_drift.py fails CI when this page and the registry disagree.
-->

# Connector inventory

This page lists every MEHO connector in two tables. The first table lists the open source connectors of the public image. The second table lists the connectors of evoila's enterprise pack, a commercial add-on with the VMware and VCF connectors. Agents and operators drive all of them through the same governed surface — pick a connector, list its operation groups, search operations, then call — so no tool name carries a vendor name.

You can add your own connectors the same way as the enterprise pack: see [Write a connector package](../extend/connector-kit.md).

## Open source connectors (public image)

These connectors are in the public image, generated from the in-process connector registry. Each row is one registered implementation. The **Docs** column links to the connector's page in the source repository.

MEHO has two kinds of connector, both first-class and indistinguishable to an agent. **Generic** connectors are built by ingesting a vendor's protocol spec (OpenAPI, GraphQL, WSDL, proto); **typed** connectors are hand-coded against a vendor SDK or transport where no usable spec exists. The **Kind** column is populated only where MEHO's published connector-spec catalog states it, and is blank otherwise — it is a property of how a connector's operations are sourced, not something this table infers.

| Connector | Product | What it does | Supported versions | Kind | Docs |
| --- | --- | --- | --- | --- | --- |
| `argocd-api-3.x` | `argocd` | — | >=2.0,<4.0 | — | [docs](https://github.com/evoila/meho/blob/main/docs/codebase/connectors-argocd.md) |
| `azure-arm-2026.10` | `azure` | Reads Azure subscriptions (VMs, Kubernetes clusters, networks, storage, costs, security and policy), changes tags and starts patch checks. | — | — | [docs](https://github.com/evoila/meho/blob/main/docs/codebase/connectors-azure.md) |
| `bind9-ssh-9.x` | `bind9` | — | — | typed | [docs](https://github.com/evoila/meho/blob/main/docs/codebase/connectors-bind9.md) |
| `entra-graph-1.0` | `entra` | Reads Microsoft Entra ID users, groups, app registrations, service principals and role assignments. | — | — | [docs](https://github.com/evoila/meho/blob/main/docs/codebase/connectors-entra.md) |
| `freeipa-jsonrpc-4.x` | `freeipa` | Reads FreeIPA users, groups, hosts and access rules; creates host groups and rules, deletes hosts and host groups, and sets the password policy. | >=4.9,<5.0 | — | [docs](https://github.com/evoila/meho/blob/main/docs/codebase/connectors-freeipa.md) |
| `gcloud-rest-1.0` | `gcloud` | — | — | — | [docs](https://github.com/evoila/meho/blob/main/docs/codebase/connectors-gcloud.md) |
| `gh-rest-3` | `gh` | Reads GitHub repositories, issues, pull requests and projects, and creates issues, comments, repositories and commits. | — | generic | [docs](https://github.com/evoila/meho/blob/main/docs/codebase/connectors-github.md) |
| `harbor-rest-2.x` | `harbor` | — | >=2.0,<3.0 | generic | [docs](https://github.com/evoila/meho/blob/main/docs/codebase/connectors-harbor.md) |
| `hetzner-rest-2026.04` | `hetzner` | — | — | — | [docs](https://github.com/evoila/meho/blob/main/docs/codebase/connectors-hetzner-robot.md) |
| `hyperv-ssh-2022.x` | `hyperv` | — | — | — | [docs](https://github.com/evoila/meho/blob/main/docs/codebase/connectors-hyperv.md) |
| `k8s-1.x` | `k8s` | — | — | typed | [docs](https://github.com/evoila/meho/blob/main/docs/codebase/connectors-kubernetes.md) |
| `keycloak-admin-26.x` | `keycloak` | — | >=26.0,<27.0 | — | [docs](https://github.com/evoila/meho/blob/main/docs/codebase/connectors-keycloak.md) |
| `linux-ssh-1.x` | `linux` | — | — | — | [docs](https://github.com/evoila/meho/blob/main/docs/codebase/connectors-linux.md) |
| `loki-api-3.x` | `loki` | — | >=2.9,<4.0 | — | [docs](https://github.com/evoila/meho/blob/main/docs/codebase/connectors-loki.md) |
| `mongodb-wire-7` | `mongodb` | — | >=5,<9 | — | [docs](https://github.com/evoila/meho/blob/main/docs/codebase/connectors-mongodb.md) |
| `msad-ssh-2022.x` | `msad` | — | — | — | [docs](https://github.com/evoila/meho/blob/main/docs/codebase/connectors-msad.md) |
| `mssql-tds-2022.x` | `mssql` | — | >=13,<17 | — | [docs](https://github.com/evoila/meho/blob/main/docs/codebase/connectors-mssql.md) |
| `pfsense-ssh-2.7` | `pfsense` | — | — | — | [docs](https://github.com/evoila/meho/blob/main/docs/codebase/connectors-pfsense.md) |
| `phpipam-rest-1.x` | `phpipam` | Lists phpIPAM subnets and free addresses, and reserves or releases an IP address. | — | — | [docs](https://github.com/evoila/meho/blob/main/docs/codebase/connectors-phpipam.md) |
| `postgres-wire-16` | `postgres` | — | >=13,<18 | — | [docs](https://github.com/evoila/meho/blob/main/docs/codebase/connectors-postgres.md) |
| `prometheus-api-2.x` | `prometheus` | — | — | — | [docs](https://github.com/evoila/meho/blob/main/docs/codebase/connectors-prometheus.md) |
| `proxmox-api-8.x` | `proxmox` | — | >=7.0,<9.0 | — | [docs](https://github.com/evoila/meho/blob/main/docs/codebase/connectors-proxmox.md) |
| `rabbitmq-management-3.x` | `rabbitmq` | — | >=3.8,<5.0 | — | [docs](https://github.com/evoila/meho/blob/main/docs/codebase/connectors-rabbitmq.md) |
| `rke2-ssh-1.x` | `rke2` | — | — | — | [docs](https://github.com/evoila/meho/blob/main/docs/codebase/connectors-rke2.md) |
| `tempo-api-2.x` | `tempo` | — | >=2.0,<3.0 | — | [docs](https://github.com/evoila/meho/blob/main/docs/codebase/connectors-tempo.md) |
| `vault-1.x` | `vault` | — | — | typed | [docs](https://github.com/evoila/meho/blob/main/docs/codebase/connectors-vault.md) |
| `windns-ssh-2016.x` | `windns` | — | — | — | [docs](https://github.com/evoila/meho/blob/main/docs/codebase/connectors-windows-dns.md) |
| `winsrv-ssh-2022.x` | `winsrv` | — | — | — | [docs](https://github.com/evoila/meho/blob/main/docs/codebase/connectors-winsrv.md) |
| `wsfc-ssh-2022.x` | `wsfc` | — | — | — | [docs](https://github.com/evoila/meho/blob/main/docs/codebase/connectors-wsfc.md) |

## evoila's enterprise pack

evoila's enterprise pack is a commercial add-on. It adds the VMware and VCF connectors, and connectors for Fortinet and CrowdStrike products, as a signed package on top of the public image. Their setup guides and operation lists come with the pack. To get the pack, contact evoila.

| Product | Connectors | Product versions | What it does |
| --- | --- | --- | --- |
| VMware vCenter and vSphere | `vmware-rest-9.0`, `vmware-rest-8.0` | 8.0, 9.x | Reads and manages virtual machines, hosts, clusters, storage and networks. |
| VMware NSX | `nsx-rest-9.0`, `nsx-rest-4.2` | 4.x, 9.x | Reads and changes segments, gateways, firewall rules, load balancers and VPNs. |
| VCF SDDC Manager | `sddc-rest-9.0`, `sddc-vcf5-5.0` | 5.x, 9.x | Reads workload domains, clusters and hosts, and runs VCF lifecycle tasks. |
| VCF Installer and Cloud Builder | `installer-rest-9.1`, `installer-cb-5.0` | 5.x, 9.x | Checks a deployment plan and deploys a new VCF instance. |
| VCF fleet management and Aria Suite Lifecycle | `fleet-lcm-9.0`, `fleet-rest-9.0` | 8.x, 9.x | Installs, updates and scales the VCF management components. |
| VCF Automation and Aria Automation | `vcfa-rest-9.0`, `vcfa-vra8-8.0` | 8.x, 9.x | Reads and manages projects, catalog items, templates and deployments. |
| VCF Operations and Aria Operations | `vrops-rest-9.0`, `vrops-vrops8-8.0` | 8.x, 9.x | Reads resources, alerts, metrics and health. |
| VCF Operations for logs and Aria Operations for Logs | `vrli-rest-9.0`, `vrli-vrli8-8.0` | 8.x, 9.x | Searches logs and reads log alerts. |
| VCF Operations orchestrator | `vro-rest-9.0` | 8.x, 9.x | Reads workflows and their runs, and starts or cancels a workflow run. |
| VCF Identity Broker and Workspace ONE Access | `idb-rest-9.0`, `idb-wsa-3.3` | 3.3, 9.x | Reads users, groups and identity settings. |
| VMware Cloud Director | `vcd-rest-10.6` | 10.x | Reads and manages tenants, vApps, virtual machines, networks and edge gateways. |
| VMware HCX | `hcx-rest-9.0` | 4.7 to 9.x | Reads site pairings, service meshes and migrations, and changes service meshes. |
| VMware Site Recovery Manager | `srm-rest-9.1` | 8.8 to 9.x | Reads protection groups and recovery plans, and runs recovery tasks. |
| VMware vSphere Replication | `vr-rest-9.1` | 8.8 to 9.x | Reads and manages the replication of virtual machines. |
| VMware Aria Automation Config (Salt) | `salt-raas-8.18` | 8.x, 9.x | Reads minions and jobs, accepts minion keys and starts jobs. |
| VMware Holodeck | `holodeck-ssh-9.0` | 9.0 | Builds and manages nested VCF lab environments. |
| Fortinet FortiGate | `fortigate-rest-7.x` | 7.2 to 7.x | Reads firewalls and changes interfaces, routes, addresses, policies and IPsec VPNs. |
| Fortinet FortiManager | `fortimanager-jsonrpc-7.x` | 7.2 to 7.x | Changes addresses, groups and policies, then installs the policy package to devices. |
| Fortinet FortiAnalyzer | `fortianalyzer-jsonrpc-7.x` | 7.2 to 7.x | Searches the logs and events that FortiAnalyzer keeps for many devices. |
| Fortinet FortiMail | `fortimail-rest-7.x` | 7.2 to 7.x | Reads status, quarantine and mail queues, and changes block lists, safe lists and policies. |
| Fortinet FortiSIEM | `fortisiem-rest-7.5`, `fortisiem-rest-7.4` | 7.4 to 7.x | Reads the health of the FortiSIEM nodes and the organizations. |
| CrowdStrike Falcon | `falcon-rest-2026.11` | cloud service | Reads hosts and detections, and cuts a host off the network or gives it back. |

## Versions and how a connector is chosen

**Supported versions** is the product-version range an implementation advertises. Where a product has more than one implementation — a modern and a legacy one, say — both are registered and MEHO resolves the right one per target from the target's fingerprint, so one estate spanning old and new versions just works. A connector can also state the target versions its operations work on. MEHO checks this range each time an operation runs (at dispatch time), also when the call names the connector itself. Outside the range, MEHO refuses the call with `unqualified_target_version`, and a Sensor on that connector reads `unknown`. So pick the connector whose version matches the target, for example `vmware-rest-8.0` for vCenter 8.0.x and `vmware-rest-9.0` for vCenter 9.0.x.

## Maturity and readiness

This inventory lists what is *registered*; it does not restate a ship-state. Feature-level maturity — GA, beta, or experimental — is published in the [feature maturity index](maturity.md). Per-release, per-connector ship-state (dispatch + catalog, loader-wired, or production-ready) is stated in the [changelog](https://github.com/evoila/meho/blob/main/CHANGELOG.md) under the connector release-notes convention. The two-connector model is described in the [architecture overview](../architecture.md).
