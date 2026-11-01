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
  consume it without further shape-fixing. A list whose items are all
  strings (the `rows` of `linux.log.tail`, the lines of a ruleset) is
  matched as one text, joined with newlines, so a secret that spans
  rows (a PEM key: `BEGIN` row, body rows, `END` row) is found whole.
  The list keeps its length and every row keeps its place; a row
  inside a match loses only its part of the match. A match inside one
  row gives the same result as matching that row alone. Rows and
  matches are walked together once, so a long list with a secret in
  every row costs the same as its rows alone.
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
| `SecretFinder` | `patterns.py` | A named pattern written as code: `secret_spans(text)` returns the spans to replace. `patterns.secret_spans(pattern, text)` gives the spans of any named pattern, regex or finder |
| `BindKeySecretFinder` | `bind_secret.py` | The `bind_key_secret` finder: reads BIND comments and quoted strings the way BIND does |

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
| `api_key` | Labelled credential pairs: `api_key`, `access_token`, `refresh_token`, `auth_token`, `session_token`, `token` (bare), `secret` / `secret_key` / `secret_id`, `private_key`, `password`, `passwd`, `pwd`, `client_secret` — each followed by `=` or `:` and an 8+ char value. The label may follow `_` or `-` (`DB_PASSWORD=...`, `GITHUB_TOKEN=...`); a label glued to letters (`mypassword=`) does not match |
| `pem_private_key` | A PEM private key block, from the `BEGIN ... PRIVATE KEY` line to the matching `END` line (or to the end of the text when the `END` line is missing). Public keys and certificates do not match. Runs before `api_key` in the default policy, so a `private_key=-----BEGIN ...` pair loses the whole block |
| `kubeconfig` | YAML kubeconfig blobs (`apiVersion: v1` + `kind: Config`) |
| `password_hash` | A crypt(3) password hash as in `/etc/shadow`: `$1$`, `$2a$`/`$2b$`/`$2x$`/`$2y$`, `$5$`, `$6$`, `$7$`, `$y$`, `$gy$`, `$sha1$`, `$argon2id$`/`$argon2i$`/`$argon2d$`. The match stops at `:`, so the other `/etc/shadow` columns stay readable |
| `bind_key_secret` | The value after the `secret` keyword in a BIND `key` block (`secret "<base64>";`, TSIG and rndc keys) and after `cookie-secret` in the `options` block, with or without quotes, also when BIND comments (`/* */`, `//`, `#`) sit between the keyword and the value; the keyword and the comments stay so the file stays readable. Not a regex: `bind_secret.py` first splits the text into comments, quoted strings and code the way BIND's lexer does, then reads the value only inside the same scope. So a `secret` word in a comment can never reach past that comment, a commented-out key block is still hidden, an old secret in a comment between the keyword and its value is hidden together with the value, a key *name* like `"transfer-secret"` or `"my secret"` is never the keyword, and a `#` or `//` inside a quoted string does not start a comment (#4617, #5342 review). A quoted value must be base64 or hex. A value without quotes must be followed by `;` or a comment |
| `uuid` | RFC 4122 canonical 8-4-4-4-12 hex |
| `ipv4` | Dotted-quad with 0-255 per-octet bounds |
| `ipv6` | Full + compressed RFC 5952 forms |
| `fqdn` | ≥2 labels with non-numeric TLD start |

The `api_key` rule skips a value that already is a complete
`[REDACTED:...]` placeholder (`password: [REDACTED:param_name]`), so an
earlier pass (the engine itself, or the shared API file check of
`connectors-shared-api-file-secrets.md`) is not redacted a second time
and loses its label. Only a complete placeholder is skipped: a name
between `[REDACTED:` and `]`, and the value ends there. A value that
only starts like one (`[REDACTED:made-up`) or goes on after the `]`
(`[REDACTED:x]made-up`) is redacted whole.

The secret-*value* part of `authorization_header`, `bearer_token`,
and `api_key` consumes any non-whitespace, non-quote byte
(`patterns.py::_VALUE_CHARS`, bounded by `_VALUE_MAX`): the value is
captured to its natural delimiter — whitespace, closing quote, or end
of blob. The earlier alphanumeric-plus-`. _ - + / =` class stopped at
the first punctuation byte, so a value like `P@ssw0rd!withMore` was
matched only partially (tail leaked) or not at all (whole value
leaked). The labelled-context requirement, not the value alphabet, is
what keeps opaque IDs (Git SHAs, build hashes) from false-positiving.

