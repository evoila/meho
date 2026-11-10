# First-start checks

## Overview

`GET /first-start` (`backend/src/meho_backplane/first_start.py`) returns a
red/green list for a new install: DNS name, certificate, network, clock,
reachability of the targets. It needs no login, like `/ready`, and returns only the list.
User docs: `docs-site/install/first-start-checks.md`.

## Key types

- `FirstStartLine` (`name`, `status` = `ok` | `fail` | `warn` | `skipped`, `message`)
  and `FirstStartResponse` (`ok`, `checks`).
- `run_first_start_checks()` runs the five checks; `check_dns`,
  `check_certificate`, `check_network`, `check_clock`, `check_reachability` are one each.

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
   A source that cannot be read is red, not green.
5. `check_reachability` calls `resolve_connector_or_label` and
   `connector.fingerprint(target, operator=operator)` for every live target.
   It stores nothing (unlike `probe_and_store_fingerprint`). The console page
   passes the signed-in person (`resolve_ui_operator`). Without a person, a
   target with a `secret_ref` (login in Vault) is not contacted: its outcome
   is `needs_person`, and the line is `skipped` (or red when another system
   is down) with a count of such systems.
6. `check_destination` completes the DNS line after the certificate check:
   it calls `BACKPLANE_URL/first-start/destination?nonce=<random>` and
   compares the answer with `_destination_proof(nonce)`, an HMAC under a key
   of this install (`derive_install_key` in `operations/_validate.py`, made
   from `PARAMS_HASH_KEY` or `DATABASE_URL`, the same on every pod). While
   the certificate line is red, the DNS line stays green and says the
   destination is checked once the certificate is green.

Surfaces: `meho status --first-start` (`cli/internal/cmd/first_start.go`,
exit code 6 on a red line; `WARN` label for a warning) and `/ui/first-start`
(`ui/routes/first_start.py`).

## Dependencies

`settings.backplane_url`, `settings.keycloak_discovery_base`, the database
session maker, the connector resolver.

## Known issues

- The destination check needs MEHO to reach its own public address from
  inside the cluster. A site where that is not possible gets a red
  certificate line first ("could not connect").
- The endpoint is public. A red reachability line shows only a count of the
  systems that did not answer. Calls that arrive during a run share that
  run's task instead of starting one more (`run_public_first_start_checks`).
  After a run ends, its result is kept for `_PUBLIC_RESULT_HOLD_S` (30)
  seconds: calls in that time get it and start no run. A run that ended
  with an error is not kept; the next call starts a new run. The console page
  does not use the kept result; it runs the checks on every load.
- The console page `/ui/first-start` needs the `operator` role. It names the
  systems of the signed-in tenant that did not answer, and it uses the
  signed-in person's token for logins in Vault. When the role lift fails
  soft (`resolve_ui_operator` returns `None`), the page behaves like the
  public list for those systems.
