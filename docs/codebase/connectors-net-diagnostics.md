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
`net.tcp_check` plus the three foundations every sibling op reuses, and
the sibling ops `net.tls_inspect` (#2407, T2), `net.http_probe` (#2408,
T3), `net.dns_lookup` (#2409, T4), `net.ntp_check` (#2410, T5), and the
ICMP cohort `net.ping` / `net.trace` / `net.path_mtu` (#2411, T6) built on
that mold — all described below.

Each op queues a registrar in `__init__.py` via
`register_typed_op_registrar` (`net.tcp_check` and `net.dns_lookup` share
`ops.register_net_typed_operations`; `net.tls_inspect` →
`tls.register_net_tls_inspect_operation`; `net.http_probe` →
`http_probe.register_net_http_probe_operations`; `net.ntp_check` →
`ntp.register_net_ntp_check_operation`). Siblings therefore extend the
package by adding (at most) a module and one registrar-queue line rather
than contending for a single registrar function — which also keeps
parallel task branches from colliding on one shared file.

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

## `net.ntp_check` — clock offset/skew + stratum (T5, #2410)

`net.ntp_check(host, port=123, timeout_seconds?)` sends **one** mode-3
(client) NTPv4 packet to `host:port` over an **unprivileged** client UDP
socket and reports reachability plus the queried server's **clock offset
and skew against the backplane's own clock** (RFC 5905 §8), the
`stratum`, `ref_id`, `root_delay_ms` / `root_dispersion_ms`, and the
`leap` indicator — `ntpdate -q` / `sntp` parity. Clock skew is a common
root cause of TLS-cert-validity and Kerberos/auth failures, so "is this
appliance's clock sane from here" is a real pre-flight / post-mortem
read. It is **read-only**: it reads the time, it never sets a clock.

Lives in `connectors/net/ntp.py` with its own registrar
(`register_net_ntp_check_operation`), queued alongside `net.tcp_check`'s
in the package `__init__`. It reuses the same three foundations and the
shared timeout bounds/clamp from `ops.py`.

**No dependency — stdlib only.** A mode-3 SNTP request is a fixed 48-byte
packet (RFC 5905 §7.3): a first octet of `0x23` (leap 0, version 4, mode
3) and a transmit timestamp; the reply is the same 48-byte header.
`struct` (`"!B B b b 11I"`) builds and parses it; `asyncio`'s
`loop.create_datagram_endpoint` sends and receives it off the event loop.
No raw socket and no added pod capability — a client UDP socket needs
none.

Control flow:

1. `assert_probe_allowed(host)` — the T1 allowlist floor, before any
   socket opens.
2. A one-shot `DatagramProtocol` (`_NtpClientProtocol`) stamps the
   transmit time `t1` as close to the send as possible, fires the packet,
   and stamps the receive time `t4` on the reply. The exchange is bounded
   by `asyncio.wait_for(timeout)`.
3. `_parse_reply` checks the reply echoes our transmit timestamp in its
   origin field (an off-path / stale packet fails this → `invalid_response`),
   then computes offset and round-trip per RFC 5905 §8 with `t1`/`t4` on
   the backplane clock and the server's receive (`T2`) / transmit (`T3`)
   timestamps on the server clock:

   ```
   offset     = ((T2 - t1) + (T3 - t4)) / 2
   round_trip = (t4 - t1) - (T3 - T2)
   ```

Timestamps convert via the NTP epoch offset (`2_208_988_800` s between
1900 and 1970) and a 2**32 fraction scale; `root_delay` / `root_dispersion`
are NTP 16.16 fixed-point "short" values (`/2**16` seconds → ms). `ref_id`
renders as a 4-char ASCII refclock code at stratum ≤1 and the upstream
server's dotted IPv4 at stratum ≥2.

**Return-failures:** a timeout, DNS failure, a refused/unreachable peer,
a malformed or origin-mismatched reply, or a **kiss-o'-death** (stratum-0)
packet return `{reachable: false, reason}` with `status="ok"` — reason
codes extend the T1 set with `kod` and `invalid_response`. A KoD reply
also surfaces the 4-char `kiss_code` (e.g. `RATE`, `DENY`). None are
raised as `connector_*` errors; the `host`/`port` in the return dict are
audit-visible via `raw_payload`.

## `net.tls_inspect` — full presented certificate chain (T2, #2407)

`net.tls_inspect(host, port, server_name?, timeout_seconds?)` opens a TLS
handshake with **certificate verification off** and reports the **full
chain the server presented** — `openssl s_client -showcerts` parity. It
inspects; it never verifies (self-signed appliances are the point), so a
self-signed / expired / hostname-mismatched cert is **reported**, never
rejected.

Why pyOpenSSL and not stdlib `ssl`: on the `requires-python` floor (3.12)
`ssl.SSLSocket.getpeercert` returns only the **leaf**; the full-chain
`ssl.SSLSocket.get_unverified_chain` is 3.13+. pyOpenSSL's
`Connection.get_peer_cert_chain(as_cryptography=True)` (the
`as_cryptography` kwarg landed in pyOpenSSL 24.3) returns the whole
presented chain as `cryptography.x509.Certificate` objects directly, so
the parse reuses the `cryptography` x509 API.

Control flow (`connectors/net/tls.py`):

1. `assert_probe_allowed(host)` — the T1 allowlist floor, before any
   socket opens.
2. `asyncio.to_thread(_blocking_tls_inspect, ...)` — the blocking
   socket + handshake runs off the event loop. The context is
   `SSL.Context(TLS_CLIENT_METHOD)` with `set_verify(VERIFY_NONE)`; SNI
   is set from `server_name` (default `host`) **only for hostnames**
   (RFC 6066 forbids an IP-literal SNI).
3. Because a socket with a timeout is non-blocking under the hood,
   pyOpenSSL raises `WantReadError` / `WantWriteError`; `_run_until_ready`
   drives `do_handshake` (and the best-effort `shutdown`) to completion
   by `select`-waiting under a single `deadline` derived from the
   clamped timeout, so a stalled peer cannot pin the worker thread.
4. Each presented cert is flattened to
   `{subject, issuer, san, not_before, not_after, serial, self_signed}`
   (leaf-first). `serial` is stringified (a serial is a large integer a
   JSON number consumer would truncate); timestamps use the tz-aware
   `not_valid_*_utc` accessors.

Derived top-level fields:

- `leaf` — convenience alias for `chain[0]`; `not_after` — the leaf's,
  for the common "is it expiring" read.
- `hostname_match` — computed **independently** of the (disabled) stack
  verification: `server_name` vs the leaf SAN dNSNames (wildcard-aware,
  RFC 6125 single-leftmost-label), SAN iPAddresses for an IP
  `server_name`, falling back to the subject CN only when the cert
  carries no SAN dNSName (legacy appliances).
- `chain_complete` — whether the **last** presented cert is self-signed
  (i.e. the server sent a root), not a trust decision.

It shares the same synthetic natural key as `net.tcp_check`
(`net-probe-1.x`), reuses the return-failures contract (a refused /
timed-out / DNS-failed / non-TLS endpoint returns `handshake=false` with
a reason code — `not_in_probe_allowlist` / `timeout` / `refused` /
`dns_failure` / `unreachable` / `tls_error` — and `status="ok"`), and is
`safety_level="safe"` + `requires_approval=False`. A completed handshake
sends **no** application bytes.

## ICMP cohort — `net.ping` / `net.trace` / `net.path_mtu` (T6, #2411)

The ICMP cohort completes local-tool parity (`ping` / `traceroute` /
`tracepath`) so a local agent never drops to the shell for path
diagnosis. All three live in `connectors/net/icmp.py` behind one
registrar (`register_net_icmp_operations`), share the `net-probe-1.x`
identity, and reuse the three T1 foundations. The blocking socket work
runs off the event loop via `asyncio.to_thread`. IPv4-only, Linux-only in
v1.

The load-bearing decision is the **pod-security posture** (resolved
2026-07-12): reading ICMP replies/errors normally needs `CAP_NET_RAW`,
but that capability is deliberately **not** granted to the
credential-holding backplane pod. The cohort uses only unprivileged Linux
mechanisms, and the ABI constants (`IP_RECVERR`, `IP_MTU_DISCOVER`,
`IP_MTU`, `IP_PMTUDISC_DO`, ICMP types, `sock_extended_err` layout) are
pinned as module-level integers because the stdlib `socket` module does
not expose them.

- **`net.trace` + `net.path_mtu` — fully unprivileged via `IP_RECVERR`.**
  A connected UDP socket with increasing `IP_TTL` (trace) or
  `IP_PMTUDISC_DO` (path_mtu) reads the ICMP `TimeExceeded` /
  fragmentation-needed replies off the socket **error queue**
  (`recvmsg(MSG_ERRQUEUE)`). Readiness is polled with `poll()` on
  `POLLERR` — a socket error queue does **not** wake `select`'s
  exceptional set (that is TCP out-of-band data), so `select` would block
  the full per-hop timeout even when the error is already queued. No pod
  capability, no sysctl — works on any cluster.
- **`net.ping` (ICMP echo) — best-effort, degrades gracefully.** Opens an
  unprivileged `IPPROTO_ICMP` **datagram** socket (permitted only when the
  pod's GID is inside `net.ipv4.ping_group_range`). Where it is not, the
  socket raises `PermissionError` on creation and the op returns
  `{available: false, reason: "icmp_echo_unprivileged_unavailable"}`
  (`status="ok"`), pointing the caller at `net.tcp_check` — it **degrades,
  never crashes**, and never forces a capability grant.

Result shapes (all `status="ok"`, all audit-visible host via
`raw_payload`):

- `net.ping(host, count?, timeout_seconds?)` →
  `{available, reachable, reason, packets_sent, packets_received, rtt_ms:
  {min,avg,max}|null, host}`.
- `net.trace(host, port?, max_hops?, hop_timeout_seconds?)` →
  `{completed, reason, reached, hops: [{ttl, address|null, rtt_ms|null}],
  host, port}` — `address=null` is a silent `*` hop; an ICMP
  `DestUnreachable` (type 3) marks arrival at the destination, a
  `TimeExceeded` (type 11) an intermediate router.
- `net.path_mtu(host, port?, timeout_seconds?)` →
  `{available, mtu|null, reason, host, port}` — sends DF-set datagrams and
  reads the converged next-hop MTU from `getsockopt(IP_MTU)`.

Per-op bounds are clamped in the handler and mirrored as schema maxima:
ping `count ≤ 10`; trace `max_hops ≤ 64`, `hop_timeout ≤ 5s`, plus a
`_TRACE_HARD_WALL_SECONDS` overall ceiling; timeouts `≤ 5s`. On a
non-Linux host or a kernel that rejects the socket options, trace/path_mtu
return `completed:false` / `available:false` with a
`*_mechanism_unavailable` reason rather than raising.

### Chart posture (`deploy/charts/meho/`)

No pod-security change ships by default: `trace`/`path_mtu` are
unprivileged everywhere and `ping` degrades where the sysctl is absent.
The chart adds an **optional, default-off** `netdiag.pingGroupRange`
value that, when set to a `"<low> <high>"` GID range, renders a
`net.ipv4.ping_group_range` **pod sysctl** into the Deployment's
`securityContext.sysctls`. It is documented with a security note in
`values.yaml`: the range must include the pod's running GID, the sysctl
is *safe* on Kubernetes 1.29+ but *unsafe* (needs a kubelet allowlist) on
older clusters, and enabling it only widens who may open unprivileged
ICMP datagram sockets — it never grants `CAP_NET_RAW`. Empty (default)
renders no sysctl. A privileged `CAP_NET_RAW` sidecar remains the
documented escalation path (not implemented).

The probe allowlist itself is a first-class chart value:
`netdiag.probeAllowlist` in `values.yaml` renders into
`MEHO_NETDIAG_PROBE_ALLOWLIST` on the backplane ConfigMap
(`templates/configmap.yaml`), which the Deployment injects into the
container via `envFrom.configMapRef` — so a populated value reaches
`connectors/net/allowlist.py` with no `extraEnv` escape hatch. It is
schema-typed in `values.schema.json` as a plain optional string:
deliberately absent from any `required` list and carrying no `minLength`,
so the safe default `""` validates on every install (and surfaces under
`helm show values`). That default renders `MEHO_NETDIAG_PROBE_ALLOWLIST: ""`,
which — given the inverted allow-only-what-is-listed semantics — keeps
the connector **inert** (every probe denied). This mirrors the typed
`config.targetSsrfAllowlist` treatment (#2240) so both fail-closed
allowlists share one configuration shape, differing only in default
posture. Chart-only — the allowlist parser is untouched.

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

- `connectors/net/__init__.py` — queues each op registrar
  (`register_net_typed_operations`, `register_net_tls_inspect_operation`,
  `register_net_http_probe_operations`) onto the lifespan registrar list
  via `register_typed_op_registrar` (auto-imported by the
  `_eager_import_connectors` package walk).
- `connectors/net/ops.py` — the `net_tcp_check` and `net_dns_lookup`
  handlers, their parameter / response schemas, the shared registrar
  (`register_net_typed_operations` upserts both ops), and the shared
  timeout bounds/clamp (`_DEFAULT_TIMEOUT_SECONDS` /
  `_MAX_TIMEOUT_SECONDS` / `_clamp_timeout`) reused by `tls.py`
  (package-internal import; `ops` never imports `tls`, so no cycle).
- `connectors/net/tls.py` — `net_tls_inspect` handler, its schemas, the
  cert-flattening / hostname-match / chain helpers, and its registrar.
- `connectors/net/http_probe.py` — `net_http_probe` handler, its
  parameter / response schema, the manual redirect walk
  (`_walk_redirects`), the TLS summary (`_tls_summary`), the
  size/hash-only body reader (`_consume_body_size_and_hash`), and the
  registrar.
- `connectors/net/ntp.py` — `net_ntp_check` handler, its schemas, the
  48-byte packet build/parse (`_build_request` / `_parse_reply`), the
  NTP-timestamp codec, the one-shot `_NtpClientProtocol`, and the
  registrar.
- `connectors/net/icmp.py` — the `net_ping` / `net_trace` /
  `net_path_mtu` handlers, their schemas, the low-level errqueue / ICMP
  primitives (`_checksum`, `_build_echo_request`, `_parse_extended_err`,
  `_drain_icmp_error`), the pinned Linux ABI constants, and the shared
  registrar (`register_net_icmp_operations` upserts all three).
- `connectors/net/allowlist.py` — `PROBE_ALLOWLIST_ENV`,
  `parse_probe_allowlist`, `assert_probe_allowed`,
  `ProbeNotAllowedError`.

Dispatch path: `dispatch(connector_id="net-probe-1.x",
op_id="net.tcp_check", target=None, params=...)` → param-schema
validation → module-level handler (`connector_instance=None`) → redact →
audit (`raw_payload` = handler return) → broadcast (`read` class).

## Dependencies

- `net.tcp_check` and the allowlist: standard library only (`asyncio`,
  `socket`, `ipaddress`, `time`).
- `net.tls_inspect`: `pyOpenSSL` (full presented chain via
  `get_peer_cert_chain`) + `cryptography` (x509 parsing) — both Apache-2.0
  (Python License Check clean). `pyOpenSSL` was promoted to a runtime
  dependency and `cryptography` from a dev-only + transitive install to a
  declared runtime dependency (the handler imports `cryptography.x509`
  directly).
- `net.http_probe`: `httpx` (already a project dependency) for the HTTP
  request and its `network_stream` TLS extension — no **new** runtime
  dependency is added.
- `net.dns_lookup`: **dnspython** (ISC-licensed, already present
  transitively via `email-validator` / `pymongo`; pinned direct in
  `backend/pyproject.toml` so the connector's requirement is explicit) —
  used via `dns.asyncresolver` / `dns.reversename` / `dns.rdatatype` /
  `dns.flags`.
- `net.ntp_check`: standard library only (`asyncio`, `socket`, `struct`,
  `time`) — the SNTP request/reply is a fixed 48-byte packet, so no
  runtime dependency is added.
- `net.ping` / `net.trace` / `net.path_mtu`: standard library only
  (`asyncio`, `socket`, `select`, `struct`, `errno`, `os`, `time`) — no
  new runtime dependency. The Linux socket ABI constants the stdlib does
  not surface are pinned as module-level integers.

## Known issues / deferred

- `net.tls_inspect` **inspects, never verifies** — trust-store
  verification, OCSP/CRL/revocation, and client-cert / mTLS presentation
  are explicitly out of scope (#2407). `chain_complete` is a
  "did the server send a self-signed root" signal, not a trust decision.
- No port-scoped allowlist (v1 scopes hosts only).
- Hostname allowlisting is verbatim; an operator who allowlists a CIDR
  cannot probe a hostname that merely resolves into it (list the name).
- Egress rate-limiting and raw-socket ops (D3) are deferred.
- The ICMP cohort is IPv4-only and Linux-only in v1. `net.ping` degrades
  to `available:false` where `net.ipv4.ping_group_range` excludes the
  pod's GID; a `CAP_NET_RAW` sidecar for ping-everywhere is the documented
  escalation path, not implemented (#2411). `net.path_mtu` reports the
  converged next-hop MTU from the kernel PMTU cache; a full per-hop MTU
  walk (tracepath's `+mtu` detail) is out of scope.
- `safety_level="safe"` (agent-auto-runnable) is the chosen posture; the
  reviewed alternative `"caution"` (operators auto-run, agents do not)
  is a one-line change if a security review prefers it.

## References

- Parent: Initiative #2405, Tasks #2406 (T1 `tcp_check`) + #2407 (T2
  `tls_inspect`) + #2408 (T3 `http_probe`) + #2409 (T4 `dns_lookup`) +
  #2410 (T5 `ntp_check`) + #2411 (T6 ICMP cohort
  `ping`/`trace`/`path_mtu`). RFC 5905 (NTPv4) for the `ntp_check` packet
  and offset/delay math. Mold: secret broker
  (`docs/codebase/connectors-secret-broker.md`). SSRF sibling:
  `docs/codebase/target-ssrf-guard.md`. Broadcast taxonomy:
  `docs/codebase/broadcast.md`.