The engine replaces the whole match of a regex pattern. One named
pattern, `bind_key_secret`, is code instead of a regex: a
`SecretFinder` (`patterns.py`) with one method, `secret_spans(text)`,
that returns the `(start, end)` of every secret. The engine replaces
only those spans and reports the first one in the manifest, so the
`secret` keyword and any BIND comments around the value stay readable.
`bind_secret.py` holds the finder. It keeps every scan linear: a
comment or quote that never closes is looked for once, so a payload
full of `/*` or `"` cannot keep the middleware busy.

Calibration notes (over-match vs under-match) are inline in
`patterns.py`. The Tier-1 posture deliberately leans toward
**over-redaction**: a false positive on a benign UUID is recoverable
via a scoped rule, but a missed secret is the failure the parent goal
(#800) cannot accept.

Beyond the connector-*result* boundary, the dispatch **error
builders** (`operations/_errors.py`) route every free-text diagnostic
they emit — `extras.exception_message`, `extras.upstream_message`,
`extras.upstream_fault.type` / `.faultstring`,
`extras.detail`, and the `error` summary tails built from them —
through `_sanitize_free_text`: a Tier-1 pass with the packaged
default policy (pinned via `get_default_policy()`, deliberately
bypassing the resolver override table so a registered shadow-mode
policy cannot disable it), followed by the length cap (256 chars, or
2 KB for upstream body text, #3567). Redaction
runs **before** the cap so a secret straddling the cap boundary
cannot survive truncation as an unmatchable cleartext fragment.
Since #5031 the same sanitizer first replaces the value of each quoted
secret-named pair in the text (see "Errors, diagnostics and saved test
captures" below).

## Errors, diagnostics and saved test captures (#5031)

The secret removal also covers what is not a normal result: error texts,
HTTP diagnostics, traces and the test captures saved in the repository.

**One name rule for secret fields.** The Tier-1 patterns find a secret by
a label inside one string (`password=...`). A vendor answer often holds a
secret as a field: `{"password": "..."}`, or the same pair as JSON text in
an error body, where a quote sits between the name and the `:`.
`redaction/secret_fields.py` closes both cases:

- `scrub_secret_fields(node)` walks a dict / list / str. An entry whose key
  is a secret name gets `[REDACTED:param_name]` in place of its value
  (a whole sub-tree, too). Each string goes through the text step.
- `scrub_secret_fields_in_text(text)` replaces the value of each quoted
  name-value pair whose name is a secret name. The name and the value may
  each use `"` or `'` (JSON `"name": "value"`, Python `'name': 'value'`,
  and Python's `'name': "it's"` for a value that holds a single quote), and
  the pair may be escaped JSON inside a JSON string (`\"name\": \"value\"`).
  A small scanner reads the value from the opening quote to the closing
  quote and steps over each escape, so a quote inside the value does not
  end it, and there is no length limit. The value may also be an object or
  a list (`"credentials": {"value": "..."}`, `"password": ["..."]`): the
  scanner walks it to its closing bracket (a bracket inside a quoted string
  does not count) and replaces the whole object or list with the quoted
  marker, as the walk does for data. A value with no closing quote or
  bracket (a diagnostic cut by a length cap) is replaced up to the end of
  the text. `iter_quoted_secret_fields(text)` lists the same pairs; the
  capture check uses it.

The name rule is the shared `broadcast.events.is_secret_param_name`
(`password`, `encryptedPassword`, `sessionToken`, `client_secret`,
`licenseKey`, ...), the one the broadcast scrub and the approval echo use.
Numbers, booleans and `null` stay. A value an earlier step already masked
(`[REDACTED:...]`, a trace marker, `***`) stays, so its label is kept.

**Where it runs.**

| Place | What runs |
| --- | --- |
| Error free text (`operations/_errors.py::_sanitize_free_text`) | the text step, then Tier-1, then the length cap |
| Every error result (`dispatcher._audit_error_and_return` → `operations/_vendor_text.py::redact_error_result`) | the walk, then Tier-1 (default policy, pinned), over every string and field of `error`, `extras` and `result`; this covers the fields copied straight from the vendor (`validation_errors` of an HTTP 422, `permission_headers` of an HTTP 403), any field a connector adds, and the vendor's answer in `result` of an error a handler returns or of a multi-step operation's error envelope. If redaction fails, the result keeps only `error_code` and a fixed text: `result` and the result handle are dropped and `delivery` is `unavailable`. The audit row and the stored approval result get the cleaned copy. |
| Multi-step error envelope (`dispatcher._reduce_and_audit_composite_error` → `redact_error_payload`) | the walk, then Tier-1 (default policy, pinned), **before** the reducer stores the full envelope behind a result handle, so the stored rows, the handle's sample rows and the inline summary are all clean. The envelope went through the operator's own policy first, and a shadow-mode policy leaves every secret in place; the cleanup of the inline result runs later and cannot reach the stored copy. The audit row's `raw_payload` gets a fingerprint for every value this cleanup hid (`_protect_secret_values`). If this cleanup fails, the envelope is withheld like a reducer failure (`delivery` is `unavailable`), nothing is stored, and `raw_payload` is one fingerprint of the whole envelope. |
| Trace bodies and header values (`flight_recorder/_content.py::scrub_content`) | the walk, then the trace's credential-shape policy, for every recorded request and answer body |

**What does not change.** The normal result path: a secret-named field in a
normal answer is removed only for a secret read (see "Secret reads" below).
The audit row's `raw_payload` keeps the answer for replay, with a keyed
fingerprint in place of each hidden value. The row of a multi-step error
envelope is stored the same way: the handler returned the envelope like a
normal answer. A connector's own trace mask
(`HttpConnector.trace_response_body`) still runs first.

**The shared test.** `backend/tests/test_redaction_planted_secrets.py`
plants a token, a password field and an encrypted password field. For
every registered connector it runs one made-up operation through the real
dispatcher: once with a normal answer and once for each error kind (HTTP
500, 422, 403 and 401 with a JSON body, a refused session login, a raised
error whose text holds the vendor's JSON or a Python dict, an error result
the handler returns with the vendor's answer in `result`, and a multi-step
operation's error envelope with the vendor's answer inside). It checks the
caller's result, the committed audit row and every MEHO log line. A second
test hands a normal answer and error answers, with an `Authorization`
header and a password in the request body, to the trace redaction
(`redact_span`) for every connector and checks the recorded span. The
normal answer holds only labelled secrets, for the reason above. A new
connector is covered as soon as it is registered.

`backend/tests/test_operations_composite_error_redaction.py` drives the
multi-step error envelope through the real JSONFlux reducer under a
shadow-mode policy and checks the inline result, the handle's sample rows,
the stored rows and the audit row.

**The capture check.** `backend/tests/test_capture_secret_scan.py` scans
every saved test capture: each file under `backend/tests/fixtures/` and
`backend/tests/data/` that is not Python. It fails on:

- the marker of the planted values (`planted-secret-`), so a value planted
  by a test never ends up in a saved capture;
- common secret shapes: the Tier-1 credential patterns (not `kubeconfig`,
  which also matches any rendered `ConfigMap`), a private key block, an AWS
  access key id, GitHub and Slack tokens;
- a secret-named field with a value, as a quoted pair (JSON, Python with
  either quote, escaped JSON; found by `iter_quoted_secret_fields`), as an
  object or a list under a secret name (it counts when a string value
  inside looks like a secret), as YAML or as XML.

It skips values that are clearly not secrets: a `<placeholder>`, a masked
value, a `${...}` or `{{ ... }}` reference, a URL with no user part, a
value that names a secret field, and values shorter than 8 characters.
The redaction fixtures (`backend/tests/redaction_fixtures/`) are not
scanned: they hold made-up secret shapes on purpose. The check runs in the
required unit test job, and its own tests prove that it fails on a planted
capture.

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
`pem_private_key`, `kubeconfig`, `password_hash`, `bind_key_secret`). Identifier patterns (`uuid` / `ipv4` / `ipv6` / `fqdn`)
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

### Secret reads: names without values, `reveal_secret` for people only

The Tier-1 engine matches only **labelled** secret shapes inside string
leaves (`password=…`, `Bearer …`); a structured `{"password": "<value>"}`
dict carries no label in the leaf, so the engine passes it through
verbatim. A secret read returns exactly this shape. The dispatcher adds a
second layer, `operations/_secret_values.py`, right after the
connector-boundary redaction (`dispatcher._protect_secret_values`):

- **Which ops.** A secret read is a `credential_read` op
  (`broadcast/events.py:_CREDENTIAL_READ_OPS`) or an op of
  `_secret_values._VALUE_HIDING_OPS`.
- **Default view** (`hide_secret_values`):
  - `vault.kv.read` keeps the key names under `data` and hides every value
    (`[REDACTED:secret_value]`), whatever the key name is (`ssh_key`,
    `value`, `pin`, a number). `version` stays. An answer of another shape
    is hidden whole.
  - `vmware.composite.vm.guest.env.read` keeps each variable name and hides
    the value (`NAME=[REDACTED:secret_value]`).
  - Every other secret read: values under secret-shaped key names are
    replaced with `[REDACTED:param_name]`
    (`broadcast.events.scrub_secret_named_values(..., hide_numbers=True)`),
    numbers included; booleans and `None` stay.
- **`reveal_secret: true`.** `dispatch()` strips this reserved control
  flag **before** hashing / schema validation / handler forwarding (the op
  schemas are `additionalProperties: false`). With the flag, a person gets
  the redacted answer with the secret fields put back as the connector
  returned them (`reveal_hidden_values`: every field the default view
  hides, so a PEM private key comes back whole; the redaction of the other
  fields stays). An agent principal, or a
  person's hosted agent run (an RFC 8693 actor is bound), is refused before
  the policy gate: `status="denied"`, a synchronous `POLICY_DECISION` audit
  row, and no value. The people's call sites are the `/ui` Vault console
  (`ui/routes/vault/routes.py`, reveal-on-click) and CLI `meho secret
  read` (`cli/internal/cmd/secret/read.go`). Service principals may still
  reveal.
- **Audit stamp.** `reveal_secret_var` (bound with the same token/finally
  discipline as `policy_decision_var`) lands as `payload['reveal_secret']`
  on the audit row of a secret read (`True` = raw asked for, `False` =
  default view). Other dispatches leave it `None`, so the key is absent.
- **Fail closed.** The success-path audit row is required
  (`require_audit=True`): when it does not commit, the caller gets
  `audit_commit_failed` and no value, also for a reveal.
- **Stored audit copy** (`fingerprint_hidden_values`). For **every** op,
  the dispatcher walks the raw answer and the default view side by side.
  Each leaf or subtree that the view hides (Tier-1 redaction, the name
  scrub, or the value hiding above) is stored in `raw_payload` as
  `[FINGERPRINT:hmac-sha256:<hex>]`: HMAC-SHA256 under the server key of
  the audit params hash (`_validate.compute_audit_digest_hash`) over the
  value's canonical JSON. Equal values give equal fingerprints inside one
  install, so an auditor can tell that two rows held the same value
  without seeing it. This holds also when a person revealed the value. For
  a multi-step error envelope the error cleanup (`redact_error_payload`,
  pinned default policy) runs before the comparison, so a value that only
  the cleanup hides, for example a bearer token under a shadow-mode
  policy, is fingerprinted too; when the cleanup fails, the stored copy is
  one fingerprint of the whole envelope. Rows written before this change
  are not rewritten; they age off with the 90-day `raw_payload` retention.
- **Credential mints.** An op that makes a new credential
  (`classify_op(op_id) == "credential_mint"`, for example
  `vault.auth.approle.generate_secret_id` or `harbor.robot.create`) is
  the other way round. The caller gets the new secret, but
  `dispatcher._apply_redaction_middleware` runs the key-name walk over
  `redaction.raw`, so the audit `raw_payload` keeps only the other fields
  (`[REDACTED:param_name]` in place of the secret).

`secret.move` remains the recommended no-transit path for piping a
credential onward; `vault.kv.versions` is the metadata-only browse.

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
under `backend/tests/`, so it is picked up by the required unit
sweep in `.github/workflows/ci.yml` — since #3251 the
`python-unit-shard` matrix (whichever shard its file lands
in), reported through the `python-unit-lane` fan-in that carries
the branch-protection required context. That check is in the
required-status-checks set, so a round-trip mismatch blocks merge
by configuration -- no new workflow step needed. Adding new fixture pairs only requires
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
  `bind_key_secret` must come first: it needs the BIND comment between
  the `secret` keyword and the value to be whole, and `api_key` or a
  header rule can redact a credential inside that comment and take
  the closing `*/` with it. The packaged policies list it first and
  `test_redaction_patterns.py` checks that order (#4620 review).
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
  loop. `write_audit_row` reads it through `resolve_agent_session_id()`
  into the real `audit_log.agent_session_id` column (already on the
  schema via migration `0014`, G8.2-T1 #1009). Inside an agent run the
  run id wins; outside one the helper falls back to the MCP session id
  of a direct MCP call (#4672), and to `None` for a chassis HTTP call
  with no session.
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

A value the redaction hid is stored as a fingerprint (see "Secret
reads"), so the replay cannot fire on it again. Recorded manifest entries
whose path holds a fingerprint (`_secret_values.fingerprinted_paths`) are
left out of the comparison.

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

## Flight-recorder fail-closed redaction (F2)

Task [#3213](https://github.com/evoila/meho/issues/3213) implements **F2**
of the dispatch flight recorder decision
([`docs/decisions/dispatch-flight-recorder.md`](../decisions/dispatch-flight-recorder.md),
Initiative [#3207](https://github.com/evoila/meho/issues/3207)). It is a
**separate engine** from the connector-boundary redaction above, with a
different job. The connector-boundary engine masks *content* (credential
and infra-leak shapes) in a response before a caller or LLM sees it; the
flight-recorder engine decides what a per-dispatch *trace* may persist at
all, and it is **fail-closed on every axis**: where it cannot *prove* a
datum secret-free it drops the datum and flags the trace uncertain.

Why fail-closed is load-bearing: the operator override on F5 makes traces
readable by agents *"as long as there are no secrets in there."* That
condition is discharged only by this engine. A blocklist, a best-effort
scrub, or a "record and hope" posture would fail open the first time a
vendor invents a new auth header — so the engine never does any of those.

It is a **pure library**:
`backend/src/meho_backplane/redaction/flight_recorder/`. No capture wiring
(that is [#3214](https://github.com/evoila/meho/issues/3214)), no storage
(that is [#3212](https://github.com/evoila/meho/issues/3212)), no I/O, no
clocks, no global mutable state.

```text
   captured span (headers + bodies + op context)
                    │
                    ▼
   redact_span(...)  ──►  classify_body_exclusion(op_id)   [F2.3]
        │                        │ excluded? → body never recorded
        ├── redact_headers()     │ unplaceable? → uncertain
        │     allowlist [F2.1]   ▼
        ├── redact_body(paths)   per-connector body-path scrub [F2.2]
        │     + credential-shape net (reuses the Tier-1 engine)
        ▼
   SpanRedaction(request/response headers+bodies, uncertain, reasons)
        │  uncertain == True ⇒ caller degrades to operator-only (F5)
```

**F2.1 — header allowlist, not blocklist** (`headers.py`). Only the
enumerated `HEADER_ALLOWLIST` names survive; every other header is
stripped **unread** (the loop `continue`s on the name check before the
value is ever touched). The allowlist holds only headers whose values are
structurally non-secret — content metadata, protocol negotiation,
caching, correlation/rate-limit — and every credential/cookie/token/
signed-URL/client-identity header is absent by construction:

| Recorded (allowlisted) | Stripped unread (never allowlisted) |
| --- | --- |
| `content-type`, `content-length`, `content-encoding`, `content-language`, `content-range` | `authorization`, `proxy-authorization`, `www-authenticate`, `proxy-authenticate` |
| `accept*`, `connection`, `transfer-encoding`, `host`, `user-agent`, `server`, `via`, `allow` | `cookie`, `set-cookie` |
| `cache-control`, `date`, `age`, `expires`, `etag`, `vary`, `if-*`, `range`, `retry-after` | every `x-*-token`, `x-api-key`, `x-csrf-token`, `x-xsrf-token`, `x-vault-token`, `x-amz-security-token` |
| `x-request-id`, `x-correlation-id`, `traceparent`, `tracestate`, `x-ratelimit-*`, `audit-id` (Kubernetes request id), `warning` (RFC 7234; Kubernetes deprecation and admission notices) | `location`, `content-location`, `referer` (signed URLs / tokens) |
| `strict-transport-security`, `x-content-type-options`, `x-frame-options`, `referrer-policy`, `content-security-policy` | `forwarded`, `x-forwarded-*`, `x-real-ip` (client identity / PII) |

**Query strings** (`query.py`, #3841). The vendor-call seam drops the
query from a recorded URL. A transport that needs the query on the span
(Kubernetes: `dryRun`, `fieldManager`) passes it to `redact_query(params,
safe_names)`. Every parameter name is kept. A value is kept only when its
name is on the caller's safe list, and it still gets the credential-shape
scrub. Every other value becomes `[redacted]` without being read. A query
shape the engine cannot walk records nothing and is uncertain.

As defense-in-depth the value of every *surviving* header is still run
through the credential-shape net (`_content.py`), so a `Bearer …` pasted
into a `user-agent` is scrubbed too. Residual assumption (accepted): an
*opaque, non-shaped* secret placed in a surviving free-text header
(`user-agent`, `server`, `via`) is not caught by the shape net — the
allowlist admits these header *names* on the basis that their values are
vendor-non-secret, the same structured-body caveat that applies to an
undeclared, non-shaped body leaf.

**F2.2 — per-connector body-path redaction** (`bodies.py`). A connector
declares dotted-path globs (`BodyPathRedactionConfig.paths`, glob grammar
reused verbatim from `redaction.path_glob`) that are scrubbed from both
request and response bodies; a glob landing on an interior node redacts
the **whole subtree**. Underneath, the same credential-shape net runs
over every surviving leaf, so a shaped secret at an *undeclared* nested
path is still caught. JSON is the only structurally-redactable shape:
a str/bytes body is parsed when the content-type is JSON or unknown;
anything else fails closed (see F2 uncertainty). An XML body (`text/xml`,
`application/xml`, `*+xml`, for example a vim25 SOAP answer) is never
recorded either, but it is not dropped bare: `_xml_digest.py` records a
digest in its place — `{"omitted": <marker>, "content_sha256": <hex>,
"fault_types": {<type name>: <count>}}` (#3708). The fault types come
from each vim `fault` element (`missingSet[].fault`, task errors) and each
`soapenv:Fault/detail` child; the `LocalizedMethodFault` wrapper is
skipped. Only type names are read, never fault text, and a name that is
not identifier-shaped is dropped. The parse uses `defusedxml` and streams,
so a body cut at the size cap still counts the faults before the cut. This
lets an operator see, for example, that every property of a host came back
in `missingSet` with `NoPermission`. One structural rule runs
without any per-connector config: every property `value` inside an OVF
`PropertyParams` block (`additional_parameters[] {type: "PropertyParams",
properties: [{id, value}]}`) is redacted — those are OVF property inputs
which commonly carry appliance credentials under a vendor-specific `id` no
key-name heuristic or shape net can place — keeping the `id` and every
non-secret field (network mappings, name, placement) intact.

A connector can also mask a body itself before the span is recorded.
`HttpConnector.trace_response_body(response)` returns the body the trace
records in place of the raw bytes, or `None` for the raw bytes (the
default). The shared engine still runs on what it returns. This is for a
vendor answer that carries a secret in a field the shape net cannot place
and the op family does not exclude: the HCX connector masks every
secret-named field (`password`, `vcPassword`, `token`, ...) of every
answer this way, so a stored pairing password never reaches the trace
(`docs/codebase/connectors-hcx.md`). The hook runs only while a trace is
being captured.

**F2.3 — hard-excluded op families** (`families.py`). Some op families
carry secrets *as their body* — credential reads, session mints, token
issuance, password resets, key material — so their bodies are **never
recorded**, regardless of the path config. The **primary** check is
single-sourced: `classify_body_exclusion` delegates to the authoritative
`broadcast.events.classify_op`, and any op it calls `credential_read` /
`credential_mint` / `credential_write` (its curated `_CREDENTIAL_*_OPS`
allowlists — `vault.kv.read`, `harbor.robot.create`, `vault.kv.put`,
`k8s.secret.create`, …) never records a body here either. Those ops carry
structured secret bodies (`{"data": {"password": …}}`) whose opaque
values have no in-leaf label the shape net could catch, so this
delegation — not the pattern net — is what protects them, and the two
planes cannot drift. The vSphere guest-ops composites that log into the
guest (`vmware.composite.vm.guest.{process.list,env.read,file.read,file.write,program.run}`)
are pinned in `_CREDENTIAL_WRITE_OPS` for the same reason: their guest OS
password rides the downstream vim `NamePasswordAuthentication` block in the
*request* body, not an op param or a declared property, so this `classify_op`
delegation — not the shape/pattern net — is what keeps their vendor-call
bodies from ever being recorded (`net.show` is not pinned: it sends no
in-guest login). On top of that, a **broadening net** covers generic
(`METHOD:/path`) ops and hand-authored tags the typed classifier does not
see: case-sensitive `fnmatchcase` globs (`SECRET_FAMILY_PATTERNS`, e.g.
`*token*`, `*secret*`, `*login*`, `*.auth.*`, `*:*/key`) plus
token-split descriptor tags (`SECRET_FAMILY_TAGS`, so hyphenated
compounds like `secret-read` / `credential-mint` match). Over-exclusion
is deliberate — declining to record a benign body only loses debugging
exhaust; under-exclusion leaks.

| Family | Source | Records body? |
| --- | --- | --- |
| secret-bearing (credential-read / -mint / -write) | **single-sourced** with `broadcast.events.classify_op` (`_CREDENTIAL_*_OPS`) | never |
| secret-bearing (generic / tag-declared) | broadening `SECRET_FAMILY_PATTERNS` globs + token-split `SECRET_FAMILY_TAGS` | never |
| destructive / delete-shaped | **single-sourced** with the grant guard's `Settings.service_grant_delete_shaped_patterns` + HTTP `DELETE` + `destructive` tag + a vendor delete action name (`Destroy`, `Delete`, `Remove`, `Unregister`, also after a vSAN prefix, or an `action=delete` query; the ingest parser's word list in `meho_backplane/delete_names.py`, #4632) | never |
| everything else | — | yes (subject to F2.2) |

The destructive family is **single-sourced** with the delete-shaped
classifier at `operations/service_grants.py` (`_delete_shaped_reason_by_pattern`
/ `_delete_shaped_reason_by_descriptor`) so the two lists cannot drift;
`classify_body_exclusion` reads the same `Settings` tuple rather than
re-declaring it. The vendor action name is the third sign (#4632): a
VI-JSON delete such as `POST:/VirtualMachine/{moId}/Destroy_Task` is a
`POST`, so neither the method nor a pattern catches it. The classifier
reads the parser's word list from `meho_backplane/delete_names.py` (a module
that imports nothing from MEHO) and puts such an op in the `destructive`
family, so its body is never recorded. This honours the pending cross-ref amendment to
[`governed-delete-operations.md`](../decisions/governed-delete-operations.md)
noted in the flight-recorder decision's "Interactions with sibling
decisions" section — the destructive family joins the hard-exclusion set
now; the reciprocal pointer back from that doc is the deferred edit. When
the `destructive` safety tier ([#3196](https://github.com/evoila/meho/issues/3196))
lands, the classifier already covers it via the tag it promotes.

Deployment specs and depot credentials are kept out of trace bodies like
credential writes: the operations that send a domain spec, an SddcSpec or the
depot password (`sddc.domain.validate` / `create`,
`installer.sddc.spec.validate`, `installer.sddc.bringup.start` / `retry`,
`installer.composite.sddc.bringup`, `installer.system.depot.set`) carry the
tag `credential`, a secret tag word (`SECRET_FAMILY_TAGS`).
The ingested (`METHOD:/path`) copies of the same VCF calls have no such tag.
`SECRET_FAMILY_PATTERNS` names them, so their bodies are not recorded either:
`POST:/v1/domains`, `POST:/v1/domains/validations`, `POST:/v1/sddcs`,
`POST:/v1/sddcs/*` (validate, resource calculation, the discovery logins),
`PATCH:/v1/sddcs/*` (retry), `GET:/v1/sddcs/*/spec`,
`PUT:/v1/system/settings/depot` and `PATCH:/v1/system/proxy-configuration`.
Status reads such as `GET:/v1/sddcs/{id}` keep their bodies.

The dispatcher's error path uses the same secret check (#4854). For an
operation that `classify_op` pins as `credential_write`, or whose tags
have a word from `SECRET_FAMILY_TAGS`, the vendor's text in an error
result is replaced with one fixed text before the audit row is written
(`operations/_vendor_text.py`, through the shared
`families.secret_tag_tokens`). So the error result and the audit row of
such an operation keep no vendor text either. The details are in
`docs/codebase/error-message-shape.md` ("The info-leak boundary").

**F2 uncertainty (the F5 degrade)**. Any state the engine cannot *prove*
fully redacted returns `uncertain=True`, which the caller must map to
operator-only visibility. Uncertainty triggers:

| Trigger | Outcome |
| --- | --- |
| headers not a mapping | no headers recorded, uncertain |
| unparseable / binary / unknown non-JSON body | body omitted, uncertain |
| XML body | body omitted, fault-type digest + SHA-256 recorded, uncertain |
| malformed JSON | body omitted, uncertain |
| truncated mid-token (`truncated=True`) | parsed prefix scrubbed, still uncertain (a split secret matches no shape) |
| body-path config fault at match time | body omitted, uncertain |
| unplaceable op family (missing/blank op id) | body omitted, uncertain |

A *placed* family exclusion is **certain** (the omission is deliberate and
safe, so the trace stays agent-readable with a blank body); only an
*unplaceable* op is uncertain. Fail-closed composition: a span is
uncertain if any one artefact is uncertain.

Adversarial coverage lives in
`backend/tests/test_flight_recorder_redaction.py` (129 cases): synthetic
`PLACEHOLDER`-shaped secrets planted in unknown headers, allowlisted
header values, declared and undeclared nested body paths, oversized bodies
truncated mid-token, malformed JSON, and binary bodies — none survive, and
uncertainty fires wherever proof is impossible.

## Per-connector structural redactors

The Tier-1 engine keys on **labelled** secret shapes inside string leaves
(`password=…`, `Bearer …`). A structured payload whose secret is an
arbitrary, operator-chosen value with no in-leaf label slips through by
design — a Kubernetes `Secret`'s base64 `data` map, an ArgoCD repository
`sshPrivateKey`. Those are closed at the **connector boundary** by small,
pure, structural redactors the read handlers apply to their own output
before it reaches the JSONFlux reducer / result handle, the audit row, or
the broadcast feed. Each is the same shape: a recursive walk returning a
new structure, matched to that connector's payload contract.

| Connector | Module | What it scrubs |
| --- | --- | --- |
| keycloak | `connectors/keycloak/redaction.py` | `ClientRepresentation.secret`, `UserRepresentation.credentials`, and the generic credential key names, recursively (#1394). |
| rabbitmq | `connectors/rabbitmq/redact.py` | `amqp://user:pass@` URI userinfo + any `*password*` / `*secret*` key (#2233). |
| kubernetes | `connectors/kubernetes/redaction.py` | Every `data` / `stringData` value of a `kind: Secret` object — replaced with a fixed placeholder + a `sha256:` digest, key names kept — in single-object, list, and dynamic `k8s.cr.*` reads (#3501). Also the kind-less items of a `SecretList` envelope and every annotation value of a Secret (keys kept), since an annotation such as `kubectl.kubernetes.io/last-applied-configuration` can repeat its values (#3841, #3878). |
| argocd | `connectors/argocd/redaction.py` | Repository credential fields (`password` / `sshPrivateKey` / `tlsClientCertKey` / `bearerToken` / `githubAppPrivateKey`) on `argocd.repo.list` (#3501). ArgoCD destination-cluster creds are dropped separately by `argocd.cluster.list`'s whole-`config` strip (#2855). |

The kubernetes redactor is deliberately **structural, not pattern-based**:
it redacts every Secret value regardless of whether the value matches a
named credential pattern, keeping the key inventory + a per-value digest
so a read still answers "which keys does this Secret carry?" without ever
returning the bytes. It leaves `k8s.secret.read_to_ref` (#3496) untouched
— that op returns only a Vault `secret_ref` + a SHA-256, never a `data`
map, so there is nothing for the walk to match.

The flight recorder reuses the same walk (#3841). The Kubernetes transport
hook (`connectors/kubernetes/_wire_span.py`) parses every JSON request and
response body and runs `redact_kubernetes_payload` over it before the body
reaches the shared F2 redaction path. The F2 engine on its own keys on
labelled shapes and would keep an opaque base64 Secret value, and ops such
as `k8s.apply` or `k8s.cr.info` are not secret-bearing families, so the
structural pass is what masks a Secret manifest or a Secret read on the
`vendor_call` span. The span's `response_object` summary is then built from
the redacted body, never from the raw response. A response body over the
64 KiB span cap is not handed to the F2 path at all: the hook passes an
explicit omission reason (`record_sdk_vendor_call(response_body_omitted=...)`),
the span stores `BODY_OMITTED_MARKER`, and the trace is marked uncertain.
This closes the case where the F2 cap cuts only a trailing newline, the cut
body still parses, and an unmasked Secret value would otherwise be kept.

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
- Flight-recorder redaction (F2; #3213): decision
  [`docs/decisions/dispatch-flight-recorder.md`](../decisions/dispatch-flight-recorder.md),
  Initiative [#3207](https://github.com/evoila/meho/issues/3207); library
  `backend/src/meho_backplane/redaction/flight_recorder/`; single-sourced
  destructive classifier `operations/service_grants.py`
  (`Settings.service_grant_delete_shaped_patterns`).
