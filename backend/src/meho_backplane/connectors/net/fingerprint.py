# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Network-diagnostics typed op ``net.fingerprint`` + its registrar.

``net.fingerprint(host)`` looks at one address and says which product it
likely is, without adding it as a target and without logging in. It runs
four read-only checks at the same time:

* the TLS certificate on port 443 (it reuses ``net.tls_inspect``);
* the page title of ``https://<host>/`` (it follows only redirects to the
  same host, and never returns the page body);
* the vSphere "about" answer: a ``RetrieveServiceContent`` call on
  ``https://<host>/sdk``. vCenter and ESXi answer it without a login with
  their product name, version and build;
* the SSH banner on port 22 (the first line the server sends; MEHO sends
  nothing).

:mod:`meho_backplane.connectors.net.fingerprint_rules` turns these answers
into a product, a version, a confidence and the list of rules that
matched. When nothing matches, or two products tie, the answer is
``unknown``.

The address must be in the tenant's probe allowlist
(:func:`~meho_backplane.connectors.net.allowlist.assert_probe_allowed`).
The check runs before any socket opens. An address outside the list
fails the call with ``connector_probe_refused``, and the dispatcher writes
an error audit row for it.

The registered-target probe (``targets.probe``) cannot be reused here: it
picks a connector by the target's known product and logs in with the
target's credentials. This op has neither.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.adapters.http import _insecure_ssl_context
from meho_backplane.connectors.net.allowlist import assert_probe_allowed
from meho_backplane.connectors.net.fingerprint_rules import (
    Evidence,
    classify,
    clip,
    extract_title,
    parse_vsphere_about,
)
from meho_backplane.connectors.net.ops import (
    _DEFAULT_TIMEOUT_SECONDS,
    _MAX_TIMEOUT_SECONDS,
    _clamp_timeout,
)
from meho_backplane.connectors.net.tls import net_tls_inspect
from meho_backplane.operations.typed_register import register_typed_operation

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "NET_FINGERPRINT_PARAMETER_SCHEMA",
    "net_fingerprint",
    "register_net_fingerprint_operation",
]

_HTTPS_PORT: Final[int] = 443
_SSH_PORT: Final[int] = 22

#: The most bytes MEHO reads from one HTTP answer. Enough for a page title
#: or the vSphere "about" answer; the rest is never read.
_MAX_BODY_BYTES: Final[int] = 64 * 1024

#: Redirects to the same host that MEHO follows to find the page title.
_MAX_SAME_HOST_REDIRECTS: Final[int] = 3

#: RFC 4253 section 4.2: the SSH identification line is at most 255 bytes.
_MAX_SSH_BANNER_BYTES: Final[int] = 255

#: The vSphere call that returns the "about" block without a login.
_VSPHERE_ABOUT_REQUEST: Final[bytes] = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">'
    b"<soapenv:Body>"
    b'<RetrieveServiceContent xmlns="urn:vim25">'
    b'<_this type="ServiceInstance">ServiceInstance</_this>'
    b"</RetrieveServiceContent>"
    b"</soapenv:Body>"
    b"</soapenv:Envelope>"
)

_CHECK_SCHEMA_BASE: dict[str, Any] = {
    "checked": {
        "type": "boolean",
        "description": "True when the check got an answer it could read.",
    },
    "reason": {
        "type": ["string", "null"],
        "description": "Null when checked; otherwise why the check got no answer.",
    },
}

_NULLABLE_TEXT: dict[str, Any] = {"type": ["string", "null"]}

NET_FINGERPRINT_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "host": {
            "type": "string",
            "minLength": 1,
            "description": (
                "One IP address or host name to identify. Must be covered by "
                "the probe allowlist (MEHO_NETDIAG_PROBE_ALLOWLIST, or the "
                "tenant's own list), or the call is refused before any socket "
                "opens. Address ranges are not accepted."
            ),
        },
        "timeout_seconds": {
            "type": "number",
            "exclusiveMinimum": 0,
            "maximum": _MAX_TIMEOUT_SECONDS,
            "description": (
                "Time limit for each check in seconds (default 5, max 30). "
                "The checks run at the same time."
            ),
        },
    },
    "required": ["host"],
    "additionalProperties": False,
    "examples": [
        {"host": "192.0.2.10"},
        {"host": "<host>", "timeout_seconds": 10},
    ],
}

