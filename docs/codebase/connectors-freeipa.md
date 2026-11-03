<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# FreeIPA connector (`connectors/freeipa`)

## Overview

The FreeIPA connector reads and changes a Red Hat Identity Management (IdM)
server. IdM is Red Hat's build of FreeIPA. One target is one IPA server.

- Connector ID: `freeipa-jsonrpc-4.x` (product `freeipa`), plus a wildcard
  row, so every `freeipa` target finds it. The supported range is FreeIPA
  `>=4.9,<5.0` (Red Hat Enterprise Linux 8 and later).
- FreeIPA has no OpenAPI description. Its API is JSON-RPC, so the connector
  has typed (hand-written) operations only. Nothing is ingested.
- Thirty-four operations. Seventeen read (`safe`, no approval). Seventeen
  change something, and each change waits until a second person approves:
  - six change host groups, their members, automember rules and hosts;
  - eleven change sudo commands, sudo rules, access rules (HBAC) and
    password policies.
- Writes for users, groups and roles come in a later Task of Initiative
  #4772.

Code: [`backend/src/meho_backplane/connectors/freeipa/`](../../backend/src/meho_backplane/connectors/freeipa/).
Onboarding for operators: [`docs/cross-repo/freeipa-onboarding.md`](../cross-repo/freeipa-onboarding.md).

## Registering targets

```bash
meho targets add <ipa-target-name> --product freeipa --host <ipa-server-name> \
  --secret-ref <vault-path-of-the-ipa-login> --tls-ca-pin "$(cat ca.crt)"
```

- `host` is the IPA server's host name. FreeIPA refuses a request whose
  `Referer` header does not name the server's own host name (HTTP 400). When
  `host` is an IP address or an alias, set `tls_server_name` to the server's
  host name. MEHO then uses it for the `Referer` and for the certificate check.
- `tls_ca_pin` holds the IPA CA (`/etc/ipa/ca.crt` on the server). IPA signs
  its web certificate with its own CA.
- The Vault secret holds `username` and `password` of an IPA user.
- Only `auth_model=shared_service_account` (or unset) is accepted. Other
  modes raise `NotImplementedError`.

## Key types

| Type | Module | Role |
| --- | --- | --- |
| `FreeipaConnector` | `connector.py` | Login, session cache, `call()`, fingerprint, the handler methods of the reads and host writes |
| `FreeipaAccessWriteOps` | `access_writes.py` | The handler methods of the sudo, access rule and password policy writes; `FreeipaConnector` inherits them |
| `FreeipaAnswer` | `rpc.py` | A successful answer: `result`, `principal`, `server_version` |
| `FreeipaCommandError` | `rpc.py` | The server answered a command with `error`, or a write did not do all of its change (`NotDone`); names the command, error and text |
| `FreeipaRequestRefusedError` | `connector.py` | HTTP 400; the message names the `Referer` rule |
| `FreeipaTypedOp` | `_op_common.py` | Metadata of one operation, as the registrar passes it on |
| `FreeipaTargetLike` | `session.py` | The target fields the connector reads |

## Control flow

### Registration

`connectors/freeipa/__init__.py` registers the connector twice (versioned and
wildcard) and queues `register_freeipa_typed_operations`. At start-up the
registrar stores every operation of `FREEIPA_TYPED_OPS` with
`source_kind="typed"`.

### Login and session

1. The first call for a credential reads `username` and `password` from Vault
   under the caller's identity (`load_basic_credentials`).
2. `POST /ipa/session/login_password` with the form fields `user` and
   `password`, `Referer: https://<server name>/ipa` and `Accept: text/plain`.
   This login runs on the pooled HTTP client directly, so the flight
   recorder never records it.
3. The answer sets the session cookies. The connector keeps them and sends
   them as one `Cookie` header, with the `Referer`, on every call.
4. The session is cached per credential (`credential_cache_key`: tenant,
   secret and host). The cache is read without a lock. A login takes the
   lock of its own credential only, so a login that hangs for one IPA server
   never holds up a call for another credential or another tenant. A call as
   MEHO's system identity never reads a cached session.
