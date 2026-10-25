# Connector: salt (Aria Automation Config 8.x and VCF 9.1 Salt)

## Overview

The `salt` connector is the hand-written `HttpConnector` subclass for the
RaaS RPC API of VMware Salt: the API of Aria Automation Config 8.x and of
VCF 9.1 Salt. RaaS means "Returner as a Service"; it is the Salt server
that the Salt masters report to. It is registered under
`(product="salt", version="8.18", impl_id="salt-raas")` and as the
product wildcard, so a target with no version yet still resolves to it.
The `>=8.0,<10.0` range keeps Aria Automation Config 8.x and VCF 9.1 Salt
on the same class: both serve the same RPC API.

Today the connector ships:

- the **login** to the RaaS HTTP bridge,
- **version detection** (MEHO reads the RaaS version), and
- six **reads**: the server, the minions (the managed machines) and their
  keys, the saved targets, the job runs and their results.

Package: `backend/src/meho_backplane/connectors/salt/`.

| Operation | What it does | Vendor call (RPC `resource.method`) | Risk | Versions |
|---|---|---|---|---|
| `salt.about` | RaaS version, Python version, the version of each RaaS part, and the masters RaaS knows with their status and last contact. MEHO uses the same version read to detect the version. | `api.get_versions`, `master.get_master_presence` | read (`safe`, no approval) | 8.18, 9.1+ |
| `salt.minion.list` | One page of minions: minion id, master, key state, presence, last contact, OS, OS release, kernel, Salt version. | `minions.get_minion_key_state`, `minions.get_minion_presence`, `minions.get_minion_cache` | read (`safe`, no approval) | 8.18, 9.1+ |
| `salt.key.list` | Minion keys by state: accepted, pending, rejected, denied. | `minions.get_minion_key_state` | read (`safe`, no approval) | 8.18, 9.1+ |
| `salt.job.list` | Recent job runs, newest first: jid, function, saved target, state, user, start time and minion counts. Filter by state to find failed runs. | `cmd.get_cmds` | read (`safe`, no approval) | 8.18, 9.1+ |
| `salt.job.result` | The results of one run (one `jid`), one row per minion: success, return code, errors, the failed states (kind, outcome, timing), and the return when it is small (at most 8 KiB). | `ret.get_returns` | read (`safe`, no approval) | 8.18, 9.1+ |
| `salt.target.list` | Saved targets (minion groups): id, name, description and, per master, the match type and match text. Pillar data is not read. | `tgt.get_target_group` | read (`safe`, no approval) | 8.18, 9.1+ |

All six operations exist on every covered version, so none needs a
version check. The changes (accept, reject and delete keys; run a state or
a command) come in a later Task.

**Two calls differ from the first plan.** The RaaS 8.18 reference has no
`master.get_master_status`; `salt.about` uses `master.get_master_presence`.
And there is no `jobs` resource: `job.get_jobs` lists saved job
definitions, not runs. The runs with state, user and start time come from
`cmd.get_cmds`, so `salt.job.list` uses it.

## Registering targets

Register one target per RaaS server:

```bash
meho targets add <salt-target-name> --product salt --host <raas-address> \
  --secret-ref <vault-path-of-the-raas-login>
```

The login is a RaaS user (for example the built-in `root` user or a
service user with read permissions). The Vault secret holds `username`
and `password`. Only `auth_model=shared_service_account` (or unset) is
accepted; other modes raise `NotImplementedError`.

## Key types

- `SaltConnector` (`connector.py`) -- the connector class. Caches one
  session per target and tenant (`target_cache_key`). `rpc(target,
  operator, call, kwarg)` calls one RPC method and returns its `ret`.