_NET_FINGERPRINT_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "host": {"type": "string", "description": "The checked address (audit-visible)."},
        "reachable": {
            "type": "boolean",
            "description": "True when at least one check got an answer.",
        },
        "product": {
            "type": "string",
            "description": (
                "Likely product: vcenter, esxi, nsx or sddc (SDDC Manager); "
                "'unknown' when no rule matched or two products tie."
            ),
        },
        "vendor": _NULLABLE_TEXT,
        "version": {
            "type": ["string", "null"],
            "description": "Likely version, only when the system states it itself.",
        },
        "build": _NULLABLE_TEXT,
        "confidence": {
            "type": "string",
            "enum": ["high", "medium", "low", "none"],
            "description": "How sure the answer is. 'none' goes with product 'unknown'.",
        },
        "matched": {
            "type": "array",
            "description": "The rules that matched, with the value each one saw.",
            "items": {
                "type": "object",
                "properties": {
                    "product": {"type": "string"},
                    "source": {"type": "string"},
                    "field": {"type": "string"},
                    "value": {"type": "string"},
                    "weight": {"type": "integer"},
                },
                "required": ["product", "source", "field", "value", "weight"],
                "additionalProperties": False,
            },
        },
        "evidence": {
            "type": "object",
            "description": "What each check saw. Never a page body.",
            "properties": {
                "tls": {
                    "type": "object",
                    "properties": {
                        **_CHECK_SCHEMA_BASE,
                        "subject": _NULLABLE_TEXT,
                        "issuer": _NULLABLE_TEXT,
                        "san": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["checked", "reason", "subject", "issuer", "san"],
                    "additionalProperties": False,
                },
                "http": {
                    "type": "object",
                    "properties": {
                        **_CHECK_SCHEMA_BASE,
                        "status": {"type": ["integer", "null"]},
                        "title": _NULLABLE_TEXT,
                        "server": _NULLABLE_TEXT,
                        "path": _NULLABLE_TEXT,
                    },
                    "required": ["checked", "reason", "status", "title", "server", "path"],
                    "additionalProperties": False,
                },
                "vsphere_about": {
                    "type": "object",
                    "properties": {
                        **_CHECK_SCHEMA_BASE,
                        "name": _NULLABLE_TEXT,
                        "full_name": _NULLABLE_TEXT,
                        "vendor": _NULLABLE_TEXT,
                        "version": _NULLABLE_TEXT,
                        "build": _NULLABLE_TEXT,
                        "api_type": _NULLABLE_TEXT,
                        "product_line_id": _NULLABLE_TEXT,
                    },
                    "required": ["checked", "reason"],
                    "additionalProperties": False,
                },
                "ssh": {
                    "type": "object",
                    "properties": {**_CHECK_SCHEMA_BASE, "banner": _NULLABLE_TEXT},
                    "required": ["checked", "reason", "banner"],
                    "additionalProperties": False,
                },
            },
            "required": ["tls", "http", "vsphere_about", "ssh"],
            "additionalProperties": False,
        },
    },
    "required": [
        "host",
        "reachable",
        "product",
        "vendor",
        "version",
        "build",
        "confidence",
        "matched",
        "evidence",
    ],
    "additionalProperties": False,
}

_NET_FINGERPRINT_WHEN_TO_USE = (
    "Identify one unknown IP address or host name: 'what is this system?'. "
    "Looks at the TLS certificate, the web page title, the vSphere 'about' "
    "answer and the SSH banner, and returns the likely product (vcenter, "
    "esxi, nsx, sddc), the likely version, how sure it is, and the "
    "evidence. It does not log in and does not add a target. One address "
    "only: no ranges, no port scans. The address must be in the probe "
    "allowlist; one that is not fails with connector_probe_refused."
)

