# FortiGate connector

MEHO reads FortiGate firewalls through the FortiGate connector. It talks to
the FortiOS REST API with an API token. One FortiGate gives several MEHO
targets: one for the whole device, and one for each VDOM (a virtual firewall
inside the FortiGate).

Today the connector has six device reads. It also has the checks that every
later FortiGate change runs before and after it is sent. The later FortiGate
work (addresses, policies, routes, VPN, VDOMs and HA, traffic, health checks)
builds on this page; the docs work for FortiGate completes it.

Package: `backend/src/meho_backplane/connectors/fortios/`.

## Overview

| What | Value |
|---|---|
| Product | `fortigate` |
| Connector ID | `fortigate-rest-7.x` (implementation `fortigate-rest`, version `7.x`) |
| Covered versions | FortiOS 7.2 up to, but not including, 8.0 (`>=7.2,<8.0`) |
| Tested release lines | 7.2, 7.4, 7.6 (`versions.TESTED_RELEASE_LINES`) |
| Registration | the versioned row plus the wildcard row `("fortigate", "", "")` |
| Kind | typed operations only; FortiOS has no complete, open API file |
| Operations | six `safe` reads in the group `fortios-device` |
| Login | API token, `Authorization: Bearer <token>`; no password, no cookies |

The implementation is `fortigate-rest`, not `fortios-rest`: MEHO reads the
product from the first part of the connector ID, so the ID must start with
`fortigate`. The operation IDs start with `fortios.`.

## Targets

| Kind | `extras` | Used for |
|---|---|---|
| Device | `scope: device`, optional `management_vdom` (default `root`) | status, HA, VDOMs, licence, resources, device-wide changes |
| VDOM | `scope: vdom`, `vdom: <name>` | reads and changes inside one VDOM |

- The connector sends the target's VDOM as `vdom=<name>` on **every** call.
  A device target sends its management VDOM, because some FortiOS releases
  return empty fields without one.
- No operation takes a VDOM parameter. A query that tries to set `vdom` or
  `global` is refused, so an agent cannot reach another VDOM.
- A device operation on a VDOM target, or a VDOM operation on a device
  target, is refused before any call (`target.require_scope`).
- A VDOM name has letters, digits, `-` and `_`, at most 31 characters.
- The token lives in the target's Vault secret (`secret_ref`), field
  `api_token`. MEHO reads it under the caller's identity. Use a REST API
  administrator with the least rights it needs, and limit its trusted hosts
  to MEHO's address. A VDOM target gets a token limited to that VDOM.
- TLS checks stay on. Pin the device's certificate or CA with `tls_ca_pin`,
  and set `tls_server_name` when the host is an IP address.

## Operations

All six are `safe` reads on a device target, in the group `fortios-device`.

| Operation | FortiOS calls | Returns |
|---|---|---|
| `fortios.system.status` | `GET monitor/system/status`, `monitor/system/firmware`, `monitor/license/status` | host name, model, serial, hardware or VM, version, build, FortiCare and VM licence state |
| `fortios.ha.status` | `GET monitor/system/ha-peer`, `monitor/system/ha-checksums`, `cmdb/system/ha` | HA mode and group, members with role and priority, `in_sync`, checksums, `device_id` |
| `fortios.vdom.list` | `GET cmdb/system/vdom` | the VDOMs |
| `fortios.central_management.status` | `GET monitor/system/central-management/status` | whether FortiManager controls the device |
| `fortios.license.status` | `GET monitor/license/status` | licences and support contracts (no account or company names) |
| `fortios.resource.usage` | `GET monitor/system/resource/usage?scope=global` | current CPU, memory, disk, sessions, setup rates |

`fortios.ha.status` reads only `mode` and `group-name` from `cmdb/system/ha`;
that answer also holds the HA password, which is removed anyway.

Every answer has `version_warning`: `null` on a tested release line, a text
on a covered but untested one.

The group texts of the later groups (`fortios-network`, `fortios-policy`,
`fortios-vpn`, `fortios-traffic`) are in `typed_ops.FORTIOS_GROUP_TEXTS`. A
group appears in MEHO when its first operation is registered.

## Detection

`FortiOSConnector.fingerprint` reads status and firmware, then the HA
members, the VDOM list and the FortiManager state. It records:

- `version` (for example `7.4.4`), `build`, and `edition` (`hardware` or `vm`);
- under `extras`: model, host name, the serial of the unit that answered,
  `scope` and `vdom`, `ha_members`, `vdoms`, `fortimanager_controls`,
  `version_warning`;
- `extras.device_id`: one ID for all targets of one device. For an HA pair it
  is `ha:` plus the sorted member serials, so it stays the same after a
  failover; for one unit it is `serial:<serial>`. Checks and the shared
  device budget use it to treat all targets of one device as one.

