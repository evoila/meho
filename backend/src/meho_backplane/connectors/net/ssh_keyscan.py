# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Network-diagnostics typed op ``net.ssh_keyscan`` + its registrar.

A ``net.*`` sibling probe on the T1 keystone (``net.tcp_check``). It
performs an ``ssh-keyscan``-parity read: open the SSH transport
**handshake** to ``host:port`` — key exchange only, **no
authentication** and **no application bytes** — read the server's
presented host key(s), and return each key's type, base64 blob, SHA256
(and legacy MD5) fingerprint, and a ready-to-paste OpenSSH
``known_hosts`` line, plus the joined ``known_hosts`` block.

**Why this op exists.** ``SshConnector`` verifies host keys fail-closed:
an SSH-transport target with no ``known_hosts`` pin in its Vault secret
refuses to connect at all (see
:mod:`meho_backplane.connectors.adapters.ssh`). Before this op the only
way to obtain that pin was to run ``ssh-keyscan`` on an operator shell
with direct reach to the target — an out-of-band, ungoverned step. This
op mints the pin **through the backplane**, on the same policy / audit /
broadcast dispatch path every operation uses. Like ``ssh-keyscan`` it
trusts whoever answers on the wire, so the ``sha256_fingerprint`` must be
verified out-of-band before the pin is trusted; the ``note`` field says so.

**Why asyncssh, not a second SSH library.** asyncssh is already the
backplane's SSH transport (every SSH-based connector inherits
:class:`~meho_backplane.connectors.adapters.ssh.SshConnector`), and it
exposes :func:`asyncssh.get_server_host_key` — a coroutine that connects,
completes the SSH handshake, and returns the server host key it
presented **without authenticating and without verifying** it. That is
exactly the keyscan primitive, native-async, no new dependency, and no
credential ever offered. Restricting ``server_host_key_algs`` to one key
type per call yields that type's host key (the transport negotiates a
single host key per connection), so one handshake per requested type
collects the full set — the same shape ``ssh-keyscan`` uses.

This op inherits the three keystone foundations verbatim:

* **Probe allowlist** — :func:`~meho_backplane.connectors.net.allowlist.assert_probe_allowed`
  screens the dialed ``host`` *before* any socket opens
  (``MEHO_NETDIAG_PROBE_ALLOWLIST`` empty ⇒ every probe refused). A
  refusal propagates to the dispatcher's ``connector_probe_refused``
  arm (the same shape as ``net.tls_inspect``) instead of being reported
  as a failed scan.
* **Audit-visible host:port** — the return dict carries the literal
  ``host``/``port`` (a host:port is not a secret), so the durable audit
  row's ``raw_payload`` answers "who scanned what".
* **Return-failures contract** — a refused, timed-out, DNS-failed, or
  no-host-key endpoint is the **product**, not an error: the handler
  returns ``{"scanned": false, "reason": <code>, ...}`` with dispatch
  ``status="ok"``. It never raises a ``connector_*`` error for a scan
  that ran. An allowlist refusal is the deliberate exception: nothing was
  dialed, so it fails the dispatch instead.

