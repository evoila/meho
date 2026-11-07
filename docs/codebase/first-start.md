# First-start checks

## Overview

`GET /first-start` (`backend/src/meho_backplane/first_start.py`) returns a
red/green list for a new install: DNS name, certificate, clock, reachability
of the targets. It needs no login, like `/ready`, and returns only the list.
User docs: `docs-site/install/first-start-checks.md`.

## Key types

- `FirstStartLine` (`name`, `status` = `ok` | `fail` | `skipped`, `message`)
  and `FirstStartResponse` (`ok`, `checks`).
- `run_first_start_checks()` runs the four checks; `check_dns`,
  `check_certificate`, `check_clock`, `check_reachability` are one each.

## Control flow

1. `check_dns` resolves the host of `BACKPLANE_URL`.
2. `check_certificate` (skipped on DNS fail or plain http) opens TLS to
   `BACKPLANE_URL` with full verification; the `SSLCertVerificationError`
   verify code picks the fix sentence; then the end date is checked
   (`CERT_MIN_DAYS_LEFT` = 30).
3. `check_clock` compares `now()` with the `Date` header of the Keycloak
   discovery answer and with PostgreSQL `now()` (`CLOCK_MAX_SKEW_S` = 60).
   A source that cannot be read is red, not green.
4. `check_reachability` calls `resolve_connector_or_label` and
   `connector.fingerprint(target, operator=None)` for every live target. It
   stores nothing (unlike `probe_and_store_fingerprint`).

Surfaces: `meho status --first-start` (`cli/internal/cmd/first_start.go`,
exit code 6 on a red line) and `/ui/first-start`
(`ui/routes/first_start.py`).

## Dependencies

`settings.backplane_url`, `settings.keycloak_discovery_base`, the database
session maker, the connector resolver.

## Known issues

- DNS is checked for "resolves" only. MEHO does not know the address it is
  served on, so it cannot compare.
- The endpoint is public. A red reachability line shows only a count of the
  systems that did not answer. The public answer is kept for 30 seconds, so
  many calls run the checks once.
- The console page `/ui/first-start` needs the `operator` role. It names the
  systems of the signed-in tenant that did not answer.
- With no person's token, a target whose login is in Vault cannot be read, so
  it shows as not answering.
