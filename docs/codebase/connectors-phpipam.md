# phpIPAM connector

MEHO books and frees IP addresses in phpIPAM (an IP address management tool)
through the phpIPAM connector. An agent or an operator can:

- list the subnets and see how full each one is,
- list the free addresses of one subnet,
- reserve one address for a host name, with a note,
- release one reserved address.

A reserve and a release wait until a second person approves them. Every call
has an audit row, like every other MEHO operation.

Package: `backend/src/meho_backplane/connectors/phpipam/`.

No live proof has run yet. The code is built from the phpIPAM 1.8.2 API
reference and the 1.8.2 source of its API, and it is tested against a fake
phpIPAM only. The live proof (list, free addresses, reserve with approval,
read back, release with approval) is a separate step a person runs on a real
phpIPAM.

## Overview

| What | Value |
|---|---|
| Product | `phpipam` |
| Connector ID | `phpipam-rest-1.x` (implementation `phpipam-rest`, version `1.x`) |
| Registration | the versioned row plus the wildcard row `("phpipam", "", "")` |
| Kind | typed (hand-written) operations only |
| Operations | four, in the group `ipam` |
| API | phpIPAM REST API, `/api/{app_id}/...` |
| Path check | `backend/tests/test_connectors_phpipam_spec_reconcile.py` (a pinned list; phpIPAM has no API file to compare with) |

Why typed: phpIPAM publishes its API as a web page, not as an OpenAPI file.
So there is nothing to ingest, and the four operations are written by hand.

The version `1.x` is the phpIPAM major version. phpIPAM's API does not report
its version, so the fingerprint leaves `version` empty and
`supported_version_range` is not set. The wildcard row lets every `phpipam`
target find the connector.

## Setup in phpIPAM

1. In phpIPAM, open **Administration > API** and turn the API on.
2. Create an API app. Its **App id** goes into the target (`extras.app_id`).
3. Give the app the permission **Read / Write**. With **Read** only, the two
   reads work and a reserve or release is refused (HTTP 401).
4. Pick the app security. Both work with MEHO:
   - **SSL with App code token**: phpIPAM shows an app code. Store it in the
     secret as `token`.
   - **SSL with User token**: MEHO logs in as a phpIPAM user. Store the user
     in the secret as `username` and `password`. The user needs rights on the
     sections whose subnets MEHO should see and change.
5. Use HTTPS. phpIPAM refuses API calls without SSL for both security modes.

## Target shape

| Field | Value |
|---|---|
| `product` | `phpipam` |
| `host`, `port` | the phpIPAM web server |
| `extras.app_id` | required: the App id of the API app |
| `extras.base_path` | optional: the sub-path phpIPAM runs under, for example `/phpipam`; empty by default |
| `secret_ref` | the path of the secret in MEHO's credential store |
| `auth_model` | `shared_service_account` (the default) |

`extras.scheme`, `verify_tls`, `tls_ca_pin` and `tls_server_name` work as for
every HTTP connector.

## Secret shape

The secret at `secret_ref` holds one of:

- `{"token": "<app code>"}` for an app with "SSL with App code token", or
- `{"username": "...", "password": "..."}` for an app with "SSL with User token".

When both are present, the app code wins. MEHO reads the secret under the
operator's identity. A wrong shape fails with an error that names the field
names it found, never a value.

## Login and token

- With an app code, MEHO sends it in the `phpipam-token` header of every call.
- With a user, MEHO logs in once with `POST /api/{app_id}/user/` (HTTP Basic)
  and keeps the token phpIPAM returns. It sends that token in the
  `phpipam-token` header.
- When phpIPAM refuses a user token (HTTP 401 or 403; the token runs out after
  some hours), MEHO logs in again once and sends the call again. phpIPAM checks
  the token before it runs a call, so the refused call changed nothing.
- A second refusal, or a refused app code, becomes `connector_auth_failed`.
  MEHO then forgets the cached secret, so a corrected secret works on the next
  call without a restart.

The secret and the token never appear in a parameter, a result, an audit row,
an error text or a log. The login call is sent without a trace (the flight
recorder never sees its answer), and the trace filter of the connector masks
every `token` and `password` field in the answers it records. The test
`test_planted_secrets_stay_out_of_results_audit_and_errors` checks this with
planted values for both secret shapes.

## Operations

| Operation | What it does | Risk | Approval |
|---|---|---|---|
| `phpipam.subnet.list` | Subnets with id, CIDR, description, section and how full each is. Filter with `cidr`. | `safe` | no |
| `phpipam.subnet.free_addresses` | The first free addresses of one subnet (`limit`, default 20) and the count of all free ones. | `safe` | no |
| `phpipam.address.reserve` | Reserve one address with a host name, a description and a note. Name the address with `ip`, or set `first_free: true`. | `caution` | yes |
| `phpipam.address.release` | Release one reserved address. DNS records stay. | `dangerous` | yes |

A list with more than 50 rows comes back as a link to the stored result (a
result handle); the agent reads more rows with `result_query`. A large subnet
can have thousands of free addresses, so ask for a larger `limit` only when you
need it.

### phpipam.subnet.list

Reads `GET /api/{app_id}/subnets/`, or `GET /api/{app_id}/subnets/cidr/{subnet}/{mask}/`
when `cidr` is set. Folders are left out. For the first 50 subnets of the
answer it also reads `GET /api/{app_id}/subnets/{id}/usage/` (at most four at
the same time) and shows `used`, `free`, `max_hosts` and `used_percent`. The
other rows show `null` there; narrow the list with `cidr` to see them.

