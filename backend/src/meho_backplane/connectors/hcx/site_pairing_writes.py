# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The bodies of ``hcx.site_pairing.set`` and ``hcx.certificate.trust``.

Pair: ``POST /hybridity/api/cloudConfigs`` with
``{"remote": {"username", "password", "url"}}``. The user and password are
read from the Vault path ``remote_secret_ref`` under the caller's identity
(after an approval: the approver) and go only into that request body. The
op is pinned ``credential_write`` in :mod:`meho_backplane.broadcast.events`,
so the flight recorder keeps no body of the call. A started pairing answers
``{"data": {"jobId"}}``; the caller follows the job with ``hcx.job.get``.
When the Manager does not trust the remote site's certificate, it answers
with an error whose ``data`` holds the certificate. Then nothing is paired
and the op returns the certificate, so a person can check it and trust it
with ``hcx.certificate.trust``. MEHO never trusts a certificate by itself.

Unpair: ``DELETE /hybridity/api/endpointPairing/{endpointId}``, after a
check that no service mesh uses the site
(``GET /hybridity/api/interconnect/serviceMesh``). When one does, the op
removes nothing and answers ``status: in_use`` with the mesh ids. That is
an answer, not an error: the dispatcher withholds the error text of every
error of this op, because the op sends a password.

Trust: ``POST /hybridity/api/admin/certificates`` with ``{"certificate"}``
(the call VMware's Terraform provider for HCX uses on the Manager's main
API). The answer is ``{"success", "completed"}``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import httpx
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vault_creds import (
    load_param_secret_data,
    strip_credential_value,
)
from meho_backplane.connectors.hcx.session import HcxTargetLike
from meho_backplane.connectors.hcx.typed_reads import (
    _envelope_notes,
    _redact_object,
    _segment,
    redact_secrets,
    rows_of,
)

if TYPE_CHECKING:
    from meho_backplane.connectors.hcx.connector import HcxConnector

__all__ = [
    "VENDOR_CALLS",
    "certificate_facts",
    "hcx_certificate_trust_impl",
    "hcx_site_pairing_set_impl",
]

_CLOUD_CONFIGS_PATH: Final = "/hybridity/api/cloudConfigs"
_ENDPOINT_PAIRING_PATH: Final = "/hybridity/api/endpointPairing/{endpointId}"
_SERVICE_MESH_PATH: Final = "/hybridity/api/interconnect/serviceMesh"
_CERTIFICATES_PATH: Final = "/hybridity/api/admin/certificates"

#: Every vendor call of this module, as ``(method, path template)``.
#: The reconcile lane checks each one against the pinned HCX API files.
VENDOR_CALLS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("GET", _CLOUD_CONFIGS_PATH),
        ("POST", _CLOUD_CONFIGS_PATH),
        ("DELETE", _ENDPOINT_PAIRING_PATH),
        ("GET", _SERVICE_MESH_PATH),
        ("POST", _CERTIFICATES_PATH),
    }
)

_JOB_READ: Final = "hcx.job.get"
_SHA256_HEX_DIGITS: Final = 64


@dataclass
class _SecretRefTarget:
    """The target shape :func:`load_param_secret_data` reads: the ref from the params."""

    name: str
    host: str
    secret_ref: str | None


def _fingerprint(der: bytes) -> str:
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def _normalised_fingerprint(value: str) -> str:
    """64 upper-case hex digits with colons, or ``ValueError``."""
    digits = value.replace(":", "").strip().upper()
    if len(digits) != _SHA256_HEX_DIGITS or any(c not in "0123456789ABCDEF" for c in digits):
        raise ValueError("expected_sha256 must be 64 hex digits, with or without colons")
    return ":".join(digits[i : i + 2] for i in range(0, len(digits), 2))


