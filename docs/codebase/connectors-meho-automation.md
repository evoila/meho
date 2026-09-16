# Connector: meho-automation add-on (mehoauto-rest)

## Overview

The meho-automation **add-on** is a paired, backplane-adjacent workflow
runner. This connector registers it as a **versioned, profile-backed generic
connector target** so the backplane can drive the add-on's operator-facing
launch / validate / gate-decision API through `call_operation` — the same
auth → policy → audit → JSONFlux → broadcast path any vendor API rides —
instead of an out-of-band header-trust call. The add-on remains a peer
control plane reached only over the wire; there is **no reverse code
coupling** (the backplane imports nothing from the add-on; an unpaired
backplane is byte-identical).

It is a **profile-backed generic connector**, not a typed connector and not a
per-op MCP tool family:

- **No hand-coded connector class.** The dispatchable
  `ProfiledRestConnector` is synthesised from the shipped
  `ExecutionProfile` at boot by `stamp_catalog_profiled_connectors`.
- **No vendored spec.** The connector is ingested at registration time from
  the add-on's published `/openapi.json` (OpenAPI 3.1) via the operator
  `--spec` on-ramp. The add-on's OpenAPI is **not committed to this repo**.
  The add-on publishes its **full** API (~30 mutating routes: tenant / site /
  adoption / run DELETEs, fleet import, blueprint / environment / deployment
  CRUD), but this connector's op set is **closed** to exactly launch /
  validate / gate / resume plus the two run-read GETs a governed launcher needs
  to observe the run it created (#3699) and nudge a failed node in (#3707) — a
  declarative catalog op-allowlist enforces that closure in code (see
  [Ingest op allowlist](#ingest-op-allowlist) below). The sibling run-node
  **skip** route is human-only in the add-on and is deliberately kept off this
  surface (see [Run-node control](#run-node-control-resume-vs-skip)).
- **No new agent tools.** The six curated ops ride `op_id` under
  `call_operation`; the MCP working surface is unchanged (no per-op tools).

Dispatch triple: `(product="mehoauto", version="0.1.0",
impl_id="mehoauto-rest")`. The product slug is **hyphen-free** on purpose:
the connector-id parse recovers the product from the first hyphen segment of
`impl_id`, so a hyphenated product (`meho-automation`) would not round-trip
(the same constraint that gave `sddc-manager → sddc`,
`vcf-automation → vcfa`). The add-on's Kubernetes Service name
(`meho-automation`) and the Keycloak audience (`meho-automation`) are
decoupled from this slug and unaffected.

## Shipped artifacts (in this repo)

| Artifact | Path |
|---|---|
| Catalog row | `operations/ingest/catalog.yaml` (`product: mehoauto`) |
| ExecutionProfile | `connectors/profiles/meho_automation_minimal.yaml` |
| Ingest safety floor | `connectors/meho_automation/ingest_safety.py` |

The `connectors/meho_automation/` package holds **only** the safety-floor
registration — a profile-backed connector has no hand-coded class. It is
eager-imported at boot like every other connector subpackage.

## Ops in scope

| Op (`op_id` after ingest) | Purpose | Safety tier |
|---|---|---|
| `POST:/api/v1/runs` | Launch a run | `caution` (no approval park) |
| `POST:/api/v1/blueprints/{blueprint_id}/validate` | Validate a blueprint (read-side dry-run) | `caution` (no approval park) |
| `POST:/api/v1/runs/{run_id}/gates/{node_id}/decision` | Decide an in-run gate | `caution` (no approval park) |
| `POST:/api/v1/runs/{run_id}/nodes/{node_id}/resume` | Re-check or re-run a failed run node (agent nudge; `action` = `recheck`\|`rerun`) | `caution` (no approval park) |
| `GET:/api/v1/runs/{run_id}` | Read a single run (carries `awaiting_decision_nodes` + per-node states — the authoritative terminality / gate-readiness source) | `safe` |
| `GET:/api/v1/runs` | List runs (set-shaped) | `safe` |

The request body of the write ops follows the add-on's run-launch schema; the
backplane forwards it verbatim as the `call_operation` params, and the add-on
validates it and rejects a body missing any field its schema requires.
`params` / `target_ref` are advanced per-node overrides.

**Run reads (#3699).** The two GETs let a governed launcher observe the run it
created through the same dispatch path, instead of inferring terminality from a
second gate-decision call (a write used as a read). They are read-class, so the
generic verb heuristic lands them `safe` — below the `caution` write floor — and
the connector-owned safety floor pins no key for them (it only pins the four
write ops; see [the pin](#safety-tiers-and-the-pin)). `GET:/api/v1/runs` is
set-shaped: a large run list rides the backplane's result-handle / JSONFlux
reducer path (v0.1-spec §4) automatically once the op is dispatchable — no agent
ever sees a raw multi-megabyte list. At ingest these two reads populate the
add-on's **Run Monitoring** operation group (LLM-proposed and operator-reviewed
against the live `/openapi.json`; the group exists but is empty until the reads
are allowlisted), so an agent discovers the read surface via
`list_operation_groups` → `search_operations`.

**`GET /api/v1/blueprints` (published-blueprint list) stays OUT of scope.**
#3699 flagged it as a lower-priority "consider" for input-contract discovery (a
launcher enumerating what it may launch). It is deliberately not allowlisted
here: the task's proven gap is *observing a launched run*, and blueprint-catalog
discovery is a separate concern that can be added under its own change if a
governed launcher needs it. Keeping it out holds the op set to the minimum the
lab proof exercised.

### Run-node control (resume vs skip)

The add-on exposes two run-node control routes for a **failed** node:
`POST /api/v1/runs/{run_id}/nodes/{node_id}/resume` (body
`{"action":"recheck"|"rerun"}`) and `POST .../skip`. Only **resume** is on this
connector's surface (#3707): a governed launcher that has observed a failed node
through the run-read GETs can nudge it — re-check or re-run — through the same
`call_operation` dispatch path instead of an out-of-band call. Resume is a
write (POST), so it rides the `caution` write floor with **`requires_approval=False`**
(a nudge on the add-on's own run, audited synchronously; the add-on's in-run
gate nodes remain the human control, the same operator decision as
launch/gate/validate).

The sibling **skip** route — which abandons a node — is a **human-only**
decision in the add-on and is deliberately kept **off** this surface: it is
added to **no** production file (allowlist, safety floor, or profile). Because
the `op_allowlist` keys on `(method, path)` only, the skip pair shares resume's
method but not its path, so it never matches the allowlist and is dropped before
persistence like every other non-allowlisted route — an agent cannot reach it
through `call_operation`. A negative test
(`test_shipped_mehoauto_allowlist_excludes_the_run_node_skip_op` in
`test_operations_ingest_catalog.py`) and a wide-spec-ingest drop assertion
(`test_connectors_meho_automation.py`) enforce that omission.

### Safety tiers and the pin

The four **write** routes are POSTs, so the generic ingest heuristic
classifies each `caution` by default (#3563). The connector-owned floor
(`meho_automation_safety_floor`) **pins** the decided tiers so a spec
re-ingest — or a future change to the verb heuristic — cannot silently drift
them:

- launch + gate → `caution`, **`requires_approval=False`** (no backplane
  approval park);
- validate → `caution`, **`requires_approval=False`**. Validate is a
  read-side dry-run that dispatches nothing, but an ingested POST never sits
  below the `caution` floor, so it rides `caution` too. `caution` executes
  immediately with no approval park, so the tier is operationally identical
  to a lower one here; pinning it `caution` keeps the decided tier the same
  whether or not the floor registration is in force at ingest time.
- run-node resume → `caution`, **`requires_approval=False`** (#3707). An agent
  nudge that re-checks or re-runs a failed node on the add-on's own run,
  audited synchronously; the add-on's in-run gate nodes remain the human
  control. The sibling human-only **skip** route is deliberately off this
  surface (see [Run-node control](#run-node-control-resume-vs-skip)), so it is
  neither allowlisted nor floor-pinned.

The two **run-read GETs** (#3699) are read-class: the same verb heuristic
lands them `safe`, below the `caution` write floor, so the floor pins **no**
key for them. A drift-guard test keeps this honest — the floor's pinned keys
must be a subset of the catalog allowlist (a pinned op must be persistable),
and the allowlist's only extra entries are exactly these two GET reads
(`test_operations_ingest_catalog.py`).

**Security note — launch executes without a backplane park.** Launch is a
potentially destructive lifecycle operation, but this connector does **not**
park it for a MEHO-side approval. That is a deliberate operator decision
(governed-launch design T-dec-4): the add-on's own **in-run gate nodes** are
the human control point, so a second backplane-side approval on the launch
call would be redundant. Enabling a launch-capable connector, and the first
live launch, remain operator-gated actions. The gate-decision op is exposed
as an ordinary `caution` op here (the add-on's gate model is the human
control); MEHO's own approval-decision verbs
(`meho_approvals_approve/reject`) are unrelated and remain human-only with no
MCP path.

## Auth — oauth2_mint with an external, per-target issuer

The add-on validates an inbound realm bearer whose issuer (the identity
provider) is a **different host** from the add-on API, so the profile uses
`oauth2_mint` with an external token endpoint (public #3571 gave `oauth2_mint`
its `token_url` / `scope` / `audience` fields). The backplane mints a
client-credentials token against the realm and presents it as `Bearer`.

The realm token endpoint is a **per-deployment value** (each deployment mints
against its own realm), so it is **not** hard-coded in the shipped, public
profile. Instead it is sourced **per-target from the Vault-backed target
credential** as a `token_url` field, alongside `client_id` / `client_secret`:

```
auth:
  scheme: oauth2_mint
  secret_fields: [client_id, client_secret, token_url]
  audience: meho-automation      # non-secret; the audience the add-on accepts
```

`token_url` is declared in `secret_fields`, so it is **mandatory in Vault**
(a fail-closed read error if the operator omits it). At dispatch the
`oauth2_mint` scheme resolves the endpoint via
`_oauth2_login_path_from_secret` (`connectors/_shared/profile_auth.py`) with
this precedence: credential `token_url` (validated fail-closed with the same
`_validate_token_url` SSRF rule the profile field clears — `https` for a
public host, plaintext `http` only for a cluster-internal / private issuer) →
profile `auth.token_url` → target-relative `/realms/master/...`. A credential
bundle without a `token_url` (e.g. keycloak) falls straight through to the
existing behaviour, so this override is invisible to every other
`oauth2_mint` user.

The Vault credential the target's `secret_ref` points at therefore holds:

```json
{
  "client_id": "...",
  "client_secret": "...",
  "token_url": "https://<realm-token-endpoint>/protocol/openid-connect/token"
}
```

All three fields are **mandatory in Vault**: the shipped `mehoauto/0.1.0`
profile lists `client_id`, `client_secret`, and `token_url` in its
`auth.secret_fields`, so a bundle missing any one fails closed at dispatch.
Land the confidential `client_secret` **server-side** rather than pasting it
through an operator's shell — the secret broker's Keycloak client-secret
source (#3621) moves a realm client's secret straight into the target's Vault
`secret_ref` (ref shape
`keycloak:<target>/<realm>/clients/<client-id>#secret`; see
[docs/codebase/connectors-secret-broker.md](connectors-secret-broker.md)),
so the value never transits an operator terminal or shell history.

## Registration recipe (operator, on a deployed backplane)

1. **Register the target.** The resolver matches a target to the versioned,
   profile-backed connector by `(product, version)`, and a profiled connector
   registers no wildcard fallback — so the target must carry a `version`
   before it can resolve (or dispatch, or even probe). `meho targets add`
   sets `--version` but has no `--extras` flag; the plain-`http` scheme lives
   in the `extras` column, and only `meho targets import` can write it.
   Register in two moves:

   ```
   # a) Create the target with its product + operator-asserted version.
   #    (No ingress, no TLS — the add-on Service is plain http in-cluster.)
   meho targets add automation-addon \
     --product mehoauto --version 0.1.0 \
     --host meho-automation --port 8000 \
     --secret-ref <vault-kv-path-holding-client_id/client_secret/token_url>
   ```

   ```yaml
   # b) automation-addon-scheme.yaml — carry the plain-http scheme in extras.
   #    The HTTP adapter builds the base URL as {scheme}://{host}[:port] from
   #    extras.scheme (default https), so an in-cluster Service with no TLS
   #    needs scheme=http. The add-on OpenAPI declares no `servers` block, so
   #    the connector derives the base URL from host / port / extras.scheme.
   #    `name`, `product`, and `host` are required on every import entry (even
   #    with --update); `product`/`host` must repeat the values set in (a).
   targets:
     - name: automation-addon
       product: mehoauto
       host: meho-automation
       extras:
         scheme: http
   ```

   ```
   # `targets import --update` is a sparse update that leaves the version set
   # in (a) intact (name/product are stripped as immutable; host is
   # re-asserted to the same value).
   meho targets import --update automation-addon-scheme.yaml
   ```

   Prerequisites, both about the backplane reaching the add-on Service: admit
   the add-on's host on the backplane's SSRF / outbound allowlist, and allow
   a NetworkPolicy path from the backplane pod to the add-on Service on the
   chosen port.

2. **Ingest from the add-on's OpenAPI document** via the `--spec` on-ramp —
   there is no vendored spec and no catalog upstream, so `--catalog` is not
   the path (the listing's `next_step` hint says so). A server-side `--spec`
   URL fetch is **https-only** (the SSRF guard rejects `http`, `file://`, and
   bare paths), and the add-on's in-cluster Service is plain-http and
   unreachable from an operator workstation — so do **not** point `--spec` at
   the add-on's live URL. Instead take the add-on's committed OpenAPI document
   (or run its export script) at the **deployed ref**, save it locally, and
   pass it as a `file://` source: the CLI reads a `file://` (or `docs:`)
   source client-side and uploads the bytes inline, so no local path or
   non-https scheme ever reaches the backplane.

   ```
   meho connector ingest \
     --product mehoauto --version 0.1.0 --impl mehoauto-rest \
     --spec file:///abs/path/to/mehoauto.openapi.json
   ```

   That OpenAPI document is the add-on's **full** API (~30 mutating routes),
   but the catalog op-allowlist (see below) drops every route except the
   six curated ops **before persistence** regardless of how wide the
   document is, so exactly six `EndpointDescriptor` rows land —
   `POST /api/v1/runs` (launch),
   `POST /api/v1/blueprints/{blueprint_id}/validate` (validate),
   `POST /api/v1/runs/{run_id}/gates/{node_id}/decision` (gate),
   `POST /api/v1/runs/{run_id}/nodes/{node_id}/resume` (run-node resume),
   `GET /api/v1/runs/{run_id}` (single-run read), and
   `GET /api/v1/runs` (run list) — **staged / disabled**
   (`is_enabled=false`, `source_kind=ingested`) with the tiers above (the
   four writes pinned `caution`, the two reads `safe`). The ingest result
   reports the dropped count + the dropped `(method, path)` list. Because the
   ~26 dropped mutating routes — including the human-only run-node `.../skip`
   sibling of resume — are never persisted and never staged, the
   `enable` in the next step cannot cascade `is_enabled=true` onto a tenant/run
   DELETE, a fleet import, or the skip route.

3. **Review the LLM-proposed op groups + per-group hints**, then **enable**
   the connector (staged → enabled) once the surface looks right. Enable is
   safe: only the six allowlisted ops exist to enable. The two run-read GETs
   land in the **Run Monitoring** group — confirm its when-to-use hint reads
   well before enabling.

   ```
   meho connector enable mehoauto-rest-0.1.0
   ```

4. **Dispatch** with the EXACT `connector_id` and `op_id` as **positional
   arguments** — `meho operation call <connector_id> <op_id>`; they are not
   flags:

   ```
   meho operation call mehoauto-rest-0.1.0 \
     'POST:/api/v1/blueprints/{blueprint_id}/validate' \
     --target automation-addon --params '{"blueprint_id": "...", "inputs": {...}}'
   ```

### Recipe notes (do not skip)

- **The execution profile's `token_url` MUST be set to the realm's token
  endpoint.** The add-on authenticates against an **external** issuer, and the
  backplane has **no safe default token endpoint** for an external issuer — so
  the `token_url` is carried per-target in the Vault credential (see the auth
  section above). A credential bundle without a `token_url` falls through to
  the profile's `auth.token_url` and then to a target-relative default, which
  is wrong for an external issuer and will fail auth. Populate `token_url` in
  the Vault credential with the realm's `.../protocol/openid-connect/token`
  URL.
- **The issuer host must be admitted by the SSRF target allowlist, and target
  TLS verification must stay on.** The `token_url` is validated fail-closed by
  the same `_validate_token_url` SSRF rule the profile field clears (`https`,
  host on the outbound allowlist). Admit the realm's issuer host on that
  allowlist; do **not** disable TLS verification on the target to work around
  a cert problem — fix the trust chain instead.

## Ingest op allowlist

The add-on's `/openapi.json` publishes its full API — ~30 mutating routes
across tenant / site / adoption / run (including DELETEs), fleet import, and
blueprint / environment / deployment CRUD. This connector's design invariant
is that its op set is **closed** to exactly six operations — four writes and
the two run-read GETs (#3699):

| op | `(method, path)` | tier |
|---|---|---|
| launch | `POST /api/v1/runs` | `caution` |
| validate | `POST /api/v1/blueprints/{blueprint_id}/validate` | `caution` |
| gate decision | `POST /api/v1/runs/{run_id}/gates/{node_id}/decision` | `caution` |
| run-node resume | `POST /api/v1/runs/{run_id}/nodes/{node_id}/resume` | `caution` |
| single-run read | `GET /api/v1/runs/{run_id}` | `safe` |
| run list | `GET /api/v1/runs` | `safe` |

The sibling `POST /api/v1/runs/{run_id}/nodes/{node_id}/skip` route is
**not** on this list — it is human-only in the add-on (see
[Run-node control](#run-node-control-resume-vs-skip)).

That closure is enforced in code, declared in data. The catalog row
(`operations/ingest/catalog.yaml`, product `mehoauto`) declares an
`op_allowlist` naming exactly those six `(method, path)` pairs. On the
ingest path — for both a first ingest and any re-ingest of a wider spec —
`operations/ingest/op_allowlist.py` drops every parsed operation outside the
allowlist **before persistence**: dropped ops are never written and never
staged, so `enable_connector`'s `is_enabled=true` cascade cannot reach them.
The ingest result carries the dropped count and the dropped `(method, path)`
list, and one structured log line (`ingest_op_allowlist_dropped`) records the
drop.

The allowlist `(method, path)` pairs are kept consistent with the
connector-owned safety floor's pinned keys
(`connectors/meho_automation/ingest_safety.py`) by a drift-guard test. The
floor pins only the four write ops (`caution`); the two GET reads land `safe`
naturally and need no floor entry. So the invariant the guard enforces is a
**subset** relation, not equality: every floor-pinned key must be on the
allowlist (a pinned op must be persistable), and the allowlist's only extra
entries are exactly the two run-read GETs. The allowlist is generic: any
catalog product **may** declare an `op_allowlist`; absence keeps the historical
behaviour (every parsed op is persisted).

## Boot guards

At boot the shipped row + profile clear, in order (`main.py` lifespan):

- `load_catalog()` — the row parses (`spec_resource: null`,
  `profile_resource` set, `catalog_ingest: spec-only`);
- `validate_catalog_registry_coverage()` — profile-backed rows are exempt
  from the class-presence + triple-registration checks;
- `validate_shipped_artifacts()` — dry-run-parses the profile
  (`ExecutionProfile.model_validate` + `validate_execution_profile`); there is
  no `spec_resource`, so nothing is parsed there and nothing is leaked;
- `stamp_catalog_profiled_connectors()` — synthesises + registers
  `ProfiledRestConnector_mehoauto_0_1_0` under the `(mehoauto, 0.1.0,
  mehoauto-rest)` v2 key, resolvable for a target of that fingerprint.

## References

- Catalog / profile mechanism: `docs/codebase/connector-auth-coverage.md`
  (auth catalog), the shipped `_fixture/1.0`, `vmware/9.0`, `sddc/9.0` rows.
- `oauth2_mint` external issuer: public #3571.
- Add-on pairing contract (identity / capability / event / audit planes):
  `docs/codebase/addon-contract.md`, `docs/codebase/addon-pairing.md`.
