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
`tls_inspect` / `http_probe` / `dns_lookup`) reuses.

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

## `net.http_probe` (T3, #2408)

`net.http_probe(url, method=HEAD|GET, timeout_seconds?)` issues a
**single** HTTP request from the backplane and reports the
reachability/identity surface — `status`, response `headers`, the
`redirect_chain`, a `tls` summary, `timing_ms`, `final_url`, and the
body's `body_size` / `body_sha256` — but **never the response body**. It
is a reachability/identity probe, not a fetch/exfil path (the anti-exfil
floor). Lives in `connectors/net/http_probe.py` with its own registrar
(`register_net_http_probe_operations`), queued alongside
`net.tcp_check`'s registrar in the package `__init__`.

It reuses the same three foundations plus two probe-specific rules:

1. **Fresh client, manual redirects.** Unlike the target-coupled
   `HttpConnector` (per-target pooled client), the op builds a fresh
   `httpx.AsyncClient(follow_redirects=False)` per call and walks
   redirects itself — httpx's own follower would dial the next host
   *before* the allowlist could see it.
2. **Per-hop redirect re-gating (open-redirect SSRF floor).** Every
   redirect hop's host is passed through `assert_probe_allowed` **before
   it is followed**. A redirect to a non-allowlisted host (e.g. a
   cloud-metadata / credential host) halts the walk with
   `{"reachable": true, "reason": "blocked_redirect",
   "blocked_redirect": "<host>"}` and is **never dialed** — the concern
   noted at `adapters/http.py:260`. The redirect count is bounded
   (`_MAX_REDIRECTS`) and the whole walk runs under one
   `asyncio.wait_for(timeout)` ceiling.

The body is streamed chunk-by-chunk only to accumulate a running length
and SHA-256 (`_consume_body_size_and_hash`); the full body is never
materialised or returned. The `tls` summary is read off httpx's
`network_stream` response extension (`ssl_object`) before the stream is
consumed — negotiated version, cipher, ALPN, and the peer cert's
subject/issuer/`notAfter` (public identity, never private material);
`null` for plain HTTP. `method` is restricted to `HEAD`/`GET` by a schema
`enum`, so the dispatcher rejects anything else with `invalid_params`
before the handler runs. Reason codes extend the T1 set with
`invalid_url`, `blocked_redirect`, `too_many_redirects`, and `tls_error`.

Because it issues an HTTP request, `net.http_probe` adds **`httpx`** to
the family's dependency surface (already a project dep — no new dep).

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
  **and** `register_net_http_probe_operations` onto the lifespan
  registrar list via `register_typed_op_registrar` (auto-imported by the
  `_eager_import_connectors` package walk). Each sibling op has its own
  registrar so the family extends without editing a shared function body.
- `connectors/net/ops.py` — `net_tcp_check` handler, its parameter /
  response schema, and the registrar.
- `connectors/net/http_probe.py` — `net_http_probe` handler, its
  parameter / response schema, the manual redirect walk
  (`_walk_redirects`), the TLS summary (`_tls_summary`), the
  size/hash-only body reader (`_consume_body_size_and_hash`), and the
  registrar.
- `connectors/net/allowlist.py` — `PROBE_ALLOWLIST_ENV`,
  `parse_probe_allowlist`, `assert_probe_allowed`,
  `ProbeNotAllowedError`.

Dispatch path: `dispatch(connector_id="net-probe-1.x",
op_id="net.tcp_check", target=None, params=...)` → param-schema
validation → module-level handler (`connector_instance=None`) → redact →
audit (`raw_payload` = handler return) → broadcast (`read` class).

## Dependencies

`net.tcp_check` and the allowlist are standard library only (`asyncio`,
`socket`, `ipaddress`, `ssl`, `time`). `net.http_probe` additionally uses
`httpx` (already a project dependency) for the HTTP request and its
`network_stream` TLS extension — no **new** runtime dependency is added.

## Known issues / deferred

- No port-scoped allowlist (v1 scopes hosts only).
- Hostname allowlisting is verbatim; an operator who allowlists a CIDR
  cannot probe a hostname that merely resolves into it (list the name).
- Egress rate-limiting and raw-socket ops (D3) are deferred.
- `safety_level="safe"` (agent-auto-runnable) is the chosen posture; the
  reviewed alternative `"caution"` (operators auto-run, agents do not)
  is a one-line change if a security review prefers it.

## References

- Parent: Initiative #2405, Task #2406. Mold: secret broker
  (`docs/codebase/connectors-secret-broker.md`). SSRF sibling:
  `docs/codebase/target-ssrf-guard.md`. Broadcast taxonomy:
  `docs/codebase/broadcast.md`.
