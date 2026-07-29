# Secret broker (`connectors/secret`)

## Overview

The secret broker moves a single credential field from one store to
another **server-side**, so the agent driving the move never observes
the value. It exists because an autonomous agent can orchestrate almost
every operational step except moving a secret from system A to system B:
reading the value into the agent's context lands it in the model/API
transcript (forcing a rotation), and bouncing to a human terminal breaks
unattended operation. The broker is the safe primitive for "take the
value at A, put it at B, agent never observes it, with provenance."

This task (#1577, Initiative #581 T1) establishes the **mechanism**: the
adapter protocol, the kind-keyed registry, the synthetic `secret.move`
typed op + handler, and the first adapter pair (vault-kv source AND
vault-kv sink). Sibling tasks add further adapter kinds (#1578 keycloak
sink), the approval-queue gating (#1579), the CLI verb (#1580), and the
docs page (#1581). Those reuse the names established here.

## Core invariant

The secret value must **never** appear in:

- the op params / the agent's args / the transcript,
- a log event,
- the op response or a broadcast payload,
- the audit row.

Only the move **status**, the value's **SHA-256**, and its **byte
length** are surfaced. This holds **by construction**, not by relying on
boundary redaction:

- The agent submits only declarative `<kind>:<ref>` references. The
  value is read inside the backplane, never passed in params.
- `SecretMaterial` (the in-memory carrier) redacts its value in
  `__repr__` / `__str__`, so a stray log-bind or f-string renders
  `<SecretMaterial len=N sha256=…>`, not the value.
- The handler returns only `{status, value_sha256, length}`. The
  dispatcher stores `params_hash` (not the params) in the audit row's
  `payload`, and the handler's return dict in `raw_payload` — so neither
  audit column carries the value.

## Key types

- **`SecretMaterial`** (`endpoints.py`) — wraps the moved value as
  `bytes`. Exposes `value` (read once by a sink's `write_secret`),
  `length`, and `value_sha256`. `__repr__`/`__str__` redact.
- **`SecretEndpoint`** (`endpoints.py`) — a `runtime_checkable`
  `typing.Protocol`. A source adapter implements
  `async read_secret(operator) -> SecretMaterial`; a sink adapter
  implements `async write_secret(operator, material) -> None`. Both take
  the request-scoped `Operator` so the store access runs under the
  operator's own credentials. An adapter that is both (vault-kv)
  implements both methods.
- **`SecretRef`** + **`parse_secret_ref`** (`endpoints.py`) — parse a
  `"<kind>:<ref>"` intent string on the **first** colon (so the ref may
  contain colons). Both halves must be non-empty.
- **`SECRET_ENDPOINT_REGISTRY`** + **`register_secret_endpoint`**
  (`endpoints.py`) — kind-string → endpoint-factory registry. Each
  adapter registers its kind(s) at import time. The move handler resolves
  a parsed `<kind>` to the factory that builds the per-move endpoint from
  the `ref`. This is the extension seam sibling adapter tasks register
  into; duplicate-kind registration raises.
- **`VaultKvSecretEndpoint`** (`vault_endpoint.py`) — the first adapter,
  registered under kind `"vault"`. Addresses a KV-v2 secret as
  `<path>#<field>` (the `#<field>` fragment is required and selects one
  field). Reads via `read_secret_version` + the KV-v2 `data.data`
  double-unwrap + `strip_credential_value`; writes via
  `create_or_update_secret` (a single-field body, `cas=None`). Both
  through `vault_client_for_operator`.
- **`KeycloakCredentialSecretEndpoint`**
  (`connectors/keycloak/secret_endpoint.py`) — the second adapter (#1578),
  registered under kind `"keycloak"`. **Sink-only**: keycloak credentials
  are write-only (Keycloak hashes them; the plaintext is unrecoverable),
  so `read_secret` raises `NotImplementedError`. Addresses one user
  credential as `<target>/<realm>/<username>#password` (the `#password`
  field is the only writable one). `write_secret` resolves the target by
  name (tenant-scoped `resolve_target`), gets the `KeycloakConnector`
  instance from the dispatcher's instance cache, resolves username→UUID
  via `_find_user_uuid`, and PUTs `.../users/{id}/reset-password` with a
  permanent password CredentialRepresentation via `_write_admin` — it
  opens no HTTP client of its own. The value is `strip_credential_value`-d
  before the PUT. This is the broker's **second kind**, so a cross-kind
  `vault:` → `keycloak:` move proves the initiative's "≥2 kinds" DoD.
- **`secret_move`** + **`register_secret_broker_operations`**
  (`ops.py`) — the module-level handler and its lifespan registrar.

## Control flow

A move dispatches through the standard typed-op path:

1. A caller dispatches `connector_id="secret-broker-1.x"`,
   `op_id="secret.move"`, `target=None`, params
   `{"from": "<kind>:<ref>", "to": "<kind>:<ref>", "reason": ...}`.
2. `parse_connector_id("secret-broker-1.x")` →
   `("secret", "1.x", "secret-broker")` (the natural key the descriptor
   is registered under). The version segment is digit-led (`1.x`) and the
   product is the head's first hyphen segment, both required for the id
   to round-trip — a colon form or a non-digit-led version would make the
   descriptor unreachable.
3. The dispatcher validates params against the op's
   `parameter_schema`, then runs the policy gate. Because the op is
   `requires_approval=True`, an ordinary dispatch is parked at
   `awaiting_approval` and the handler never runs; the approval-resume
   path (`_approved=True`) skips the gate and runs the handler.
4. The handler is **module-level** (no `self`), so
   `_resolve_connector_instance` returns `(None, None, None)` and the op
   dispatches with `connector_instance=None`. The synthetic `secret`
   product has no connector class.
5. `secret_move` parses both refs, resolves each kind to a
   `SecretEndpoint` via the registry, `read_secret` from the source
   (value enters memory only as a `SecretMaterial`), `write_secret` to
   the sink — entirely server-side. It returns
   `{status, value_sha256, length}`.
6. The dispatcher redacts → reduces → audits the return dict. Since the
   return carries no value, the audit row's `raw_payload` and `payload`
   carry no value.

## The synthetic identity

`secret-broker-1.x` is the first **synthetic** connector identity in the
codebase: no vendor connector backs it. The package `__init__` calls
neither `register_connector` nor `register_connector_v2` (every other
connector subpackage registers a connector class). It only:

- imports `vault_endpoint` for its module-level
  `register_secret_endpoint("vault", …)` side effect, and
- queues `register_secret_broker_operations` onto the lifespan registrar
  list via `register_typed_op_registrar`.

The lifespan's `_eager_import_connectors` pass imports the
`connectors/secret/` subpackage (it walks every subpackage), so both
import-time effects land before `run_typed_op_registrars` runs.

## Change-class posture

The op registers `safety_level="dangerous"` + `requires_approval=True`,
so the **existing** approval gate parks an unapproved move (#1577 sets
the posture and relies on the existing gate). The policy refinement
(#1579) reuses the same substrate — no new infrastructure:

- **Ref-only `proposed_effect`.** A park-time preview builder
  (`move_preview.py`, registered via `register_preview_builder("secret.move", …)`)
  populates `ApprovalRequest.proposed_effect` with a ref-only summary —
  the parsed `{kind, ref}` of `--from`/`--to` plus the operator `reason`,
  never the value. `secret.move` classifies `"other"` (so the
  credential-class preview suppression does not fire) and carries no
  value in its params, so the summary is value-free by construction.
- **Time-boxed scope.** The grant is the existing `AgentPermission`
  (`op_pattern`/`verdict`/`expires_at`) + the approval's `expires_at` —
  no bearer token. A grant whose `expires_at` has passed is excluded by
  the resolver, so an agent reverts to baseline. Note the `dangerous`
  safety lattice: an agent with **no live grant** is *denied* (not
  parked), and a **live** `auto-execute` grant is *capped to
  needs-approval* (the `dangerous` ceiling) — so an agent can never
  auto-execute a credential move; a live grant only lifts the verdict
  off the deny baseline to park. A parked row past its `expires_at` is
  swept to `EXPIRED` and is no longer decidable (`/decide` → 409).

## Dependencies

- `meho_backplane.operations.typed_register` — `register_typed_operation`
  (op upsert), `register_typed_op_registrar` (lifespan wiring).
- `meho_backplane.operations.dispatcher` — the dispatch path, the policy
  gate, the redact→reduce→audit sequence (value-free for this op).
- `meho_backplane.operations._lookup.parse_connector_id` — the
  connector-id round-trip the synthetic identity is built to satisfy.
- `meho_backplane.auth.vault.vault_client_for_operator` — the
  operator-scoped Vault client (JWT/OIDC login, token revoked on exit).
- `meho_backplane.connectors._shared.vault_creds` — `strip_credential_value`
  + the `data.data` double-unwrap shape, mirrored by the vault adapter.

## Known issues / scope boundaries

- vault-kv (source+sink) and keycloak (sink-only, #1578) are the
  registered adapter kinds; further kinds are separate tasks reusing the
  `SecretEndpoint` contract.
- The vault adapter forwards the `ref` to hvac's `path=` and defaults the
  mount to `"secret"`; a non-default mount is a richer-ref-grammar
  follow-up, not wired here.
- The `reason` param is recorded for the approver/audit trail but is not
  read by the handler; it is surfaced to the approver in the ref-only
  `proposed_effect` summary (#1579).
- **Read-surface projection (#2496):** the synthetic `secret-broker-1.x`
  identity has no connector class, so `resolve_authoring_kind`'s resolver
  replay misses. It keys on the row's own `source_kind` to recognize this
  class-less typed mold, so the connector listing and
  `meho.connector.review` report `secret-broker-1.x` as `kind="typed"` /
  `dispatchable=true` rather than the `ingested-shim` dead end.

## References

- `backend/src/meho_backplane/connectors/secret/endpoints.py`
- `backend/src/meho_backplane/connectors/secret/vault_endpoint.py`
- `backend/src/meho_backplane/connectors/secret/ops.py`
- `backend/src/meho_backplane/connectors/secret/move_preview.py`
- `backend/src/meho_backplane/connectors/secret/__init__.py`
- `backend/tests/test_connectors_secret_broker.py`
- `backend/tests/test_secret_move_approval.py`
- Initiative #581 (G0.22 Secret broker), Goal #221.
