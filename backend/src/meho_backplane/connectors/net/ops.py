# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Network-diagnostics typed op ``net.tcp_check`` + its registrar.

``net.*`` is a **synthetic** typed-op product like ``secret.*``: no
vendor connector backs it, so the package calls neither
``register_connector`` nor ``register_connector_v2``. The op is
registered under the natural key
``(product="net", version="1.x", impl_id="net-probe")``, so the wire
``connector_id`` is ``net-probe-1.x`` — which round-trips through
:func:`~meho_backplane.operations._lookup.parse_connector_id` back to
``("net", "1.x", "net-probe")`` (digit-led version, product is the
head's first hyphen segment). The handler is a **module-level**
function, so the dispatcher resolves it with ``connector_instance=None``
and ``target=None`` — the probe target is a param, not a registered
``Target``.

``net.tcp_check`` opens a TCP connection to ``host:port`` under a
bounded timeout, measures the connect latency, and closes immediately.
Three foundations every later ``net.*`` op reuses land here:

* **Probe allowlist** — the handler calls
  :func:`~meho_backplane.connectors.net.allowlist.assert_probe_allowed`
  on the exact dialed host *before* opening a socket.
  ``MEHO_NETDIAG_PROBE_ALLOWLIST`` empty ⇒ every probe refused (the
  connector is inert).
* **Audit-visible host:port** — the handler's return dict carries the
  literal ``host``/``port`` (unlike ``secret.move``'s refs, a
  host:port is not a secret), so the durable audit row's
  ``raw_payload`` answers "who probed what". The dispatcher stores the
  handler's return value as ``raw_payload`` verbatim.
* **Return-failures contract** — a refused, refused-by-peer, timed-out,
  or DNS-failed probe is the **product**, not an error: the handler
  returns ``{"connected": false, "reason": <code>, ...}`` with the
  dispatch ``status="ok"``. It never raises a ``connector_*`` error for
  a failed connection. Only an unexpected bug would propagate.

``safety_level="safe"`` + ``requires_approval=False`` make the probe
agent-auto-runnable and ungated, so the probe allowlist is the sole
floor. The reviewed alternative (``"caution"`` — operators auto-run,
agents do not) is a one-line change if the security review prefers it.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import time
from typing import TYPE_CHECKING, Any, Final

import structlog

from meho_backplane.connectors.net.allowlist import (
    ProbeNotAllowedError,
    assert_probe_allowed,
)
from meho_backplane.operations.typed_register import register_typed_operation

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "NET_TCP_CHECK_PARAMETER_SCHEMA",
    "net_tcp_check",
    "register_net_typed_operations",
]

_log = structlog.get_logger(__name__)

#: Default connect timeout when the caller omits ``timeout_seconds``.
_DEFAULT_TIMEOUT_SECONDS: Final[float] = 5.0
#: Hard ceiling on the connect timeout — a probe must not become a way
#: to pin an event-loop task open indefinitely. Also the schema
#: ``maximum`` so the dispatcher rejects an over-long request before the
#: handler runs; the clamp is belt-and-suspenders for direct calls.
_MAX_TIMEOUT_SECONDS: Final[float] = 30.0

NET_TCP_CHECK_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "host": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Hostname or IP literal to probe. Must be covered by "
                "MEHO_NETDIAG_PROBE_ALLOWLIST or the probe is refused "
                "before any socket opens."
            ),
        },
        "port": {
            "type": "integer",
            "minimum": 1,
            "maximum": 65535,
            "description": "TCP port to attempt a connection to.",
        },
        "timeout_seconds": {
            "type": "number",
            "exclusiveMinimum": 0,
            "maximum": _MAX_TIMEOUT_SECONDS,
            "description": (
                "Connect timeout in seconds (default 5, max 30). A "
                "connection that does not complete in time returns "
                "connected=false with reason='timeout'."
            ),
        },
    },
    "required": ["host", "port"],
    "additionalProperties": False,
}

_NET_TCP_CHECK_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "connected": {
            "type": "boolean",
            "description": "True iff the TCP handshake completed within the timeout.",
        },
        "reason": {
            "type": ["string", "null"],
            "description": (
                "Null on success; otherwise a failure code: "
                "not_in_probe_allowlist, timeout, refused, dns_failure, unreachable."
            ),
        },
        "latency_ms": {
            "type": ["number", "null"],
            "description": "Connect latency in milliseconds on success; null on any failure.",
        },
        "host": {"type": "string", "description": "The probed host (audit-visible)."},
        "port": {"type": "integer", "description": "The probed port (audit-visible)."},
    },
    "required": ["connected", "reason", "latency_ms", "host", "port"],
    "additionalProperties": False,
}

_NET_TCP_CHECK_WHEN_TO_USE = (
    "Check whether a TCP port is reachable from the backplane's network "
    "vantage — e.g. 'can we reach the database on 5432?', 'is the load "
    "balancer's 443 open from here?'. A non-mutating reachability probe: "
    "it opens a connection, measures latency, and closes immediately. A "
    "failed connect (refused/timeout/DNS) is a normal result, not an "
    "error. The destination must be inside MEHO_NETDIAG_PROBE_ALLOWLIST."
)

