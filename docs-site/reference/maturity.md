<!--
  GENERATED FILE — do not edit by hand (#2678).
  Source of truth: backend/src/meho_backplane/features.py
  (FEATURE_MATURITY). Regenerate from backend/ with:
      uv run python scripts/generate_maturity_index.py
  The freshness gate in
  backend/tests/test_maturity_surface_drift.py fails CI when
  this page and the registry disagree.
-->

# Feature maturity index

Every MEHO feature carries an explicit maturity tier — **GA**, **Beta**, or **Experimental** — declared once in the feature-maturity registry ([`backend/src/meho_backplane/features.py`](https://github.com/evoila/meho/blob/main/backend/src/meho_backplane/features.py)) and propagated to every surface you touch: `[beta]` / `[experimental]` prefixes on MCP tool descriptions, `x-maturity` in the REST OpenAPI document, CLI help labels, and the `/ui` console's area badges (which link to this page). This page is the road-to-prod-ready roadmap for every non-GA feature: what tier it is in, the milestone it targets, and the issue where its gaps and promotion gate are tracked.

The classification is the provisional [#2664](https://github.com/evoila/meho/issues/2664) table, pending clean-room eval round 1 ([#2665](https://github.com/evoila/meho/issues/2665)).

## GA features

Carries the 1.0 stability promise. Entry criteria: clean-room eval score ≥ 4 for both usefulness and correctness; works on both credential backends where applicable; contract surfaces under the [#2662](https://github.com/evoila/meho/issues/2662) stability gates; a docs task-guide page; no open P1s.

- `approvals`
- `audit`
- `auth_tenancy`
- `memory_knowledge`
- `net_diagnostics`
- `targets`
- `typed_connector_reads`

## Beta features

Works end-to-end somewhere real; gaps are known and tracked on the feature's tracking issue (its promotion gate); surfaces may change with a deprecation notice.

| Feature | Target GA | Gaps & promotion gate |
| --- | --- | --- |
| [broadcast](#broadcast) | v1.0.0 | [#2665](https://github.com/evoila/meho/issues/2665) |
| [gsm_backend](#gsm_backend) | v1.0.0 | [#2667](https://github.com/evoila/meho/issues/2667) |
| [satellite_gateway](#satellite_gateway) | v1.0.0 | [#2665](https://github.com/evoila/meho/issues/2665) |
| [scheduler](#scheduler) | v1.0.0 | [#2668](https://github.com/evoila/meho/issues/2668) |
| [sensors](#sensors) | v1.0.0 | [#2668](https://github.com/evoila/meho/issues/2668) |
| [topology](#topology) | v1.0.0 | [#2665](https://github.com/evoila/meho/issues/2665) |
| [ui_console](#ui_console) | v1.0.0 | [#2665](https://github.com/evoila/meho/issues/2665) |
| [write_surfaces](#write_surfaces) | v1.0.0 | [#2665](https://github.com/evoila/meho/issues/2665) |

### broadcast

- **Tier:** beta
- **Target GA milestone:** v1.0.0
- **Gaps & promotion gate:** known gaps and the road to promotion are tracked in [#2665](https://github.com/evoila/meho/issues/2665).

### gsm_backend

- **Tier:** beta
- **Target GA milestone:** v1.0.0
- **Gaps & promotion gate:** known gaps and the road to promotion are tracked in [#2667](https://github.com/evoila/meho/issues/2667).

### satellite_gateway

- **Tier:** beta
- **Target GA milestone:** v1.0.0
- **Gaps & promotion gate:** known gaps and the road to promotion are tracked in [#2665](https://github.com/evoila/meho/issues/2665).

### scheduler

- **Tier:** beta
- **Target GA milestone:** v1.0.0
- **Gaps & promotion gate:** known gaps and the road to promotion are tracked in [#2668](https://github.com/evoila/meho/issues/2668).

### sensors

- **Tier:** beta
- **Target GA milestone:** v1.0.0
- **Gaps & promotion gate:** known gaps and the road to promotion are tracked in [#2668](https://github.com/evoila/meho/issues/2668).

### topology

- **Tier:** beta
- **Target GA milestone:** v1.0.0
- **Gaps & promotion gate:** known gaps and the road to promotion are tracked in [#2665](https://github.com/evoila/meho/issues/2665).

### ui_console

- **Tier:** beta
- **Target GA milestone:** v1.0.0
- **Gaps & promotion gate:** known gaps and the road to promotion are tracked in [#2665](https://github.com/evoila/meho/issues/2665).

### write_surfaces

- **Tier:** beta
- **Target GA milestone:** v1.0.0
- **Gaps & promotion gate:** known gaps and the road to promotion are tracked in [#2665](https://github.com/evoila/meho/issues/2665).

## Experimental features

May change or vanish without notice; sits outside the 1.0 stability promise. No committed GA milestone unless stated.

| Feature | Target GA | Gaps & promotion gate |
| --- | --- | --- |
| [agent_runtime](#agent_runtime) | _none committed_ | [#2656](https://github.com/evoila/meho/issues/2656) |
| [connector_ingest](#connector_ingest) | _none committed_ | [#2661](https://github.com/evoila/meho/issues/2661) |
| [doc_collections](#doc_collections) | _none committed_ | [#2661](https://github.com/evoila/meho/issues/2661) |
| [two_world_ops](#two_world_ops) | _none committed_ | [#2661](https://github.com/evoila/meho/issues/2661) |

### agent_runtime

- **Tier:** experimental
- **Target GA milestone:** none committed — outside the 1.0 promise
- **Gaps & promotion gate:** known gaps and the road to promotion are tracked in [#2656](https://github.com/evoila/meho/issues/2656).

### connector_ingest

- **Tier:** experimental
- **Target GA milestone:** none committed — outside the 1.0 promise
- **Gaps & promotion gate:** known gaps and the road to promotion are tracked in [#2661](https://github.com/evoila/meho/issues/2661).

### doc_collections

- **Tier:** experimental
- **Target GA milestone:** none committed — outside the 1.0 promise
- **Gaps & promotion gate:** known gaps and the road to promotion are tracked in [#2661](https://github.com/evoila/meho/issues/2661).

### two_world_ops

- **Tier:** experimental
- **Target GA milestone:** none committed — outside the 1.0 promise
- **Gaps & promotion gate:** known gaps and the road to promotion are tracked in [#2661](https://github.com/evoila/meho/issues/2661).
