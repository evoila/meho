# Redaction (connector-boundary, Tier-1)

Initiative [#805](https://github.com/evoila/meho/issues/805) (G11.4 Safety,
C1) ships a sanitization middleware that redacts every connector response
before it reaches a caller or LLM. This document covers the foundation
slice landed by Task [#1070](https://github.com/evoila/meho/issues/1070):
the **declarative policy schema** + the **Tier-1 named-pattern regex
engine** + the **named-pattern library**. The middleware that wires the
engine into `dispatcher._reduce_or_error` (C1-b, #1071), the Tier-2
Microsoft Presidio NER adapter (C1-c, #1072), the round-trip CI gate
(C1-d, #1073), and the agent-invocation audit row (C2-b, #1074) are
sibling tickets and land on top of this surface.

## Overview

A redaction **policy** is a YAML document declaring one or more
**rules**. Each rule binds a **named pattern** (a pre-compiled regex
known to the engine, e.g. `bearer_token`, `kubeconfig`, `uuid`) to an
**action** (`redact` / `mask` / `hash`) and optionally a **scope**
(connector_id, tenant, op) limiting which calls the rule fires on.

The **engine** walks a nested dict / list / str payload, applies the
rules in policy order, and returns:

- `redacted` — the same payload shape with string leaves rewritten per
  the matching rule's action.
- `manifest` — a tuple of `RedactionManifestEntry` records, one per
  rule firing per leaf, carrying `type`, `count`, `span`, `reason`,
  and `path`. C1-b will persist this verbatim into the audit row.

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

## Dependencies

- **PyYAML** (`yaml.safe_load`) — already a transitive dep; matches
  the precedent set by `operations/ingest/catalog.py`.
- **Pydantic v2** — already pinned in `backend/pyproject.toml`.
- **Python stdlib** — `re`, `hashlib`, `importlib.resources`,
  `collections.abc`. No third-party regex / NER libraries here;
  Tier-2 (#1072) adds Microsoft Presidio.

No new runtime dependencies were added by Task #1070.

## Known issues / future work

- **Wide patterns benefit from policy ordering.** The engine applies
  rules in policy order; a later rule sees the already-redacted
  output of earlier rules. Place narrow patterns (`api_key`,
  `bearer_token`) before wide ones (`fqdn`, `uuid`) in the YAML.
- **Multiple matches collapse into one manifest entry per leaf.** The
  manifest tracks `count` and the span of the *first* match; if an
  audit consumer needs per-match spans, that is a Tier-1 extension
  worth filing after C1-b lands and we see real consumption patterns.
- **No per-tenant policy resolution yet.** This task ships the schema
  and engine only. The middleware (#1071) is responsible for
  resolving "which policy applies to this call" from settings / DB;
  the engine accepts the policy as a parameter and applies it
  verbatim.
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
