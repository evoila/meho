<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Secret broker — `meho secret move` (operator runbook)

> The secret broker copies **one credential field** from one store to
> another **server-side**, so the agent driving the move — and the
> operator running the CLI — never observe the cleartext. The agent
> submits only declarative `<kind>:<ref>` references; the backplane reads
> the value, writes it to the sink, and returns only the move **status**,
> the value's **SHA-256**, and its **byte length** — never the value. The
> move is change-class (`safety_level="dangerous"`,
> `requires_approval=True`): it routes through the existing approval
> queue before it executes. This page is the operator/reviewer-facing
> guide to the intent schema, the no-observe guarantee and *why* it
> holds, and the enlarged threat model. The engineering companion is
> [`docs/codebase/connectors-secret-broker.md`](../codebase/connectors-secret-broker.md).

## When to use it (and when not)

Use `secret.move` when an operational flow needs a credential **copied
from store A to store B** and reading the value into the agent's context
would force a rotation (e.g. provisioning a standby database, or seeding
a Keycloak user's password from a credential held in Vault). The broker
is the safe primitive for *"take the value at A, put it at B, the agent
never observes it, with provenance."*

Do **not** reach for it to *read* a secret — there is no read verb, by
design. A read would land the value in the transcript. The broker only
*moves*. It also moves a **single named field**, not a whole secret body:
naming the field is what keeps the `value_sha256` / `length` in the
response meaningful as provenance.

The op is operator-only on the CLI surface (per CLAUDE.md postulate 5);
agents reach `secret.move` through the narrow-waist `search_operations` /
`call_operation` meta-tools, never a dedicated agent verb.

## 1. The move intent

### 1.1 The CLI verb

```bash
meho secret move \
  --from <kind>:<ref> \
  --to   <kind>:<ref> \
  --reason 'why this move is happening' \
  [--json] [--backplane <url>]
```

`meho secret move` is a thin Cobra layer over
`POST /api/v1/operations/call`, pre-baking the synthetic broker
`connector_id` `secret-broker-1.x` so an operator does not type it on
every dispatch
([`cli/internal/cmd/secret/secret.go`](../../cli/internal/cmd/secret/secret.go),
[`move.go`](../../cli/internal/cmd/secret/move.go)). What the verb sends
across the wire is **only** the two references and the reason:

```json
{ "from": "<kind>:<ref>", "to": "<kind>:<ref>", "reason": "…" }
```

There is deliberately **no** `--value` / `--secret` / `--password` flag
of any kind. The value is never a CLI argument, flag, env var, or prompt,
so it never lands in `argv`, shell history, `ps` output, or the op
params.

> **`--reason` is required by the CLI, optional in the op schema.**
> `meho secret move` marks all three of `--from` / `--to` / `--reason`
> required ([`move.go`](../../cli/internal/cmd/secret/move.go),
> `MarkFlagRequired`). The backend op
> `parameter_schema` requires only `from` and `to`; `reason` is optional
> there ([`ops.py`](../../backend/src/meho_backplane/connectors/secret/ops.py),
> `SECRET_MOVE_PARAMETER_SCHEMA`). The CLI tightens the contract so every
> operator-initiated move carries an audit justification for the
> approver — a direct `call_operation` may omit it, but you should not.

### 1.2 The `<kind>:<ref>` grammar

A reference is a `"<kind>:<ref>"` string. The split is on the **first**
colon ([`parse_secret_ref`](../../backend/src/meho_backplane/connectors/secret/endpoints.py)),
so the store-specific `ref` may itself contain colons. Both halves must
be non-empty — a malformed intent is rejected at param validation
(`pattern: ^\S+:\S+$`,
[`ops.py`](../../backend/src/meho_backplane/connectors/secret/ops.py)),
not deep inside the handler. `kind` selects the store adapter from the
registry; `ref` is the store-specific address that adapter interprets.

Two kinds ship today (the initiative's "≥2 connector kinds" definition of
done). A move may be **same-kind** (`vault` → `vault`) or **cross-kind**
(`vault` → `keycloak`):

| `kind` | Role | `ref` grammar | Example |
| --- | --- | --- | --- |
| `vault` | source **and** sink | `<path>#<field>` — a KV-v2 path with a required `#<field>` fragment selecting one field | `vault:secret/db/prod#password` |
| `keycloak` | sink **only** | `<target>/<realm>/<username>#password` — `#password` is the only writable field | `keycloak:rdc-keycloak/evba/operator-a#password` |

**vault** (`vault:` —
[`vault_endpoint.py`](../../backend/src/meho_backplane/connectors/secret/vault_endpoint.py)).
A vault-kv adapter is both a source (`read_secret`) and a sink
(`write_secret`). The `#<field>` fragment is **required** — the broker
moves one field, not a whole secret body. The mount defaults to `secret`
(the deployment KV-v2 mount); a non-default mount is a richer-ref-grammar
follow-up, not wired here. Reads and writes go through
[`vault_client_for_operator`](../../backend/src/meho_backplane/auth/vault.py#L199)
under the **operator's** own Vault token (JWT/OIDC login, revoked on
exit), so the move runs inside the operator's existing authorization
envelope and per-operator Vault policy — see
[`connector-vault-policy.md`](./connector-vault-policy.md) for the read
(§2) and write (§6) ACL stanzas the operator's token needs.

**keycloak** (`keycloak:` —
[`secret_endpoint.py`](../../backend/src/meho_backplane/connectors/keycloak/secret_endpoint.py)).
A keycloak adapter is a **sink only**: Keycloak hashes credentials and
never serves the plaintext back over the Admin REST API, so a keycloak
**source** has nothing to read and `read_secret` raises a clear
unsupported error (the dispatcher maps it to a `connector_error` naming
the kind, never a value). The sink resolves `<target>` tenant-scoped by
name, resolves `<username>`→UUID, and `PUT`s a permanent password
`CredentialRepresentation` to `.../users/{id}/reset-password` by reusing
the connector's existing admin-write path. Two identities are in play
here: the **operator's JWT** authorises only the per-operator target
resolution, while the Keycloak admin `PUT` itself is minted under the
**connector's own admin credential** — the operator's token never reaches
Keycloak.

### 1.3 The response — value-free by schema

On success the op returns **only**:

```json
{ "status": "moved", "value_sha256": "<hex>", "length": <int> }
```

The response schema is `additionalProperties: false` over exactly these
three fields ([`ops.py`](../../backend/src/meho_backplane/connectors/secret/ops.py),
`_SECRET_MOVE_RESPONSE_SCHEMA`), so nothing value-derived beyond the
SHA-256 and the byte length can ride out. The CLI renders the same three
scalars and nothing else
([`move.go`](../../cli/internal/cmd/secret/move.go), `moveResultKeyOrder`).

`value_sha256` is the **provenance** signal: an auditor can confirm the
value that landed at the sink is the value that was read from the source
(the SHA-256 is computed over the exact bytes written, after whitespace
normalization), without anyone ever seeing the value. `length` is the
byte length of the same bytes.

### 1.4 What the agent submits vs. what the backplane does

| The agent / operator submits | The backplane does, server-side |
| --- | --- |
| `from` `<kind>:<ref>` — an **address** | parse → resolve the kind's adapter → `read_secret` into an in-memory `SecretMaterial` |
| `to` `<kind>:<ref>` — an **address** | resolve the kind's adapter → `write_secret(material)` into the sink |
| `reason` — audit justification | record it on the audit row (via the params hash) + surface it to the approver |
| *(nothing else — no value)* | hash + measure the bytes; return `{status, value_sha256, length}` |

The value enters memory **only** as a `SecretMaterial` between the source
read and the sink write
([`ops.py`](../../backend/src/meho_backplane/connectors/secret/ops.py),
`secret_move`). It never crosses back to the caller.

## 2. The no-observe guarantee — and *why* it holds

The core invariant (Initiative #581): **the secret value never appears**
in the agent's args / the transcript, a log event, the op response, a
broadcast payload, an audit row, or the approval's `proposed_effect`.
This holds **by construction**, not by relying on boundary redaction.
Each claim below ties to the mechanism that enforces it.

| The value is absent from… | …because |
| --- | --- |
| **CLI args / argv / shell history / `ps`** | there is no value-bearing flag — `--from`/`--to`/`--reason` carry only addresses + a reason ([`move.go`](../../cli/internal/cmd/secret/move.go)). The value is never typed. |
| **Op params** | `additionalProperties: false` on the param schema rejects a smuggled value field; the params carry only references + reason ([`ops.py`](../../backend/src/meho_backplane/connectors/secret/ops.py), `SECRET_MOVE_PARAMETER_SCHEMA`). |
| **The op response** | the handler returns only `{status, value_sha256, length}`; the response schema is `additionalProperties: false` over those three ([`ops.py`](../../backend/src/meho_backplane/connectors/secret/ops.py)). |
| **Logs** | the only in-memory carrier, [`SecretMaterial`](../../backend/src/meho_backplane/connectors/secret/endpoints.py), overrides `__repr__`/`__str__` to render `<SecretMaterial len=N sha256=…>` — so a stray `logger.info(..., material=m)` or f-string renders the redacted form, never the value. Adapters log only field **names**/paths, never values, mirroring [`strip_credential_value`](../../backend/src/meho_backplane/connectors/_shared/vault_creds.py#L104)'s "No secret in logs" discipline. |
| **The audit row** | this is the load-bearing distinction. The generic dispatch path persists the raw payload behind redaction ([`dispatcher.py`](../../backend/src/meho_backplane/operations/dispatcher.py) module docstring, step 9). For `secret.move` there is **no raw value for the audit row to carry in the first place**: the value was never in the params (only references) and never in the response (only hash + length). The audit row stores the value-free params + the value-free return. There is nothing to redact. |
| **The approval `proposed_effect`** | the park-time preview builder ([`move_preview.py`](../../backend/src/meho_backplane/connectors/secret/move_preview.py)) summarises only the parsed `{kind, ref}` of `--from`/`--to` plus the operator `reason`. It does **no store I/O at all**, so it cannot observe a value to leak. |
| **Broadcast / event payloads** | [`classify_op("secret.move")`](../../backend/src/meho_backplane/broadcast/events.py) returns `"other"` (`.move` is in neither the read- nor write-suffix set, and `secret.move` is in no credential allowlist). The broadcast carries the same value-free params + response the audit row sees — there is no value in them to broadcast. |

The transfer is **server-side**: read from source → write to sink, both
under the operator's identity (for the vault adapter, via
`vault_client_for_operator`). The value lives only inside the
`SecretMaterial` for the duration of one handler call, is read back
exactly once by the sink's `write_secret`, and is otherwise inert.

This is the honest difference from the generic connector path. A normal
op can carry secret-bearing params or a credential-minting response, so
the dispatch path leans on redaction and the credential broadcast classes
to keep values out of logs/broadcasts. `secret.move` does not need that
safety net for the **value**, because the value is never in the params or
the response to begin with — only its hash and length.

## 3. Threat model

### 3.1 The enlarged blast radius

The broker is a deliberate trust shift. The existing connector model only
ever **reads** a vendor credential under the acting operator's identity
and uses it to talk to *that one vendor* — so a compromised backplane can
read no more than the currently-acting operator could
([`connector-vault-policy.md`](./connector-vault-policy.md) §5). The
secret broker breaks that single-store assumption: a single synthetic
`secret.move` op can **read from one store and write to another**,
potentially across two different kinds (`vault` → `keycloak`). The
backplane is now a credential-bearing **intermediary** that touches both
sides of a move.

That is the new blast radius to reason about. The mitigations below each
map to a primitive that **actually ships** — there is no god-mode token
and no value ever rests in a durable artifact.

### 3.2 Mitigations (each maps to a shipped primitive)

1. **Operator-context credential reads — no god-mode.** The vault
   adapter reads and writes under the **operator's own** Vault token via
   [`vault_client_for_operator`](../../backend/src/meho_backplane/auth/vault.py#L199),
   bounded by that operator's Vault policy (per-operator RBAC + audit
   through one role — see
   [`connector-vault-policy.md`](./connector-vault-policy.md) §2/§5/§6).
   The backplane never holds a broad backplane policy for the move. The
   operator identity is carried by
   [`Operator`](../../backend/src/meho_backplane/auth/operator.py#L118),
   whose `raw_jwt` is `repr=False` so it never leaks via `repr()`.

2. **Change-class / `dangerous` classification.** The op registers
   `safety_level="dangerous"` + `requires_approval=True`
   ([`ops.py`](../../backend/src/meho_backplane/connectors/secret/ops.py),
   `register_secret_broker_operations`). In the policy gate
   ([`auth/permissions.py`](../../backend/src/meho_backplane/auth/permissions.py))
   `dangerous` carries **two distinct properties** that are worth
   stating precisely, because they are stronger than "parked":

   - **Default with no live grant ⇒ `deny`** (not park). An agent with no
     matching `AgentPermission` grant — or one whose grant has expired —
     is **denied** the move; it does not park and can never auto-execute.
   - **Ceiling ⇒ `needs-approval`.** Even a grant that carries
     `auto-execute` is **capped to `needs-approval`** for a `dangerous`
     op. So a live grant only lifts the verdict off the deny baseline
     *to park for approval* — it can never make a credential move
     auto-execute.

   In short: no grant ⇒ denied; a live grant ⇒ parked for four-eyes; an
   `auto-execute` grant ⇒ still only parked. A credential move never runs
   without a human in the loop.

3. **Mandatory approval-queue gating (four-eyes).** A change-class
   dispatch parks at `status=awaiting_approval`
   ([`dispatcher.py`](../../backend/src/meho_backplane/operations/dispatcher.py))
   and the handler never runs until a human approves through the queue
   (`POST /api/v1/approvals/{id}/decide`,
   [`api/v1/approvals.py`](../../backend/src/meho_backplane/api/v1/approvals.py)).
   The CLI surfaces `awaiting_approval` verbatim (rendered, **exit 0** —
   parked is not failed) rather than treating it as an error
   ([`move.go`](../../cli/internal/cmd/secret/move.go),
   `renderMoveResult`); re-dispatch after approval.

4. **Time-boxed grant + approval `expires_at`.** The scope is the
   existing
   [`AgentPermission`](../../backend/src/meho_backplane/db/models.py)
   row — an `op_pattern` glob (e.g. `secret.*`), a `target_scope`, a
   `verdict`, and a bounded `expires_at` — **not** a bearer token an
   operator passes on the command line. An expired grant is excluded by
   the resolver, so the agent reverts to the deny baseline. The pending
   approval request likewise carries an `expires_at`; a parked row swept
   to `EXPIRED` is no longer decidable (`/decide` → 409).

5. **`params_hash` tamper-evidence on the approved intent.** The approval
   request stores a `params_hash` — a SHA-256 over the canonicalised
   params. The approver decides against the exact `{from, to, reason}`
   references that were submitted; a post-approval tamper of the params
   would not match the hash the approval was granted against.

6. **Ref-only `proposed_effect` + hash-only audit.** The approver sees
   the parsed `{kind, ref}` of source and sink plus the reason
   ([`move_preview.py`](../../backend/src/meho_backplane/connectors/secret/move_preview.py)),
   and the audit row carries the value-free params hash + the value-free
   return — so neither the approval surface nor the durable audit trail
   ever holds the value (§2).

### 3.3 Deferred — not built in this initiative (do **not** assume these ship)

These are deferred follow-ups (related deferred work tracks under G11.7
[#1397](https://github.com/evoila/meho/issues/1397)). The page names them
so a reader does not assume a capability that the shipped code does not
provide:

- **Grant token-minting (approval-as-capability).** The deferred
  "approval-as-capability" follow-up where the backplane **mints** a
  scoped bearer grant token an operator would pass to the CLI as a token
  flag. No task builds it. The shipped substrate is the stored
  `AgentPermission` verdict + the four-eyes
  `POST /api/v1/approvals/{id}/decide` path (§3.2) — **not** a passed-in
  grant-token flag. There is no token flag on `meho secret move`.
- **Diff-shaped approval.** Showing the approver a rendered structured
  diff of the proposed effect. Deferred. The shipped approval surfaces
  `proposed_effect` (the ref-only summary) + `params_hash`, not a
  rendered diff.
- **General secret storage** — the broker *moves* a value between two
  existing stores; it is not a store and does not retain anything.
- **Non-secret data movement** — the broker moves credential material
  only. Moving arbitrary non-secret data is a separate, deferred concern.
- **Vault ACL policy provisioning** for a write path — that belongs in
  the [`connector-vault-policy.md`](./connector-vault-policy.md)
  write-stanza family (§6), referenced here, not re-specified.

## 4. Worked examples

Same-kind move (Vault KV → Vault KV — promote a credential to a standby
path):

```bash
meho secret move \
  --from vault:secret/db/prod#password \
  --to   vault:secret/db/standby#password \
  --reason 'provision standby DB credential'
```

Cross-kind move (Vault KV → Keycloak user password — seed an operator
account from a Vault-held credential, proving the "≥2 kinds" surface):

```bash
meho secret move \
  --from vault:secret/identity/operator-a#password \
  --to   keycloak:rdc-keycloak/evba/operator-a#password \
  --reason 'seed operator-a Keycloak password from Vault'
```

Both park first:

```text
secret-broker-1.x secret.move — status=awaiting_approval (12ms)
  parked for human approval — approve via the approval queue, then re-dispatch
```

After a second operator approves the parked request
(`POST /api/v1/approvals/{id}/decide`), re-run the same command. On
success the CLI prints only the value-free confirmation:

```text
secret-broker-1.x secret.move — status=moved (34ms)
  status:        moved
  value_sha256:  2c6136bb6fb27fb8f45b46fb566e9ce3501e838dc9199e11b761d6425367e35c
  length:        14
```

(The `value_sha256` is the SHA-256 of the moved bytes; `length` is their
byte count — here a 14-byte credential. An auditor can confirm the value
that landed at the sink matches the value read from the source by
comparing this digest, without ever seeing the value.)

## References

- Engineering companion (mechanism, control flow, synthetic identity):
  [`docs/codebase/connectors-secret-broker.md`](../codebase/connectors-secret-broker.md)
- Adapter protocol + `SecretMaterial` + ref parser + registry:
  [`backend/src/meho_backplane/connectors/secret/endpoints.py`](../../backend/src/meho_backplane/connectors/secret/endpoints.py)
- `secret.move` op + handler + schemas + synthetic identity:
  [`backend/src/meho_backplane/connectors/secret/ops.py`](../../backend/src/meho_backplane/connectors/secret/ops.py)
- Vault KV adapter (source+sink, `<path>#<field>`):
  [`backend/src/meho_backplane/connectors/secret/vault_endpoint.py`](../../backend/src/meho_backplane/connectors/secret/vault_endpoint.py)
- Keycloak sink (write-only, `<target>/<realm>/<user>#password`):
  [`backend/src/meho_backplane/connectors/keycloak/secret_endpoint.py`](../../backend/src/meho_backplane/connectors/keycloak/secret_endpoint.py)
- Ref-only park-time `proposed_effect`:
  [`backend/src/meho_backplane/connectors/secret/move_preview.py`](../../backend/src/meho_backplane/connectors/secret/move_preview.py)
- CLI verb (references-not-values, `awaiting_approval` rendering):
  [`cli/internal/cmd/secret/secret.go`](../../cli/internal/cmd/secret/secret.go),
  [`cli/internal/cmd/secret/move.go`](../../cli/internal/cmd/secret/move.go)
- Operator-context Vault client (per-operator RBAC):
  [`backend/src/meho_backplane/auth/vault.py`](../../backend/src/meho_backplane/auth/vault.py#L199)
- "No secret in logs" discipline the adapters mirror:
  [`backend/src/meho_backplane/connectors/_shared/vault_creds.py`](../../backend/src/meho_backplane/connectors/_shared/vault_creds.py#L104)
- Operator identity (`raw_jwt` `repr=False`):
  [`backend/src/meho_backplane/auth/operator.py`](../../backend/src/meho_backplane/auth/operator.py#L118)
- `dangerous` safety lattice (deny-by-default, needs-approval ceiling):
  [`backend/src/meho_backplane/auth/permissions.py`](../../backend/src/meho_backplane/auth/permissions.py)
- Dispatch policy gate + audit + redaction (why the value would otherwise
  land in an audit row):
  [`backend/src/meho_backplane/operations/dispatcher.py`](../../backend/src/meho_backplane/operations/dispatcher.py)
- Approval decide route:
  [`backend/src/meho_backplane/api/v1/approvals.py`](../../backend/src/meho_backplane/api/v1/approvals.py)
- Broadcast op classification (`secret.move` → `other`):
  [`backend/src/meho_backplane/broadcast/events.py`](../../backend/src/meho_backplane/broadcast/events.py)
- Per-target Vault policy (read §2, write §6, blast-radius §5):
  [`connector-vault-policy.md`](./connector-vault-policy.md)
- Initiative [#581](https://github.com/evoila/meho/issues/581) (G0.22
  Secret broker), Goal [#221](https://github.com/evoila/meho/issues/221);
  deferred follow-ups under
  [#1397](https://github.com/evoila/meho/issues/1397).