``safety_level="safe"`` + ``requires_approval=False``: a read-only
handshake that offers no credential and sends no application data, so the
probe allowlist is the sole floor (same posture as ``net.tls_inspect``).
The presented host key is **public** material — never a private key —
safe to log, audit, and hand to an agent.
"""

from __future__ import annotations

import asyncio
import socket
from typing import TYPE_CHECKING, Any, Final

import asyncssh

from meho_backplane.connectors.net.allowlist import assert_probe_allowed

# Reuse the shared probe timeout bounds/clamp from the keystone module.
# ``ops`` never imports this module (the __init__ queues each registrar
# independently), so this package-internal import forms no cycle.
from meho_backplane.connectors.net.ops import (
    _DEFAULT_TIMEOUT_SECONDS,
    _MAX_TIMEOUT_SECONDS,
    _clamp_timeout,
)
from meho_backplane.operations.typed_register import register_typed_operation

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "NET_SSH_KEYSCAN_PARAMETER_SCHEMA",
    "net_ssh_keyscan",
    "register_net_ssh_keyscan_operation",
]

#: The default SSH port. A ``known_hosts`` entry omits the port for 22 and
#: uses the ``[host]:port`` form otherwise (OpenSSH convention).
_DEFAULT_SSH_PORT: Final[int] = 22

#: Canonical host-key type → the ``server_host_key_algs`` set to request
#: for it. The value is the ``known_hosts`` key type exactly as
#: ``asyncssh`` reports it via ``key.get_algorithm()`` / the OpenSSH
#: export prefix. RSA maps to the SHA-2 signature variants **and** the
#: legacy ``ssh-rsa`` name: a modern server disables the SHA-1 ``ssh-rsa``
#: signature algorithm by default (OpenSSH 8.8+) but still serves an RSA
#: **host key** negotiated via ``rsa-sha2-256`` / ``-512`` — asyncssh
#: returns that host key with ``get_algorithm() == "ssh-rsa"`` regardless,
#: so requesting the whole family robustly collects the RSA host key.
_KEY_TYPE_ALGOS: Final[dict[str, tuple[str, ...]]] = {
    "ssh-ed25519": ("ssh-ed25519",),
    "ecdsa-sha2-nistp256": ("ecdsa-sha2-nistp256",),
    "ecdsa-sha2-nistp384": ("ecdsa-sha2-nistp384",),
    "ecdsa-sha2-nistp521": ("ecdsa-sha2-nistp521",),
    "ssh-rsa": ("rsa-sha2-512", "rsa-sha2-256", "ssh-rsa"),
}

#: The default set of host-key types to scan when the caller omits
#: ``key_types`` — the modern types in a stable preference order (RSA
#: last). Each is fetched with its own handshake.
_DEFAULT_KEY_TYPES: Final[tuple[str, ...]] = (
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "ssh-rsa",
)

#: Operator guidance surfaced verbatim in every result's ``note`` field.
_NOTE: Final[str] = (
    "SSH host public keys (public handshake material — never a private "
    "key). To pin a linux-ssh (or any SSH-transport) target, patch its "
    "Vault secret so the known_hosts field holds these lines, e.g. "
    "`vault kv patch <secret_ref> known_hosts=<known_hosts>`; SshConnector "
    "then verifies the host key fail-closed on every connect. Like "
    "ssh-keyscan, this trusts whoever answers on the wire, so verify each "
    "sha256_fingerprint against an out-of-band source of truth before you "
    "pin it."
)

NET_SSH_KEYSCAN_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "host": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Hostname or IP literal to scan for SSH host keys. Must be "
                "covered by MEHO_NETDIAG_PROBE_ALLOWLIST or the probe is "
                "refused before any socket opens."
            ),
        },
        "port": {
            "type": "integer",
            "minimum": 1,
            "maximum": 65535,
            "description": "SSH port to open the transport handshake to (default 22).",
        },
        "timeout_seconds": {
            "type": "number",
            "exclusiveMinimum": 0,
            "maximum": _MAX_TIMEOUT_SECONDS,
            "description": (
                "Per-key-type handshake timeout in seconds (default 5, max "
                "30). A handshake that does not complete in time returns "
                "scanned=false with reason='timeout'."
            ),
        },
        "key_types": {
            "type": "array",
            "items": {"type": "string", "enum": list(_KEY_TYPE_ALGOS)},
            "minItems": 1,
            "uniqueItems": True,
            "description": (
                "Optional list of host-key types to fetch (one handshake "
                "each). Omit to scan all common types "
                "(ssh-ed25519, ecdsa-sha2-nistp256/384/521, ssh-rsa)."
            ),
        },
    },
    "required": ["host"],
    "additionalProperties": False,
}

_KEY_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "description": (
                "Host-key type as it appears in a known_hosts line "
                "(e.g. ssh-ed25519, ecdsa-sha2-nistp256, ssh-rsa)."
            ),
        },
        "base64": {
            "type": "string",
            "description": "The host key blob, base64-encoded (the third known_hosts field).",
        },
        "sha256_fingerprint": {
            "type": "string",
            "description": (
                "OpenSSH SHA256 fingerprint (SHA256:<base64>) — verify this "
                "out-of-band before trusting the pin."
            ),
        },
        "md5_fingerprint": {
            "type": "string",
            "description": (
                "OpenSSH legacy MD5 fingerprint (MD5:aa:bb:...); ssh-keyscan -E md5 parity."
            ),
        },
        "known_hosts_line": {
            "type": "string",
            "description": (
                "A single ready-to-pin OpenSSH known_hosts line for this "
                "key: '<host> <type> <base64>' (host is '[host]:port' when "
                "port is not 22)."
            ),
        },
    },
    "required": ["type", "base64", "sha256_fingerprint", "md5_fingerprint", "known_hosts_line"],
    "additionalProperties": False,
}

_NET_SSH_KEYSCAN_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "scanned": {
            "type": "boolean",
            "description": "True iff at least one host key was collected.",
        },
        "reason": {
            "type": ["string", "null"],
            "description": (
                "Null on success; otherwise a failure code: timeout, "
                "refused, dns_failure, unreachable, no_host_key. A host "
                "outside MEHO_NETDIAG_PROBE_ALLOWLIST is not reported here — "
                "it fails the dispatch with connector_probe_refused."
            ),
        },
        "host": {"type": "string", "description": "The scanned host (audit-visible)."},
        "port": {"type": "integer", "description": "The scanned port (audit-visible)."},
        "keys": {
            "type": "array",
            "items": _KEY_ITEM_SCHEMA,
            "description": "Collected host keys, one per negotiated type (empty on any failure).",
        },
        "known_hosts": {
            "type": "string",
            "description": (
                "All collected known_hosts lines joined by newlines — paste "
                "verbatim into a target's Vault secret as known_hosts. "
                "Empty on any failure."
            ),
        },
        "note": {
            "type": "string",
            "description": (
                "Operator guidance on how to pin the result and the verify-out-of-band caveat."
            ),
        },
    },
    "required": ["scanned", "reason", "host", "port", "keys", "known_hosts", "note"],
    "additionalProperties": False,
}

_NET_SSH_KEYSCAN_WHEN_TO_USE = (
    "Fetch an SSH server's host key(s) to pin a target — the "
    "'ssh-keyscan' read: 'what host key does this box present so I can "
    "pin it in known_hosts?'. SshConnector verifies host keys "
    "fail-closed, so an SSH-transport target with no known_hosts pin "
    "cannot be dialed; this op mints that pin through the backplane "
    "instead of an out-of-band shell ssh-keyscan. It opens the SSH "
    "handshake (key exchange only — no login, no credentials, no "
    "application bytes) and returns each key's type, base64, SHA256 "
    "fingerprint, and a ready-to-paste known_hosts line. A refused, "
    "timed-out, or non-SSH endpoint is a normal result, not an error. "
    "The destination must be inside MEHO_NETDIAG_PROBE_ALLOWLIST; one "
    "that is not fails with connector_probe_refused rather than reporting "
    "a (false) empty scan."
)

_NET_SSH_KEYSCAN_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Use to obtain a target's SSH host-key pin (known_hosts lines + "
        "SHA256 fingerprints) before registering or first dialing an "
        "SSH-transport target, since host-key verification is fail-closed "
        "without a pin. Read-only: no credential is offered and no "
        "application bytes are sent — only the SSH key exchange runs."
    ),
    "parameter_hints": {
        "host": "Required. Hostname or IP literal. Must be allowlisted for probing.",
        "port": "Optional. SSH port (default 22).",
        "timeout_seconds": "Optional. Per-key-type handshake timeout (default 5, max 30).",
        "key_types": (
            "Optional. Host-key types to fetch (ssh-ed25519, "
            "ecdsa-sha2-nistp256/384/521, ssh-rsa). Omit to scan all."
        ),
    },
    "output_shape": (
        "On success: {'scanned': true, 'reason': null, 'keys': [{'type', "
        "'base64', 'sha256_fingerprint', 'md5_fingerprint', "
        "'known_hosts_line'}, ...], 'known_hosts': '<lines joined>', "
        "'note': <pin-flow guidance>, 'host', 'port'}. Pin flow: patch the "
        "target's Vault secret with known_hosts=<the known_hosts field> "
        "(e.g. via a vault kv patch), which SshConnector enforces "
        "fail-closed. On a refused / timed-out / non-SSH endpoint: the same "
        "keys with scanned=false, reason set, keys=[] and known_hosts='' — "
        "still a successful (status=ok) op. A host outside "
        "MEHO_NETDIAG_PROBE_ALLOWLIST is NOT a reading: the op fails with "
        "error_code='connector_probe_refused' and no socket was opened."
    ),
}


def _resolve_key_types(raw: Any) -> tuple[str, ...]:
    """Return the ordered, de-duplicated set of host-key types to scan.

    A missing / empty ``key_types`` yields :data:`_DEFAULT_KEY_TYPES`. A
    caller-supplied list is filtered to the known types (the schema enum
    already enforces this on the dispatch path; the filter keeps a direct
    handler call bounded too) and de-duplicated while preserving order.
    """
    if not raw:
        return _DEFAULT_KEY_TYPES
    seen: set[str] = set()
    resolved: list[str] = []
    for item in raw:
        key = str(item)
        if key in _KEY_TYPE_ALGOS and key not in seen:
            seen.add(key)
            resolved.append(key)
    return tuple(resolved) if resolved else _DEFAULT_KEY_TYPES


def _known_hosts_line(host: str, port: int, key_type: str, key_b64: str) -> str:
    """Build one OpenSSH known_hosts line for *host*.

    OpenSSH omits the port for the default 22 (``host type base64``) and
    uses the bracketed ``[host]:port`` form otherwise — the shape
    ``asyncssh.import_known_hosts`` (which ``SshConnector`` parses the pin
    with) matches against the dialed host:port.
    """
    host_field = host if port == _DEFAULT_SSH_PORT else f"[{host}]:{port}"
    return f"{host_field} {key_type} {key_b64}"


def _key_entry(host: str, port: int, key: asyncssh.SSHKey) -> dict[str, Any]:
    """Flatten one presented host key into the audit-safe report shape.

    The OpenSSH export (``<type> <base64>[ <comment>]``) supplies both the
    known_hosts key type and the base64 blob; the SHA256 / MD5 fingerprints
    come from asyncssh's ``get_fingerprint`` (already in the exact OpenSSH
    ``SHA256:<base64>`` / ``MD5:aa:bb:..`` form). No private material is
    ever touched — a host key is public handshake material.
    """
    type_and_b64 = key.export_public_key("openssh").decode("ascii").split()
    key_type = type_and_b64[0]
    key_b64 = type_and_b64[1]
    return {
        "type": key_type,
        "base64": key_b64,
        "sha256_fingerprint": key.get_fingerprint("sha256"),
        "md5_fingerprint": key.get_fingerprint("md5"),
        "known_hosts_line": _known_hosts_line(host, port, key_type, key_b64),
    }


def _result(host: str, port: int, keys: list[dict[str, Any]], reason: str | None) -> dict[str, Any]:
    """Assemble the uniform response payload (success or failure)."""
    return {
        "scanned": bool(keys),
        "reason": reason,
        "host": host,
        "port": port,
        "keys": keys,
        "known_hosts": "\n".join(entry["known_hosts_line"] for entry in keys),
        "note": _NOTE,
    }


async def net_ssh_keyscan(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Fetch the SSH host key(s) ``host:port`` presents, without authenticating.

    Op-id: ``net.ssh_keyscan``. Synthetic typed op (``target`` is always
    ``None``); the dispatcher has validated the schema. Flow: screen
    ``host`` against the probe allowlist → for each requested key type run
    :func:`asyncssh.get_server_host_key` (SSH handshake only — no auth, no
    application bytes, no host-key verification) under
    :func:`asyncio.wait_for` → flatten each presented key to its type /
    base64 / fingerprints / known_hosts line.

    A server that offers no host key of a requested type raises
    :exc:`asyncssh.KeyExchangeFailed`, which is caught per type and skipped
    (the next type is still tried). A refused / timed-out / DNS-failed /
    otherwise-unreachable endpoint returns ``scanned=false`` with a reason
    code and ``status="ok"`` (the return-failures contract), and a
    connect-level failure short-circuits the remaining types so the whole
    op stays bounded. When the handshake succeeds but no requested type is
    available, ``reason="no_host_key"``. An allowlist refusal is the
    exception: nothing was dialed, so :exc:`ProbeNotAllowedError`
    propagates to the dispatcher's ``connector_probe_refused`` arm. The
    return dict carries the literal ``host``/``port`` for the durable audit
    row.
    """
    host = str(params["host"])
    port = int(params.get("port", _DEFAULT_SSH_PORT))
    timeout = _clamp_timeout(params.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS))
    key_types = _resolve_key_types(params.get("key_types"))

    assert_probe_allowed(host, tenant_id=operator.tenant_id)

    keys: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    connect_reason: str | None = None
    for key_type in key_types:
        try:
            key = await asyncio.wait_for(
                asyncssh.get_server_host_key(
                    host, port=port, server_host_key_algs=list(_KEY_TYPE_ALGOS[key_type])
                ),
                timeout=timeout,
            )
        except asyncssh.KeyExchangeFailed:
            # Connected, but the server presents no host key of this type.
            # Skip it and try the next requested type — not a failure yet.
            continue
        except TimeoutError:
            # asyncio.wait_for raises builtin TimeoutError on timeout (== asyncio.TimeoutError).
            # Caught before OSError below since TimeoutError subclasses OSError.
            connect_reason = "timeout"
            break
        except socket.gaierror:
            # DNS resolution failed (also an OSError subclass — catch first).
            connect_reason = "dns_failure"
            break
        except ConnectionRefusedError:
            connect_reason = "refused"
            break
        except (OSError, asyncssh.Error):
            # Network/host unreachable, reset by a non-SSH peer, protocol
            # error, etc. asyncssh.Error is not an OSError, so both bases
            # are caught here; KeyExchangeFailed was already handled above.
            connect_reason = "unreachable"
            break
        if key is None:
            continue
        entry = _key_entry(host, port, key)
        dedup = (entry["type"], entry["base64"])
        if dedup in seen:
            continue
        seen.add(dedup)
        keys.append(entry)

    if keys:
        # We collected at least one key; a connect-level error on a later
        # type does not undo a successful scan.
        return _result(host, port, keys, None)
    # Connected but no requested type was available ⇒ no_host_key; otherwise
    # the connect-level reason from the first failed handshake.
    return _result(host, port, keys, connect_reason or "no_host_key")


