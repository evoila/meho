# MEHO threat model

This file tells a security scanner (and any security researcher) what
MEHO is, where untrusted input comes in, which security promises must
always hold, and how we rate what you find.

Report problems privately to **security@meho.ai**. Our disclosure
process is in [`SECURITY.md`](../SECURITY.md). Please do not open public
GitHub issues for security problems.

Paths below are under `backend/src/meho_backplane/` unless they start
with `backend/`, `cli/`, `clients/` or `deploy/`.

## What MEHO does

MEHO is a governance backplane: one shared control layer between AI
agents, human operators and large vendor APIs.

- People and AI agents use MEHO to work with systems such as vCenter,
  NSX, SDDC Manager, Kubernetes, Vault, cloud APIs, databases, and Linux
  or Windows hosts over SSH.
- They use a small, fixed set of MCP tools and `meho` CLI commands. A
  vendor operation is data (an `op_id` passed to `call_operation`), not
  its own tool.
- Every vendor operation goes through one shared path in
  `operations/dispatcher.py`: login, permission check, approval when
  needed, credential lookup, the vendor call, redaction, result
  reduction (MEHO shrinks a large result and stores it), audit and
  broadcast. The REST API, the MCP endpoint, the CLI and the operator
  web console all use this path for operations.
- MEHO runs on Kubernetes (Helm chart in `deploy/charts/meho/`). It uses
  PostgreSQL with pgvector, Valkey, Keycloak for login (OIDC), and Vault
  or GCP Secret Manager for credentials.

### Who talks to MEHO

