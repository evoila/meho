<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# FreeIPA onboarding: the IPA user, the CA, the target

This page tells an operator how to connect one Red Hat Identity Management
(FreeIPA) server to MEHO. When you are done, an agent can call
`call_operation` with connector `freeipa-jsonrpc-4.x` on your target, and
MEHO saves an audit row for every call.

The two steps people get wrong most often:

- **The host name.** FreeIPA refuses every request whose `Referer` header
  does not name the server's own host name. If the target's `host` is an IP
  address or an alias, set `tls_server_name` to the server's host name.
- **The password expiry.** A password set by an admin runs out at once. Set
  a password expiry date in the future for MEHO's IPA user, or the login
  fails with `password-expired`.

The code is in
[`backend/src/meho_backplane/connectors/freeipa/`](../../backend/src/meho_backplane/connectors/freeipa/).
The engineering page is
[`docs/codebase/connectors-freeipa.md`](../codebase/connectors-freeipa.md).

## What you get

Seventeen read-only operations, stored when MEHO starts. Nothing to ingest.

- `freeipa.about`: the server version, the API version, and the IPA user
  MEHO logs in as.
- Hosts and host groups: `freeipa.host.list` (also the hosts of one host
  group), `freeipa.host.show`, `freeipa.hostgroup.list`,
  `freeipa.hostgroup.show`.
- Users, groups, roles and password policy: `freeipa.user.list`,
  `freeipa.user.show`, `freeipa.group.list`, `freeipa.group.show`,
  `freeipa.role.list`, `freeipa.role.show`, `freeipa.pwpolicy.show`.
- Sudo and access rules: `freeipa.sudorule.list`, `freeipa.sudorule.show`,
  `freeipa.hbacrule.list`, `freeipa.hbacrule.show`, and `freeipa.hbac.test`
  (may this user use this service on this host?).

MEHO cannot change anything on the IPA server yet. Writes come in later
Tasks of Initiative #4772.

## Step 1: make an IPA user for MEHO

Use a separate IPA user, not `admin`. On a host enrolled in the domain, as an
IPA admin:

```bash
ipa user-add meho-svc --first=MEHO --last=Service --password
ipa user-mod meho-svc --password-expiration=20271231000000Z
```

The second command matters: a password set by an admin runs out at once.

FreeIPA lets every logged-in user read most users, groups and hosts. Check in
step 5 that each read works with this user. If a read fails with an access
error (`ACIError`), give the user a role with the matching read permission.

## Step 2: get the IPA CA

IPA signs its web certificate with its own CA. Copy the CA from the server:

```bash
scp root@ipa01.example.test:/etc/ipa/ca.crt ./ipa-ca.crt
```

## Step 3: put the secret in Vault

The secret holds two keys. MEHO reads it with the caller's own Vault
identity, so the Vault policy must let your operators read this path (see
[`connector-vault-policy.md`](connector-vault-policy.md)).

```bash
vault kv put -mount=secret freeipa/ipa01 \
  username="meho-svc" \
  password="<the password from step 1>"
```

MEHO never writes the password or the session cookie into audit rows, logs,
traces or stored results.

## Step 4: add the target

```yaml
targets:
  - name: ipa01
    product: freeipa
    host: ipa01.example.test
    port: 443
    secret_ref: freeipa/ipa01
    auth_model: shared_service_account
    tls_ca_pin: |
      -----BEGIN CERTIFICATE-----
      ...the content of ipa-ca.crt...
      -----END CERTIFICATE-----
```

| Field | Value |
| --- | --- |
| `host` | the IPA server's host name; one target per IPA server |
| `tls_server_name` | only when `host` is an IP address or an alias: the server's host name |
| `tls_ca_pin` | the IPA CA from step 2 |

```bash
meho targets import ipa01.yaml
meho targets probe ipa01 --json
```

A good probe has `reachable: true`, vendor and product `freeipa`, the server
version, and in `extras` the API version and the principal of `meho-svc`.

## Step 5: check it end to end

Ask an agent, or run through the CLI:

1. `freeipa.about`: the version and `principal` match your server and user.
2. `freeipa.host.list` with a host group: the same hosts as
   `ipa hostgroup-show <group>`.
3. `freeipa.hbac.test` for one user, host and service (for example `sshd`):
   the same answer as `ipa hbactest --user=<u> --host=<h> --service=sshd`.

## When something fails

| Error | Cause | Fix |
| --- | --- | --- |
| HTTP 400, the message names `tls_server_name` | the `Referer` does not name the server's own host name | set `tls_server_name`, or use the server's host name as `host` |
| login 401 `password-expired` | the IPA password of MEHO's user ran out | set a new password and a future `--password-expiration` |
| login 401 `invalid-password` | wrong user name or password in Vault | fix the secret; the next call reads it again |
| login 401 `user-locked` | too many failed logins | `ipa user-unlock meho-svc` |
| certificate check failed | `tls_ca_pin` missing or wrong | put the content of `/etc/ipa/ca.crt` into `tls_ca_pin` |
| `ACIError` on one read | the user may not read this kind of object | give the user a role with the read permission |

## References

- FreeIPA JSON-RPC usage: `doc/api/jsonrpc_usage.md` in
  [`freeipa/freeipa`](https://github.com/freeipa/freeipa).
- Engineering page: [`docs/codebase/connectors-freeipa.md`](../codebase/connectors-freeipa.md).
