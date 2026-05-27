# Redaction (connector-boundary, Tier-1)

Initiative [#805](https://github.com/evoila/meho/issues/805) (G11.4 Safety,
C1) ships a sanitization middleware that redacts every connector response
before it reaches a caller or LLM. This document covers two landed
slices:

* The foundation
  ([#1070](https://github.com/evoila/meho/issues/1070)): declarative
  policy schema, Tier-1 named-pattern regex engine, named-pattern
  library.
* The connector-boundary wiring
  ([#1071](https://github.com/evoila/meho/issues/1071)): the
  middleware that sits in `dispatcher._reduce_or_error` and runs
  capture-raw → audit-raw → redact → reduce on every dispatch.

Pending sibling tickets: the Tier-2 Microsoft Presidio NER adapter
(C1-c, [#1072](https://github.com/evoila/meho/issues/1072)), the
round-trip CI gate (C1-d,
[#1073](https://github.com/evoila/meho/issues/1073)), and the
agent-invocation audit row (C2-b,
[#1074](https://github.com/evoila/meho/issues/1074)).

## Overview

A redaction **policy** is a YAML document declaring one or more
**rules**. Each rule binds a **named pattern** (a pre-compiled regex
known to the engine, e.g. `bearer_token`, `kubeconfig`, `uuid`) to an
**action** (`redact` / `mask` / `hash`) and optionally a **scope**
(connector_id, tenant, op) limiting which calls the rule fires on.

The **engine** walks a nested dict / list / str payload, applies the
rules in policy order, and returns:

- `redacted` — same *nesting* as the input, but normalised to
  JSON-shaped containers: `Mapping` subtypes (e.g. `OrderedDict`,
  `defaultdict`) flatten to `dict` and `Sequence` subtypes (other
  than `str` / `bytes` / `bytearray`) flatten to `list`. Only string
  leaves change content; everything else is structure. The result is
  always JSON-serialisable so the downstream JSONFlux reducer can
  consume it without further shape-fixing.
- `manifest` — a tuple of `RedactionManifestEntry` records, one per
  rule firing per leaf, carrying `rule`, `pattern`, `action`,
  `count`, `span`, `reason`, and `path`. C1-b will persist this
  verbatim into the audit row.

The engine is **pure and side-effect-free**: no I/O, no logging, no
clocks. Identical inputs produce identical outputs (load-bearing for
the C1-d round-trip CI gate).

## Key types

All types are Pydantic v2 frozen models, defined in
`backend/src/meho_backplane/redaction/`.

| Type | Module | Role |
| --- | --- | --- |
| `RedactionAction` | `policy.py` | `Literal["redact", "mask", "hash"]` |
| `RedactionScope` | `policy.py` | Optional connector_id/tenant/op predicate |
| `RedactionRule` | `policy.py` | One named-pattern → action binding |
| `RedactionPolicy` | `policy.py` | Versioned bundle of rules |
| `RedactionPolicyError` | `policy.py` | One typed exception for any parse / validation failure |
| `RedactionManifestEntry` | `engine.py` | One manifest row (rule, pattern, action, count, span, reason, path) |
| `RedactionResult` | `engine.py` | Engine return value: `redacted` + `manifest` |

`PATTERN_NAMES` (`patterns.py`) is the source of truth for the named
catalogue; the policy schema validates `rule.pattern` against it at
parse time.

## Control flow

```
                          parse_policy(yaml_str)
                          load_policy_yaml(pkg, file)
                                    |
                                    v
                          +-------------------+
                          | RedactionPolicy   |   immutable, frozen
                          | (rules in order)  |
                          +-------------------+
                                    |
       connector response  ─┐       |
       (dict / list / str)  │       v
                            └─►  redact(payload, policy,
                                        connector_id, tenant, op)
                                          |
                                          v
                              for each rule, in policy order:
                                   1. scope.matches(...) — skip if no
                                   2. walk dict/list/str leaves
                                   3. at each str leaf:
                                         pat.finditer(leaf)
                                         pat.sub(replacement, leaf)
                                         append RedactionManifestEntry
                                          |
                                          v
                              RedactionResult(redacted, manifest)
```

The engine **never raises** for an input shape it can't redact
(numbers, booleans, `None`, bytes pass through). The single failure
mode is `KeyError` from `get_pattern(...)` when a `RedactionRule` was
constructed via `model_construct` against an unknown name; the policy
schema's `field_validator` rejects unknown names at load time, so
production traffic cannot reach that branch.

## Named-pattern catalogue

| Name | Targets |
| --- | --- |
| `authorization_header` | `Authorization: Bearer ...` / `Basic ...` header lines |
| `bearer_token` | Bare `Bearer <opaque>` outside an Authorization header |
| `jwt` | `ey<base64url>.<base64url>.<base64url>` three-segment tokens |
| `api_key` | Labelled credential pairs (`api_key=`, `password:`, `client_secret=`...) |
| `kubeconfig` | YAML kubeconfig blobs (`apiVersion: v1` + `kind: Config`) |
| `uuid` | RFC 4122 canonical 8-4-4-4-12 hex |
| `ipv4` | Dotted-quad with 0-255 per-octet bounds |
| `ipv6` | Full + compressed RFC 5952 forms |
| `fqdn` | ≥2 labels with non-numeric TLD start |

Calibration notes (over-match vs under-match) are inline in
`patterns.py`. The Tier-1 posture deliberately leans toward
**over-redaction**: a false positive on a benign UUID is recoverable
via a scoped rule, but a missed secret is the failure the parent goal
(#800) cannot accept.

## Action semantics

| Action | Replacement |
| --- | --- |
| `redact` | `[REDACTED:<pattern>]` (fixed marker; default) |
| `mask` | `*…*<last-4-chars>` (length-preserving; shape-correlatable) |
| `hash` | `sha256:<first-12-hex-of-SHA-256(match)>` (stable; replay-correlatable) |

Choose `redact` for raw secrets, `mask` when downstream consumers
need to spot duplicates without seeing the value, and `hash` when an
audit replay needs to confirm the same identifier appears across
calls.

## Connector-boundary middleware (#1071)

The middleware lives at
`backend/src/meho_backplane/redaction/middleware.py` and is the seam
the dispatcher imports. Its public surface is one function and one
return type:

| Symbol | Module | Role |
| --- | --- | --- |
| `apply_connector_boundary_redaction(raw, *, connector_id, tenant, op)` | `middleware.py` | Resolves the policy, normalises *raw*, applies the engine, returns a `RedactionMiddlewareResult`. |
| `RedactionMiddlewareResult` | `middleware.py` | Carries `raw` (JSON-normalised input), `redacted` (engine output), `manifest` (tuple of entries), `policy_id` (resolved policy's id). |
| `manifest_to_audit_payload(manifest)` | `middleware.py` | Serialises the manifest to a list of plain dicts so the audit insert accepts it without per-row Pydantic round-trip. |
| `normalize_for_audit(value)` | `middleware.py` | Coerces a handler return value to JSON-shaped containers (Pydantic models flatten, tuples / sets / bytes normalised). |

### Policy resolution

`resolver.py` answers "which policy applies to this call?" via a six-step
specificity ladder, from most specific to least:

1. `(connector_id, tenant, op)` — full-tuple override.
2. `(connector_id, op)` — per-connector, per-op (tenant wildcard).
3. `(connector_id, tenant)` — per-connector, per-tenant.
4. `connector_id` — per-connector default.
5. `tenant` — tenant-wide default across every connector.
6. The packaged **default-safe** policy
   (`policies/default.yaml`) — the conservative fallback.

The default-safe policy is the load-bearing guarantee: a connector
landing without a registered override **still gets credentials
stripped** (`authorization_header`, `bearer_token`, `jwt`, `api_key`,
`kubeconfig`). Identifier patterns (`uuid` / `ipv4` / `ipv6` / `fqdn`)
are deliberately not in the default — most connector responses are
mostly identifiers, and a global default that redacts them would
blank operator-facing summaries. Operators wanting them at the global
level register a wider policy via `register_policy(policy)` (no
connector/tenant/op filter = global override).

Policy registration is process-global mutable state. Tests call
`clear_overrides()` in a fixture teardown to keep registrations from
leaking; production today runs without any overrides (per-tenant
policy authoring is a follow-on).

### Dispatcher integration

`dispatcher._execute_and_audit` calls the middleware once per
successful handler return, between "handler returned a raw response"
and "JSONFlux reducer consumes it":

```
       handler returns raw payload
                   │
                   ▼
       _apply_redaction_middleware(raw, connector_id, operator, op_id)
                   │
                   ▼
          RedactionMiddlewareResult
            │ raw / redacted / manifest / policy_id
            ▼
       _reduce_or_error(raw=redacted, raw_payload_for_audit=raw, …)
                   │
                   ▼
       audit_and_broadcast_safe(
           …,
           raw_payload=redaction.raw,
           redaction_manifest=manifest_to_audit_payload(redaction.manifest),
           redaction_policy_id=redaction.policy_id,
       )
```

The reducer (`JsonFluxReducer` in production, `PassThroughReducer`
in unit tests) sees the **redacted** payload; the caller and any
LLM consuming `OperationResult.result` therefore see redacted
strings. The raw payload only flows into the audit row, never out
of the dispatcher's `OperationResult`.

Middleware failures (regex compile error, default policy load
crash) are caught and converted to a structured `connector_error`
`OperationResult` so the dispatcher's never-raises contract is
preserved — a redactor failure must not leak raw payloads through a
500 with no audit record.

### Audit columns

Migration `0030` adds two nullable JSON columns to `audit_log`
(`raw_payload`, `redaction_manifest`); the resolved policy id is
mirrored into the existing `payload` JSON column as
`payload['redaction_policy_id']` so the broadcast event
(serialising `payload`, not the dedicated columns) carries policy
attribution. Pre-G11.4 audit rows leave both columns NULL.

Why two new columns rather than one `audit_log.payload` extension:

- `raw_payload` is potentially **large** (a connector list response
  can be megabytes); keeping it in a dedicated column lets the
  audit-query surface skip it on list endpoints without parsing
  JSON to find the field.
- `redaction_manifest` is **structured** (a list of dicts with a
  fixed schema); a future GIN index on the JSONB column (e.g.
  "find every audit row where the `kubeconfig` rule fired in the
  last 30 days") can be added without a column rewrite. The
  `payload` column already hosts heterogeneous handler-bound
  extras, so adding indexable redaction fields there would be a
  query-plan headache later.

Error-path audit rows (handler raised before producing a response)
have no raw payload to redact and leave both columns NULL — the
existing `result_status='error'` shape already records the
exception class / message in `payload`. Reducer-failure rows do
keep the redaction artefacts: the middleware ran successfully
before the reducer crashed, so the raw + manifest are still
recovery-grade evidence.

## Dependencies

- **PyYAML** (`yaml.safe_load`) — already a transitive dep; matches
  the precedent set by `operations/ingest/catalog.py`.
- **Pydantic v2** — already pinned in `backend/pyproject.toml`.
- **Python stdlib** — `re`, `hashlib`, `importlib.resources`,
  `collections.abc`, `threading` (resolver lock). No third-party
  regex / NER libraries here; Tier-2 (#1072) adds Microsoft Presidio.

No new runtime dependencies were added by Task #1070 or #1071.

## Known issues / future work

- **Wide patterns benefit from policy ordering.** The engine applies
  rules in policy order; a later rule sees the already-redacted
  output of earlier rules. Place narrow patterns (`api_key`,
  `bearer_token`) before wide ones (`fqdn`, `uuid`) in the YAML.
- **Multiple matches collapse into one manifest entry per leaf.** The
  manifest tracks `count` and the span of the *first* match; if an
  audit consumer needs per-match spans, that is a Tier-1 extension
  worth filing after C1-b lands and we see real consumption patterns.
- **Manifest `span` is indexed against the per-rule input, not the
  true original leaf.** When two rules fire on the same leaf in one
  policy, the second rule sees the already-redacted output of the
  first; the span it records is an offset into *that* rewritten
  string, not the original. For a single-rule policy the span equals
  the offset in the true original. Replay consumers that need to
  reconstruct the substring `span` indexes into must re-apply earlier
  rules in policy order first. The diagnostic value of `span` is
  consequently bounded; `count`, `rule`, and `path` remain the
  load-bearing manifest fields.
- **No per-tenant policy authoring path yet.** The resolver supports
  per-(connector_id, tenant, op) registration via `register_policy`
  (#1071), but there is no DB-backed policy table or operator-facing
  UI for landing rules — production today runs on the packaged
  default-safe policy only. The per-tenant authoring UX is a
  follow-on; the registration shape exists so the middleware can be
  exercised in tests and so the policy id reaches the audit row
  ready for an operator-managed source.
- **Bytes payloads are passed through.** The JSONFlux reduce boundary
  produces JSON-shaped values, so the engine's contract excludes
  bytes; if a future connector emits binary payloads at the
  redaction boundary, this surface needs revisiting.

## References

- Parent goal: [#800](https://github.com/evoila/meho/issues/800)
  (G11 Agentic ops runtime) §"trust boundary has to be the API".
- Parent initiative: [#805](https://github.com/evoila/meho/issues/805)
  (G11.4 Safety) §Approach for the tiered design.
- Task: [#1070](https://github.com/evoila/meho/issues/1070).
- Seam: `dispatcher._reduce_or_error`
  (`backend/src/meho_backplane/operations/dispatcher.py`) — the
  C1-b middleware (#1071) sits here.
- YAML-as-package-data precedent:
  `backend/src/meho_backplane/operations/ingest/catalog.py` +
  `catalog.yaml`.
- Microsoft Presidio (Tier-2; #1072):
  <https://github.com/microsoft/presidio>.