| Caller | How it logs in |
|---|---|
| A person, or an MCP client such as Claude Code that the person signs in (console, CLI, MCP) | The person's Keycloak login (`principal_kind=user`) |
| An agent principal (for example MEHO's own agent runtime on a schedule or event, or an external agent with its own identity) | Keycloak client credentials (`principal_kind=agent`) |
| A service account or paired add-on | Keycloak client credentials (`principal_kind=service`) |
| A satellite runner (remote executor in another network) | Keycloak client credentials (`principal_kind=runner`) |
| A webhook sender (Alertmanager, Grafana, and others) | No token. A per-source signature or shared secret |

### Attackers we care about

1. Someone on the network with **no valid login**.
2. A **logged-in caller with low rights**: a person with role
   `read_only` or `operator`, or any agent principal. This is the main
   case. Assume an AI agent can be hostile, for example after a prompt
   injection. MEHO's promises must hold even then.
3. A **hostile vendor API, host or API spec** that MEHO reads. Its
   content is untrusted.
4. A **caller in one tenant** who tries to reach another tenant's data.

Not in the model: an admin who uses admin rights as designed, and
anyone who already controls Vault, Keycloak, the database, or the
Kubernetes cluster.

## Where untrusted input enters

1. **MCP endpoint** (`POST /mcp`): `mcp/server.py`, `mcp/handlers.py`,
   tools in `mcp/tools/`, resources in `mcp/resources/`.
   - Token check: `mcp/auth.py` (MCP audience only; runner tokens are
     refused).
   - Body limit 1 MiB. Tool arguments are checked against each tool's
     JSON schema before the tool runs.
2. **REST API** (`/api/v1/...`): routers in `api/v1/`, app and
   middleware in `main.py` and `middleware.py`.
   - Token check: `verify_jwt_and_bind` → `auth/jwt.py`. Role checks:
     `require_role` and `require_human_principal` in `auth/rbac.py`.
3. **Open endpoints (no login)**: `/`, `/healthz`, `/ready`, `/version`,
   `/metrics` (`metrics_access.py`), `/api/v1/auth-config`,
   `/.well-known/oauth-protected-resource` (`api/well_known.py`), the
   API description (`/docs`, `/redoc`, `/openapi.json`), and the console
   login and static files.
4. **Webhooks**: `POST /api/v1/events/ingest/{source_slug}`
   (`api/v1/events_ingest.py`, `events/ingest/`).
   - No token. Each source checks an HMAC-SHA256 signature (with an
     optional replay window), a static header token, or HTTP Basic,
     using a secret from Vault. Comparisons are constant-time. Body
     limit 256 KiB.
   - Event bodies then go to vendor normalizers in `events/normalizers/`
     and can start agent runs (`events/matcher.py`).
5. **Operator web console** (`/ui`): `ui/routes/`, login in `ui/auth/`.
   - OIDC login with PKCE. The session cookie is HttpOnly, Secure and
     SameSite=Strict. Tokens are stored encrypted on the server
     (`ui/auth/session_store.py`).
   - CSRF tokens (`ui/csrf.py`), safe login redirects, anti-framing
     headers (`ui/security_headers.py`), Jinja autoescape
     (`ui/templating.py`).
   - Uploads: target YAML import (`ui/routes/connectors/import_router.py`),
     topology bulk import (`ui/routes/topology/batch_parse.py`) and
     knowledge-base files (`ui/routes/kb/routes.py`).
6. **API specs that become connectors** (OpenAPI 3.0 and 3.1 only):
   `operations/ingest/openapi.py`, `operations/ingest/refs.py`.
   - Size limit 20 MiB. A remote spec is fetched over https only, with
     the SSRF check, a pinned address (MEHO connects only to the
     address it checked) and re-checked redirects.
   - Safe YAML loading, a check against the OpenAPI metaschema, and
     local `$ref` only (`#/components/...`).
   - Spec text is also sent to an LLM to group operations
     (`operations/ingest/llm_groups.py`).
7. **Vendor API responses and host output**: back through the HTTP and
   SSH adapters (`connectors/adapters/http.py`, `connectors/adapters/ssh.py`),
   redaction (`redaction/`), and result reduction
   (`operations/jsonflux_reducer.py`).
   - The HTTP adapter limits response size, follows only same-origin
     redirects, and checks TLS (or a pinned CA).
   - Connectors that parse XML (for example `connectors/vmware_rest/soap.py`,
     `connectors/pfsense/`) use `defusedxml`.
8. **Knowledge, memory and docs**: `mcp/tools/knowledge.py`,
   `mcp/tools/memory.py`, `api/v1/kb.py`, `api/v1/memory.py`,
   `kb/`, `memory/`.
   - Bulk knowledge import reads only under `KB_INGEST_ROOT`
     (`kb/service.py`, `kb/file_walker.py`).
   - Docs search (`mcp/tools/docs.py`) forwards to an external docs
     service over https (`auth/corpus.py`); MEHO does not store those
     documents.
9. **Satellite runner and gateway**: `runner/`, `gateway/`,
   `api/v1/gateway.py`, `api/v1/checks.py`. The runner calls the central
   instance; work items flow back to it (`runner/executor.py`).
10. **Add-on pairing and service accounts**: `api/v1/addon_pairing.py`,
    `operations/addon_pairing.py`, `api/v1/service_grants.py`.
11. **Operation parameters that reach commands, paths, URLs or queries.**
    All parameters are first checked against the operation's JSON schema
    (`validate_params` in `operations/_validate.py`). Then:
    - Linux shell commands over SSH: values are quoted with
      `shlex.quote`; file content goes as base64; some values must match
      a character allowlist (`connectors/linux/ops_write.py`).
    - Linux file paths: reads and writes are limited to fixed root
      folders (`confine_read_path`, `confine_write_path` in
      `connectors/linux/`).
    - PowerShell runs over SSH with `-EncodedCommand`, and values are
      single-quoted (`connectors/_shared/pwsh.py`). Used by the Windows,
      Active Directory, DNS, failover cluster and Hyper-V connectors.
    - URL paths: path parameters are percent-encoded, and dot segments
      (`.` or `..` in a path) are refused (`_substitute_path` in
      `operations/_branches.py`).
    - SQL and database commands: Postgres sessions are read-only
      (`connectors/postgres/session.py`); MSSQL checks identifiers and
      binds parameters (`connectors/mssql/session.py`); MongoDB allows a
      fixed list of commands (`connectors/mongodb/session.py`).
    - Network probes (`connectors/net/`) are denied unless an allowlist
      permits the destination.
12. **MEHO's own agent runtime**: `agent/run.py`, `agent/invocation.py`,
    tools in `agent/toolset.py`. Its tool calls go through the same
    dispatch path and policy gate. Model calls use Pydantic AI.
13. **Go CLI** (`cli/`): device-code login (`cli/internal/auth/`),
    tokens in the OS keyring or a `0600` file (`cli/internal/auth/store.go`).
    The backplane URL must be https, except loopback with an explicit
    flag (`cli/internal/backplane/backplane.go`).

## Security promises that must always hold

These are the controls the code enforces. Breaking one is a real
finding.

1. **Login is required.** Every route needs a valid Keycloak token or
   console session, except the open endpoints and the webhook endpoint
   listed above.
   - Tokens: RS256 only; issuer, audience, expiry and `sub` are checked
     (`auth/jwt.py`). `tenant_id` and `tenant_role` come only from the
     signed token.
   - The MCP endpoint accepts only tokens that carry its own audience
     (`mcp/auth.py`). Runner tokens only reach the runner gateway and
     check paths (`RUNNER_ALLOWED_PATH_PREFIXES` in `middleware.py`).
2. **Roles and grants decide what a caller can do.**
   - Roles: `read_only`, `operator`, `tenant_admin` (`auth/operator.py`,
     `auth/rbac.py`). `call_operation` needs at least `operator`.
   - Every operation goes through the policy gate (`policy_gate` in
     `operations/_validate.py`).
   - Agent principals have default rules by risk level
     (`auth/permissions.py`): safe runs; caution waits for approval;
     dangerous is refused unless a grant lets it wait for approval;
     destructive is always refused. No grant can let a caution or
     dangerous operation run without approval.
   - On MCP, the operator tools are listed and callable only in a
     session that holds the `mcp:admin` scope (`mcp/registry.py`,
     `mcp/handlers.py`).
3. **Risky operations wait until a second person approves.**
   - Operations wait until a second person approves, according to
     their risk level and the tenant's policy (`policy_gate` in
     `operations/_validate.py`, `operations/approval_queue.py`).
   - A destructive operation always waits for a second person, and no
     standing grant can skip that. It also needs the hash of an earlier
     preview. (Agent principals cannot request one at all, see above.)
   - The person who asked cannot approve their own request
     (`_check_self_approval`). Self-approval is off by default.
   - Only a human principal can approve or reject. Agent, service and
     runner principals cannot.
   - Approve, reject and grant-elevate have **no MCP path at all**.
     `tools/call` refuses them before the tool lookup
     (`mcp/human_only.py`).
   - An approval runs the exact parameters that were approved (a hash is
     checked), runs only once, and an expired request cannot be
     approved.
