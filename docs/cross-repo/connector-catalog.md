<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Connector-spec catalog

> Operator-facing reference for the curated connector-spec catalog
> (Goal [#214](https://github.com/evoila/meho/issues/214) raw-REST ingest
> on-ramp; Task [#743](https://github.com/evoila/meho/issues/743)). The
> ingestion mechanics live in
> [`connector-ingestion.md`](connector-ingestion.md); this doc is the map
> of *which* spec to ingest for each shipped connector and *where* the
> vendor hosts it.

## Why this exists

MEHO's generic-ingestion path (the G0.7 pipeline) supports the full vendor
REST surface — but only once an operator ingests the vendor's OpenAPI
spec. Doing that by hand means knowing four things per connector: that a
connector is generic-ingestable at all, where the vendor publishes its
spec, which `spec.info.version` maps to which release, and which version
label aligns with a registered connector class. The v0.3.0 RDC dogfood
showed that friction directly: operators concluded "the vmware connector
is 13 composites and that's it" because the ingest on-ramp was implicit.

The catalog makes that knowledge a machine-readable artifact: one entry
per `(product, version)` with the recommended spec source(s) and the
connector class that covers the label.

## Where the catalog lives

The data file ships as **package data** at
[`backend/src/meho_backplane/operations/ingest/catalog.yaml`](../../backend/src/meho_backplane/operations/ingest/catalog.yaml),
colocated with its loader
([`catalog.py`](../../backend/src/meho_backplane/operations/ingest/catalog.py)).

> **Note on the path.** Task #743 first named `docs/connector-specs/
> catalog.yaml`, but the backend image build context is `backend/` and the
> wheel only packages `src/meho_backplane`, so a repo-root `docs/` file is
> not present in a deployed container. The catalog must be readable at
> startup and over the API, so it ships as package data resolved through
> `importlib.resources` — the same pattern as `alembic.ini` / `alembic/`
> (see `find_alembic_ini` in `db/migrations.py`). This document is the
> discoverable pointer in its place.

The backplane loads + validates the catalog at startup (a malformed
catalog crashes the lifespan, so CI's app-boot smoke fails) and serves it
read-only at `GET /api/v1/connectors/catalog`.

## Entry schema

Each entry is validated by `ConnectorSpecEntry`:

| Field | Meaning |
|---|---|
| `product` / `version` / `impl_id` | The connector triple. `(product, version)` is unique and is the `--catalog <product>/<version>` lookup key. |
| `requires_connector_class` | The registered connector class `__name__` that covers this version label. A regression test asserts every value is present in `all_connectors_v2()`. |
| `upstream` | Public URL(s) where the vendor hosts the spec. Operators (and the CLI) fetch directly — MEHO does not redistribute. `null` marks a **typed** connector with no ingestable spec. |
| `spec_info_version` | The `spec.info.version` MEHO observed when smoke-testing the spec; PEP 440 when present. `null` until a connector's spec is ingest-verified through MEHO — these are empirical values, not guesses. |
| `sha256` | Advisory content hash MEHO smoke-tested against; optional, not enforced. |
| `notes` | Operator context: sharp edges, version quirks, appliance-served vs portal-hosted, curation status. |

## Current entries

| Product / version | Shape | Spec source |
|---|---|---|
| `vmware` 9.0 | generic | `vcenter.yaml` + `vi-json.yaml` (appliance-served at `https://<vcenter-fqdn>/`; mirrored on the Broadcom Developer Portal) |
| `sddc-manager` 9.0 | generic | SDDC Manager API (appliance-served; Broadcom Developer Portal) |
| `harbor` 2.x | generic | `goharbor` `api/v2.0/swagger.yaml` — **Swagger 2.0**, needs conversion to OpenAPI 3.x before ingest |
| `nsx` 4.2 | generic | `/api/v1/spec/openapi/nsx_api.yaml` (appliance-served; Broadcom Developer Portal mirror) |
| `vault` 1.x | typed | none (hand-coded connector) |
| `k8s` 1.x | typed | none (typed; per-minor OpenAPI ingest is a future Goal #214 investigation) |
| `bind9` 9.x | typed | none (SSH-only; no REST surface) |

`spec_info_version` and `sha256` are `null` across the board in this first
catalog: they are populated only after a connector's spec has been
ingest-verified through MEHO, not from desk research. Fill them in as part
of each connector's first verified `--catalog` ingest.

## Operator workflow

1. `meho connector catalog list` — see which `(product, version)` entries
   exist and which connector classes are registered.
2. `meho connector ingest --catalog <product>/<version>` — resolve the
   entry, fetch the upstream spec(s), and ingest under the recommended
   triple.
3. `meho connector review <connector_id>` → `meho connector enable <id>` —
   vet the LLM-summarised groups and turn the operations on.

> The `catalog list` and `ingest --catalog` verbs are tracked in
> [#915](https://github.com/evoila/meho/issues/915) (the CLI half split
> out of #743 per the connector `-T` convention). Until they land, use the
> explicit `meho connector ingest --spec <url>` form from
> [`connector-ingestion.md`](connector-ingestion.md), reading the `upstream`
> URL(s) from the catalog by hand.

## Adding or updating an entry

1. Add/edit the entry in
   [`catalog.yaml`](../../backend/src/meho_backplane/operations/ingest/catalog.yaml).
2. Set `requires_connector_class` to a class that is actually registered
   (`all_connectors_v2()`), or the regression test
   (`backend/tests/test_operations_ingest_catalog.py`) fails.
3. Populate `spec_info_version` / `sha256` only from a real
   MEHO-verified ingest of that spec.
4. Run `uv run pytest tests/test_operations_ingest_catalog.py` from
   `backend/`.

## References

- [`connector-ingestion.md`](connector-ingestion.md) — the ingest runbook
  this catalog feeds.
- [`docs/codebase/connector-release-readiness.md`](../codebase/connector-release-readiness.md)
  — the three-state (composite / dispatch-catalog / loader-wired) rubric
  the catalog operationalises.
- [Broadcom Developer Portal](https://developer.broadcom.com/xapis) —
  vSphere, NSX, SDDC Manager OpenAPI references.
- [Harbor swagger](https://github.com/goharbor/harbor/blob/main/api/v2.0/swagger.yaml).