_NET_FINGERPRINT_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call before registering a target when you do not know what runs on "
        "an address. Read-only and no login. It checks the TLS certificate "
        "on port 443, the page title of https://<host>/ (same-host redirects "
        "only; the body is never returned), the vSphere 'about' answer on "
        "/sdk and the SSH banner on port 22. One address only: no ranges and "
        "no port scans."
    ),
    "parameter_hints": {
        "host": "Required. One IP address or host name inside the probe allowlist.",
        "timeout_seconds": "Optional. Time limit per check (default 5, max 30).",
    },
    "output_shape": (
        "{'host', 'reachable', 'product', 'vendor', 'version', 'build', "
        "'confidence': high|medium|low|none, 'matched': [{product, source, "
        "field, value, weight}], 'evidence': {'tls', 'http', "
        "'vsphere_about', 'ssh'}}. product='unknown' with confidence='none' "
        "means no rule matched or the evidence disagreed. reachable=false "
        "means no check got an answer. An address outside the probe "
        "allowlist is not a reading: the call fails with "
        "error_code='connector_probe_refused'."
    ),
    "next_step": (
        "When the product is known, register the address as a target with "
        "`targets.register` using that product. When it is 'unknown', do not "
        "guess: look at the evidence, or ask an operator. For the full "
        "certificate chain call `net.tls_inspect`."
    ),
}


def _no_answer(reason: str, **fields: Any) -> dict[str, Any]:
    return {"checked": False, "reason": reason, **fields}


async def _check_tls(operator: Operator, host: str, timeout: float) -> dict[str, Any]:
    """Read the certificate on port 443 with ``net.tls_inspect``."""
    result = await net_tls_inspect(
        operator, None, {"host": host, "port": _HTTPS_PORT, "timeout_seconds": timeout}
    )
    leaf = result.get("leaf")
    if not result.get("handshake") or not leaf:
        reason = result.get("reason") or "no_certificate"
        return _no_answer(reason, subject=None, issuer=None, san=[])
    return {
        "checked": True,
        "reason": None,
        "subject": clip(leaf.get("subject")),
        "issuer": clip(leaf.get("issuer")),
        "san": [s for s in (clip(v) for v in leaf.get("san", [])[:20]) if s],
    }


def _make_client(timeout: float) -> httpx.AsyncClient:
    """A fresh client: no proxy from the environment, no redirects, no certificate check.

    The certificate is not checked (the shared no-verify context of the HTTP
    adapter) because appliances use self-signed certificates, and this op
    only reads what the system says about itself. Nothing is sent that
    needs a trusted peer: no login and no secret.
    """
    return httpx.AsyncClient(
        verify=_insecure_ssl_context(),
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(timeout),
    )


async def _read_bounded(response: httpx.Response) -> bytes:
    data = bytearray()
    async for chunk in response.aiter_bytes():
        data.extend(chunk)
        if len(data) >= _MAX_BODY_BYTES:
            break
    return bytes(data[:_MAX_BODY_BYTES])