4. **Audit comes first and cannot be changed.**
   - For any operation that changes something, MEHO does not report
     success unless its audit row is written (`operations/_audit.py`).
     MCP calls and approval decisions are audited the same way
     (`mcp/audit.py`, `operations/approval_queue.py`).
   - On PostgreSQL, a database trigger rejects UPDATE and DELETE on the
     `audit_log` table (`backend/alembic/versions/0100_*`). MEHO's only
     change to the table is the scheduled clearing of the raw payload
     column after its retention period (`0103_*`, `audit_retention.py`).
5. **Target credentials stay inside MEHO.**
   - A target stores only a reference to its secret (`secret_ref`).
     MEHO reads the secret at call time
     (`connectors/_shared/vault_creds.py`, `gsm_creds.py`) and uses it
     to connect. The secret is not part of the result.
   - A target's `secret_ref`, and every `vault.kv.*` path, must stay
     under the caller's tenant prefix (`api/v1/targets.py`,
     `connectors/vault/tenant_scope.py`).
   - Results, broadcasts and stored traces go through redaction
     (`redaction/`, `broadcast/events.py`).
6. **Stored results belong to the caller.** A large result comes back
   as a link to a stored result. Only the same user in the same tenant
   can read it; for anyone else it looks like it does not exist
   (`connectors/result_handle_store.py`, `operations/result_query.py`).
   Queries on it use bound parameters, and the query engine has file
   and network access turned off (`jsonflux/query/`).
7. **Tenants are separated.** Every query filters on the tenant from
   the token. Only a `platform_admin` can act across tenants
   (`authorize_tenant_scope` in `auth/rbac.py`). Broadcast streams,
   stored results and Vault paths all include the tenant.
8. **SSH checks the host key.** An SSH connection is refused before it
   opens when the target's secret has no `known_hosts` pin
   (`connectors/adapters/ssh.py`). All SSH-based connectors use this.
