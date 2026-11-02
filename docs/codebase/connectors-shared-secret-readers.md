# Connector caches: one Vault read per caller (`SecretReaders`)

## Overview

`backend/src/meho_backplane/connectors/_shared/secret_readers.py` holds
one small class, `SecretReaders`. Every connector cache uses it to decide
who may use a cached entry.

A connector keeps a credential, a login token or a client per target, so
the target's secret is read from Vault once and not on every call. That
read is what checks the caller: Vault answers under the caller's own
login. `SecretReaders` keeps that check when the cache is warm:

- A cached entry is handed to a caller only after that caller's own Vault
  read of the target's secret passed once in this process.
- The first caller's read fills the cache. Every later caller reads the
  secret once too, under their own login, and then uses the cached entry.
- A refused read raises, and the caller is not recorded. So Vault's answer
  reaches every caller, also when the cache is warm.
- The vendor still sees one login per credential. A new caller gets the
  cached token after their read, without a second vendor login. This
  matters because some vendors allow only one active session per user
  (see `connectors-shared-vault-creds.md § Connector cache keys`).

Callers are told apart by `operator.sub`:

- A person is one caller.
- The check runner (the background job that runs Sensors) has its own
  `sub` and its own token, so it is its own caller and reads the secret
  with its own Vault login.
- The synthesised system operator (operator-less probes) is never
  recorded. It runs the loader every time, as before. On a live Vault that
  read fails closed, because its placeholder token is not a valid login.

The SSH connection pool (`connectors/adapters/ssh.py`) does not use this
class. It holds open, logged-in sessions, so it keeps one connection per
caller and target instead (see `docs/architecture/connector-auth.md`).

## Key types

- **`SecretReaders`** — the callers who have read a target's secret in
  this process. Keyed on the tenant-unique target key
  (`target_cache_key`) and `operator.sub`.
  - `has_read(target, operator) -> bool` — whether the caller has read.
    Always `False` for the system operator.
  - `record(target, operator)` — remember a passed read. Call it right
    after the loader returned. The system operator is not recorded.
  - `ensure_read(target, operator, read)` — run `read` (the connector's
    credential loader, bound to the target and the operator) once for a
    caller who has not read yet, then record them. The result of `read`
    is not needed: the cache already holds the entry.
  - `forget(target)` — forget every reader of the target. Called from
    `invalidate_credentials`, so a target change makes every caller read
    the new secret.
  - `clear()` — forget everything. Called from `aclose`.
  - `subs(target)` — the recorded callers (for tests).

Every method is synchronous apart from `ensure_read`, and none of them
awaits while it changes state, so a connector calls them under its own
cache lock without a second lock.

## Control flow

Two shapes cover every cache.

A **credential cache** (the secret itself, used on every request):

```python
cached = self._creds_cache.get(cache_key)
if cached is not None and self._readers.has_read(target, operator):
    return cached
raw = await self._credentials_loader(target, operator)   # Vault, under the caller
self._creds_cache[cache_key] = raw
self._readers.record(target, operator)
return raw
```

A **token or session cache** (a vendor login minted from the secret):

```python
cached = self._session_tokens.get(cache_key)
if cached is not None:
    await self._readers.ensure_read(
        target, operator, lambda: self._credentials_loader(target, operator)
    )
    return cached                      # the shared login, after the caller's own read
creds = await self._credentials_loader(target, operator)
self._readers.record(target, operator)
... log in, cache the token ...
```

A connector with both caches lets the credential cache do the read: on a
token hit it calls its own `_load_credentials(target, operator)`, which
returns at once for a caller who has read and runs the loader for anyone
else.

`invalidate_credentials(target)` drops the cached entries of the target
and calls `forget(target)`. The dispatcher calls that hook after a failed
login, and the target update route calls it after a target change
(`operations/_handler_resolve.py`, `forget_target_in_connectors`).

## The caches that use it

| Connector | Cache | Key |
| --- | --- | --- |
| Shared VCF helper `CredentialsCache` (VCF Fleet, VCF Operations, VCF Operations for Logs, Fleet LCM) | `_cache` | target |
| VCF Operations, VCF Operations for Logs, Fleet LCM | `_session_tokens` | credential (+ login inputs) |
| VCF Automation | `_provider_tokens`, `_tenant_tokens` (one readers list per plane: the planes may read different secrets) | credential per plane |
| vSphere (`vmware_rest`) | `_session_tokens`, and the SOAP sessions `_vsan_sessions`, `_pbm_cookies` / `_pbm_profile_managers`, `_file_access_cookies` | target |
| NSX | `_session_tokens` | target |
| Harbor, Argo CD, Hetzner Robot, RabbitMQ, Cloud Builder | `_creds_cache` | target |
| VCF Installer, SDDC Manager (VCF 5 is `sddc_vcf5`) | `_creds_cache` and `_session_tokens` | target / credential |
| Proxmox | `_creds_cache` (the ticket cache follows it) | target / credential |
| Kubernetes | `_api_clients`, `_ws_api_clients` | target |
| GitHub | `_installation_tokens`, `_pat_tokens` | target |
| Keycloak | `_admin_tokens` | target |
| Cloud Director, vRA 8, HCX, Salt, SRM and vSphere Replication (`vmware_shared/dr_rest.py`), vRO | the session or login cache | credential (+ login inputs) |
| Profiled (ingested) connectors | `_session_tokens` | credential |
| Azure, Entra ID | `_credentials`, `_tokens` | target + secret / login + scope |
| Identity Broker (`idb`), phpIPAM | `_credentials`, `_tokens` | credential / target + secret |

Not in the list, on purpose:

- The HTTP client pool (`adapters/http.py`, `_clients`) holds no
  credential. Auth headers are set per request.
- Google Cloud (`gcloud`) reads the target's secret on every call before
  its token cache, so every caller is checked on every call already.
- The SSH connection pool keeps one connection per caller and target.

## Dependencies

- `connectors/_shared/cache_key.py` — `target_cache_key`.
- `connectors/_shared/system_operator.py` — `is_system_operator`.

## Known issues

- A caller stays recorded until the target changes, the connector closes
  or the process restarts. A caller whose Vault access is taken away after
  their read keeps using the cached entry until then. The cached entry
  itself lives as long today, so this adds no new window.
- One list per connector instance and process. A deployment with several
  processes keeps one list per process, like the caches it guards.

## References

- `docs/architecture/connector-auth.md`, § "Cache scoping under
  `shared_service_account`".
- `docs/codebase/connectors-shared-vault-creds.md`, § "Connector cache
  keys" and § "Who may use a cached entry".
- Tests: `backend/tests/test_secret_readers.py`,
  `backend/tests/test_connector_caches_read_per_caller.py`.
