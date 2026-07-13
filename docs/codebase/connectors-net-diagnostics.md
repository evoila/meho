# Network diagnostics connector (`connectors/net`)

## Overview

The `net.*` connector gives the backplane a **network vantage**: ops
that open a socket toward an operator-named `host:port` to answer
reachability and connectivity questions ("can we reach the database on
5432 from here?", "is 443 open on the load balancer?"). It is the second
**synthetic** connector after the secret broker — no vendor connector
backs it, there is no `Connector` subclass, and the package calls neither
`register_connector` nor `register_connector_v2`. The handlers are
module-level functions the dispatcher routes to with
`connector_instance=None` / `target=None`; the probe destination is a
**param**, not a registered `Target`.

This page describes the keystone (#2406, Initiative #2405 T1):
`net.tcp_check` plus the three foundations every sibling op (T2–T4:
`tls_inspect` / `http_probe` / `dns_lookup`) reuses, and the T4 op
`net.dns_lookup` (#2409) built on that mold.

## Core mechanism

`net.tcp_check(host, port, timeout_seconds?)`:

1. screens `host` against the probe allowlist (`assert_probe_allowed`)
   **before** any socket opens;
2. `asyncio.open_connection(host, port)` under `asyncio.wait_for` with a
   bounded timeout;
3. measures the connect latency and closes the connection immediately;
4. returns a structured dict — always, whether the connect succeeded or
   failed.

The op is registered under the natural key
`(product="net", version="1.x", impl_id="net-probe")`, so the wire
`connector_id` is `net-probe-1.x`, which round-trips through
`parse_connector_id` back to `("net", "1.x", "net-probe")`.
`safety_level="safe"` + `requires_approval=False` make it
agent-auto-runnable and ungated.

## `net.dns_lookup` — full `dig` parity (#2409)

`net.dns_lookup(name, type?, resolver?, timeout_seconds?)` resolves DNS
from the backplane's vantage via **dnspython** (`dns.asyncresolver`,
which runs the query off the event loop natively — no
`asyncio.to_thread` wrapper). It is the same synthetic, targetless op
under the same `net-probe-1.x` connector, registered alongside
`net.tcp_check` by `register_net_typed_operations`.

- **Forward**: `type` (default `A`, one of
  A/AAAA/CNAME/MX/TXT/SRV/NS/SOA/PTR) drives `resolve(name, type)`.
- **Reverse**: when `name` is an IP literal, the `type` is ignored and a
  PTR lookup runs via `dns.reversename.from_address` (mirrors `dig -x`).
- **Chosen resolver**: an optional `resolver` IP sets
  `resolver.nameservers = [resolver]`, so an operator can compare "what
  the pod's resolver returns" against an authoritative/other nameserver
  — the split-horizon case that forced registering a vCenter by IP. A
  non-IP `resolver` is rejected with `reason="bad_resolver"` (dnspython's
  `nameservers` setter accepts only IPs, and an unresolved name pins no
  server).

Success returns `{resolved: true, name, type, resolver: "system"|<ip>,
records: [{type, value, ttl}], authoritative, authenticated_data, reason:
null}`. `authoritative` is the answer's AA flag; `authenticated_data` is
the DNSSEC AD flag **reported, not validated** (chain validation, AXFR,
and `dig +trace` are out of scope, #2409).

**Gating (one guard, uniformly — #1177):** the queried `name` is passed
through `assert_probe_allowed` before any query (a hostname matched
verbatim, an IP by range), and a custom `resolver` IP is gated the same
way — querying an internal resolver or resolving internal names is itself
mild recon. Both must be allowlisted or the lookup is refused with
`reason="not_in_probe_allowlist"`.

**Return-failures:** `NXDOMAIN` / `NoAnswer` / `NoNameservers` (SERVFAIL)
/ `dns.exception.Timeout` map to `{resolved: false, reason:
nxdomain|no_answer|servfail|timeout}` with `status="ok"`; a missing
system resolver config with no chosen resolver maps to
`reason="no_resolver"`. None are raised as `connector_*` errors. The
`name`/`type`/`resolver` in the return dict are audit-visible via
`raw_payload`.

## The three foundations

### 1. Probe allowlist — `MEHO_NETDIAG_PROBE_ALLOWLIST` (inverted)

`connectors/net/allowlist.py` is a deliberate sibling of
`targets/ssrf_guard.py`, with the **opposite default**:

- The SSRF guard is *deny-non-public-minus-allowlist*: a destination is
  dialable unless it is non-public and not opted back in. Its default
  (unset) is permissive for public addresses.
- The probe allowlist is *allow-only-what-is-listed*: the parsed set is
  the **whole permitted probe space**, and **empty ⇒ deny everything**,
  so the connector is **inert** until an operator opts a range in.

That opposite default is why the two do not share a parser: reusing the
SSRF guard verbatim would carry its permissive-when-unset default into a
surface where unset must mean closed. Because `net.*` ops are
`safety_level="safe"` (agent-auto-runnable), this allowlist is the
**sole floor** on a network-vantage recon primitive.

Matching:

- an **IP literal** is allowed iff it falls inside a listed
  CIDR/network/bare-IP;
- a **hostname** is allowed iff it is listed **verbatim**
  (case-insensitive, trailing dot stripped). A name is *not*
  resolved-then-network-matched: a resolve-to-allow step would let a DNS
  answer that changed between the check and the dial (rebind) widen the
  permitted space. Verbatim matching keeps the floor fail-closed and
  TOCTOU-free.

There is no port dimension in v1 (the allowlist scopes hosts; the per-op
timeout bounds the probe). A port-scoped allowlist is a follow-up only
if an operator needs one (#1177: one closed-set config, no DSL).

### 2. Audit-visible host:port

Unlike `secret.move` (whose refs are secret-adjacent and are only
hashed), a probed `host:port` is not a secret and is the exact thing an
auditor needs. The dispatcher stores the handler's **return dict** as
the audit row's `raw_payload` verbatim; the handler therefore includes
the literal `host`/`port` in its return value so the durable row answers
"who probed what". (The op params themselves are only hashed into
`payload`.)

### 3. Return-failures contract

A refused, timed-out, or DNS-failed connect is the **product**, not an
error. The handler catches `asyncio.TimeoutError` / `socket.gaierror` /
`ConnectionRefusedError` / `OSError` and returns
`{"connected": false, "reason": <code>, "latency_ms": null, host, port}`
with the dispatch `status="ok"`. It never raises a `connector_*` error
for a failed connection — only an unexpected bug would propagate. Reason
codes: `not_in_probe_allowlist`, `timeout`, `refused`, `dns_failure`,
`unreachable`. This is the shared mold T2–T4 follow.

Exception-ordering note: `asyncio.TimeoutError` is the builtin
`TimeoutError` on Python 3.11+, and `TimeoutError`, `socket.gaierror`,
and `ConnectionRefusedError` are all `OSError` subclasses — so the
handler's `except` arms are ordered specific-before-general
(timeout → DNS → refused → generic `OSError`).

## Broadcast classification

`net.*` ops classify as `read` in the broadcast sensitivity taxonomy
(`broadcast/events.py::classify_op`). They are matched by a **product
prefix** arm (`op_id.startswith("net.")`), in the same shape as the
`audit.` / `approval.` / `GET:` arms — not by a verb suffix. A dotted
read suffix like `.check` cannot match: the last dotted segment of
`net.tcp_check` is `tcp_check` (underscore-joined), so
`"net.tcp_check".endswith(".check")` is `False`. The prefix arm keeps
the whole family classified `read` without polluting `_READ_SUFFIXES`
with underscore-bearing entries that would over-match unrelated
connectors' ops.

## Key types / control flow

- `connectors/net/__init__.py` — queues `register_net_typed_operations`
  onto the lifespan registrar list via `register_typed_op_registrar`
  (auto-imported by the `_eager_import_connectors` package walk).
- `connectors/net/ops.py` — the `net_tcp_check` and `net_dns_lookup`
  handlers, their parameter / response schemas, and the shared registrar
  (`register_net_typed_operations` upserts both ops).
- `connectors/net/allowlist.py` — `PROBE_ALLOWLIST_ENV`,
  `parse_probe_allowlist`, `assert_probe_allowed`,
  `ProbeNotAllowedError`.

Dispatch path: `dispatch(connector_id="net-probe-1.x",
op_id="net.tcp_check", target=None, params=...)` → param-schema
validation → module-level handler (`connector_instance=None`) → redact →
audit (`raw_payload` = handler return) → broadcast (`read` class).

## Dependencies

`net.tcp_check` is standard library only (`asyncio`, `socket`,
`ipaddress`, `time`). `net.dns_lookup` adds **dnspython** (ISC-licensed,
already present transitively via `email-validator` / `pymongo`; pinned
direct in `backend/pyproject.toml` so the connector's requirement is
explicit) — used via `dns.asyncresolver` / `dns.reversename` /
`dns.rdatatype` / `dns.flags`.

## Known issues / deferred

- No port-scoped allowlist (v1 scopes hosts only).
- Hostname allowlisting is verbatim; an operator who allowlists a CIDR
  cannot probe a hostname that merely resolves into it (list the name).
- Egress rate-limiting and raw-socket ops (D3) are deferred.
- `safety_level="safe"` (agent-auto-runnable) is the chosen posture; the
  reviewed alternative `"caution"` (operators auto-run, agents do not)
  is a one-line change if a security review prefers it.

## References

- Parent: Initiative #2405, Tasks #2406 (`tcp_check`) / #2409
  (`dns_lookup`). Mold: secret broker
  (`docs/codebase/connectors-secret-broker.md`). SSRF sibling:
  `docs/codebase/target-ssrf-guard.md`. Broadcast taxonomy:
  `docs/codebase/broadcast.md`.