def _transport_reason(exc: Exception) -> str:
    if isinstance(exc, TimeoutError | httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        return "unreachable"
    return "http_error"


def _same_host_redirect(response: httpx.Response, host: str) -> httpx.URL | None:
    """Return the redirect target when it stays on *host* over https, else None."""
    if not response.is_redirect:
        return None
    location = response.headers.get("location")
    if not location:
        return None
    target = response.url.join(location)
    if target.scheme != "https" or target.host != host.lower() or target.port not in (None, 443):
        return None
    return target


async def _http_title_walk(client: httpx.AsyncClient, url: httpx.URL, host: str) -> dict[str, Any]:
    for _hop in range(_MAX_SAME_HOST_REDIRECTS + 1):
        async with client.stream("GET", url) as response:
            next_url = _same_host_redirect(response, host)
            if next_url is None:
                body = await _read_bounded(response)
                return {
                    "checked": True,
                    "reason": None,
                    "status": response.status_code,
                    "title": extract_title(body.decode("utf-8", errors="replace")),
                    "server": clip(response.headers.get("server")),
                    "path": clip(response.url.path),
                }
        url = next_url
    return _no_answer("too_many_redirects", status=None, title=None, server=None, path=None)


async def _check_http(host: str, timeout: float) -> dict[str, Any]:
    """Read the page title of ``https://<host>/``."""
    try:
        start = httpx.URL(scheme="https", host=host, path="/")
        async with _make_client(timeout) as client:
            return await asyncio.wait_for(_http_title_walk(client, start, host), timeout)
    except (TimeoutError, httpx.HTTPError, httpx.InvalidURL) as exc:
        return _no_answer(_transport_reason(exc), status=None, title=None, server=None, path=None)


async def _vsphere_about_call(client: httpx.AsyncClient, url: httpx.URL) -> dict[str, Any]:
    headers = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": "urn:vim25"}
    async with client.stream("POST", url, content=_VSPHERE_ABOUT_REQUEST, headers=headers) as resp:
        body = await _read_bounded(resp)
    about = parse_vsphere_about(body)
    if about is None:
        return _no_answer("not_vsphere")
    return {"checked": True, "reason": None, **about}


async def _check_vsphere_about(host: str, timeout: float) -> dict[str, Any]:
    """Ask ``https://<host>/sdk`` for its "about" block (no login needed)."""
    try:
        url = httpx.URL(scheme="https", host=host, path="/sdk")
        async with _make_client(timeout) as client:
            return await asyncio.wait_for(_vsphere_about_call(client, url), timeout)
    except (TimeoutError, httpx.HTTPError, httpx.InvalidURL) as exc:
        return _no_answer(_transport_reason(exc))


async def _read_banner(host: str) -> bytes:
    reader, writer = await asyncio.open_connection(host, _SSH_PORT)
    try:
        return await reader.readuntil(b"\n")
    except asyncio.LimitOverrunError:
        return await reader.read(_MAX_SSH_BANNER_BYTES)
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


async def _check_ssh(host: str, timeout: float) -> dict[str, Any]:
    """Read the SSH banner on port 22. MEHO sends no bytes."""
    try:
        line = await asyncio.wait_for(_read_banner(host), timeout)
    except TimeoutError:
        return _no_answer("timeout", banner=None)
    except ConnectionRefusedError:
        return _no_answer("refused", banner=None)
    except asyncio.IncompleteReadError:
        return _no_answer("not_ssh", banner=None)
    except OSError:
        return _no_answer("unreachable", banner=None)
    text = line[:_MAX_SSH_BANNER_BYTES].decode("ascii", errors="replace").strip()
    if not text.startswith("SSH-"):
        return _no_answer("not_ssh", banner=None)
    return {"checked": True, "reason": None, "banner": clip(text)}


async def _collect(operator: Operator, host: str, timeout: float) -> Evidence:
    tls, http, about, ssh = await asyncio.gather(
        _check_tls(operator, host, timeout),
        _check_http(host, timeout),
        _check_vsphere_about(host, timeout),
        _check_ssh(host, timeout),
    )
    return Evidence(tls=tls, http=http, vsphere_about=about, ssh=ssh)


async def net_fingerprint(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Identify the product at ``host`` from what it shows without a login.

    Op-id: ``net.fingerprint``. Synthetic typed op (``target`` is always
    ``None``). The allowlist check runs first; a refusal propagates as
    :exc:`ProbeNotAllowedError`, which the dispatcher turns into
    ``connector_probe_refused``. A check that gets no answer is part of the
    result, not an error.
    """
    host = str(params["host"]).strip()
    timeout = _clamp_timeout(params.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS))

    assert_probe_allowed(host, tenant_id=operator.tenant_id)

    evidence = await _collect(operator, host, timeout)
    verdict = classify(evidence)
    return {
        "host": host,
        "reachable": evidence.any_answer(),
        **verdict,
        "evidence": evidence.as_dict(),
    }


async def register_net_fingerprint_operation(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert the ``net.fingerprint`` typed op into ``endpoint_descriptor``.

    Same synthetic natural key as the other ``net.*`` ops
    (``net-probe-1.x``). ``safe`` and no approval: it only reads what the
    system shows to anyone, and the probe allowlist limits where it can
    connect.
    """
    await register_typed_operation(
        product="net",
        version="1.x",
        impl_id="net-probe",
        op_id="net.fingerprint",
        handler=net_fingerprint,
        group_key="probe",
        when_to_use=_NET_FINGERPRINT_WHEN_TO_USE,
        summary="Identify the product and version at one address, without a login or a target.",
        description=(
            "Says which product likely runs at one address (vCenter, ESXi, "
            "NSX Manager or SDDC Manager), its likely version, how sure it "
            "is, and the evidence. No login, no target. 'unknown' when "
            "nothing matches. The address must be in the probe allowlist."
        ),
        parameter_schema=NET_FINGERPRINT_PARAMETER_SCHEMA,
        response_schema=_NET_FINGERPRINT_RESPONSE_SCHEMA,
        tags=["net", "probe", "read", "diagnostics", "fingerprint"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=_NET_FINGERPRINT_LLM_INSTRUCTIONS,
        embedding_service=embedding_service,
    )