### phpipam.subnet.free_addresses

Reads the subnet, its child subnets and its used addresses, then walks the
subnet from the start and skips every used address. That needs three reads,
however large the subnet is.

- For IPv4 up to /30, the network and broadcast addresses are not free,
  unless the subnet is a pool in phpIPAM. For IPv6, the first address
  (subnet-router anycast) is not free.
- A subnet marked full in phpIPAM has no free addresses.
- A subnet with child subnets is refused: its addresses live in the children.

### phpipam.address.reserve

1. Checks the input: the host name, the address (in the subnet, not the
   network or broadcast address), and `ip` or `first_free` (exactly one).
2. Reads the subnet and the address (or, with `first_free`, the used
   addresses of the subnet).
3. Already reserved for the same host name (case and a trailing dot do not
   count)? Then nothing is written: `status: nothing_to_do`. With
   `first_free`, any address of the subnet with this host name counts.
4. Reserved for another host name? The call fails with
   `PhpipamAddressTakenError`, which names that host name. Nothing is written.
5. Sends `POST /api/{app_id}/addresses/` (`subnetId`, `ip`, `hostname`,
   `description`, `note`), or `POST /api/{app_id}/addresses/first_free/{subnet_id}/`.
   A 409 (someone reserved it a moment before) is handled like steps 3 and 4.
6. Reads the address back: `verification` is `verified` when it has the host
   name, `not_verified` when not, `unknown` when the read-back failed.

### phpipam.address.release

1. Reads the subnet and the address. Not reserved? `status: nothing_to_do`.
2. Sends `DELETE /api/{app_id}/addresses/{ip}/{subnet_id}/`. MEHO never sends
   `remove_dns`: DNS records stay with the DNS connectors.
3. Reads the address back: `verified` when it is gone.

The result names the host name the address had (`released_hostname`).

### A lost answer

When the answer to the write is lost (a time-out or a broken connection), the
result is `status: unknown` and `verification: unknown`. MEHO makes no other
call: it never sends the write twice. Both changes are safe to run twice, so
running the same call again shows what happened.

### Approval card

A reserve or a release waits for a second person. The approval card shows the
subnet, the address (for `first_free`, the address phpIPAM would give now),
the host name, whether the address is free now, and who holds it.

## Text from phpIPAM

Descriptions, host names and notes come from phpIPAM. Anyone who can edit
phpIPAM chooses them. They are data, never instructions: the guidance for the
AI says so, MEHO cuts them (500 characters, host names 255) and never acts on
them. phpIPAM's error messages go only into `upstream_message` of the error
(cut to 200 characters; the dispatcher filters secrets out of it).

## Errors

| Error | When |
|---|---|
| `PhpipamRefusedError` | MEHO refused the call before any change (bad input, folder, subnet with child subnets). |
| `PhpipamAddressTakenError` | Reserve: another host name holds the address. Names the holder. |
| `PhpipamApiError` | phpIPAM answered with an error MEHO does not handle itself. `http_status` and `upstream_message` say what. |
| `PhpipamConfigError` | `extras.app_id`, `extras.base_path` or the secret shape is wrong. |
| `connector_auth_failed` | phpIPAM refused the login or the token. |

## Paths and the path check

All paths are in `connectors/phpipam/_paths.py`, with the reference list of
the vendor calls and what phpIPAM answers, pinned to phpIPAM 1.8.2. phpIPAM
has no API file, so the path check pins the list of `METHOD:/path` pairs
instead of comparing with a spec. See the `phpipam-rest-1.x` entry in
[`spec-reconcile-guards-standard.md`](../decisions/spec-reconcile-guards-standard.md#phpipam-rest-1x--no-api-file-exists-the-check-pins-the-list-5070).

## Fingerprint and probe

`GET /api/{app_id}/user/` without a token. Every phpIPAM 1.x answers it with
its `{code, success, message}` shape (usually 401 or 403). Such an answer
means the API is reachable. `extras.api_code` and `extras.api_message` show
what phpIPAM said, for example "Invalid application id" for a wrong
`extras.app_id`.

## Key types

- `PhpipamConnector` (`connector.py`): login, token cache, `api_call` (the one
  way to call phpIPAM), fingerprint, probe and the handler methods.
- `resolve_target_config`, `parse_credentials`, `load_credentials_from_vault`
  (`session.py`): the target settings and the secret.
- `Subnet`, `read_subnet`, `read_address` (`_common.py`): the shared reads.
- `reads.py`, `writes.py`: the operation bodies and the approval previews.
- `typed_ops.py`, `_schemas.py`: the operation table, the group text, the
  schemas and the registrar.

## Tests

- `backend/tests/test_connectors_phpipam.py`: the four operations, the login
  and the read-back, against a fake phpIPAM (`backend/tests/_phpipam_fake.py`).
- `backend/tests/test_connectors_phpipam_governed.py`: through the dispatcher:
  registration, approval and audit, the result handle, planted secrets.
- `backend/tests/test_connectors_phpipam_spec_reconcile.py`: the pinned path list.

## Known issues

- No live proof yet (see the top of this page).
- `phpipam.subnet.list` reads the usage of the first 50 subnets only.

## References

- phpIPAM API reference: <https://phpipam.net/api/api_reference/>
- phpIPAM API documentation: <https://phpipam.net/api/api_documentation/>
- phpIPAM source, tag `v1.8.2`: <https://github.com/phpipam/phpipam/tree/v1.8.2/api>
