<!--
SPDX-License-Identifier: AGPL-3.0-only
Copyright (c) 2026 evoila Group
-->

# Topology Resolution Layer

The resolution layer is what turns MEHO's topology graph from a bag of
per-connector entities into a cross-system graph. It decides when a
Kubernetes Node and a VMware VM are the *same* physical host, when a
GCP Instance is the underlying compute for a K8s Node, and when a
REST connector's `base_url` points at a discovered Ingress. Every one
of those cross-system links — a `SAME_AS` edge — is produced by the
code documented here.

This file is the mental-model reference for anyone touching
`meho_app/modules/topology/resolution/`, `meho_app/modules/topology/clustering.py`,
`meho_app/modules/topology/hostname_matcher.py`, the relevant parts of
`meho_app/modules/topology/service.py`, and the `topology_same_as*` tables.

## Overview

Resolution is a two-phase pipeline:

1. **Discovery** produces a candidate — a pair of entities that may
   represent the same underlying resource — and writes it into
   `topology_same_as_suggestion` (pending) or, for high-confidence
   deterministic evidence, directly into `topology_same_as` (confirmed).
2. **Verdict** decides whether a pending suggestion becomes a confirmed
   `SAME_AS` link, a `rejected` dead-end (still stored, to prevent
   re-discovery), or stays `pending` for a human.

There are three independent discovery paths and one verdict path.
They all share a single storage shape and a single eligibility guard.

## Storage shape

Five Postgres tables, all in `meho_app/modules/topology/models.py`, all
created in the single squashed migration
`meho_app/modules/topology/alembic/versions/0001_squash.py`.

| Table | Role |
|---|---|
| `topology_entities` | Every discovered resource. Connector-owned (pod, VM) or external (URL). |
| `topology_embeddings` | 1024-D Voyage AI vector per entity. HNSW index with cosine-ops. |
| `topology_relationships` | Directed within-connector edges (e.g. `Pod -runs_on-> Node`). |
| `topology_same_as` | Confirmed cross-connector correlations. |
| `topology_same_as_suggestion` | Pending correlations awaiting a verdict. |

### Entity identity

An entity is uniquely named by a quad, not by its display name:

```
(tenant_id, connector_id, entity_type, canonical_id)
```

Enforced by `idx_topology_entity_identity` (unique index, see models.py).
`canonical_id` is built by `ConnectorTopologySchema.build_canonical_id`
in `meho_app/modules/topology/schema/base.py` and encodes the entity's
scope: a Pod in namespace `prod` named `nginx` has `canonical_id =
"prod/nginx"`; a VMware VM with moref `vm-42` has `canonical_id = "vm-42"`.
The `name` column is display-only.

This matters for resolution: an entity is unique *within its own connector*
by construction. Two rows that look like the same thing can only come from
different connectors, which is exactly the condition the resolver expects.

### `verified_via` provenance

`topology_same_as.verified_via` is a `TEXT[]` column that records how a
confirmed link was produced. Typical values:

- `["user_approved", "hostname_match"]`
- `["embedding_similarity", "llm_analysis", "IP: 10.0.0.5"]`
- `["deterministic_resolution", "match_type:ip_address",
   "matched_values:{\"matched_ip\":\"192.168.1.10\"}", "confidence:1.0"]`

The last shape — JSON inside a string inside an array column — is a
known tech-debt smell; see Known issues below.

### Cascade semantics

Every foreign key to `topology_entities(id)` is `ON DELETE CASCADE`. A
connector's own topology entity referenced by
`connectors.topology_entity_id` therefore cleans up embeddings,
relationships, same-as rows, and suggestions atomically when the
connector is deleted.

## The three discovery paths

### Path C — Deterministic attribute resolution

The strictest, and the preferred path. Lives in
`meho_app/modules/topology/resolution/`.

- `DeterministicResolver` (resolver.py) orchestrates a priority-sorted
  chain of matchers.
- Three matchers implement `BaseMatcher`:
  - `ProviderIDMatcher` (priority 1): parses K8s `spec.providerID`
    values (GCE / vSphere / AWS EKS / Azure AKS) and compares against
    cloud-VM identifiers.
  - `IPAddressMatcher` (priority 2): extracts IPs from
    connector-specific raw_attributes and intersects the sets.
  - `HostnameMatcher` (resolution/matchers/hostname.py, priority 3):
    extracts and normalizes hostnames, then intersects.
- Every successful match returns `MatchEvidence(match_type,
  matched_values, confidence, auto_confirm)`.

Flow:

```
resolve_pair(a, b):
  if a.connector_id == b.connector_id: return None   # SAME_AS is cross-connector only
  if not _are_eligible(a, b):         return None   # schema-driven guard (see below)
  for matcher in priority_order:
      ev = matcher.match(a, b)
      if ev: return ev
  return None
```

`auto_confirm=True` evidence causes `TopologyService.resolve_entity_pair`
to write a confirmed `topology_same_as` row directly. `auto_confirm=False`
evidence (not currently produced by any matcher but reserved) would write
a pending suggestion.

Matchers are bidirectional: each `match(a, b)` must try both orderings
of the pair, because callers (notably `resolve_batch`) do not know which
entity is the K8s side vs. the cloud side.

### Path A — Hostname/IP match against connector targets

Lives in `meho_app/modules/topology/hostname_matcher.py` (the legacy
top-level file). Triggered when a new topology entity is stored and
detects that the entity's hostname or IP matches some connector's
`base_url` target.

Examples:
- K8s Ingress with host `api.myapp.com` plus a REST connector targeting
  `https://api.myapp.com/...` — the Ingress gets a `SAME_AS` to that
  connector's own topology entity.
- VMware VM with guest IP `192.168.1.10` plus a REST connector targeting
  `http://192.168.1.10:8080/...` — same pattern.

The flow writes a `topology_same_as_suggestion`, then routes it based on
confidence:

| Confidence band | Action |
|---|---|
| `>= suggestion_auto_approve_threshold` (default 0.90) | auto-approve — creates confirmed `SAME_AS` |
| `>= suggestion_llm_verify_threshold` (default 0.70) | hand to LLM verifier (verdict path) |
| below | left pending for manual review |

Confidence per match type: hostname exact = 0.95, IP exact = 0.90,
partial hostname = 0.70.

This path's class name is currently `HostnameMatcher`, which collides
with the resolution-layer `HostnameMatcher` — see Known issues.

### Path B — Embedding-similarity clustering

Lives in `meho_app/modules/topology/clustering.py`. Runs periodically
or on demand (not triggered per-entity). Scans every cross-connector
entity pair whose embeddings are cosine-similar above a threshold,
filters by eligibility, and writes pending suggestions for new candidates.

The heavy lifting is a single raw SQL query —
`TopologyRepository.find_cross_connector_similar_pairs` in
`meho_app/modules/topology/repository.py` — that self-joins the
`topology_embeddings` table with `a.entity_id < b.entity_id` to produce
each pair exactly once, filters for different connectors, and orders by
pgvector's cosine distance (`<=>` operator). The HNSW index on
`topology_embeddings` makes the ordering tractable.

This path's confidence equals the cosine similarity of the embeddings.
Because embedding similarity alone is not assertional (two different
K8s clusters can produce near-identical pod descriptions), Path B
suggestions almost always route through the LLM verifier.

## The eligibility guard

Before any matcher or clustering compares a pair, the pair must pass
`SameAsEligibility`. Declared in
`meho_app/modules/topology/schema/base.py`:

```python
@dataclass
class SameAsEligibility:
    can_match: list[str]         # whitelist of entity types
    matching_attributes: list[str] # informational hints (JMESPath)
    never_match: list[str]        # blacklist (overrides can_match)

    def can_correlate_with(self, other_entity_type: str) -> bool: ...
```

Attached per-entity-type on each connector's topology schema. Example
from `meho_app/modules/topology/schema/kubernetes.py`:

- `Node.same_as = SameAsEligibility(can_match=["VM", "Instance", "Host"], ...)`
- `Pod.same_as = None` — a sentinel meaning "this entity type cannot
  participate in any cross-system correlation" (pods are ephemeral;
  correlating them to VMs would produce stale links within hours).

Eligibility is consulted by both the resolver and clustering, but with
**different semantics** — the resolver requires both sides to agree
(AND), clustering accepts either side agreeing (OR). See Known issues.

## The verdict path (LLM verifier)

For pending suggestions in the mid-confidence band
(`suggestion_llm_verify_threshold <= confidence <
suggestion_auto_approve_threshold`), `SuggestionVerifier` in
`meho_app/modules/topology/suggestion_verifier.py` calls a
classifier-model LLM (default: `config.classifier_model`) via
`confirm_same_as_with_llm` in
`meho_app/modules/topology/correlation.py`.

The LLM is instructed to return structured JSON matching:

```python
class LLMCorrelationResult(BaseModel):
    is_same_resource: bool
    confidence: float                      # 0.0 - 1.0
    reasoning: str
    matching_identifiers: list[str]
```

The `pydantic_ai.Agent(..., output_type=LLMCorrelationResult)`
construction feeds the JSON schema of the model (including `Field`
descriptions) to the LLM, so parse failures are rare.