- `SaltTargetLike`, `SaltSessionLoader`, `load_session_credentials_from_vault`
  (`session.py`) -- the minimum target shape and the credential loader
  (Vault under the operator's identity; tests inject a static loader).
- `unwrap_rpc`, `SaltRpcError` (`connector.py`) -- read the RPC answer
  `{"riq", "ret", "error", "warnings"}`: return `ret`, or raise with the
  vendor's `error.message` and `error.detail` (each cut to 500 characters).
- `SaltServerVersion`, `parse_server_version` (`connector.py`) -- the
  version read. `version` is the leading number part of the RaaS version
  text (`8.18.0` from `8.18.0+build`); the full text is the fingerprint's
  `build`.
- `*_RPC` constants (`connector.py`) -- every RPC method the connector
  calls, as `(resource, method)`. The reconcile lane reads them.
- `reads.py` -- the bodies of the six reads.
- `returns.py` -- what `salt.job.result` shows of one job return row.
- `trace.py` -- the filter the flight recorder runs on every `/rpc` answer.
- `SALT_TYPED_OPS`, `register_salt_typed_operations` (`typed_ops.py`) --
  the op list, the group texts and the registrar. The op metadata itself
  is in `_ops_minions.py` (server, minions, keys, saved targets) and
  `_ops_jobs.py` (job runs and results); `_op_common.py` holds the
  `SaltTypedOp` dataclass, the key and job state lists and the shared
  schema parts.

## Control flow

### Registration

Importing the package registers the class twice (versioned and wildcard)
and queues `register_salt_typed_operations` on the typed-op registrar
list. The lifespan runner writes the six descriptor rows before the first
dispatch. No spec ingest is needed.

### Login

From the vendor page "API Usage" of the RaaS 8.18 reference:

1. `GET /account/login` with HTTP Basic (the account read from Vault).
   The answer sets cookies, among them `_xsrf`.
2. Every RPC call is `POST /rpc` with the same HTTP Basic header, the
   cookies, and `X-Xsrftoken: <value of the _xsrf cookie>`. When there is
   no `_xsrf` cookie, the connector uses an `X-Xsrftoken` answer header if
   there is one, and else sends no xsrf header (xsrf is off on that
   server). The body is `{"resource": ..., "method": ..., "kwarg": {...}}`;
   kwarg values that are `None` are left out.
3. The session (Basic header, cookies, xsrf value) is cached per target
   and tenant. Two targets never share a session.
4. A `401` on a later call reaches the dispatcher, which calls
   `invalidate_session(target)`. That drops only this target's session
   and clears the pooled client's cookies for it. The dispatcher runs the
   call once more, and the connector logs in again and reads Vault again.
   A `401` or `403` at login itself raises `ConnectorAuthError` naming
   the target.
5. An RPC answer with `error` set raises `SaltRpcError`; the message names
   the RPC method and the vendor's text, and reaches the operation's
   answer.

### Version

`server_version(target, operator)` calls `api.get_versions` and reads
`ret["raas"]["raas"]` (or `ret["raas"]` when it is text). It never raises:
a failure is named in `error`. A `401` drops this target's session, so the
next call logs in again. `fingerprint()` and `probe()` use it.

### The reads

- **Paging.** The list reads take `limit` (default 100 for minions and
  keys, 50 for the others) and `page` (the first page is 0; left out when
  not given). The answer has `count`: how many rows match over all pages.
  `limit` 0 (no limit in RaaS) is refused by the schema.
- **`salt.minion.list`** reads one page of keys (sorted by minion id),
  then one presence read and one grains read for the same `master_id` /
  `minion_id` filter, each with a limit of 2000 rows. It joins them by
  master and minion. A minion id can exist under two masters, so a key
  row that names its master takes only the presence and grains of that
  pair; when the pair is missing, the fields stay `null`. Only a key row
  with no master id falls back to the minion id alone, and only when it
  is unique. On an estate with more than 2000 minions, presence
  and OS can be missing for some rows; then `details_complete` is false
  and the agent should narrow the read with `master_id` or `minion_id`.
- **`salt.job.result`** shows the return of each minion only when it is
  at most 8 KiB as JSON and its content can be checked. A state run (a
  return whose every value has a `result`) is shown as a list with one
  entry per state: `state` (module and function, `cmd.run`), `name`,
  `sls`, `result`, `changed`, `run_num`, `start_time` and `duration`. The
  state id, the `comment` and the `changes` text (a file diff, a command's
  output) are left out: they are free text that can quote a command with a
  password. `name` is filled only for `file`, `pkg`, `service`, `user`,
  `group` and `sysctl` states, whose name is a path or an object name; the
  name of a `cmd` state is the command, and the name of a `git` or `pip`
  state can be a URL with a password (`_NAME_STATE_MODULES` in
  `returns.py`). Every kept field is checked by shape (a boolean, a
  number, a time, a state file name). The state keys are not checked as
  field names: a state id such as `reset_password` would otherwise hide
  the whole state run. A key is only shown as its module and function,
  and the fields of each state are scrubbed. A number,
  `true`, `false` or `null` is shown as is. Every other return (a text, a
  list, a dict that is not a state run) is `null` with
  `return_withheld: true`, because `file.read` or `cmd.run` can return a
  password under no name that a rule could match. A return is also
  `null` with `return_truncated: true` when it, or the state list shown
  for it, is bigger than 8 KiB. The state list can be the bigger one,
  because every state entry has eight fields. `return_bytes` is the size
  the server sent. For a state
  run, `states_total`, `states_failed` and up to 20 `failed_states` (the
  same entries) are always filled, so a failure is visible without the
  full return.
- **Secrets.** `salt.about` returns no part of `opts` (the RaaS server
  settings, which hold database settings). `salt.target.list` asks for no
  pillar data and drops `pillars` and `metadata`. In job returns and
  function arguments, every value under a secret-shaped key (`password`,
  `token`, `private_key`, ...) and every PEM private key is replaced by
  `[REDACTED:param_name]` (the shared `Scrubber` of
  `connectors/_shared/api_file_values.py`). Job results withhold pillar
  data, like `salt.target.list`: for `pillar.*`, `config.get`,
  `config.option` and `config.items` the return is `null` with
  `return_bytes` and `return_withheld: true`, and a `pillar` argument in
  `fun_args` reads `[REDACTED:param_name]`. A positional argument has no
  name the scrubber could match (`user.setpassword <user> <password>`), and
  one module can mix name arguments with value arguments
  (`grains.setval <key> <value>`), so `fun_args` is shown only for a short
  list of functions whose arguments are only names and keys
  (`_SAFE_ARG_FUNS` in `returns.py`: `state.apply`, `pkg.remove`,
  `service.restart`, `grains.get`, `pillar.get`, `test.ping` and the
  like). Two of them take a value after the key: the second positional
  argument of `pillar.get` and `grains.get` is a default, which can be a
  password. It reads `[REDACTED:param_name]`, in positional form, as a
  `default` keyword and as `default=...` text (`_NAME_ARG_COUNT` and
  `_VALUE_KWARGS`). For every other function `fun_args` is `null` with
  `fun_args_withheld: true`. The dispatcher's redaction then runs over the
  whole answer as for every connector.
- **Traces.** The flight recorder stores the answer of each `/rpc` call
  only after `SaltConnector.trace_response_body` has run it through
  `trace_payload` (`trace.py`). The stored `ret` keeps only the fields the
  read that made the call uses, under their vendor names. Every other
  field is left out: custom grains and pillar data of the minion cache,
  command arguments of `cmd.get_cmds`, the `pillars` and `metadata` of
  saved targets, the server settings (`opts`). Job returns and arguments
  pass the `salt.job.result` rules. The answer of a method no read calls
  keeps no `ret`. All kept values are also scrubbed of secret-named
  values. So a value a read does not show never reaches the stored trace.
- Each read returns a dict with one list of flat rows, so MEHO stores a
  long list behind a result handle; `result_scalars` keeps the counts
  inline.

### Shutdown

`aclose()` clears the cached sessions, then closes the httpx pool.

## Dependencies

- `HttpConnector` (`connectors/adapters/http.py`) -- pooled httpx client,
  `_post_json` (the RPC call; not retried), TLS settings per target.
- `connectors/_shared/vcf_auth.py` -- `basic_auth_header`,
  `is_acceptable_auth_model`, `session_establish_auth_error`.
- `connectors/_shared/vault_creds.py` -- `load_basic_credentials`.
- `connectors/_shared/api_file_values.py` -- `Scrubber` for job returns.

## Tests

- `backend/tests/test_connectors_salt.py` -- respx-mocked RaaS server:
  login and headers, each read through `dispatch()` (the RPC calls sent
  and the rows returned), an RPC error and an HTTP error reaching the
  answer, a `401` that logs in again for one target only, secret
  replacement, the checked returns, the withheld arguments and the size
  cap in job results, no password, cookie or xsrf value in a result, no
  withheld value and no unread field (a custom grain, for example) in a
  stored trace, registration shape, version parsing, fingerprint.
- `backend/tests/test_connectors_salt_spec_reconcile.py` -- every `*_RPC`
  method is in the pinned RaaS 8.18 reference list
  (`salt-raas-8.18/salt-raas-8.18-reference-operations.tsv` on the spec
  shelf). Without the shelf the lane skips locally; CI checks the file
  out and fails when it is missing.

## Known issues

- **VCF 9.1 and 9.1.1 Salt** have no public API reference. The method
  names are taken from the 8.18 reference. A lab proof (or `api.discover`
  on a running 9.1 server) must confirm them.
- **Answer fields.** Many reference pages only say the method returns a
  dict. The rows read the field names the pages and their examples show,
  with `.get`, so a missing field is `null`, not an error.
- **LDAP users.** The HTTP bridge login with HTTP Basic is the vendor's
  documented way. A login for a user of another authentication backend
  (the Python client's `config_name`) is not supported yet.

## References

- VMware Salt RaaS 8.18 API reference: <https://developer.broadcom.com/xapis/vmware-salt-raas/8.18/>
  (pages "API Usage", `rpc_api`, `rpc_master`, `rpc_minions`, `rpc_cmd`,
  `rpc_ret`, `rpc_tgt`).
- `docs/codebase/connectors-srm.md` -- the connector this one copies its
  shape from.
