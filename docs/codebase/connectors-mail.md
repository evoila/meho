# Mail connector (`connectors/mail`)

## Overview

The `mail.*` connector gives the backplane an **outward delivery
channel**: one typed op, `mail.send`, that delivers a plain-text email
through a deployment-level SMTP block. It is a **synthetic** connector
on the `net.*` mould (#2717, Initiative #2716) — no vendor connector
backs it, there is no `Connector` subclass, and the package calls
neither `register_connector` nor `register_connector_v2`. The handler is
a module-level function the dispatcher routes to with
`connector_instance=None` / `target=None`; recipients are **params**,
not a registered `Target`.

The op is registered under the natural key
`(product="mail", version="1.x", impl_id="mail-smtp")`, so the wire
`connector_id` is `mail-smtp-1.x`, which round-trips through
`parse_connector_id` back to `("mail", "1.x", "mail-smtp")`.

Two consumers share one implementation:

- **Dispatch** — agents and operators reach `mail.send` through the
  normal `call_operation` path (policy + audit + broadcast).
- **Direct import** — the checks notifier (#2719,
  `meho_backplane.checks.notify`) imports `transport.send_email()` and
  calls it in-process on every claimed Dashboard rollup transition that
  crosses the Dashboard's configured floor. See
  `docs/codebase/checks-notifications.md`.

Both run the identical recipient-allowlist floor and return-failures
contract, because both are the same function. An empty
`MAIL_RECIPIENT_ALLOWLIST` therefore makes Dashboard notification inert
too, not just agent-initiated sends.

## Key types

- `transport.MailSendResult` — frozen dataclass `{sent: bool,
  reason: str | None}`; `sent=True` ⇔ `reason is None`.
- `transport.send_email(*, to, subject, body)` — the single shared
  async transport. Screens every recipient, refuses when unconfigured,
  builds the `email.message.EmailMessage`, runs the blocking
  `smtplib` session via `asyncio.to_thread` (the socket is bound to
  the thread that opened it, so the whole session lives in one
  `to_thread` hop).
- `allowlist.assert_recipient_allowed(address)` /
  `parse_recipient_allowlist()` — the recipient floor.
- `ops.mail_send(operator, target, params)` — the dispatch handler; a
  thin adapter returning the audit-visible payload.
- `ops.register_mail_typed_operations` — the registrar, queued by the
  package `__init__` via `register_typed_op_registrar`.

## Control flow

`mail.send(to, subject, body)`:

1. every recipient is screened by `assert_recipient_allowed` **before
   any SMTP connection opens** — all-or-nothing (one refused recipient
   refuses the whole send, keeping the audit answer exact);
2. an empty `MAIL_SMTP_HOST` or `MAIL_FROM` refuses with
   `reason="smtp_unconfigured"`;
3. the `EmailMessage` is built (plain text only) and the blocking SMTP
   session runs on a worker thread: connect → `ehlo` → (`starttls` +
   re-`ehlo` when enabled) → (`login` when a username is configured) →
   `send_message` with an explicit envelope → `quit`;
4. the handler returns `{sent, reason, to, subject}` — the durable
   audit row's `raw_payload` records **who was mailed about what** and
   never the message body.

**TLS shape (RFC 8314):** port 465 selects implicit TLS
(`smtplib.SMTP_SSL` — TLS from the first byte, `starttls` moot); any
other port opens plaintext and upgrades via `starttls()` + re-`ehlo()`
when `MAIL_SMTP_STARTTLS` is set (default true).

**Credentials never ride a cleartext socket.** `_send_sync` carries an
`encrypted` flag — true from construction under implicit TLS, true
again once `starttls()` returns — and `login()` is gated on it.
`smtplib.login()` imposes no TLS precondition of its own, so
`MAIL_SMTP_STARTTLS=false` on a non-465 port with `MAIL_SMTP_USERNAME`
set would otherwise put base64-wrapped (not encrypted) credentials on
the wire. That combination is refused with
`reason="smtp_auth_requires_tls"` instead — the same posture as
Prometheus Alertmanager's `require_tls`, which defaults to true. The
flag is checked at the `login()` call site rather than pre-computed
from `starttls`/`port`, so a future change to how TLS is established
cannot leave the guard behind.

**Return-failures contract** (mould parity with `net.*`): a refused,
unconfigured, or delivery-failed send is the **product**, not an error
— dispatch `status="ok"` with `sent=false` and a stable reason code:

| reason | trigger |
|---|---|
| `not_in_recipient_allowlist` | a recipient failed the floor (incl. empty allowlist / unparseable address) |
| `smtp_unconfigured` | `MAIL_SMTP_HOST` or `MAIL_FROM` unset |
| `smtp_connect_error` | `SMTPConnectError` or socket-level `OSError` (refused / DNS / timeout) |
| `smtp_auth_requires_tls` | a username is configured but the channel is still cleartext |
| `smtp_auth_error` | `SMTPAuthenticationError` |
| `smtp_recipients_refused` | `SMTPRecipientsRefused` (server rejected every recipient) |
| `smtp_error` | any other `SMTPException` |

The `except` ordering in `transport._send_sync` is load-bearing:
`SMTPException` subclasses `OSError` (since Python 3.4), so the SMTP
arms must precede the socket-level `OSError` arm.

## Configuration

One deployment-level SMTP block (no per-tenant config, #2717 scope),
all in `Settings` (`settings.py`, env-mapped in `get_settings`):

| env var | default | meaning |
|---|---|---|
| `MAIL_SMTP_HOST` | `""` | MTA host; empty ⇒ transport unconfigured |
| `MAIL_SMTP_PORT` | `587` | 465 ⇒ implicit TLS (`SMTP_SSL`) |
| `MAIL_SMTP_STARTTLS` | `true` | upgrade via STARTTLS on non-465 ports; off + a username ⇒ `smtp_auth_requires_tls` |
| `MAIL_SMTP_USERNAME` | `""` | `login()` runs only when set, and only on an encrypted channel |
| `MAIL_SMTP_PASSWORD` | `""` | `repr=False`; never logged |
| `MAIL_FROM` | `""` | From header + envelope sender; empty ⇒ unconfigured |
| `MAIL_RECIPIENT_ALLOWLIST` | `""` | the hard floor; empty ⇒ inert |

**Allowlist semantics** (inverted default, like
`MEHO_NETDIAG_PROBE_ALLOWLIST`): comma-separated full addresses
(`oncall@example.com`) and/or domains (`example.com`, leading `@`
accepted) naming the **entire** permitted recipient space. Matching is
case-insensitive on the whole address; a domain entry does **not**
match subdomains. An address the parser cannot positively read as
`local@domain` is refused — which also makes SMTP envelope injection
(CR/LF in a recipient) structurally impossible past the gate. A
*subject* carrying a line break is rejected one layer earlier, by
`mail.send`'s parameter schema (`ops._SINGLE_LINE_PATTERN`), so the
dispatch returns the structured validation error (`status="error"`,
`extras["error_code"] == "invalid_params"`). The pattern excludes every
character `str.splitlines()` treats as a boundary — not just CR/LF, but
also VT, FF, the information separators, NEL, and U+2028/U+2029 —
because that is exactly the set `EmailMessage` refuses
(`len(value.splitlines()) > 1`). Rejecting it at the schema keeps the
return-failures contract intact: were the value to reach header
assignment it would raise `ValueError` and surface as a
`connector_error`. The screen is the schema, so it covers every
dispatched call but not the direct-import path — an in-process caller
such as the #2719 notifier is responsible for composing a single-line
subject. The checks notifier does exactly that: it folds every control
character out of the operator-authored Dashboard name before building
its subject (`checks/notify.py::_single_line`, see
`docs/codebase/checks-notifications.md`).

Malformed allowlist entries raise loudly at parse time rather than
being silently kept: whitespace, more than one `@`, an empty local part
or domain part (`foo@`, `@`), or a domain that folds away to nothing
(`...`). A bare `@` is the case worth naming — it used to parse to
`domains={""}`, matching no address while making the allowlist look
non-empty, so a typo that had disabled every send reported as an
ordinary "not listed" refusal instead of the "connector is inert"
diagnostic.

## Safety posture

`safety_level="caution"` + `requires_approval=False`: human/service
principals default-allow (operators and the checks notifier auto-run),
while agent principals land on the policy gate's `caution` ⇒
needs-approval default — and the ceiling keeps a permission row from
loosening past needs-approval (`auth/permissions.py`). The recipient
allowlist is the hard floor on every path either way.

`classify_op("mail.send")` falls through to `other` (full-detail
broadcast) — the broadcast payload carries recipients + subject, which
are audit-visible by design.

## Dependencies

Stdlib only: `smtplib` + `email.message.EmailMessage` +
`asyncio.to_thread` (no `aiosmtplib`; blocking SMTP on a worker thread
is fine at alert-mail volume). Structured logging via `structlog` —
events carry host/port/reason only, never the password or body.

## Known issues

- The SMTP session timeout is a module constant
  (`transport._SMTP_TIMEOUT_SECONDS`, 30 s), not a settings knob — no
  consumer has asked to tune it (#1177 substrate minimalism).
- No HTML, attachments, templating, or per-tenant SMTP — explicitly out
  of scope for #2717; further transports (Slack/PagerDuty/webhook) are
  new connectors when a consumer asks.

## References

- `backend/src/meho_backplane/connectors/mail/` — package
  (`transport.py`, `allowlist.py`, `ops.py`).
- `backend/tests/test_connectors_mail.py` — contract coverage.
- Mould: `backend/src/meho_backplane/connectors/net/` +
  [`connectors-net-diagnostics.md`](connectors-net-diagnostics.md).
- Task #2717; Initiative #2716 (checks alerting delivery); notifier
  consumer #2719.
- Python 3.14 `smtplib`:
  <https://docs.python.org/3.14/library/smtplib.html>; RFC 8314
  (implicit TLS on 465); RFC 3207 (STARTTLS re-EHLO).