The verdict table:

| LLM output | Action |
|---|---|
| `is_same_resource=True` and `confidence >= threshold` | approve → confirmed `SAME_AS` |
| `is_same_resource=False` and `confidence >= threshold` | reject → suggestion marked `rejected` (kept, prevents re-discovery) |
| below threshold or call fails | stay `pending` for manual review |

The full LLM response is always persisted to
`topology_same_as_suggestion.llm_verification_result` (JSONB), so the
*why* is visible to a human reviewer even for rejected or uncertain
suggestions. `llm_verification_attempted` flips to `true` before the
status update, which means a failed LLM call does not cause an infinite
retry loop.

## Traversal — the resolution layer's exit

Resolution's output is consumed by `TopologyService.lookup` in
`meho_app/modules/topology/service.py`, which the agent's
`lookup_topology` tool calls. Three stages:

1. **Find the starting entity** — exact-name match via
   `get_entity_by_name`, falling back to semantic search via
   `_semantic_search` (which reuses the same embedding infrastructure
   Path B uses for clustering).
2. **Walk the graph** — `TopologyRepository.traverse_topology` does a
   depth-first DFS with a visited-set and `max_depth` bound. Direct
   relationships and `SAME_AS` edges both cost one depth step. Tenant
   safety is enforced at every step (the `tenant_id` check is redundant
   with the SAME_AS query filter, on purpose — defense in depth).
3. **Collect context** — three distinct shapes returned to the agent:
   - `topology_chain`: the ordered DFS walk.
   - `same_as_entities`: confirmed cross-system equivalents with
     `verified_via` provenance.
   - `possibly_related`: soft candidates from embedding similarity
     that have not (yet) been confirmed.

The distinction between `same_as_entities` and `possibly_related` is
load-bearing — the agent should reason about them differently.

## Dependencies

- Reads from: `topology_entities`, `topology_embeddings`,
  `topology_relationships`, `topology_same_as`,
  `topology_same_as_suggestion`, `connectors` (for `base_url` and
  `topology_entity_id`).
- Writes to: `topology_same_as`, `topology_same_as_suggestion`
  (verdict path also updates `llm_verification_attempted`,
  `llm_verification_result`, `status`, `resolved_*`).
- External services: Voyage AI embeddings (via
  `TopologyEmbeddingService`), pydantic-ai + configured classifier LLM
  (for Path B / verdict path).
- Configuration: `suggestion_auto_approve_threshold`,
  `suggestion_llm_verify_threshold`,
  `suggestion_llm_approve_confidence`, `classifier_model` — all from
  `meho_app/core/config.py`.

## Known issues

All items are filed under initiative #388 (Topology resolution layer —
correctness, hygiene, and safe scaling). Items 5 and 13 are deferred;
the rest are active.

1. **Duplicate `HostnameMatcher` class name.** The legacy
   `hostname_matcher.py:HostnameMatcher` (entity-to-connector-target)
   and the resolution-layer
   `resolution/matchers/hostname.py:HostnameMatcher`
   (entity-to-entity) share a class name. Rename the legacy one to
   better reflect its actual role. — #389
2. **`verified_via` is stringly-typed provenance.** JSON-in-strings
   wedged into `TEXT[]`. Replace with a proper JSONB schema matching
   the pattern already used by `llm_verification_result`. — #390
3. **Eligibility symmetry is inconsistent.** `DeterministicResolver`
   uses AND-symmetry; `ClusteringService` uses OR-symmetry. Same
   concept, different answers for the same pair. Extract a single
   `SameAsEligibilityChecker` and pick one semantic (AND recommended).
   — #391
4. **`traverse_topology` is N+1 against Postgres.** Each recursive
   step issues 3 queries. For `max_depth=10` that is dozens to hundreds
   of round trips per lookup. Replace with a single recursive CTE. —
   #392