A token that may not read the HA members, the VDOM list or the FortiManager
state (a VDOM token, for example) leaves that field `null`; detection still
works. A model that is not a FortiGate, or a version outside 7.2 to 7.x, is
refused: `reachable` is false, `extras.refused` is true and `extras.error`
says why. The token never appears in the result.

## Errors

| What happens | What the caller sees |
|---|---|
| HTTP 401 | `connector_auth_failed`, cause `token_refused`. Sent once; never retried. |
| HTTP 403 | `connector_auth_failed`, cause `missing_rights`: the token's profile lacks the rights. |
| TLS check fails | `connector_tls_verify_failed`; detection says to set `tls_ca_pin` and `tls_server_name`. |
| Answer with `status` other than `success` | `connector_error` with the FortiOS error number and the cleaned `cli_error`. |
| Other HTTP error | `connector_error` with the status and the cleaned FortiOS error. |
| Version 8.0 or older than 7.2 in an answer | the read is refused. |

The connector checks FortiOS's answer wrapper (`status`, `error`,
`cli_error`), not only the HTTP code (`_answer.unwrap_answer`).

## Secret removal

`redaction.scrub` runs on every answer inside the connector, before the
handler returns. So the audit row, a stored result, the trace and the
broadcast feed only see the cleaned values. It replaces with `[REDACTED]`:

- any text that starts with `ENC ` (FortiOS's mark for a stored secret),
  whatever the field name;
- any PEM private key;
- the value of a known secret field (`password`, `psksecret`, `passwd`,
  `secret`, `private-key`, ...), a name with a secret word in it, or a name
  that ends in `-key` / `_key` (except `q_origin_key`, `mkey`, `public-key`).

Error texts pass `redaction.scrub_text`, and no error keeps the raw HTTP
answer attached. The stored trace of a call is cleaned the same way
(`FortiOSConnector.trace_response_body`). The limit: a secret typed into a
free-text field, such as a comment, is not found.

Saved test data: `backend/tests/_fortios_fixtures.py` holds made-up answers
only; a test checks that it holds no `ENC` value and no secret field value.
Clean any new recorded answer with `redaction.scrub` before you commit it.

## Write checks

`write_checks.py` holds the checks that every FortiGate change runs. No
change operation exists yet; the later FortiGate work calls these.

1. `check_before_write` refuses the change (`FortiOSWriteRefusedError` with a
   `code`) when:
   - the version is not on the tested list (`version_untested`);
   - FortiManager controls the device (`fortimanager_controls`): "change it
     in FortiManager". MEHO never falls back to a direct change;
   - the live object is not `expected_before` (`object_changed`); for a
     create, `expected_before` is `null` and the object must not exist;
   - for a change or delete, the entries that use the object are not
     `expected_references` (`references_changed`), MEHO cannot read them, or
     the change carries no list because the preview could not read it
     (`references_unknown`). The list comes from
     `monitor/system/object/usage`. A create has no users, so it skips this
     check and makes no usage call.
2. `send_write` sends the change once, never retried. A lost answer (a
   timeout, a dropped connection, a failover) gives `unknown`, and MEHO does
   not send it again.
3. `verify_after_write` reads the object back and compares the fields that
   were sent (secret fields are not compared), then compares the HA
   checksums up to five times, two seconds apart. The outcome is `verified`,
   `not_verified` (with reasons) or `unknown` (the read-back failed). A token
   that may not read the HA checksums leaves `ha_in_sync` `null`.

`register_write_preview(op_id, WritePreviewSpec(...))` registers the preview
an approver sees: the object now and after, the entries that use it, the
VDOM, the HA state and the FortiManager state. The preview's
`expected_before` and `expected_references` are the values the change must
carry.

## To confirm on the lab appliance

The answer shapes come from public sources: Fortinet's community articles,
Fortinet's Ansible collection for FortiOS (read for facts only; it is GPL,
no code was copied) and open monitoring tools. Fortinet's full REST
reference needs a login. The first live run should confirm:

- the field names of `monitor/system/object/usage` (`currently_using` with
  `path`, `name`, `mkey`);
- that `mode: normal` with a server set is the right sign of FortiManager
  control;
- which reads a VDOM-limited token may make (central management state, HA
  checksums).

## Tests

- `backend/tests/test_connectors_fortios.py`: registration, the six reads,
  the token and VDOM on every call, target kinds, detection (hardware HA
  pair, single VM, VDOM target, FortiOS 8.0 and 7.0, a non-FortiGate model,
  an untested 7.x), and the 401 / 403 / TLS messages.
- `backend/tests/test_connectors_fortios_redaction.py`: planted `ENC` values
  and secret fields stay out of answers, audit rows, error texts, traces and
  saved test data.
- `backend/tests/test_connectors_fortios_write_checks.py`: the checks before
  and after a write, the lost answer, and the preview.

All tests mock HTTP with `respx` and use made-up names. No live device runs
in CI.
