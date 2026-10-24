# Connector: vro (VMware Orchestrator 8.x and the VCF 9 Operations orchestrator)

## Overview

The `vro` connector is the hand-written `HttpConnector` subclass for the
Orchestrator REST API (`/vco/api`). Orchestrator runs the workflows behind
Aria Automation and VCF Automation and many custom runbooks. When a
deployment fails in a workflow, the log of the failed run is in
Orchestrator. This connector lets an agent read it.

It is registered under `(product="vro", version="9.0", impl_id="vro-rest")`
and as the product wildcard, so a target with no version yet still
resolves to it. The `>=8.0,<10.0` range keeps Orchestrator 8.x (on its own
or inside Aria Automation 8.x) and the VCF 9 Operations orchestrator on one
class: both serve the same `/vco/api` paths.

Today the connector ships the login, version detection and seven reads.
Changes (run a workflow, cancel a run, and more) come in a later Task.

Package: `backend/src/meho_backplane/connectors/vro/`.

| Operation | What it does | Vendor call | Risk | Versions |
|---|---|---|---|---|
| `vro.about` | Version, build, build date, API version, server id, time zone and features. MEHO uses the same call to detect the version. | `GET /vco/api/about` | read (`safe`, no approval) | 8.18, 9.x |
| `vro.health` | Server state, overall health and one row per service check. | `GET /vco/api/healthstatus?showDetails=true` | read (`safe`, no approval) | 8.18, 9.x |
| `vro.workflow.list` | Workflows, by part of the name (`name~`) or by category name (`categoryName=`). One flat row per workflow. | `GET /vco/api/workflows` | read (`safe`, no approval) | 8.18, 9.x |
| `vro.workflow.get` | One workflow with its declared inputs and outputs (name, type, description). No default values. | `GET /vco/api/workflows/{workflowId}`, `GET /vco/api/workflows/{workflowId}/content` | read (`safe`, no approval) | 8.18, 9.x |
| `vro.execution.list` | The runs (executions) of one workflow: id, state, start, end, user. One flat row per run. | `GET /vco/api/workflows/{workflowId}/executions` | read (`safe`, no approval) | 8.18, 9.x |
| `vro.execution.get` | One run: state, dates, user, error text, current step, inputs and outputs. Secret values come back replaced. | `GET /vco/api/workflows/{workflowId}/executions/{executionId}` | read (`safe`, no approval) | 8.18, 9.x |
| `vro.execution.logs` | The log lines of one run, at most `max_lines` (default 200, at most 2000); each text cut at 2000 characters. | `GET /vco/api/workflows/{workflowId}/executions/{executionId}/syslogs` | read (`safe`, no approval) | 8.18, 9.x |

All seven reads exist on every covered version, so none needs a version
check. If a later operation exists only on some versions, the Task that
adds it adds the check.

**Why `vro.execution.*` and not `vro.run.*`.** The Task proposed
`vro.run.list`, `vro.run.get` and `vro.run.logs`. "run" is a write word
for the operation-contract check (`write_name` rule), so a read named
`vro.run.*` would fail it. The vendor calls a run an "execution", so the
reads use that word. A change that runs a workflow can still be called
`vro.workflow.run`.

## Registering targets

```bash
# Orchestrator with vSphere single sign-on (8.x on its own, or VCF 9):
meho targets add <orchestrator-name> --product vro --host <orchestrator-address> \
  --secret-ref <vault-path-of-the-login>

# Orchestrator inside Aria Automation 8.x:
meho targets add <orchestrator-name> --product vro --host <automation-address> \
  --secret-ref <vault-path-of-the-login> --extra login=automation
```

The Vault secret holds `username` and `password`. Only
`auth_model=shared_service_account` (or unset) is accepted; other modes
raise `NotImplementedError`.

## Key types

- `VroConnector` (`connector.py`) -- the connector class. Caches one
  `Authorization` value per target and tenant (`target_cache_key`).
- `VroTargetLike`, `VroCredentialsLoader`, `login_mode` (`session.py`) --
  the target shape, the credential loader type and the login mode check.
- `VroServerVersion`, `parse_server_version` (`connector.py`) -- the
  version read.
- `VroTypedOp` and the group keys (`op_types.py`), the op metadata
  (`read_ops.py`) and the registrar (`typed_ops.py`).
- `typed_reads.py` -- the read bodies, `flat_rows`, `redact_secrets` and
  `VENDOR_CALLS` (every vendor call, for the reconcile lane).

## Control flow

### Login

The login mode comes from the target's `extras["login"]`:

- `basic` (default): HTTP Basic on every call. This is the login of an
  Orchestrator that uses vSphere single sign-on. The vendor reference
  says Basic over single sign-on works only when
  `com.vmware.o11n.sso.basic-authentication.enabled = true` is set on the
  Orchestrator.