9. **Targets cannot point at internal addresses.** A target host must
   be a public address unless an operator allowlists ranges
   (`MEHO_TARGET_SSRF_ALLOWLIST`). HTTP connections dial only an address
   that passed the check, so a DNS change between check and connect
   does not help (`targets/ssrf_guard.py`,
   `connectors/_shared/pinned_transport.py`).
10. **Satellite runners run only approved work.** A runner is bound to
    its own name and can reach only its gateway and check paths, never
    `/mcp`. It
    runs safe reads. It runs a caution-level write only when the write is
    signed by the central instance, on the runner's allowlist, and
    approved. It never runs dangerous or destructive operations
    (`runner/executor.py`, `runner/work_item_signing.py`,
    `auth/runner_guard.py`).
11. **Stored text is labelled as untrusted.** Knowledge and memory
    bodies, broadcast text, docs text, webhook event bodies and check
    evidence are wrapped in a labelled envelope when they are served to
    an agent (`untrusted_text.py`).

## Components that matter most and least

Most important:

- The shared call path: `operations/dispatcher.py`,
  `operations/_validate.py`, `operations/approval_queue.py`,
  `operations/_audit.py`.
- Login and permissions: `auth/`, `middleware.py`, `mcp/auth.py`,
  `mcp/handlers.py`, `mcp/human_only.py`, `ui/auth/`, `ui/csrf.py`.
- Credentials and redaction: `connectors/_shared/` (credential loading,
  pinned transport, PowerShell quoting), `connectors/vault/`,
  `redaction/`.
- Code that turns parameters into commands, paths, URLs or queries:
  `connectors/adapters/`, `connectors/linux/`, the Windows connectors,
  and the database connectors' `session.py` files.
- Untrusted-input parsers: `operations/ingest/`, `events/ingest/`,
  `events/normalizers/`, `jsonflux/`.
- Stored results: `connectors/result_handle_store.py`,
  `operations/result_query.py`.
- The satellite runner: `runner/`, `gateway/`, `auth/runner_guard.py`.

Less important, but in scope:

- The individual vendor connectors in `connectors/<vendor>/` are about
  40 % of the code. Their read operations matter less than the shared
  code above. Their write and delete operations, and any place where
  they build a command, path, URL or query from a parameter, matter
  more.
- Topology, retrieval, scheduler, checks and broadcast feeds.
- The Go CLI (`cli/`) runs on the user's machine. Token storage and TLS
  handling matter there.
- `clients/` holds the Claude Code plugin (an `mcp-remote` launcher,
  hook scripts and skills) and the Claude Desktop extension. Both run
  on the user's machine and start the third-party `mcp-remote` package.
- The Helm chart (`deploy/charts/meho/`): pod security settings,
  network policy, and how secrets reach the pods.

## How to exercise it

The image has everything needed to work offline:

- `/src` is the repository checkout.
- `/src/backend/.venv` holds the backend and all test tools. It is on
  `PATH`, so `python` and `pytest` work directly.
- Go 1.26 (`/usr/local/go`), the module cache, and the CLI binary with
  debug info at `/src/cli/bin/meho`.
- `helm`, for rendering `deploy/charts/meho/`.

There is no PostgreSQL, Valkey, Keycloak or Vault in the image, and no
LLM. Use the in-process test setup instead:

- The unit tests start the real FastAPI app in-process on SQLite, with
  fake OIDC keys. See `backend/tests/conftest.py` and
  `backend/tests/_oidc_jwt_helpers.py` (`mint_token`,
  `mock_discovery_and_jwks`).
- Vendor APIs are faked with `respx` (HTTP) and small fakes in
  `backend/tests/` (for example `_vault_fakes.py`, `_ssh_vault_stub.py`).

Commands:

```bash
# One backend test file
cd /src/backend && python -m pytest tests/test_auth_jwt.py

# The whole backend unit lane (the same files CI runs), in three
# slices like CI, to keep memory low. About 20 minutes on 2 CPUs.
cd /src/backend
for s in 1 2 3; do
  python -m pytest -n 2 --dist loadscope $(find tests -name 'test_*.py' \
    -not -path 'tests/integration/*' -not -path 'tests/migrations/*' \
    | sort | awk -v s="$s" 'NR % 3 == s - 1')
done

# Go CLI
cd /src/cli && go test ./...
```

