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
  keys, the saved targets, the job runs and their results, and
- six **changes**: accept, reject and delete minion keys; run a state or a
  shell command on a target; stop a running job. Every change waits for a
  person's approval.

Package: `backend/src/meho_backplane/connectors/salt/`.

| Operation | What it does | Vendor call (RPC `resource.method`) | Risk | Versions |
|---|---|---|---|---|
| `salt.about` | RaaS version, Python version, the version of each RaaS part, and the masters RaaS knows with their status and last contact. MEHO uses the same version read to detect the version. | `api.get_versions`, `master.get_master_presence` | read (`safe`, no approval) | 8.18, 9.1+ |
| `salt.minion.list` | One page of minions: minion id, master, key state, presence, last contact, OS, OS release, kernel, Salt version. | `minions.get_minion_key_state`, `minions.get_minion_presence`, `minions.get_minion_cache` | read (`safe`, no approval) | 8.18, 9.1+ |
| `salt.key.list` | Minion keys by state: accepted, pending, rejected, denied. | `minions.get_minion_key_state` | read (`safe`, no approval) | 8.18, 9.1+ |
| `salt.job.list` | Recent job runs, newest first: jid, function, saved target, state, user, start time and minion counts. Filter by state to find failed runs. | `cmd.get_cmds` | read (`safe`, no approval) | 8.18, 9.1+ |
| `salt.job.result` | The state of one run (one `jid`) and its progress, then its results, one row per minion: success, return code, errors, the failed states (kind, outcome, timing), and the return when it is small (at most 8 KiB). The status read of a started job. | `cmd.get_cmds`, `ret.get_returns` | read (`safe`, no approval) | 8.18, 9.1+ |
| `salt.target.list` | Saved targets (minion groups): id, name, description and, per master, the match type and match text. Pillar data is not read. | `tgt.get_target_group` | read (`safe`, no approval) | 8.18, 9.1+ |
| `salt.key.accept` | Accept pending minion keys (also rejected or denied ones with a flag). Returns each key's state before and after. | `minions.get_minion_key_state`, `minions.set_minion_key_state` (`state: accept`) | change (`caution`) | 8.18, 9.1+ |
| `salt.key.reject` | Reject minion keys (also accepted or denied ones with a flag). Returns each key's state before and after. | `minions.get_minion_key_state`, `minions.set_minion_key_state` (`state: reject`) | change (`dangerous`) | 8.18, 9.1+ |
| `salt.key.delete` | Delete minion keys. Needs a preview; the card lists each key and its state. | `minions.get_minion_key_state`, `minions.set_minion_key_state` (`state: delete`) | change (`destructive`) | 8.18, 9.1+ |
| `salt.job.run_state` | Apply states (or the highstate) to the minions of a target. Returns the `jid` as soon as RaaS accepts the job. Needs a preview. | `tgt.get_target_group_minions`, `cmd.get_cmds`, `cmd.route_cmd` (`cmd: local`, `fun: state.apply`) | change (`destructive`) | 8.18, 9.1+ |
| `salt.job.run_command` | Run one shell command on the minions of a target. Returns the `jid`. Needs a preview that shows the command in full. | `tgt.get_target_group_minions`, `cmd.get_cmds`, `cmd.route_cmd` (`cmd: local`, `fun: cmd.run`) | change (`destructive`) | 8.18, 9.1+ |
| `salt.job.cancel` | Stop a running job (the vendor's soft stop). | `cmd.get_cmds`, `cmd.stop_job` | change (`dangerous`) | 8.18, 9.1+ |

All twelve operations exist on every covered version, so none needs a
version check.

**Risk and approval.** Every change has `requires_approval: true`. A
person's call waits until a second person approves it. An agent's call to
`salt.key.accept` waits for approval; `salt.key.reject` and
`salt.job.cancel` need a grant for the `dangerous` risk level; the three
`destructive` changes are always refused for an agent, so only a person
runs them, after `preview_operation`.

**Three calls differ from the first plan.** `salt.job.cancel` calls the
vendor's own stop method `cmd.stop_job` with the `jid`, not
`cmd.route_cmd` with `saltutil.kill_job`: a kill job would need a target
of its own, and `cmd.stop_job` is the documented way ("Pass a 'soft_kill'
command through to the masters"). Also: The RaaS 8.18 reference has no
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
  session per credential (`credential_cache_key`: tenant, secret and
  login host), so two targets that share one credential share one session. `rpc(target,
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
- `key_writes.py` -- the bodies of the three key changes and the preview
  builder of `salt.key.delete`.
- `job_writes.py` -- the bodies of the run and stop changes, the target
  resolution, the secret check of a command, and the preview builders of
  `salt.job.run_state` and `salt.job.run_command`.
- `SaltChangeRefusedError` (`connector.py`) -- MEHO refuses a change before
  it calls the vendor; the message says why and what to do.
- `returns.py` -- what `salt.job.result` shows of one job return row.
- `trace.py` -- the filter the flight recorder runs on every `/rpc` answer.
- `SALT_TYPED_OPS`, `register_salt_typed_operations` (`typed_ops.py`) --
  the op list, the group texts and the registrar. The op metadata itself
  is in `_ops_minions.py` (server, minions, keys, saved targets) and
  `_ops_jobs.py` (job runs and results), `_ops_key_writes.py` (key
  changes) and `_ops_job_writes.py` (run and stop); `_op_common.py` holds
  the `SaltTypedOp` dataclass, the key and job state lists, the plain job
  status of each vendor job state, and the shared schema parts.

## Control flow

### Registration

Importing the package registers the class twice (versioned and wildcard)
and queues `register_salt_typed_operations` on the typed-op registrar
list. The lifespan runner writes the twelve descriptor rows before the
first dispatch. No spec ingest is needed. Importing the package also
imports `key_writes` and `job_writes`, which register the preview builders.

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
- **`salt.job.result`** first reads the run with `cmd.get_cmds` (`jid`,
  limit 1): `job_state` (the vendor's state), `job_status` (the same in
  plain words: queued, running, paused, stopping, stopped, skipped,
  succeeded, failed, incomplete, unknown), `job_ended`, and the progress
  counts `expected`, `returned`, `returned_good`, `returned_failed`. When
  the server does not answer (no connection, a timeout, or HTTP 502, 503 or
  504), the read returns `reachable: false`, `job_status: "not reachable"`,
  the `jid`, `checked_at` and the error, and does not say the job failed. A
  `401` is not caught: the dispatcher logs in again and runs the read once
  more. The read then shows the return of each minion only when it is
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

### The key changes

`salt.key.accept`, `salt.key.reject` and `salt.key.delete` take 1 to 50
keys, each as an exact `master_id` and `minion_id`. The schema refuses a
space, a comma and the glob characters `* ? [ ]`, so a change never reaches
more keys than the caller named. The body:

1. reads the state of each key with `minions.get_minion_key_state`
   (`master_id`, `minion_id`) and keeps the row that matches exactly;
2. refuses the whole call when one key does not exist;
3. calls `minions.set_minion_key_state` once with `state` (`accept`,
   `reject` or `delete`), `minions` as `[master_id, minion_id]` pairs, and
   the include flags the vendor page lists for that state
   (`include_rejected` and `include_denied` for accept, `include_accepted`
   and `include_denied` for reject);
4. reads the states again. The answer has `key_state_before` and
   `key_state_after` per key, and `changed_count`. The master applies the
   change, so a key can show its old state for a few seconds.

`salt.key.delete` is `destructive`. Its preview builder reads the keys and
fills the approval card: `object` (the count and a hash of the keys with
their states), `children` (each key with its state) and `irreversibility`.
It refuses when a key does not exist. The approved run reads the keys
again and compares the hash with the card. When a key or its state
changed (or the run has no card), it returns `status:
"precondition_failed"` and deletes nothing.

### Run a state or a command

`salt.job.run_state` (`fun: state.apply`) and `salt.job.run_command`
(`fun: cmd.run`) are `destructive`: such code can change anything on many
machines. The target is either `tgt_uuid` (a saved target) or `tgt` with
`tgt_type` (glob by default) and `master_id` (`*` by default); the schema
takes exactly one of the two (`oneOf`).

1. **Preview.** The builder resolves the target to the minions it matches
   now, with `tgt.get_target_group_minions` (`tgt_uuid`, or `tgt` as
   `{master_id: {"tgt": ..., "tgt_type": ...}}`). The card holds `fun`,
   `arg` (the state names, or the command text in full), the target as
   given, `minion_count`, `minions_sha256` (a hash of the sorted
   `[master_id, minion_id]` pairs), and every minion as a child. The
   builder refuses a target that matches no minion, more than 500 minions,
   or an id with a comma, a space or a glob character.
2. **Approved run.** The body resolves the target again. When `fun`,
   `arg`, the target or the minion hash differ from the card, or the run
   has no card, it returns `status: "precondition_failed"`, `started:
   false`, and starts nothing.
3. **Start.** `cmd.route_cmd` gets `cmd: local`, the function, its `arg`
   (`{"kwarg": {...}}`, the vendor page's `arg` shape) and a
   `list` target per master that names exactly the approved minions. So
   the run never reaches a minion the approver did not see, even when the
   target expression matches more minions by then.

`salt.job.run_state` takes `states` (applied in one call as a comma list;
left out, it applies the highstate), `test` (a dry run) and `saltenv`.
`salt.job.run_command` takes `command`. Every value goes under `kwarg`:
the states as `mods`, the command as `cmd`. A minion reads a plain
argument of the form `name=value` as a keyword, so a command such as
`LANG=C locale` sent as a plain argument would not reach `cmd.run` as its
command. MEHO refuses a command that looks like it holds a secret: a
`name=value`, `name: value` or `--name value` whose name is secret-shaped
(`password`, `token`, `api_key`, ...), a password in a common
command-line form, or text the shared redaction engine would change. So the command can be shown in
full on the card and in the audit row. This check is a help, not a
guarantee; the guidance tells callers never to put a secret into a command.

### Vendor jobs

The vendor does the work after the start call returns.

- **Start answer.** `status: "accepted"` and `started: true` mean RaaS
  took the job; they never mean the work is done. The answer has the `jid`
  and `status_read: "salt.job.result"`. When RaaS returns no `jid`, the
  answer says so and tells the caller to find the run with `salt.job.list`.
- **Status read.** `salt.job.result` with the `jid`, until `job_ended` is
  true (see "The reads").
- **A job that already runs.** Before a start, the body lists the jobs of
  the same function that have not ended (`cmd.get_cmds` with the states
  new, retrieved, resuming, resumed, pausing, paused, stopping). When one
  has the same `list` target, the answer is `status: "already_running"`
  with that job's `jid`, and nothing starts. This is a help, not a lock:
  two callers can both check before either starts.
- **A lost reply.** The caller does not start again blindly. It reads
  `salt.job.list` (function, user, newest first) to find the `jid` first.
  The guidance tells callers to pass `async: true` to `call_operation`
  (`docs/codebase/async-governed-dispatch.md`), so MEHO keeps the start's
  answer, with the `jid`, under a run handle.
- **A login that expired.** The dispatcher logs in again on `401` and runs
  the status read once more.
- **No answer.** The status read says `not reachable` with the `jid` and
  the time. MEHO keeps no job state between calls; the caller keeps the
  `jid` (or the async run handle of the start).
- **Multi-step changes.** Each change makes one vendor change call, so no
  change can stop half-way.

`salt.job.cancel` reads the run with `cmd.get_cmds`. It refuses an unknown
`jid`. For a job that has ended it returns `status: "not_running"`, for a
job that is stopping `already_stopping`, and sends nothing. Otherwise it
calls `cmd.stop_job` with the `jid` and returns `status: "accepted"` and
`stop_jid` (the job id of the stop command). Work a minion already did
stays done.

### Secrets in the changes

- Results hold minion ids, key states, function names, counts and job ids;
  no password, cookie or xsrf value.
- `salt.job.result` withholds the text a command printed (see "The
  reads"), so a command's output never reaches an answer.
- The flight recorder stores no vendor body of a `destructive` operation.
  For `salt.job.cancel` it keeps only the job fields of the reads and the
  stop's job id.
- An error of a change reaches the answer with the vendor's text, after the
  dispatcher's normal redaction. The key changes carry the tag `minion`,
  not `key`: the `key` tag would mark them as secret-bearing, and a minion
  key is a public key.

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
- `backend/tests/test_connectors_salt_writes.py` -- the six changes
  through the dispatcher, with a respx-mocked RaaS: risk levels; each
  change waits for approval, runs the right RPC call after approval, and
  passes a vendor error on; the destructive ones are refused without a
  `preview_hash` and for an agent, their cards list every key or minion,
  and the full flow (preview, wait, a second person approves, run) works;
  drift after the approval gives `precondition_failed`; a run reaches only
  the card's minions; a running job is not started twice; a command with a
  secret is refused; the status read gives the job state, says `not
  reachable`, and logs in again after a `401`; the stored traces.
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
- **Field names of the change answers.** The reference pages give
  `tgt.get_target_group_minions` and `minions.set_minion_key_state` only
  as "dict", and `cmd.route_cmd` as "str". The target resolution reads a
  dict keyed by master whose value is a list of minion ids (or of rows
  with `minion_id`). The check for a running job compares the `tgt` field
  of each `cmd.get_cmds` row; a server that does not return it never
  matches, so the check finds nothing. A lab proof must confirm both.
- **Stop a command.** `cmd.stop_job` is a soft stop. It stops a state run
  between two states; a lab proof must check that it also stops a long
  `cmd.run`.
- **LDAP users.** The HTTP bridge login with HTTP Basic is the vendor's
  documented way. A login for a user of another authentication backend
  (the Python client's `config_name`) is not supported yet.

## References

- VMware Salt RaaS 8.18 API reference: <https://developer.broadcom.com/xapis/vmware-salt-raas/8.18/>
  (pages "API Usage", `rpc_api`, `rpc_master`, `rpc_minions`, `rpc_cmd`,
  `rpc_ret`, `rpc_tgt`).
- `docs/codebase/connectors-srm.md` -- the connector this one copies its
  shape from.