5. A refused login answers 401 and names the reason in
   `X-IPA-Rejection-Reason` (for example `invalid-password`,
   `password-expired`, `user-locked`). The error message carries the reason.
   A 400 at login raises `FreeipaRequestRefusedError`.

### One command

`FreeipaConnector.call(target, operator, command, args, options)`:

1. Builds `{"method": command, "params": [args, options], "id": 0}`. Options
   with value `None` are left out. `version` is always `2.257`, the API
   version the connector was written for. The server checks only the major
   part (`2`), so an older 4.x server accepts it too.
2. Sends it with `_post_json` (no retry, recorded by the flight recorder with
   the shared redaction; the `Cookie` header is not stored).
3. A 400 raises `FreeipaRequestRefusedError`. Every other HTTP error goes to
   the dispatcher as it is. On a 401 (an expired session) the dispatcher
   calls `invalidate_session(target, rejected=<request>)` and runs the call
   once more. The session is dropped only when the rejected request sent it,
   and the pooled client's cookie jar is cleared.
4. `unwrap_answer` raises `FreeipaCommandError` when `error` is set (FreeIPA
   still answers HTTP 200 then). Otherwise it returns the decoded `result`,
   the `principal` and the server `version`.
5. `decode_values` turns FreeIPA's tagged values into text:
   `{"__datetime__": "20261029120000Z"}` becomes `"2026-10-29T12:00:00Z"`,
   `{"__dns_name__": "x."}` becomes `"x."`. `__base64__` values stay as they are.

### Version

`fingerprint()` runs `ping`. Its summary is `IPA server version <v>. API
version <v>`. `version` is the server version; `extras` holds `api_version`
and `principal`. A failure gives `reachable=False` with the error. A 401 drops
the session first. `probe()` uses the same read.

### The reads

Bodies are in `reads.py`, metadata in `ops_directory.py` and `ops_access.py`.
A list read returns `{rows, total, truncated}` and sends `sizelimit` (default
100, at most 2,000). A read of one object returns `{entry}`. Rows and entries
keep FreeIPA's own attribute names; most values are lists. Members are in
keys named `<attribute>_<object type>`, for example `member_host`.

| Operation | Command | Group | Parameters |
| --- | --- | --- | --- |
| `freeipa.about` | `ping` | `server` | none |
| `freeipa.host.list` | `host_find` | `hosts` | `text`, `hostgroup` (sent as `in_hostgroup`), `limit` |
| `freeipa.host.show` | `host_show` | `hosts` | `fqdn` |
| `freeipa.hostgroup.list` | `hostgroup_find` | `hosts` | `text`, `limit` |
| `freeipa.hostgroup.show` | `hostgroup_show` | `hosts` | `name` |
| `freeipa.user.list` | `user_find` | `identity` | `text`, `limit` (active users only) |
| `freeipa.user.show` | `user_show` | `identity` | `uid` |
| `freeipa.group.list` | `group_find` | `identity` | `text`, `limit` |
| `freeipa.group.show` | `group_show` | `identity` | `name` |
| `freeipa.role.list` | `role_find` | `identity` | `text`, `limit` |
| `freeipa.role.show` | `role_show` | `identity` | `name` |
| `freeipa.pwpolicy.show` | `pwpolicy_show` | `identity` | `group` (none: the global policy) |
| `freeipa.sudorule.list` | `sudorule_find` | `access` | `text`, `limit` |
| `freeipa.sudorule.show` | `sudorule_show` | `access` | `name` |
| `freeipa.hbacrule.list` | `hbacrule_find` | `access` | `text`, `limit` |
| `freeipa.hbacrule.show` | `hbacrule_show` | `access` | `name` |
| `freeipa.hbac.test` | `hbactest` | `access` | `user`, `host` (sent as `targethost`), `service`, `rules` |

`freeipa.hbac.test` returns `{granted, matched, notmatched, warnings, errors}`.
`granted` is FreeIPA's `value`. FreeIPA's `warning` and `error` lists are
renamed, so they cannot be mistaken for a failed call.

### The host writes

