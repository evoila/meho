<!--
  GENERATED FILE — do not edit by hand.
  Regenerate from backend/ with: uv run python scripts/generate_reference_docs.py
  The freshness gate in backend/tests/test_reference_docs_drift.py fails CI when this page and the registry disagree.
-->

# Connector inventory

Every connector MEHO can resolve a target to, generated from the in-process connector registry. Each row is one registered implementation: agents and operators drive them all through the same governed surface — pick a connector, list its operation groups, search operations, then call — so the vendor never leaks into the tool names.

MEHO has two kinds of connector, both first-class and indistinguishable to an agent. **Generic** connectors are built by ingesting a vendor's protocol spec (OpenAPI, GraphQL, WSDL, proto); **typed** connectors are hand-coded against a vendor SDK or transport where no usable spec exists. The **Kind** column is populated only where MEHO's published connector-spec catalog states it, and is blank otherwise — it is a property of how a connector's operations are sourced, not something this table infers.

| Connector | Product | Supported versions | Kind |
| --- | --- | --- | --- |
| `argocd-api-3.x` | `argocd` | >=2.0,<4.0 | — |
| `bind9-ssh-9.x` | `bind9` | — | typed |
| `fleet-lcm-9.0` | `fleet` | >=9.0,<10.0 | — |
| `fleet-rest-9.0` | `fleet` | >=8.0,<10.0 | — |
| `gcloud-rest-1.0` | `gcloud` | — | — |
| `gh-rest-3` | `gh` | — | generic |
| `harbor-rest-2.x` | `harbor` | >=2.0,<3.0 | generic |
| `hetzner-rest-2026.04` | `hetzner` | — | — |
| `holodeck-ssh-9.0` | `holodeck` | — | — |
| `hyperv-ssh-2022.x` | `hyperv` | — | — |
| `installer-rest-9.1` | `installer` | >=9.1,<10.0 | — |
| `k8s-1.x` | `k8s` | — | typed |
| `keycloak-admin-26.x` | `keycloak` | >=26.0,<27.0 | — |
| `loki-api-3.x` | `loki` | >=2.9,<4.0 | — |
| `mongodb-wire-7` | `mongodb` | >=5,<9 | — |
| `msad-ssh-2022.x` | `msad` | — | — |
| `mssql-tds-2022.x` | `mssql` | >=13,<17 | — |
| `nsx-rest-9.0` | `nsx` | >=4.0,<10.0 | generic |
| `pfsense-ssh-2.7` | `pfsense` | — | — |
| `postgres-wire-16` | `postgres` | >=13,<18 | — |
| `prometheus-api-2.x` | `prometheus` | — | — |
| `proxmox-api-8.x` | `proxmox` | >=7.0,<9.0 | — |
| `rabbitmq-management-3.x` | `rabbitmq` | >=3.8,<5.0 | — |
| `rke2-ssh-1.x` | `rke2` | — | — |
| `sddc-rest-9.0` | `sddc` | >=9.0,<10.0 | generic |
| `sddc-vcf5-5.0` | `sddc` | >=5.0,<9.0 | — |
| `tempo-api-2.x` | `tempo` | >=2.0,<3.0 | — |
| `vault-1.x` | `vault` | — | typed |
| `vcd-rest-10.6` | `vcd` | >=10.0,<11.0 | — |
| `vcfa-rest-9.0` | `vcfa` | >=9.0,<10.0 | — |
| `vcfa-vra8-8.0` | `vcfa` | >=8.0,<9.0 | — |
| `vmware-rest-9.0` | `vmware` | >=8.5,<10.0 | generic |
| `vrli-rest-9.0` | `vrli` | >=9.0,<10.0 | — |
| `vrli-vrli8-8.0` | `vrli` | >=8.0,<9.0 | — |
| `vrops-rest-9.0` | `vrops` | >=9.0,<10.0 | — |
| `vrops-vrops8-8.0` | `vrops` | >=8.0,<9.0 | — |
| `windns-ssh-2016.x` | `windns` | — | — |
| `winsrv-ssh-2022.x` | `winsrv` | — | — |
| `wsfc-ssh-2022.x` | `wsfc` | — | — |

## Versions and how a connector is chosen

**Supported versions** is the product-version range an implementation advertises. Where a product has more than one implementation — a modern and a legacy one, say — both are registered and MEHO resolves the right one per target from the target's fingerprint, so one estate spanning old and new versions just works.

## Maturity and readiness

This inventory lists what is *registered*; it does not restate a ship-state. Feature-level maturity — GA, beta, or experimental — is published in the [feature maturity index](maturity.md). Per-release, per-connector ship-state (dispatch + catalog, loader-wired, or production-ready) is stated in the [changelog](https://github.com/evoila/meho/blob/main/CHANGELOG.md) under the connector release-notes convention. The two-connector model is described in the [architecture overview](../architecture.md).
