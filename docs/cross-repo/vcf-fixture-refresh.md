# VCF management-plane fixture refresh recipe

The four VCF management-plane connectors — vROps (#829), vRLI (#830), Fleet
(#831), Automation (#832) — have no public CI simulator. Their E2E tests
(#837 / #838 / #839 / #840) replay HTTP responses recorded against a live
appliance via the refresh tool at `backend/tests/fixtures/vcf/refresh.py`.

This recipe documents how an operator re-captures fixtures when:

- a vendor API changes (response shape drift across appliance versions),
- a new endpoint is added to a connector's op set,
- a flaky fixture needs re-recording against a stable lab appliance.

The Automation connector skips this tool entirely — its dual-plane auth
shape is bespoke and its fixtures live in #840 alongside the connector.

## Prerequisites

- A reachable lab appliance for the target product (vROps / vRLI / Fleet)
  with a service-account that has read access to the endpoints the recipe
  records.
- The appliance hostname and port (default `443`).
- The repo dev env installed (`uv sync --locked --all-groups` in
  `backend/`).
- The service-account password available in an environment variable so it
  doesn't land in shell history.

## Run

From the repo root:

```bash
export VCF_PASSWORD='...'  # use a vault read, never the literal in shell history
uv run --directory backend python tests/fixtures/vcf/refresh.py \
    --connector vcf-operations \
    --target vrops-lab \
    --host vrops-lab.example.com \
    --username svc-meho \
    --password "$VCF_PASSWORD" \
    --insecure          # only on lab appliances with self-signed certs
```

The supported `--connector` values are `vcf-operations`, `vcf-logs`,
`vcf-fleet`. Each one has its own recipe (a tuple of endpoint calls + an
optional session-login path) inside `refresh.py`.

The tool:

1. Connects to `https://<host>:<port>` (TLS verification on by default;
   pass `--insecure` only against a lab appliance with a self-signed cert).
2. Establishes a session if any call in the recipe requires it (vRLI hits
   `POST /api/v2/sessions` with the `Local` provider; the helper extracts
   the `sessionId` response header).
3. Hits each recipe endpoint, capturing the HTTP method, path, status,
   response headers, and JSON body.
4. **Redacts** secret material — by default `Authorization`,
   `Set-Cookie`, `sessionId`, `X-XSRF-TOKEN`, `Cookie` headers and
   `password`, `session_token`, `sessionId`, `token`, `access_token`,
   `refresh_token` JSON keys. Add more via `--redact-header NAME` /
   `--redact-json-key KEY`.
5. Writes each fixture to
   `backend/tests/fixtures/vcf/<connector>/<fixture-name>.json`.

## Safety guards

- **Refuses to overwrite an existing fixture** unless `--force` is passed.
  Stale fixtures from a prior appliance version silently masking drift is
  one of the bigger anti-patterns in recorded-fixture suites — opt-in
  overwrite makes the operator confirm.
- **`--dry-run`** records nothing — prints the would-be writes so the
  operator can eyeball the recipe before touching disk.
- **Refuses TLS verification skip by default**. Pass `--insecure`
  explicitly when targeting a lab appliance with a self-signed cert.

## Inspecting a fixture before committing

```bash
jq . backend/tests/fixtures/vcf/vcf-operations/versions-current.json
```

Verify there are no leftover secrets. A correctly-redacted snapshot looks
like:

```json
{
  "fixture_name": "versions-current",
  "request_method": "GET",
  "request_path": "/suite-api/api/versions/current",
  "response_status": 200,
  "response_headers": {
    "content-type": "application/json",
    "set-cookie": "<redacted>"
  },
  "response_body": {
    "releaseName": "VMware Cloud Foundation Operations 9.0",
    "version": "9.0.0",
    "buildNumber": "12345678"
  },
  "recorded_at": "2026-05-21T10:00:00+00:00"
}
```

If a secret slips through (e.g. a vendor-specific header the default
denylist doesn't cover), add a `--redact-header` / `--redact-json-key`
entry, re-record with `--force`, and **submit a follow-up PR amending the
default lists** in `refresh.py` so the next operator doesn't have to
remember.

## Adding a new endpoint to a recipe

Recipes live in `backend/tests/fixtures/vcf/refresh.py` as `ConnectorRecipe`
constants. To add a new endpoint:

1. Append a `FixtureCall(fixture_name="...", method="GET", path="...", auth="basic")`
   to the connector's `calls` tuple.
2. Re-run the refresh tool with `--force` (the existing fixtures are
   overwritten as a set — pre-existing snapshots from prior runs are
   replaced atomically).
3. Commit both the recipe change and the new fixture JSON.

## Compatibility check

Before committing refreshed fixtures, run the E2E test suite against the
new snapshots:

```bash
uv run --directory backend pytest -n auto backend/tests/test_connectors_vcf_*_e2e.py
```

A connector that breaks under the new fixture set is the signal that the
vendor API shape changed — fix the connector (or roll the fixture back if
the appliance version isn't yet supported).

## Credential caveats

- Service accounts used for fixture refresh should be **read-only**. The
  recipes only issue `GET` / `POST /sessions`; no destructive verbs.
- Never commit a fixture with a real session token or password — the
  default redaction list covers the well-known fields, but a vendor-specific
  field may slip through. Always `jq .` over the committed JSON before
  pushing.
- The lab appliance the fixtures are recorded against should be on a
  stable supported version. Bleeding-edge releases trade fixture stability
  for vendor feature coverage and aren't worth re-recording weekly.

## References

- Task: https://github.com/evoila/meho/issues/841 (this tool).
- E2E consumers: vROps #837, vRLI #838, Fleet #839 (Automation #840 has
  its own dual-plane fixtures, separate path).
- Sibling pattern: `docs/cross-repo/nsx-onboarding.md` and
  `docs/cross-repo/sddc-manager-onboarding.md` cover the equivalent
  refresh story for the G3.5 connectors.