Bodies are in `writes.py`, metadata in `ops_host_writes.py`. All six are in
the `hosts` group and carry the tag `write`. Every write has
`requires_approval: true`: the dispatcher runs the body only after a second
person approved. A change is `caution`, a delete is `dangerous`.
`_op_common.write_op` builds the metadata of every write.

| Operation | Command | Risk level | Parameters | Returns |
| --- | --- | --- | --- | --- |
| `freeipa.hostgroup.create` | `hostgroup_add` | `caution` | `name`, `description` | `{entry}` |
| `freeipa.hostgroup.member.add` | `hostgroup_add_member` | `caution` | `name`, `hosts` (sent as `host`), `hostgroups` (sent as `hostgroup`); at least one of the two | `{entry, completed}` |
| `freeipa.automember.rule.create` | `automember_add` with `type=hostgroup` | `caution` | `hostgroup`, `description` | `{entry}` |
| `freeipa.automember.condition.add` | `automember_add_condition` with `type=hostgroup` | `caution` | `hostgroup`, `key`, `inclusive_regex`, `exclusive_regex` (sent as `automemberinclusiveregex`, `automemberexclusiveregex`); at least one of the two | `{entry, completed}` |
| `freeipa.host.delete` | `host_del` | `dangerous` | `fqdn`, `updatedns` (default false) | `{deleted, warnings}` |
| `freeipa.hostgroup.delete` | `hostgroup_del` | `dangerous` | `name` | `{deleted, warnings}` |

- Adding a member never creates a host. A host must exist in FreeIPA first.
- FreeIPA names an automember rule after the host group it fills, so the
  parameter is `hostgroup`. A rule acts on hosts that are added after it.
- A delete sends one name and no `continue`, so a missing object is
  FreeIPA's `NotFound` error. `warnings` holds FreeIPA's messages, for
  example that `updatedns` found no DNS records to remove.
- FreeIPA itself refuses to delete an IPA server or the host group
  `ipaservers`. A host delete also deletes the host's services and revokes
  its certificates.

### The sudo, access rule and password policy writes

The bodies are methods of `FreeipaAccessWriteOps` in `access_writes.py`;
`FreeipaConnector` inherits them, so the dispatcher binds each one to the
live connector. The metadata is in `ops_access_writes.py`. All eleven are
`caution` and wait until a second person approves. The password policy
write is in the `identity` group, next to its read; the others are in the
`access` group.

| Operation | Command | Parameters | Returns |
| --- | --- | --- | --- |
| `freeipa.sudocmd.create` | `sudocmd_add` | `command` (the full path of the program), `description` | `{entry}` |
| `freeipa.sudorule.create` | `sudorule_add` | `name`, `description` | `{entry}` |
| `freeipa.sudorule.user.add` | `sudorule_add_user` | `name`, `users` (sent as `user`), `groups` (sent as `group`); at least one of the two | `{entry, completed, external}` |
| `freeipa.sudorule.host.add` | `sudorule_add_host` | `name`, `hosts` (sent as `host`), `hostgroups` (sent as `hostgroup`); at least one of the two | `{entry, completed, external}` |
| `freeipa.sudorule.command.add` | `sudorule_add_allow_command` | `name`, `commands` (sent as `sudocmd`) | `{entry, completed}` |
| `freeipa.hbacrule.create` | `hbacrule_add` | `name`, `description` | `{entry}` |
| `freeipa.hbacrule.user.add` | `hbacrule_add_user` | `name`, `users`, `groups`; at least one of the two | `{entry, completed}` |
| `freeipa.hbacrule.host.add` | `hbacrule_add_host` | `name`, `hosts`, `hostgroups`; at least one of the two | `{entry, completed}` |
| `freeipa.hbacrule.service.add` | `hbacrule_add_service` | `name`, `services` (sent as `hbacsvc`), `service_groups` (sent as `hbacsvcgroup`); at least one of the two | `{entry, completed}` |
| `freeipa.hbacrule.disable` | `hbacrule_disable` | `name` | `{disabled}` |
| `freeipa.pwpolicy.set` | `pwpolicy_mod` | `group` (none or `global_policy`: the global policy) and at least one setting | `{entry}` |

