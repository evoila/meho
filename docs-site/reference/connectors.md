<!--
  GENERATED FILE — do not edit by hand.
  Regenerate from backend/ with: uv run python scripts/generate_reference_docs.py
  The freshness gate in backend/tests/test_reference_docs_drift.py fails CI when this page and the registry disagree.
-->

# Connector inventory

Every connector MEHO can resolve a target to, generated from the in-process connector registry. Each row is one registered implementation: agents and operators drive them all through the same governed surface — pick a connector, list its operation groups, search operations, then call — so the vendor never leaks into the tool names.

This page lists the connectors of the public image. evoila's enterprise pack adds the VMware and VCF connectors as a signed package on top of this image. You can add your own connectors the same way: see [Write a connector package](../extend/connector-kit.md).

MEHO has two kinds of connector, both first-class and indistinguishable to an agent. **Generic** connectors are built by ingesting a vendor's protocol spec (OpenAPI, GraphQL, WSDL, proto); **typed** connectors are hand-coded against a vendor SDK or transport where no usable spec exists. The **Kind** column is populated only where MEHO's published connector-spec catalog states it, and is blank otherwise — it is a property of how a connector's operations are sourced, not something this table infers.

| Connector | Product | What it does | Supported versions | Kind |
| --- | --- | --- | --- | --- |
| `argocd-api-3.x` | `argocd` | — | >=2.0,<4.0 | — |
| `azure-arm-2026.10` | `azure` | Reads Azure subscriptions (VMs, Kubernetes clusters, networks, storage, costs, security and policy), changes tags and starts patch checks. | — | — |
| `bind9-ssh-9.x` | `bind9` | — | — | typed |
| `entra-graph-1.0` | `entra` | Reads Microsoft Entra ID users, groups, app registrations, service principals and role assignments. | — | — |
| `freeipa-jsonrpc-4.x` | `freeipa` | Reads FreeIPA users, groups, hosts and access rules; creates host groups and rules. | >=4.9,<5.0 | — |
| `gcloud-rest-1.0` | `gcloud` | — | — | — |
| `gh-rest-3` | `gh` | Reads GitHub repositories, issues, pull requests and projects, and creates issues, comments, repositories and commits. | — | generic |
| `harbor-rest-2.x` | `harbor` | — | >=2.0,<3.0 | generic |
| `hetzner-rest-2026.04` | `hetzner` | — | — | — |
| `hyperv-ssh-2022.x` | `hyperv` | — | — | — |
| `k8s-1.x` | `k8s` | — | — | typed |
| `keycloak-admin-26.x` | `keycloak` | — | >=26.0,<27.0 | — |
| `linux-ssh-1.x` | `linux` | — | — | — |
| `loki-api-3.x` | `loki` | — | >=2.9,<4.0 | — |
| `mongodb-wire-7` | `mongodb` | — | >=5,<9 | — |
| `msad-ssh-2022.x` | `msad` | — | — | — |
| `mssql-tds-2022.x` | `mssql` | — | >=13,<17 | — |
| `pfsense-ssh-2.7` | `pfsense` | — | — | — |
| `phpipam-rest-1.x` | `phpipam` | Lists phpIPAM subnets and free addresses, and reserves or releases an IP address. | — | — |
| `postgres-wire-16` | `postgres` | — | >=13,<18 | — |
| `prometheus-api-2.x` | `prometheus` | — | — | — |
| `proxmox-api-8.x` | `proxmox` | — | >=7.0,<9.0 | — |
| `rabbitmq-management-3.x` | `rabbitmq` | — | >=3.8,<5.0 | — |
| `rke2-ssh-1.x` | `rke2` | — | — | — |
| `tempo-api-2.x` | `tempo` | — | >=2.0,<3.0 | — |
| `vault-1.x` | `vault` | — | — | typed |
| `windns-ssh-2016.x` | `windns` | — | — | — |
| `winsrv-ssh-2022.x` | `winsrv` | — | — | — |
| `wsfc-ssh-2022.x` | `wsfc` | — | — | — |

## Versions and how a connector is chosen

**Supported versions** is the product-version range an implementation advertises. Where a product has more than one implementation — a modern and a legacy one, say — both are registered and MEHO resolves the right one per target from the target's fingerprint, so one estate spanning old and new versions just works.

## Maturity and readiness

This inventory lists what is *registered*; it does not restate a ship-state. Feature-level maturity — GA, beta, or experimental — is published in the [feature maturity index](maturity.md). Per-release, per-connector ship-state (dispatch + catalog, loader-wired, or production-ready) is stated in the [changelog](https://github.com/evoila/meho/blob/main/CHANGELOG.md) under the connector release-notes convention. The two-connector model is described in the [architecture overview](../architecture.md).
