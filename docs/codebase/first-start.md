# First-start checks

## Overview

`GET /api/v1/first-start` (`backend/src/meho_backplane/first_start.py`)
returns a red/green list for a new install: DNS name, certificate, network,
clock, reachability of the targets. It is for people: it needs a person's
login (`is_human_principal`, so not an agent, service or runner login) with
the `tenant_admin` role or `platform_admin` (`may_see_first_start`). Every
other login gets 403 `ADMIN_PERSON_REQUIRED` before any check runs.

`GET /first-start` needs no login. It answers the fixed `SIGN_IN_ANSWER`:
one `skipped` line named `sign-in` that says how to sign in, with `ok`
false, so a script that reads `ok` never sees green without a check. It
runs no check, reads no target and contacts nothing, so a caller without a login
learns nothing about the install.
User docs: `docs-site/install/first-start-checks.md`.

## Key types

- `FirstStartLine` (`name`, `status` = `ok` | `fail` | `warn` | `skipped`, `message`)
  and `FirstStartResponse` (`ok`, `checks`).
- `first_start_list(operator)` is what both surfaces call. It runs the list
  at most once at a time per key (the tenant whose targets are checked and
  the caller's tenant): calls that arrive during a run wait on an
  `asyncio.Lock` and get its result, and a finished list is kept for
  `_RESULT_HOLD_S` (10) seconds, as `/ready` keeps its snapshot.
  `clear_first_start_cache()` drops the kept lists (tests).
- `run_first_start_checks(operator, tenant_id=...)` runs the five checks;
  `check_dns`, `check_certificate`, `check_network`, `check_clock`,
  `check_reachability` are one each.
- `reachability_scope(operator)` picks the tenant whose targets the list
  checks: `None` (all tenants) for a platform admin who is a person
  (`platform_admin` and `is_human_principal`), the operator's own tenant
  for everyone else, machine logins too.

## Control flow

1. `check_dns` resolves the host of `BACKPLANE_URL`.
2. `check_certificate` (skipped on DNS fail or plain http) opens TLS to
   `BACKPLANE_URL` with full verification; the `SSLCertVerificationError`
   verify code picks the fix sentence; then the end date is checked
   (`CERT_MIN_DAYS_LEFT` = 30).
3. `check_network` (skipped when the DNS line is red) resolves the host again
   and gives a `warn` line when any address is global (`ipaddress`
   `is_global`). Private, shared (100.64.0.0/10), loopback and link-local
   addresses give `ok`. A `warn` line never makes the overall `ok` false.
4. `check_clock` compares `now()` with the `Date` header of the Keycloak
   discovery answer (a successful answer only; an error page from a proxy
   does not count) and with PostgreSQL `now()` (`CLOCK_MAX_SKEW_S` = 60).
   Each reader returns a time or a `NoClockReading` that holds the message.
   A source that cannot be read is red, not green, and the message names the
   cause: no answer (`httpx.TransportError`), an answer MEHO cannot read, or
   the HTTP status of an error answer. The Keycloak fix names the setting in
   use: `KEYCLOAK_DISCOVERY_URL` when it is set, else `KEYCLOAK_ISSUER_URL`.
   A successful answer with no readable `Date` header is a `NoClockReading`
   with `red=False`: Keycloak is left out, the line compares with PostgreSQL
   only, and the message says why. The Keycloak of the built-in core sends no
   `Date` header. A `Date` with the zone `-0000` is read as UTC.
5. `check_reachability` calls `resolve_connector_or_label` and
   `connector.fingerprint(target, operator=operator)` for every live target.
   It stores nothing (unlike `probe_and_store_fingerprint`). An
   `asyncio.Semaphore` lets at most `_MAX_PROBES_AT_ONCE` (10) targets be
   contacted at the same time. The probes stop after
   `_REACHABILITY_BUDGET_S` (15 seconds); a target not checked by then is
   named as not checked, and the line is red. So the whole list answers
   within the 45 seconds that `meho status --first-start` waits. Both surfaces
   pass the signed-in person. `_login_needs_person` decides which targets
   with a `secret_ref` are left out. A login in Google Secret Manager
   (`gsm:`, or no scheme when `CREDENTIAL_BACKEND=gsm`) is read with
   MEHO's own Google identity, or with the caller's token when
   `GSM_WIF_AUDIENCE` is set (Workload Identity Federation, see
   `connectors/_shared/gsm_creds.py`). Neither depends on the tenant, so
   the target is contacted in every tenant. Any
   other login (Vault) is read only when the caller is a person (not a
   machine login, see `is_human_principal`) signed in to the target's
   tenant: the Vault role follows the person's tenant. Otherwise its
   outcome is `needs_person`, and the line is `skipped` (or red when another system
   is down) and names such systems. The line names the systems that did
   not answer, at most `_MAX_NAMES_SHOWN` (10) and a count of the rest.
   For the list of all tenants (`tenant_id` `None`), each name carries the
   tenant slug (`esx-2 (tenant blue)`, read by `_tenant_slugs`; the tenant
   id when the slug cannot be read).
6. `check_destination` completes the DNS line after the certificate check:
   it calls `BACKPLANE_URL/first-start/destination?nonce=<random>` and
   compares the answer with `_destination_proof(nonce)`, an HMAC under a key
   of this install (`derive_install_key` in `operations/_validate.py`, made
   from `PARAMS_HASH_KEY` only, the same on every pod). While the
   certificate line is red, the DNS line stays green and says the
   destination is checked once the certificate is green.
   Without `PARAMS_HASH_KEY`, `derive_install_key` raises
   `InstallKeyMissingError`: `/first-start/destination` answers 404, and the
   DNS line is a `warn` that names the setting. MEHO also logs
   `params_hash_key_missing` once at start.

Surfaces: `meho status --first-start` (`cli/internal/cmd/first_start.go`)
calls `GET /api/v1/first-start` with the login of `meho login`
(`AuthedClient.GetFirstStart`, one refresh on a 401). Exit code 6 on a red
line, 2 without a login (it then calls nothing), 5 without the admin role;
`WARN` label for a warning. The console page `/ui/first-start`
(`ui/routes/first_start.py`) shows the same list and shares its runs and
kept lists.

## Dependencies

`settings.backplane_url`, `settings.keycloak_discovery_base`, the database
session maker, the connector resolver.

## Known issues

- Behind a proxy, the `Date` header of the Keycloak answer comes from the
  proxy, so the clock line compares with the clock of the proxy.
- The destination check needs MEHO to reach its own public address from
  inside the cluster. A site where that is not possible gets a red
  certificate line first ("could not connect").
- A list is kept for 10 seconds, so a repair shows on a call after that.
- The install scripts (`deploy/linux-server/lib/core.sh`) do not read the
  list: they have no login. The result names where to read it after the
  first login ([linux-server-install.md](linux-server-install.md)).
- The console page `/ui/first-start` needs only a console session
  (`require_ui_session`); then `may_see_first_start` on the lifted operator
  decides, the same check as the API. So a platform admin with the reader
  role in the current tenant sees the list. When the role lift fails
  soft (`resolve_ui_operator` returns `None`), the page answers 403.