- `automation`: the Aria Automation token. The connector runs the Aria
  Automation login on the same address (`POST /csp/gateway/am/api/login`,
  then `POST /iaas/api/login`, the same code as the `vcfa-vra8`
  connector, including the optional `extras["domain"]`) and sends
  `Authorization: Bearer <token>`.
- Any other value stops the call before any request, with an error that
  names the allowed values.

The `Authorization` value is cached per target and tenant. Only a caller
with an operator JWT, other than the synthesised system operator, reads or
fills the cache. Any other caller (a probe, a background job) runs the
credential loader, which checks it: the Vault loader fails closed without
a valid JWT. Its login is not kept, so a login a real operator primed is
never served to a system caller. A `401` reaches the dispatcher, which
calls `invalidate_session(target)` (drops only this target's value) and
runs the call once more; the connector logs in again.

Every call sends `Accept: application/json`. The API answers XML when no
`Accept` header is sent.

### Version

`server_version(target, operator)` reads `GET /vco/api/about` and takes
`version` and `build-number`. It never raises: a failure is named in
`error`. A `401` on this read drops this target's cached login.
`fingerprint()` and `probe()` use it.

### Flat rows

The Orchestrator answers a list as `link[]` objects whose fields sit in
an `attributes` list of `{name, value}` pairs. `flat_rows` turns each link
into one row `{<name>: <value>, ..., href}`. The list reads return
`{items, total, start_index, count}` (logs: `{items, lines, more_lines}`).
MEHO stores a long list behind a result handle; the agent reads it with
`result_query`.

### Secrets

- `vro.workflow.get` returns no default values and no attribute values:
  they can hold secrets and are not needed to call a workflow.
- `vro.execution.get` replaces the value of a parameter whose type is
  `SecureString`, whose `encrypt-value` flag is set, or whose name says
  password, secret, token, API key, secret key, private key or licence
  key (for example `apiKey`, `secretKey`, `adminPassword`), with
  `***REDACTED***`.
- Every answer, and every vendor answer the flight recorder stores
  (`trace_response_body`), has secret-named fields replaced at any depth.
- The same goes for `{key, value}` entries, the shape a `Properties`
  value uses: when the key is a secret name, the value is replaced, at
  any depth. Entries shaped `{name, value}` are handled the same way.
- `vro.execution.logs` cuts a text at 2000 characters. The shared secret
  rules (the ones the dispatcher runs on every answer) run on the whole
  text first. They need the label and the whole value; a cut could leave
  a fragment too short for them to find later.

### Shutdown

`aclose()` clears the login cache and closes the HTTP pool.

## Dependencies

- `HttpConnector` (`connectors/adapters/http.py`): the pooled per-target
  client, retries, TLS trust, SSRF guard, flight-recorder spans.
- `connectors/_shared/vault_creds.py`: the operator-context credential read.
- `connectors/_shared/vcf_auth.py`: `basic_auth_header`,
  `is_acceptable_auth_model`.
- `connectors/vra8/_auth.py`: `vra8_login`, the Aria Automation login.
- `operations/typed_register.py`: the typed-op registrar.

## Tests

- `backend/tests/test_connectors_vro_typed_reads.py` -- both logins, a
  `401` that logs in again, an unknown login mode, every read through
  `dispatch()`, secret values replaced, line and text caps, a vendor
  error in the answer, ids that must stay one path segment, a long list
  behind a result handle, registration shape, version resolution and
  fingerprint.
- `backend/tests/test_connectors_vro_spec_reconcile.py` -- every path in
  `VENDOR_CALLS` against the pinned reference lists of Orchestrator 9.0
  and 9.1 (`vcf-operations-orchestrator-9.0/` and
  `vcf-operations-orchestrator-9.1/` on the spec shelf). The vendor
  publishes no API file, so the shelf pins a list made from the vendor's
  reference pages. Placeholder names are compared alike (`{id}` and
  `{workflowId}`), and the 9.1 list, whose pages leave out `/vco/api`, is
  read with it put in front.

## Known issues

- No reference exists for Orchestrator 8.18 or 9.1.1, so the reconcile
  lane checks the paths against 9.0 and 9.1 only. A lab proof checks every
  read on 8.18, 9.0, 9.1 and 9.1.1.
- The field names inside a list row (`categoryName`, `startDate`,
  `startedBy`, ...) come from the server's `attributes` list. The vendor
  reference shows only the `{name, value}` shape, not the names, so the
  response schema lists the usual names and the rows keep whatever the
  server sends.
- The `name` and `category` filters of `vro.workflow.list` use the
  vendor's `conditions` query (`name~<text>`, `categoryName=<name>`).
  The run and log reads send no conditions; filter their rows with
  `result_query`.

## References

- Task #4515 (this connector); #4516 (the changes: run a workflow, cancel
  a run, actions, configuration elements and packages).
- `docs/codebase/connectors-vra8.md` -- the Aria Automation 8 connector
  whose login the `automation` mode uses.
- `docs/decisions/spec-reconcile-guards-standard.md` -- the reconcile lane.