_NET_TCP_CHECK_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Use to confirm TCP reachability of a host:port from the "
        "backplane before assuming a connectivity or firewall problem. "
        "Read-only: nothing is sent on the connection, it is closed "
        "right after the handshake."
    ),
    "parameter_hints": {
        "host": "Required. Hostname or IP literal. Must be allowlisted for probing.",
        "port": "Required. TCP port (1-65535).",
        "timeout_seconds": "Optional. Connect timeout (default 5, max 30).",
    },
    "output_shape": (
        "On success: {'connected': true, 'reason': null, 'latency_ms': "
        "<float>, 'host': <str>, 'port': <int>}. On a failed or refused "
        "probe: {'connected': false, 'reason': "
        "'<not_in_probe_allowlist|timeout|refused|dns_failure|unreachable>', "
        "'latency_ms': null, 'host': <str>, 'port': <int>} — still a "
        "successful (status=ok) op."
    ),
}


def _clamp_timeout(raw: Any) -> float:
    """Resolve ``timeout_seconds`` to a bounded float.

    The schema already bounds it for the dispatch path; this keeps a
    direct handler call (tests, other handlers) inside ``(0, MAX]`` too.
    """
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT_SECONDS
    if value <= 0:
        return _DEFAULT_TIMEOUT_SECONDS
    return min(value, _MAX_TIMEOUT_SECONDS)


def _refusal(host: str, port: int, reason: str) -> dict[str, Any]:
    """Build the structured failure payload shared by every failed probe."""
    return {
        "connected": False,
        "reason": reason,
        "latency_ms": None,
        "host": host,
        "port": port,
    }


async def net_tcp_check(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Attempt a TCP connection to ``host:port`` and report reachability.

    Op-id: ``net.tcp_check``. Synthetic typed op (no vendor connector,
    ``target`` is always ``None``). The dispatcher has validated the
    param schema, so ``host`` and ``port`` are present and well-typed.

    Flow: screen the host against the probe allowlist → open a
    connection under :func:`asyncio.wait_for` → measure latency → close.
    A refused/timed-out/DNS-failed connect returns a structured
    ``connected=false`` payload with ``status="ok"`` (the return-failures
    contract); the value is never raised as a ``connector_*`` error. The
    returned dict carries the literal ``host``/``port`` so the durable
    audit row's ``raw_payload`` records what was probed.
    """
    host = str(params["host"])
    port = int(params["port"])
    timeout = _clamp_timeout(params.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS))

    try:
        assert_probe_allowed(host)
    except ProbeNotAllowedError:
        _log.info("net.tcp_check.refused", host=host, port=port, reason="not_in_probe_allowlist")
        return _refusal(host, port, "not_in_probe_allowlist")

    started = time.perf_counter()
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except TimeoutError:
        # asyncio.wait_for raises builtin TimeoutError on timeout (3.11+).
        # Caught BEFORE OSError below since TimeoutError subclasses OSError.
        return _refusal(host, port, "timeout")
    except socket.gaierror:
        # DNS resolution failed (also an OSError subclass — catch first).
        return _refusal(host, port, "dns_failure")
    except ConnectionRefusedError:
        return _refusal(host, port, "refused")
    except OSError:
        # Network/host unreachable, no route, reset, etc.
        return _refusal(host, port, "unreachable")

    latency_ms = (time.perf_counter() - started) * 1000.0
    # Close the probe connection promptly; a failure to close cleanly
    # does not change the reachability answer we already have.
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()

    return {
        "connected": True,
        "reason": None,
        "latency_ms": latency_ms,
        "host": host,
        "port": port,
    }


async def register_net_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert the ``net.tcp_check`` typed op into ``endpoint_descriptor``.

    Queued onto the lifespan-driven registrar list by the package
    ``__init__`` (via ``register_typed_op_registrar``) and run by
    :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
    after the connector eager-import pass. Idempotent: a re-run against
    unchanged text is a no-op for the embedding pipeline. ``safe`` +
    ``requires_approval=False`` — the probe allowlist is the only floor.
    """
    await register_typed_operation(
        product="net",
        version="1.x",
        impl_id="net-probe",
        op_id="net.tcp_check",
        handler=net_tcp_check,
        group_key="probe",
        when_to_use=_NET_TCP_CHECK_WHEN_TO_USE,
        summary="Check whether a TCP host:port is reachable from the backplane.",
        description=(
            "Opens a TCP connection to a host:port under a bounded "
            "timeout, measures the connect latency, and closes "
            "immediately — a non-mutating reachability probe. The "
            "destination must be inside MEHO_NETDIAG_PROBE_ALLOWLIST or "
            "the probe is refused before any socket opens (empty "
            "allowlist ⇒ the connector is inert). A refused, timed-out, "
            "or DNS-failed connect returns connected=false with a reason "
            "code and status=ok — a failed probe is the product, never a "
            "connector error."
        ),
        parameter_schema=NET_TCP_CHECK_PARAMETER_SCHEMA,
        response_schema=_NET_TCP_CHECK_RESPONSE_SCHEMA,
        tags=["net", "probe", "read", "diagnostics"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=_NET_TCP_CHECK_LLM_INSTRUCTIONS,
        embedding_service=embedding_service,
    )
