# Redaction (connector-boundary, tiered)

Initiative [#805](https://github.com/evoila/meho/issues/805) (G11.4 Safety,
C1) ships a sanitization middleware that redacts every connector response
before it reaches a caller or LLM. This document covers five landed
slices:

* The foundation
  ([#1070](https://github.com/evoila/meho/issues/1070)): declarative
  policy schema, Tier-1 named-pattern regex engine, named-pattern
  library.
* The connector-boundary wiring
  ([#1071](https://github.com/evoila/meho/issues/1071)): the
  middleware that sits in `dispatcher._reduce_or_error` and runs
  capture-raw → audit-raw → redact → reduce on every dispatch.
* The round-trip CI gate + shadow mode
  ([#1073](https://github.com/evoila/meho/issues/1073)): fixture-pair
  enforcement of policy correctness in CI, plus a policy-level
  `mode: shadow` flag for safe new-rule rollout.
* The Tier-2 Microsoft Presidio NER adapter
  ([#1072](https://github.com/evoila/meho/issues/1072)):
  capability-flagged free-text NER over policy-flagged fields,
  merging into the same manifest shape as Tier-1.
* The agent-invocation audit row + policy-replay sense
  ([#1074](https://github.com/evoila/meho/issues/1074)): per-tool-call
  audit rows fired from inside an agent loop are keyed by
  `agent_session_id`, carry `model` / `provider` / `cost`
  attribution in the JSON payload, and gain a second replay sense
  (`replay_policy`) that re-runs the recorded policy against the
  captured raw to detect regressions.

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
| `RedactionMode` | `policy.py` | `Literal["enforce", "shadow"]` (default `enforce`) |
| `RedactionScope` | `policy.py` | Optional connector_id/tenant/op predicate |
| `RedactionRule` | `policy.py` | One named-pattern → action binding |
| `RedactionPolicy` | `policy.py` | Versioned bundle of rules + a `mode` |
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
| `api_key` | Labelled credential pairs: `api_key`, `access_token`, `refresh_token`, `auth_token`, `session_token`, `token` (bare), `secret` / `secret_key` / `secret_id`, `private_key`, `password`, `passwd`, `pwd`, `client_secret` — each followed by `=` or `:` and an 8+ char value |
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
6. `(None, None, None)` — wildcard global override registered via
   `register_policy(policy)` with no scope kwargs.

When no override at any of those six levels matches, the resolver
falls through to the packaged **default-safe** policy
(`policies/default.yaml`) — the conservative unconditional fallback.

The default-safe policy is the load-bearing guarantee: a connector
landing without a registered override **still gets credentials
stripped** (`authorization_header`, `bearer_token`, `jwt`, `api_key`,
`kubeconfig`). Identifier patterns (`uuid` / `ipv4` / `ipv6` / `fqdn`)
are deliberately not in the default — most connector responses are
mostly identifiers, and a global default that redacts them would
blank operator-facing summaries. Operators wanting them at the global
level register a wider policy via `register_policy(policy)` (no
connector/tenant/op filter = global override; step 6 above). The
distinction matters: the registered wildcard policy fires *before*
the packaged default, so it can extend or replace the default's rule
set without touching `policies/default.yaml`.

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

## Shadow / detection-only mode (#1073)

A policy can declare `mode: shadow` at the top level. The engine
still walks every payload and emits the full manifest (so an
operator can watch detection counts in audit + dashboards), but
returns the **input payload unmodified** as `redacted`. This is
the safe-rollout primitive for a new rule:

```yaml
id: new-rule-rollout
version: 1
mode: shadow   # detection only -- do not yet rewrite payloads
rules:
  - name: detect-new-shape
    pattern: api_key
    action: redact
    reason: "monitoring new credential format; not enforcing yet"
```

Once the operator confirms the manifest counts are sane (no
over-detection on production traffic), the same YAML is
re-published with `mode: enforce` (or with the `mode:` line
removed -- enforce is the default) and the rule starts mutating
payloads.

Implementation contract (load-bearing):

- **Policy-level, not call-level.** The flag travels with the
  policy YAML; there is no `mode=` argument threaded through the
  middleware, the resolver, or `redact()`. A future "monitor"
  mode would extend the `RedactionMode` `Literal` union with the
  same architectural shape.
- **Detection identical to enforce.** A `shadow` policy and the
  same-rules `enforce` policy produce **identical manifests**
  (`rule`, `pattern`, `action`, `count`, `path`); only
  `redacted` differs. The C1-d round-trip fixture suite uses this
  invariant to prove the mode flag is wired correctly.
- **No middleware re-plumbing.** `apply_connector_boundary_redaction`
  reads the policy's mode via the engine; the dispatcher,
  audit-write path, and broadcast path are unchanged.

## Round-trip CI gate (#1073)

The redaction policy round-trips (capture raw → re-run policy →
diff against the agent's view is empty) are enforced by the
fixture suite at `backend/tests/redaction_fixtures/` and the
harness at `backend/tests/test_redaction_roundtrip_fixtures.py`.

Each fixture is a sub-directory:

```
backend/tests/redaction_fixtures/<fixture-name>/
  policy.yaml          # required: policy under test
  raw.json             # required: captured raw payload
  expected.json        # required: expected redacted view (or raw, in shadow mode)
  manifest.json        # optional: expected manifest projection
  labels.json          # optional: { connector_id, tenant, op }
```

The harness runs the engine for every fixture and asserts the
output equals `expected.json` -- both senses, in one `==`:

- **Leak / under-redaction:** raw secret survives into the
  engine's output but `expected.json` shows it redacted →
  equality fails → CI red.
- **Over-redaction:** engine touches a value `expected.json`
  shows untouched → equality fails → CI red.

Both failure modes are equally load-bearing per Initiative #805's
DoD: under-redaction is the safety failure (the parent goal #800
hinges on it); over-redaction is the usability failure (operators
stop trusting the system when their summaries blank out).

**Meta-tests:**
`backend/tests/test_redaction_roundtrip_meta.py` proves the gate
*would* fail if either failure mode is injected. The file
constructs in-memory fixture pairs with tampered `expected`
payloads (one for leak, one for over-redaction) and asserts the
comparator fires `AssertionError`. The meta-tests exist because
"the gate caught nothing this PR" looks the same on the CI dashboard
as "the gate is broken" -- the meta-tests rule out the second case.

**Where the gate runs.** The harness is a standard pytest file
under `backend/tests/`, so it is picked up by the existing
`python-lint-test` job in `.github/workflows/ci.yml`. That job
is in the branch-protection required-status-checks set, so a
round-trip mismatch blocks merge by configuration -- no new
workflow step needed. Adding new fixture pairs only requires
dropping them into the fixtures directory; the harness
auto-discovers them.

**Adding a fixture.** See
`backend/tests/redaction_fixtures/README.md` for the per-fixture
layout and the dummy-secret-shape convention (replace real
secrets with regex-equivalent fakes).

## Tier-2: Microsoft Presidio free-text NER (#1072)

Tier-1 catches structured leaks (credentials, kubeconfig, IP-shaped
identifiers) but cannot reach prose: a connector's `error.message`
or `result.description` field is a free-text leaf that hides PII the
regex catalogue does not target. Tier-2 is the **opt-in NER pass**
that closes that gap.

### Capability-flagged contract

A `RedactionPolicy` with no `tier2` block (or `tier2: []`) **never
loads Presidio at runtime**. The middleware's predicate
`policy_uses_tier2(policy)` is the cheap boolean check on the hot
path; only policies that opt in pay the spaCy model load + per-leaf
NER inference cost.

This is the load-bearing guarantee tested by
`tests/test_redaction_presidio.py::test_tier1_only_policy_never_imports_presidio`
(meta-path-blocked presidio import + Tier-1 policy run → middleware
returns the redacted payload without raising and `sys.modules`
contains no `presidio_*` entries).

### Policy shape

```yaml
id: my-policy
version: 1
rules:
  - name: strip-bearer
    pattern: bearer_token
    action: redact
    reason: tier1
tier2:
  - name: scrub-error-messages
    fields:
      - error.message            # one specific path
      - items.*.description      # any items[*].description leaf
      - "**.notes"               # ``notes`` at any depth
    entities:                    # default: [PERSON, IP_ADDRESS, URL]
      - PERSON
      - IP_ADDRESS
      - URL
    action: redact               # ``mask`` and ``hash`` also supported
    threshold: 0.5               # Presidio confidence floor in [0, 1]
    language: en                 # spaCy / Presidio language code
    scope:                       # same predicate shape as Tier-1
      connector_id: github
    reason: "free-text NER over user-facing error / description"
```

`fields` is the load-bearing operator decision: which payload paths
are free-text. `entities` is the Presidio recogniser set; the schema
validates against the catalogue (`PRESIDIO_SUPPORTED_ENTITIES` in
`policy.py`) so a typo like `PERSON_NAME` fails policy load with the
known-set in the error.

### Path-glob matcher

The matcher (`_glob_to_regex` in `presidio.py`) supports two
metacharacters:

| Glob | Meaning |
| --- | --- |
| `*` | Exactly one path segment. `items.*.message` matches `items.0.message` but not `items.0.nested.message`. |
| `**` | Any depth, including zero segments. `**.error.message` matches `error.message` and `a.b.error.message`. |

Everything else (literal segment text, dots) is matched verbatim.
The compiled regex is cached per-glob (`functools.lru_cache(256)`) so
the same glob amortises across calls.

### Engine lifecycle

`presidio.py` exposes `get_engines(language="en")` which returns a
frozen `Tier2EnginePair(analyzer, anonymizer)`. The first call per
language:

1. Imports `presidio_analyzer`, `presidio_analyzer.nlp_engine`, and
   `presidio_anonymizer`. The imports are inside the function body
   so a Tier-1-only policy never triggers them.
2. Constructs an `NlpEngineProvider` configured with the resolved
   spaCy model (default `en_core_web_lg`; `MEHO_REDACTION_SPACY_MODEL`
   env var overrides — typically set to `en_core_web_sm` in CI's
   unit lane and dev sandboxes).
3. Constructs `AnalyzerEngine(nlp_engine=..., supported_languages=[language])`
   and `AnonymizerEngine()`. Both are cached behind a lock; the
   `_engines` dict survives the process lifetime.

Failures during step 1–3 raise `Tier2NotAvailableError` (with the
chained cause), which the middleware catches and converts to a
structured `connector_error` `OperationResult` — the dispatcher's
never-raises contract holds even when Presidio is misconfigured.

### Manifest contract

Tier-2 emits the same `RedactionManifestEntry` shape as Tier-1, with
two distinguishing fields:

| Field | Tier-1 example | Tier-2 example |
| --- | --- | --- |
| `rule` | `strip-bearer-token` | `scrub-error-messages` |
| `pattern` | `bearer_token` | `presidio:PERSON` |
| `action` | `redact` | `redact` |
| `count` | 1 | 1 |
| `span` | (12, 48) | (8, 22) |
| `reason` | RFC 7235 secret | free-text NER |
| `path` | `headers.authorization` | `error.message` |

The `presidio:` prefix on `pattern` lets audit consumers bin Tier-1
vs Tier-2 firings without re-reading rule definitions. Multiple
Presidio matches in the same leaf collapse into one manifest entry
per `(rule, entity_type)` pair (matching Tier-1's per-leaf-per-rule
collapsing rule); `count` tracks how many matches the rule resolved.

### spaCy model provisioning

Presidio's English `AnalyzerEngine` is backed by a spaCy NER model.
The Presidio documentation recommends `en_core_web_lg` (~560 MB) for
production NER quality.

| Lane | Model | Footprint | Provisioned by |
| --- | --- | --- | --- |
| Production Docker image | `en_core_web_lg` | ~560 MB | Dockerfile bake (follow-on; today the image build inherits whatever the deploy pipeline installs) |
| CI unit lane | `en_core_web_sm` | ~12 MB | `python -m spacy download en_core_web_sm` step in `ci.yml` |
| Local dev | either | varies | `uv run python -m spacy download <model>` |

The model name is a **deployment concern, not a policy concern** —
operator-authored YAML stays portable across lanes. The
`MEHO_REDACTION_SPACY_MODEL` env var (read by `_resolved_spacy_model`)
pins the engine; CI sets it to `en_core_web_sm` for the unit lane and
the production image leaves it unset so the adapter picks the
documented default.

The tests in `test_redaction_presidio.py` and the Tier-2 path in
`test_redaction_middleware.py` are gated by
`spacy.util.is_package(...)` checks that accept either model — so
the suite stays green on every lane that has at least one model
provisioned.

## Dependencies

- **PyYAML** (`yaml.safe_load`) — already a transitive dep; matches
  the precedent set by `operations/ingest/catalog.py`.
- **Pydantic v2** — already pinned in `backend/pyproject.toml`.
- **Python stdlib** — `re`, `hashlib`, `importlib.resources`,
  `collections.abc`, `threading` (resolver lock).
- **presidio-analyzer 2.2.362** + **presidio-anonymizer 2.2.362**
  (#1072) — the Tier-2 NER adapter. Imports are lazy so Tier-1-only
  deployments incur zero NER cost at runtime. The transitive
  dependency closure includes spaCy 3.x and supporting libraries;
  the installed wheel set lands at ~80 MB.
- **spaCy NER model** — `en_core_web_lg` (Presidio default) or
  `en_core_web_sm` (CI / sandbox lane). Not a Python dependency in
  `pyproject.toml`; provisioned out-of-band via
  `python -m spacy download <model>`.

No new runtime dependencies were added by Task #1070, #1071, or #1073.

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

## Agent-invocation audit (C2-b, #1074)

The connector-boundary middleware (#1071) already writes
`raw_payload` + `redaction_manifest` + `redaction_policy_id` on every
dispatcher-written audit row. #1074 closes the agent-side of the audit
contract: dispatcher rows fired from inside an agent loop are keyed
by the run's id on `audit_log.agent_session_id`, and carry the run's
`model` / `provider` / `cost` snapshot in the JSON payload so a
consumer reading one row can attribute it without joining
`agent_run`.

The propagation hook is a pair of contextvars in
`operations/_audit.py`:

- `agent_session_id_var` — bound by the `AgentInvoker` around the
  loop; read by `write_audit_row` straight into the real
  `audit_log.agent_session_id` column (already on the schema via
  migration `0014`, G8.2-T1 #1009). `None` outside an agent run, so
  the chassis HTTP / MCP dispatch path is unchanged.
- `agent_run_audit_meta_var` — carries `AgentRunAuditMeta(model,
  provider, cost)`. The dispatcher's `_build_audit_payload` writes
  the three fields into the audit row's JSON payload under
  `agent_model` / `agent_provider` / `agent_cost`. Decimal costs are
  string-coerced (mirrors the `audit_log.duration_ms`
  `Decimal(str(...))` convention) so JSON encoding stays
  encoder-safe.

The contextvars are bound by `AgentInvoker._run_loop_to_completion`
(background task) and `AgentInvoker.stream_events` (inline SSE
coroutine). `asyncio.create_task` snapshots the contextvars, so the
background task inherits the binds for its whole life; every
per-tool-call audit row the loop produces shares the same session id
+ meta. The same shape mirrors the MCP wiring
(`meho_backplane.mcp.server._bind_mcp_session_id` /
`meho_backplane.mcp.audit`).

### Policy-replay sense

The second audit-replay sense (the first is G8.2-T3 #1011's
reconstruct-sense `replay_session`, which rebuilds *what the agent
saw* from the session lineage):

```text
audit_log row (raw_payload, redaction_manifest, payload.redaction_policy_id)
                    │
                    ▼
       replay_policy(audit_id, tenant_id, session)
                    │
                    ├──► find_policy_by_id(payload.redaction_policy_id)
                    │
                    ▼
       redact(raw_payload, policy, connector_id, tenant, op)
                    │
                    ▼
       diff(stored_manifest, replayed_manifest)
                    │
                    ▼
       PolicyReplayResult(status, missing, extra, replayed_redacted)
```

Verdict statuses:

| Status | Meaning |
| --- | --- |
| `MATCH` | The replay re-produced the recorded manifest verbatim; the policy stays deterministic for this row's raw payload. |
| `DIVERGED` | The replay produced a different manifest; `missing` / `extra` carry the delta. This is the C1-d (#1073) regression signal. |
| `AUDIT_ROW_NOT_FOUND` | No tenant-scoped row for that id (cross-tenant id is structurally indistinguishable). |
| `REPLAY_NOT_APPLICABLE` | Row exists but has no `raw_payload` / `redaction_policy_id` (pre-#1071 row, or an error-path row). |
| `POLICY_NOT_FOUND` | Recorded policy id no longer resolvable -- the policy was retired, distinct from divergence. |

The implementation lives at
`backend/src/meho_backplane/audit_query/policy_replay.py` and the
policy-by-id lookup is `meho_backplane.redaction.resolver.find_policy_by_id`.

## References

- Parent goal: [#800](https://github.com/evoila/meho/issues/800)
  (G11 Agentic ops runtime) §"trust boundary has to be the API".
- Parent initiative: [#805](https://github.com/evoila/meho/issues/805)
  (G11.4 Safety) §Approach for the tiered design.
- Tasks: [#1070](https://github.com/evoila/meho/issues/1070),
  [#1071](https://github.com/evoila/meho/issues/1071),
  [#1073](https://github.com/evoila/meho/issues/1073),
  [#1074](https://github.com/evoila/meho/issues/1074).
- Seam: `dispatcher._reduce_or_error`
  (`backend/src/meho_backplane/operations/dispatcher.py`) — the
  C1-b middleware (#1071) sits here.
- YAML-as-package-data precedent:
  `backend/src/meho_backplane/operations/ingest/catalog.py` +
  `catalog.yaml`.
- Reconstruct-sense replay (#1011):
  `backend/src/meho_backplane/audit_query/replay.py`.
- Microsoft Presidio (Tier-2; #1072):
  <https://github.com/microsoft/presidio>.