5. **`resolve_batch` is O(n·m) with no blocking.** Acceptable at
   current scale, but will not survive past a few thousand entities per
   batch. **Deferred** — filed with a scaling trigger (#393); do not
   schedule until trigger fires.
6. **SAME_AS tenant invariant enforced only at the service layer.**
   A trigger on `topology_same_as` should enforce that
   `entity_a.tenant_id == entity_b.tenant_id == NEW.tenant_id`, so
   raw-SQL paths cannot bypass it. — #394
7. **`MatchEvidence` is mutable.** Should be
   `@dataclass(frozen=True, slots=True)` — it is conceptually an
   immutable finding snapshot. — #395
8. **`ProviderIDMatcher._try_match` has four near-duplicate cloud
   blocks.** Introduce a `ProviderIDParser` protocol with one
   implementation per cloud to make the fifth cloud a drop-in. — #396
9. **SAME_AS uniqueness is ordered but the concept is unordered.** The
   unique indexes on `topology_same_as` and
   `topology_same_as_suggestion` key on the ordered triple
   `(entity_a_id, entity_b_id, ...)`, but SAME_AS is symmetric. Two
   producer paths (`store_discovery`, `store_same_as`) do not pre-check
   both directions, so `(A, B, tenant)` and `(B, A, tenant)` both
   succeed. Replace with functional unique indexes on
   `(LEAST(a, b), GREATEST(a, b), tenant)` and normalize pair ordering
   at insert. — #403
10. **`store_discovery` holds a DB transaction across an external API
    call.** `await embedding_service.generate_embedding(...)` runs
    inside the open transaction. A slow Voyage AI response holds row
    locks for the duration. Split the transaction so DB writes commit
    before external I/O. — #404
11. **Embedding generation is sequential per entity.** For 100 new
    entities, 100 serial Voyage AI calls. Voyage's batch-embed API
    accepts up to 128 inputs per call at the same latency as one
    input. — #405
12. **LLM correlation failures are silent and not retried.** A
    transient 5xx/429 from the classifier LLM drops a verification
    attempt, logged only at WARN with no metric. Add bounded retry
    with exponential backoff, and emit a structured failure signal so
    sustained outages become visible. — #406
13. **pgvector `ef_search` is not tunable at query time.** The HNSW
    index has `m=16, ef_construction=64` baked in; query-time
    `ef_search` takes Postgres's default (40). Low priority — default
    is fine at current scale. **Deferred** — filed with an
    observability trigger (#407); do not schedule until trigger fires.

## Python-specific notes

- **Priority ordering via `IntEnum`.** `MatchPriority` uses
  `IntEnum` so lower numeric value means higher priority (PROVIDER_ID=1,
  IP_ADDRESS=2, HOSTNAME=3). `DeterministicResolver.__init__` sorts by
  `m.priority`, so adding a new matcher means picking a priority and
  handing it in — no branching in the orchestrator.
- **Bidirectional match attempts.** Every matcher tries both
  `match(a, b)` and `match(b, a)` internally. The inner `_try_match`
  helpers are the ones that assume a fixed ordering; the public
  `match` is symmetric. Don't call `_try_match` from outside a matcher.
- **Set intersection as the matching primitive.** `IPAddressMatcher`
  and `HostnameMatcher` both produce a `set` of candidates per entity
  and intersect. `ipaddress.ip_address` normalizes IPv4/IPv6 so
  string-formatted variants hash equal. `normalize_hostname` iteratively
  strips domain suffixes until no more can be stripped, then lowercases.
- **`contextlib.suppress(ValueError, TypeError)`** is used in
  `IPAddressMatcher._safe_add_ip` to silently drop malformed input.
  This is idiomatic; replacing with an explicit try/except would be
  equivalent and longer.
- **`next(iter(overlap))`** in `IPAddressMatcher.match` picks an
  arbitrary set element. The match result is still correct; only the
  reported `matched_ip` is nondeterministic when two IPs overlap.
- **Raw SQL in `find_cross_connector_similar_pairs`.** The self-join
  and pgvector `<=>` operator are cleaner in raw SQL than through the
  ORM. Parameters are always bound (`:tenant_id`, `:max_distance`) — no
  injection risk.
- **`@dataclass(frozen=True)` is not currently used on
  `MatchEvidence`**, see Known issue 7.
- **`# noqa: S110` on bare `except Exception: pass`** in the URL
  parsing fallbacks is acknowledging a Bandit lint. Narrowing to
  `except (ValueError, TypeError):` would pass the lint and the audit.

## References

- Schema-driven eligibility: `meho_app/modules/topology/schema/base.py`,
  examples in `schema/kubernetes.py`, `schema/vmware.py`, `schema/gcp.py`,
  `schema/proxmox.py`, `schema/argocd.py`, `schema/github.py`,
  `schema/network_diagnostics.py`.
- Cascade delete semantics:
  `meho_app/modules/topology/alembic/versions/0001_squash.py`.
- ReAct-side consumption (not in scope here):
  `meho_app/modules/agents/react_agent/tools/lookup_topology.py`,
  `meho_app/modules/agents/shared/graph/nodes/topology_lookup_node.py`.
- Frontend visualization (not in scope here):
  `meho_frontend/src/components/topology/`.
- pgvector operators: https://github.com/pgvector/pgvector#querying.
- pydantic-ai structured output:
  https://ai.pydantic.dev/agents/#structured-output.