- Tests that need Docker (PostgreSQL or Valkey containers) skip
  themselves. The unit lane leaves out `backend/tests/integration/`
  and `backend/tests/migrations/`. CI runs these folders in separate
  jobs, and many of their tests need Docker.
- The tests here use SQLite. The audit trigger exists only on
  PostgreSQL, so tests on SQLite cannot check it. Do not report a
  missing audit trigger on SQLite as a finding.
- A few tests fail offline because they download the embedding model
  from the internet (for example `tests/test_checks_investigate.py`).
  You can ignore those failures.
- To render the chart, pass the same `--set` values as the "Helm
  template" step in `.github/workflows/chart.yml`.

## How we rate severity

**Critical**

- Running code or commands on MEHO, a satellite runner, or a target
  host without a valid login.
- Running an operation that must wait for approval without a second
  person's approval, or changing what runs after approval.
- Approving, rejecting or elevating a grant through MCP, or as an
  agent, service or runner principal.
- A caller seeing a credential that MEHO uses to connect to a target
  (password, token, key, kubeconfig) in a result, error, broadcast,
  stored result or log.
- Acting as another user, agent or tenant (token forgery or a token
  check bypass).
- Reading or changing another tenant's data.
- A change that reports success without its audit row, or changing or
  deleting audit rows through MEHO.

**High**

- A logged-in low-rights caller causes command injection (shell or
  PowerShell), path traversal, SQL injection, or SSRF to an address the
  guard should block.
- A logged-in caller gets past their role, grants or the `mcp:admin`
  scope (privilege escalation inside one tenant).
- Reading another user's stored result in the same tenant.
- An SSH connection to a host without a matching pinned host key.
- The webhook endpoint accepting a request without a valid per-source
  signature or secret.
- Stored cross-site scripting in the operator console that a
  low-rights caller can plant.
- A hostile API spec, vendor response or webhook body that leads to
  code execution, file access, or requests to internal addresses.

**Medium**

- Denial of service that one caller can trigger with small requests and
  that takes down the whole backplane.
- Stored text that escapes its untrusted-text envelope.
- Leaks of data that is not secret and stays within one tenant.

**Low or out of scope**

- Anything that needs a human admin (`tenant_admin` or
  `platform_admin` acting as designed), or control of Vault, Keycloak,
  the database or the cluster.
- A prompt injection that only makes an agent misuse its own rights,
  without breaking a promise above.
- Missing hardening (headers, version strings) without a working
  attack.

## Out of scope and things to leave alone

- Third-party MCP clients (Claude Code, Cursor, Cline, and others) and
  customer-run services next to MEHO (Vault, Keycloak, PostgreSQL,
  Valkey, the vendor systems). This follows `SECURITY.md`.
- `backend/dev/`: a local developer server with a `/dev/login`
  shortcut. It is outside the package and is never shipped or deployed.
- `api/v1/rbac_test.py`: a test route that is mounted only when
  `MEHO_ENABLE_RBAC_TEST_ROUTE` is set. It is off by default.
- Tests and test helpers (`backend/tests/`, `cli/**/*_test.go`,
  `clients/*/test/`), `examples/`, `docs/`, `docs-site/`, and the
  scripts in `scripts/` and `backend/scripts/` (CI and maintainer
  tools, not part of the product).
- Settings an operator turns on by choice, such as the SSRF allowlist,
  the self-approval break-glass setting, `known_hosts_insecure`,
  `verify_tls: false` or `extras.scheme: http` on a target, or an empty
  `VAULT_KV_TENANT_SCOPE_PREFIX`.
- Third-party dependencies, unless MEHO uses them in an unsafe way.
  Known CVEs in dependencies are tracked separately.

## How we want reports

- One problem per report. Name the promise above that breaks, the
  rights the attacker starts with (no login, agent principal,
  `read_only`, `operator`, admin), and the impact.
- Add a reproducer that runs offline in this image:
  - Backend: a pytest test under `backend/tests/` that uses the
    in-process app and the helpers above.
  - CLI: a Go test under `cli/`.
- Add a fix as a `git diff` against `main`, with a test that fails
  before the fix and passes after it.
- Send it to security@meho.ai. We follow the coordinated disclosure
  steps in `SECURITY.md` and publish advisories as GitHub security
  advisories on this repository.