async def register_net_ssh_keyscan_operation(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert the ``net.ssh_keyscan`` typed op into ``endpoint_descriptor``.

    Queued onto the lifespan-driven registrar list by the package
    ``__init__`` (a sibling registrar to ``net.tcp_check``'s), run after
    the connector eager-import pass. Registered under the same synthetic
    natural key as the keystone
    (``product="net", version="1.x", impl_id="net-probe"``), so it shares
    the ``net-probe-1.x`` wire ``connector_id``. Idempotent. ``safe`` +
    ``requires_approval=False`` — the probe allowlist is the only floor.
    """
    await register_typed_operation(
        product="net",
        version="1.x",
        impl_id="net-probe",
        op_id="net.ssh_keyscan",
        handler=net_ssh_keyscan,
        group_key="probe",
        when_to_use=_NET_SSH_KEYSCAN_WHEN_TO_USE,
        summary="Fetch an SSH server's host key(s) to pin a target — ssh-keyscan parity.",
        description=(
            "Opens the SSH transport handshake to a host:port — key "
            "exchange only, no authentication, no credentials, and no "
            "application bytes — reads the server's presented host key(s), "
            "and returns each key's type, base64 blob, SHA256 and legacy "
            "MD5 fingerprint, and a ready-to-paste OpenSSH known_hosts "
            "line, plus the joined known_hosts block. It mints the "
            "host-key pin SshConnector's fail-closed verification requires, "
            "through the backplane instead of an out-of-band shell "
            "ssh-keyscan. The presented host key is public material, never "
            "a private key. The destination must be inside "
            "MEHO_NETDIAG_PROBE_ALLOWLIST or the probe is refused before "
            "any socket opens, which fails the dispatch with "
            "connector_probe_refused. A refused, timed-out, DNS-failed, or "
            "non-SSH endpoint returns scanned=false with a reason code and "
            "status=ok — a failed scan is the product, never a connector "
            "error. Like ssh-keyscan it trusts whoever answers, so verify "
            "the sha256_fingerprint out-of-band before pinning."
        ),
        parameter_schema=NET_SSH_KEYSCAN_PARAMETER_SCHEMA,
        response_schema=_NET_SSH_KEYSCAN_RESPONSE_SCHEMA,
        tags=["net", "probe", "read", "diagnostics", "ssh"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=_NET_SSH_KEYSCAN_LLM_INSTRUCTIONS,
        embedding_service=embedding_service,
    )
