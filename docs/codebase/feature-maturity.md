# Feature-maturity registry

## Overview

`backend/src/meho_backplane/features.py` is the single source of truth
for feature maturity (#2664 / #2674). `FEATURE_MATURITY` maps every
user-facing MEHO feature to a tier — `ga` / `beta` / `experimental` —
plus, on non-GA entries, the `target_ga` milestone and the `tracking`
issue URL. Every surface that shows a maturity label derives from this
one dict; reclassifying a feature is a one-line data edit here with no
surface-specific changes anywhere (the v0.28 post-eval round in
Goal #2661 depends on that property).

Tier semantics (#2664 entry criteria): `ga` carries the 1.0 stability
promise; `beta` works end-to-end somewhere real with known, tracked
gaps and may change with deprecation notice; `experimental` may change
or vanish and sits outside the 1.0 promise. The encoded classification
is the **provisional** #2664 table, pending clean-room eval round 1
(#2665).

## Key types

- `Maturity` — `Literal["ga", "beta", "experimental"]`.
- `MaturityInfo` — `TypedDict` for one registry entry. `target_ga` and
  `tracking` are `NotRequired`: absent on GA entries (same
  absent-not-null convention as `docs` on the `/ready` gates);
  `target_ga` may be `None` on experimental entries because several
  are explicit 1.0 non-goals — advertising a committed milestone would
  overstate maturity, the exact failure this program exists to fix.
- `FEATURE_MATURITY: dict[str, MaturityInfo]` — the registry. Keys are
  the canonical feature identifiers all consuming surfaces map onto;
  renaming a key is a cross-surface break (pinned by
  `test_maturity_registry_covers_the_2664_classification`).
- `_READY_ENTRY_FEATURE` — maps the `/ready` features-block entries
  (deploy-gate names) to registry keys (feature names). The `mcp`
  entry is deliberately unmapped: it reports a build-time protocol
  constant, not a classified feature — MCP tools will inherit maturity
  from their owning feature at registration time (#2675).

## Control flow

`build_features_block(settings)` builds the pre-existing deploy-gate
entries, then merges the mapped registry entries' fields into them
(`maturity` always; `target_ga` / `tracking` on non-GA). The merge
copies into fresh per-call dicts — no aliasing of the module-level
registry, preserving the module's purity contract (pure function over
a `Settings` snapshot; tests pin this in
`test_builder_stays_pure_and_does_not_alias_the_registry`).

Consuming surfaces shipped so far (all read `FEATURE_MATURITY`
in-process — there is deliberately no dedicated REST read surface):

- `/ready` — carries the merged view for operator tooling (#2674).
- **MCP tool descriptions** (#2675) — every
  `mcp/registry.py::ToolDefinition` declares a required `feature`
  field (a registry key, or an explicit `None` for
  deliberately-unclassified surfaces such as `meho.status` and the
  runbooks family, which the provisional #2664 table does not
  classify). `register_mcp_tool` prefixes non-GA descriptions with
  `[beta]` / `[experimental]` at registration time; an unknown key
  fails validation at construction.
- **`initialize.instructions`** (#2675) —
  `mcp/maturity.py::FEATURE_MATURITY_BAND` (a registry-derived static
  band listing exactly the non-GA features) is appended after the
  assembled preamble by `mcp/server.py::_session_instructions`. Keys
  only — no tracking URLs, to honour the #1137 forbidden-token
  contract.
- **OpenAPI `x-maturity`** (#2675) —
  `api/openapi_maturity.py::inject_maturity_extensions` stamps
  top-level tag entries (mapping in `TAG_FEATURE`) plus per-operation
  overrides where a tag spans tiers (`PATH_FEATURE_OVERRIDES`; today
  the `connectors` tag's ingest-pipeline paths). Flows into the
  committed `cli/api/openapi.json` snapshot. `/ui*` tags and
  infrastructure tags (`health`, `version`, `mcp`) are deliberately
  unmapped.
- **`/ui` area-header badge chips** (#2677) —
  `meho_backplane.ui.maturity` maps sidebar surfaces onto registry
  keys and a shared Jinja include renders the chip; see
  `docs/codebase/ui.md`.

Remaining #2664 surface, not yet implemented: the docs-site
maturity-index generator plus CI drift guard (#2678). The drift
guard is where today's deliberately-unclassified surfaces (runbooks,
`meho.status`, the `conventions` REST tag) get
forced into an explicit classification decision.

The CLI's *client* half of #2676 is shipped:
`cli/internal/discovery/discovery.go`'s `Command` carries a
`maturity` field (`"beta"` / `"experimental"`; omitted for GA) and
`meho --help` renders the matching label after each dynamic command's
short description — absent or unrecognized tiers render no label, so
version skew in either direction degrades to unlabelled help. The
server half stays gated on the `/api/v1/commands` endpoint itself,
which is an unshipped Goal #11 §5 coordination point: today every
backplane 404s the manifest fetch and the CLI falls back to its
local-only command set. When the endpoint lands, its builder resolves
each advertised command's owning feature from `FEATURE_MATURITY` and
emits the field the CLI already understands.

## Dependencies

Stdlib `typing` (`Literal`, `TypedDict`, `NotRequired`) and
`meho_backplane.settings.Settings` only. Nothing outside the module
may re-declare tier values; tests assert tiers structurally and derive
expectations from the registry so a retier never requires a test edit.

## Known issues

- The classification is provisional until eval round 1 (#2665) scores
  it; the v0.28 reclassification (#2661 milestone plan) happens here.
- `tracking` URLs point at the best open issue today (concrete gap
  issues #2668 / #2667 / #2656, the eval #2665, or Goal #2661 for
  experimental features with no committed path). When a dedicated
  promotion-gate issue is filed for a feature, update its entry.

## References

- #2664 (initiative — classification table + entry criteria), #2674
  (this registry), #2675-#2678 (surface propagation), #2661 (Goal —
  milestone plan).
- `backend/tests/test_features.py` — registry contract tests;
  `backend/tests/test_features_doc_consistency.py` — deploy-gate doc
  parity (unchanged by #2674).
- `backend/tests/test_maturity_propagation.py` — #2675 surface tests;
  every expectation derives from the registry (a retier never
  requires a test edit).