def certificate_facts(pem: str) -> dict[str, Any]:
    """Parse one PEM certificate: ``{certificate, sha256, subject, issuer, not_after}``.

    Raises ``ValueError`` for text that holds a private key, more than one
    certificate, or no valid certificate.
    """
    if "PRIVATE KEY" in pem:
        raise ValueError("the text holds a private key; pass only the certificate")
    if pem.count("-----BEGIN CERTIFICATE-----") != 1:
        raise ValueError("pass exactly one certificate in PEM form")
    try:
        cert = x509.load_pem_x509_certificate(pem.strip().encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError(f"the certificate is not valid PEM: {exc}") from exc
    return {
        "certificate": cert.public_bytes(Encoding.PEM).decode("ascii"),
        "sha256": _fingerprint(cert.public_bytes(Encoding.DER)),
        "subject": cert.subject.rfc4514_string(),
        "issuer": cert.issuer.rfc4514_string(),
        "not_after": cert.not_valid_after_utc.isoformat(),
    }


def _offered_certificate(payload: Any) -> str | None:
    """The remote certificate in an error answer of the pairing call, else ``None``."""
    if not isinstance(payload, dict):
        return None
    for error in payload.get("errors") or []:
        if not isinstance(error, dict):
            continue
        for item in error.get("data") or []:
            if isinstance(item, dict) and isinstance(item.get("certificate"), str):
                return str(item["certificate"])
    return None


def _error_text(payload: Any) -> str:
    notes = _envelope_notes(payload)
    texts = []
    for error in notes["errors"]:
        if isinstance(error, dict):
            texts.append(
                " ".join(str(error[k]) for k in ("error", "text", "message") if error.get(k))
            )
    return "; ".join(text for text in texts if text)


def _json_or_none(response: httpx.Response) -> Any:
    try:
        return response.json()
    except (ValueError, json.JSONDecodeError):
        return None


def _without_request_body(
    exc: httpx.HTTPStatusError, payload: Any, password: str
) -> httpx.HTTPStatusError:
    """A copy of *exc* with the same status, no request body and no session header.

    The dispatcher copies the response body into the result and the audit
    row, so the copy holds only the secret-masked JSON answer, and nothing
    when the answer is not JSON or holds the password.
    """
    status = exc.response.status_code
    request = httpx.Request(exc.request.method, exc.request.url)
    masked = redact_secrets(payload) if payload is not None else None
    if masked is not None and password in json.dumps(masked):
        masked = None
    response = (
        httpx.Response(status, json=masked, request=request)
        if masked is not None
        else httpx.Response(status, request=request)
    )
    return httpx.HTTPStatusError(
        f"the HCX Manager answered HTTP {status} for {request.method} {request.url.path}",
        request=request,
        response=response,
    )


async def _remote_credentials(
    operator: Operator, target: HcxTargetLike, secret_ref: str
) -> tuple[str, str]:
    data = await load_param_secret_data(
        _SecretRefTarget(name=target.name, host=target.host, secret_ref=secret_ref),
        operator,
        param="remote_secret_ref",
    )
    username = strip_credential_value(data.get("username") or "")
    password = strip_credential_value(data.get("password") or "")
    if not username or not password:
        raise ValueError(
            "the Vault secret at remote_secret_ref needs the fields username and password; "
            "nothing was sent to the HCX Manager"
        )
    return username, password


def _same_url(left: Any, right: str) -> bool:
    return isinstance(left, str) and left.rstrip("/").lower() == right.rstrip("/").lower()


async def _pair(
    connector: HcxConnector, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    url = str(params["url"])
    answer: dict[str, Any] = {"action": "pair", "url": url, "endpoint_id": None, "job_id": None}
    answer |= {"status_read": None, "remote_certificate": None, "warnings": []}
    answer["service_mesh_ids"] = []
    pairings = await connector._get_json(target, _CLOUD_CONFIGS_PATH, operator=operator)
    for row in rows_of(pairings):
        if _same_url(row.get("url"), url):
            return {**answer, "status": "already_paired", "endpoint_id": row.get("endpointId")}
    username, password = await _remote_credentials(operator, target, params["remote_secret_ref"])
    body = {"remote": {"username": username, "password": password, "url": url}}
    try:
        payload: Any = await connector._post_json(
            target, _CLOUD_CONFIGS_PATH, operator=operator, json=body
        )
    except httpx.HTTPStatusError as exc:
        payload = _json_or_none(exc.response)
        if _offered_certificate(payload) is None:
            raise _without_request_body(exc, payload, password) from None
    offered = _offered_certificate(payload)
    if offered is not None:
        return {
            **answer,
            "status": "certificate_not_trusted",
            "remote_certificate": certificate_facts(offered),
        }
    data = payload.get("data") if isinstance(payload, dict) else None
    job_id = data.get("jobId") if isinstance(data, dict) else None
    if not job_id:
        text = _error_text(payload)
        text = "" if password in text else text
        raise ValueError(
            "the HCX Manager did not start the pairing" + (f": {text}" if text else "")
        )
    return {
        **answer,
        "status": "accepted",
        "job_id": job_id,
        "status_read": _JOB_READ,
        "warnings": redact_secrets(_envelope_notes(payload)["warnings"]),
    }


def _meshes_using(meshes: Any, endpoint_id: str) -> list[str]:
    """Ids (or names) of the service meshes that have a compute profile on *endpoint_id*."""
    found = []
    for mesh in rows_of(meshes):
        profiles = mesh.get("computeProfiles") or []
        if any(isinstance(p, dict) and p.get("endpointId") == endpoint_id for p in profiles):
            found.append(str(mesh.get("serviceMeshId") or mesh.get("name")))
    return found


async def _unpair(
    connector: HcxConnector, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    endpoint_id = str(params["endpoint_id"])
    path = _ENDPOINT_PAIRING_PATH.format(endpointId=_segment("endpoint", endpoint_id))
    answer: dict[str, Any] = {"action": "unpair", "url": None, "endpoint_id": endpoint_id}
    answer |= {"job_id": None, "status_read": None, "remote_certificate": None, "warnings": []}
    meshes = await connector._get_json(target, _SERVICE_MESH_PATH, operator=operator)
    in_use = sorted(_meshes_using(meshes, endpoint_id))
    if in_use:
        return {**answer, "status": "in_use", "service_mesh_ids": in_use}
    payload = await connector._post_json(target, path, operator=operator, verb="DELETE")
    if isinstance(payload, dict) and payload.get("success") is False:
        text = _error_text(payload)
        raise ValueError(
            "the HCX Manager did not remove the pairing" + (f": {text}" if text else "")
        )
    return {**answer, "status": "removed", "service_mesh_ids": []}


async def hcx_site_pairing_set_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.site_pairing.set`` -- pair with a remote site, or remove a pairing."""
    if params["action"] == "pair":
        answer = await _pair(connector, operator, target, params)
    else:
        answer = await _unpair(connector, operator, target, params)
    return _redact_object(answer)


async def hcx_certificate_trust_impl(
    connector: HcxConnector,
    operator: Operator,
    target: HcxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``hcx.certificate.trust`` -- ``POST /hybridity/api/admin/certificates``."""
    facts = certificate_facts(str(params["certificate"]))
    expected = params.get("expected_sha256")
    if expected and _normalised_fingerprint(str(expected)) != facts["sha256"]:
        raise ValueError(
            f"the certificate's SHA-256 fingerprint is {facts['sha256']}, not the expected "
            "one; nothing was trusted"
        )
    payload = await connector._post_json(
        target, _CERTIFICATES_PATH, operator=operator, json={"certificate": facts["certificate"]}
    )
    if not (isinstance(payload, dict) and payload.get("success") is True):
        text = _error_text(payload)
        raise ValueError(
            "the HCX Manager did not trust the certificate" + (f": {text}" if text else "")
        )
    return {"status": "trusted", **{k: v for k, v in facts.items() if k != "certificate"}}