- A new sudo rule or access rule is enabled but empty, so it allows nothing
  until it gets members. A sudo rule's commands run as root.
- `sudorule_add_allow_command` takes only the paths of sudo command objects,
  so `freeipa.sudocmd.create` comes first.
- A sudo rule keeps a user, group or host name that FreeIPA does not know as
  an external name (`externaluser`, `externalhost`), and FreeIPA counts it
  as added. That is how a sudo rule names a local user. The sudo bodies
  return these names in `external`, so a typo is visible. FreeIPA keeps
  the name in lower case. A group also counts when it is kept with sudo's
  group prefix (`%<group>`), for example a group from a trusted domain.
  Users and groups are matched separately. An access rule has no external
  names: an unknown name is in `failed`.
- `hbacrule_disable` answers `result: true` also for a rule that was
  disabled already.
- `freeipa.pwpolicy.set` sends only the settings given:

  | Parameter | `pwpolicy_mod` option |
  | --- | --- |
  | `max_life_days` | `krbmaxpwdlife` |
  | `min_life_hours` | `krbminpwdlife` |
  | `history_size` | `krbpwdhistorylength` |
  | `min_classes` | `krbpwdmindiffchars` |
  | `min_length` | `krbpwdminlength` |
  | `max_failures` | `krbpwdmaxfailure` |
  | `failure_reset_seconds` | `krbpwdfailurecountinterval` |
  | `lockout_seconds` | `krbpwdlockoutduration` |
  | `priority` | `cospriority` (only with `group`; the schema refuses it without) |

  FreeIPA answers `EmptyModlist` when the policy has these values already,
  and `NotFound` when the group has no policy of its own. The op has no tag
  `password`: with it, MEHO would treat the op as one that sends a secret
  and hide FreeIPA's error text.

#### A change that did not happen is never "ok"

FreeIPA answers many writes with success and lists what it could not do in
`failed`. Every write checks it with `check_done` in `_write_common.py`:

- `hostgroup_add_member` and the sudo rule and access rule add-member
  commands list each member with FreeIPA's reason, by attribute and object
  type:
  `{"member": {"host": [["web99.example.test", "no such entry"]]}}`. A host
  that is already a member is listed too (`This entry is already a member`).
- `automember_add_condition` lists conditions under
  `{"failed": {"automemberinclusiveregex": [...]}}`. A condition that the
  rule already has is not listed; FreeIPA only counts it as not added.
- A delete lists names under `result.failed`.

When `failed` has an entry, or when FreeIPA's `completed` count is lower than
the number of members or conditions sent, the write raises
`FreeipaCommandError` with the name `NotDone`. The message says how many
changes FreeIPA made and names each one it did not make, with FreeIPA's
reason. So the dispatcher answers with an error and the audit row says
`error`, also when FreeIPA made part of the change.

#### The approval previews

`writes.py` registers a preview builder for `freeipa.host.delete`
(`register_preview_builder`). When the call waits for approval, the builder
runs `host_show` with the requester's login and stores
`{host, hostgroups, indirect_hostgroups, updatedns}` in the approval
request, so the approver sees which host groups the host leaves. When
`host_show` fails (for example `NotFound`), the request still waits, and the
approval request says the preview is unavailable and why. Without a live
connector (`preview_operation`) the builder declines.

`access_writes.py` registers two more:

- `freeipa.hbacrule.disable` stores `{rule}`. For `allow_all` (in any
  case), FreeIPA's default rule that lets every user use every service on
  every host, it runs `hbacrule_find` with `ipaenabledflag=true`,
  `pkey_only` and `sizelimit` 2,000, and stores
  `{rule, warning, other_enabled_rules, truncated}`. The warning says that
  only the other enabled rules let users in after the change; when there is
  none, it says that no IPA user can log in to any host that uses IPA. For
  `allow_all` the builder declines without a live connector, and a failed
  read gives the "preview unavailable" marker, as for a host delete.
- `freeipa.pwpolicy.set` stores `{policy, changes}`. For the global policy,
  `policy` is `global_policy` and a `warning` says that the policy applies
  to every user whose groups have no policy of their own. The global policy
  is the one without `group`, and also with `group` `global_policy` in any
  case: FreeIPA builds the policy's LDAP name from the group name, and that
  name is the global policy. It reads only the parameters, so it needs no
  live connector.

The other writes show the generic echo of their parameters.

## Dependencies

- `adapters/http.py` (`HttpConnector`): pooled client, address checks, TLS
  pin, `_post_json`, flight recorder.
- `_shared/vault_creds.py` (`load_basic_credentials`), `_shared/cache_key.py`,
  `_shared/rejected_token.py`, `_shared/vcf_auth.py`
  (`session_establish_auth_error`, `is_acceptable_auth_model`).
- `operations/typed_register.py` (`register_typed_operation`).

## Tests

- `backend/tests/test_connectors_freeipa.py`: login, headers, session reuse,
  re-login after 401, one lock per credential, error messages, system
  identity, fingerprint, probe, registration, no password or session cookie
  in results and traces.
- `backend/tests/test_connectors_freeipa_rpc.py`: the wire helpers (request
  body, answer, value decoding, `ping` summary).
- `backend/tests/test_connectors_freeipa_reads.py`: every read's command,
  arguments, options and answer shape; invalid parameters are refused before
  any call.
- `backend/tests/test_connectors_freeipa_writes.py`: every host write waits
  for approval; the host delete preview; each write's command, arguments,
  options and answer shape after approval; `failed` entries and a low
  `completed` count fail the call and are named; invalid parameters are
  refused before any call.
- `backend/tests/test_connectors_freeipa_access_writes.py`: the same for the
  sudo, access rule and password policy writes, plus the external names of
  a sudo rule, the `allow_all` preview with and without other enabled
  rules, and the global password policy warning.
- `backend/tests/test_connectors_freeipa_spec_reconcile.py`: the command
  names against FreeIPA's `API.txt` (skips until the file is on the spec
  shelf; see `docs/decisions/spec-reconcile-guards-standard.md`).
- Shared fixtures: `backend/tests/_freeipa_fixtures.py`.

## Known issues

- No live proof yet. The lab proofs of the reads (#5357), of the host
  writes (#5359) and of the sudo, access rule and password policy writes
  (#5361) run on a lab IdM server.
- There are no writes that remove a member from a sudo rule or an access
  rule, enable a rule again, or delete one. To undo a typo in `external`,
  use the `ipa` command on the server for now.
- `freeipa.pwpolicy.set` covers the Kerberos settings and `priority`. The
  password quality settings of FreeIPA 4.9 and later (`maxrepeat`,
  `maxsequence`, `dictcheck`, `usercheck`, the credits) and the grace
  login limit are not offered yet. It cannot make a new group policy
  (`pwpolicy_add`).
- `freeipa.pwpolicy.show` carries the tag `password`, so MEHO hides
  FreeIPA's error text of that read.
- There is no read of automember rules yet. An agent sees a rule's
  conditions only in the answer of `freeipa.automember.condition.add`.
- Adding a host that is already a member fails, because FreeIPA lists it in
  `failed`. The message names it with FreeIPA's reason.
- FreeIPA's answer to an expired session is a 401 by its source code
  (`ipaserver/rpcserver.py`, `need_login`). The lab proof confirms it.
- A target that points at a load balancer in front of several IPA servers
  fails the `Referer` check: each server accepts only its own host name.
  Use one target per server.
- The reads return FreeIPA's default attributes, not `--all`. An attribute
  that FreeIPA shows only with `--all` is not shown yet.

## References

- FreeIPA JSON-RPC usage: `doc/api/jsonrpc_usage.md` in
  [`freeipa/freeipa`](https://github.com/freeipa/freeipa), release 4.13.4.
- FreeIPA `API.txt`, release 4.13.4 (506 commands, API version 2.257).
- Initiative #4772, Tasks #5357 (reads), #5359 (host writes) and #5361
  (sudo, access rule and password policy writes).
