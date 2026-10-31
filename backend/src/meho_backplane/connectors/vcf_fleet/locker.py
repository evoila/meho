# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Locker passwords and certificates for :class:`VcfFleetConnector`.

The bodies of four typed ops (Aria Suite
Lifecycle 8.x and VCF 9.0 fleet management, the ``/lcm`` API):

* ``fleet.locker.password.list`` -- ``GET /lcm/locker/api/v2/passwords``.
  Never returns a password.
* ``fleet.locker.password.add`` -- ``POST /lcm/locker/api/v2/passwords``.
  The password comes from Vault (``password_ref``), never from the call.
* ``fleet.locker.certificate.list`` -- ``GET /lcm/locker/api/v2/certificates``.
* ``fleet.locker.certificate.import`` --
  ``POST /lcm/locker/api/v2/certificates/import`` (the private key comes
  from Vault), or ``POST /lcm/locker/api/v2/certificates`` (the appliance
  generates the key). Never returns a private key.
The product certificate replace lives in :mod:`.product_certificate`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

import httpx

from meho_backplane.connectors._shared.vault_creds import load_param_secret_data
from meho_backplane.connectors.base import ConnectorUpstreamError

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vcf_fleet.connector import VcfFleetConnector
    from meho_backplane.connectors.vcf_fleet.session import VcfFleetTargetLike

__all__ = [
    "VENDOR_CALLS",
    "certificate_import",
    "certificate_list",
    "parse_secret_ref",
    "password_add",
    "password_list",
    "path_id",
    "vendor_error_text",
]

PASSWORDS_PATH: Final = "/lcm/locker/api/v2/passwords"
CERTIFICATES_PATH: Final = "/lcm/locker/api/v2/certificates"
CERTIFICATE_IMPORT_PATH: Final = "/lcm/locker/api/v2/certificates/import"

#: Every vendor call of this module, as ``(method, path)``. The reconcile
#: lane checks them against the pinned API file.
VENDOR_CALLS: Final[tuple[tuple[str, str], ...]] = (
    ("GET", PASSWORDS_PATH),
    ("POST", PASSWORDS_PATH),
    ("GET", CERTIFICATES_PATH),
    ("POST", CERTIFICATES_PATH),
    ("POST", CERTIFICATE_IMPORT_PATH),
)

#: Rows asked for per page of a locker list.
PAGE_SIZE: Final = 100
_MAX_PAGES: Final = 50

_PASSWORD_FIELDS: Final = (
    "vmid",
    "alias",
    "userName",
    "passwordDescription",
    "referenced",
    "createdOn",
    "lastUpdatedOn",
)
_CERTIFICATE_FIELDS: Final = (
    "vmid",
    "alias",
    "subject",
    "issuer",
    "algorithm",
    "sha1",
    "healthy",
    "referenced",
)
_CERT_INFO_FIELDS: Final = (
    "subject",
    "issuer",
    "san",
    "sha1",
    "sha256",
    "algorithm",
    "keyAlgorithm",
    "keyLength",
)
_VALIDITY_FIELDS: Final = ("issuedOn", "expiresOn", "period", "healthy")
_GENERATE_FIELDS: Final = (
    ("common_name", "cN"),
    ("organization", "o"),
    ("organization_unit", "oU"),
    ("locality", "l"),
    ("state", "sT"),
    ("country", "c"),
)

_HIDDEN_TEXT: Final = "[MEHO hid this text: it held a secret from Vault]"


@dataclass(slots=True)
class _RefTarget:
    """Points the shared Vault read at the path a call names, not the target's login."""

    name: str
    host: str
    secret_ref: str | None


class FleetLockerVendorError(ConnectorUpstreamError):
    """The appliance refused a locker change that sends a secret.

    MEHO withholds the vendor text of such an op. ``remediation`` carries
    the vendor's own message after a check that it holds no secret from
    Vault, so the caller still learns why the appliance said no.
    """

    def __init__(self, message: str, *, http_status: int, detail: str) -> None:
        super().__init__(message, http_status=http_status, upstream_message=detail)
        self.remediation = f"The appliance said: {detail}"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _text(params: dict[str, Any], name: str, *, required: bool = True) -> str | None:
    value = params.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def path_id(params: dict[str, Any], name: str) -> str:
    """One id that goes into a URL path. Refuses '/' and control characters."""
    value = _text(params, name) or ""
    if "/" in value or any(ch < " " or ch == "\x7f" for ch in value):
        raise ValueError(f"{name} must be one id, without '/' or control characters")
    return quote(value, safe="")


def _pick(raw: Any, fields: Iterable[str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return {field: raw.get(field) for field in fields}


def parse_secret_ref(raw: Any, name: str) -> tuple[str, str]:
    """Check a ``"<vault-path>#<field>"`` reference and return ``(path, field)``.

    Raises :class:`ValueError` before any Vault or vendor call. The message
    names the parameter, never a value.
    """
    if not isinstance(raw, str) or raw.count("#") != 1:
        raise ValueError(f"{name} must be one '<vault-path>#<field>' reference")
    if any(ch < " " or ch == "\x7f" for ch in raw):
        raise ValueError(f"{name} contains a control character")
    path, _, field = raw.partition("#")
    path, field = path.strip(), field.strip()
    if not path or not field:
        raise ValueError(f"{name} must give a vault path and a field: '<vault-path>#<field>'")
    return path, field


async def _read_secret(
    operator: Operator, target: VcfFleetTargetLike, params: dict[str, Any], name: str
) -> str:
    """Read the secret a ``*_ref`` parameter names, under the caller's identity."""
    path, field = parse_secret_ref(params.get(name), name)
    data = await load_param_secret_data(
        _RefTarget(name=target.name, host=target.host, secret_ref=path),
        operator,
        param=name,
    )
    value = data.get(field) if isinstance(data, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"{name}: the secret at {path!r} has no text under the field {field!r}. "
            "Nothing was sent."
        )
    return value.strip()


def _holds_secret(text: str, secrets: Iterable[str]) -> bool:
    """True when *text* holds a secret, a line of one, or its JSON-escaped form."""
    for secret in secrets:
        pieces = {secret, json.dumps(secret)[1:-1]}
        pieces |= {line.strip() for line in secret.splitlines() if len(line.strip()) >= 16}
        if any(piece and piece in text for piece in pieces):
            return True
    return False


def vendor_error_text(response: httpx.Response) -> str:
    """The vendor's ``errorCode``, ``message`` and ``recommendations``, or the raw body."""
    try:
        body = response.json()
    except ValueError:
        return (response.text or "").strip()[:2000]
    if not isinstance(body, dict):
        return json.dumps(body)[:2000]
    parts = [str(body[key]) for key in ("errorCode", "errorLabel", "message") if body.get(key)]
    recommendations = body.get("recommendations")
    if isinstance(recommendations, list) and recommendations:
        parts.append("recommendations: " + "; ".join(str(item) for item in recommendations))
    return (" | ".join(parts) or json.dumps(body))[:2000]


async def _send_secret_body(
    connector: VcfFleetConnector,
    operator: Operator,
    target: VcfFleetTargetLike,
    path: str,
    body: dict[str, Any],
    secrets: tuple[str, ...],
) -> dict[str, Any]:
    """POST a body that holds a secret. A refusal keeps the vendor text, minus the secret."""
    try:
        return await connector._post_json(target, path, operator=operator, json=body)
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        detail = vendor_error_text(exc.response)
        if _holds_secret(detail, secrets):
            detail = _HIDDEN_TEXT
        raise FleetLockerVendorError(
            f"the appliance answered HTTP {status} to POST {path}",
            http_status=status,
            detail=f"HTTP {status}: {detail}",
        ) from None


async def _read_pages(
    connector: VcfFleetConnector,
    operator: Operator,
    target: VcfFleetTargetLike,
    path: str,
    *,
    key: str,
    query: dict[str, Any],
) -> tuple[list[dict[str, Any]], int | None]:
    """Read every page of a locker list. Stops when a page brings no new row."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    total: int | None = None
    for _ in range(_MAX_PAGES):
        page: Any = await connector._get_json(
            target, path, operator=operator, params={**query, "from": len(rows), "size": PAGE_SIZE}
        )
        if not isinstance(page, dict):
            break
        if isinstance(page.get("total"), int):
            total = page["total"]
        items = [item for item in page.get(key) or [] if isinstance(item, dict)]
        new = [item for item in items if _row_key(item) not in seen]
        rows.extend(new)
        seen.update(_row_key(item) for item in new)
        if not new or len(items) < PAGE_SIZE or (total is not None and len(rows) >= total):
            break
    return rows, total


def _row_key(row: dict[str, Any]) -> str:
    return str(row.get("vmid") or row.get("alias") or json.dumps(row, sort_keys=True))


# ---------------------------------------------------------------------------
# Locker passwords
# ---------------------------------------------------------------------------


async def password_list(
    connector: VcfFleetConnector,
    operator: Operator,
    target: VcfFleetTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``fleet.locker.password.list``: one row per entry, never the password."""
    alias = _text(params, "alias", required=False)
    query = {"aliasQuery": alias} if alias else {}
    raw, total = await _read_pages(
        connector, operator, target, PASSWORDS_PATH, key="passwords", query=query
    )
    rows = [_pick(item, _PASSWORD_FIELDS) for item in raw]
    return {"passwords": rows, "count": len(rows), "total": total}


async def password_add(
    connector: VcfFleetConnector,
    operator: Operator,
    target: VcfFleetTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``fleet.locker.password.add``: the password is read from Vault and never returned."""
    alias = _text(params, "alias")
    user_name = _text(params, "user_name")
    description = _text(params, "description", required=False)
    password = await _read_secret(operator, target, params, "password_ref")
    body: dict[str, Any] = {"alias": alias, "userName": user_name, "password": password}
    if description:
        body["passwordDescription"] = description
    answer = await _send_secret_body(connector, operator, target, PASSWORDS_PATH, body, (password,))
    row = _pick(answer, _PASSWORD_FIELDS)
    return {"status": "added", **row, "alias": row.get("alias") or alias}


# ---------------------------------------------------------------------------
# Locker certificates
# ---------------------------------------------------------------------------


def _certificate_row(raw: dict[str, Any]) -> dict[str, Any]:
    row = _pick(raw, _CERTIFICATE_FIELDS)
    validity = raw.get("validity")
    row["expiresOn"] = (
        validity.get("expiresOn") if isinstance(validity, dict) else raw.get("expiresOn")
    )
    return row


async def certificate_list(
    connector: VcfFleetConnector,
    operator: Operator,
    target: VcfFleetTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``fleet.locker.certificate.list``: one row per certificate, never a key."""
    alias = _text(params, "alias", required=False)
    query = {"aliasQuery": alias} if alias else {}
    raw, total = await _read_pages(
        connector, operator, target, CERTIFICATES_PATH, key="certificates", query=query
    )
    rows = [_certificate_row(item) for item in raw]
    return {"certificates": rows, "count": len(rows), "total": total}


def _certificate_answer(answer: Any, alias: str, mode: str) -> dict[str, Any]:
    """The safe part of a certificate answer: no key, no chain, no leaf text."""
    raw = answer if isinstance(answer, dict) else {}
    validations = raw.get("validations")
    return {
        "status": "imported" if mode == "import" else "generated",
        "vmid": raw.get("vmid"),
        "alias": raw.get("alias") or alias,
        "certInfo": _pick(raw.get("certInfo"), _CERT_INFO_FIELDS) or None,
        "validity": _pick(raw.get("validity"), _VALIDITY_FIELDS) or None,
        "validations": validations if isinstance(validations, list) else [],
    }


def _generate_body(params: dict[str, Any], alias: str) -> dict[str, Any]:
    body: dict[str, Any] = {"alias": alias}
    for name, vendor_name in _GENERATE_FIELDS:
        value = _text(params, name, required=name == "common_name")
        if value:
            body[vendor_name] = value
    for name, vendor_name in (("hostnames", "host"), ("ip_addresses", "ip")):
        values = params.get(name) or []
        if not isinstance(values, list) or not all(isinstance(v, str) and v for v in values):
            raise ValueError(f"{name} must be a list of non-empty strings")
        if values:
            body[vendor_name] = values
    if params.get("key_size") is not None:
        body["size"] = int(params["key_size"])
    return body


async def certificate_import(
    connector: VcfFleetConnector,
    operator: Operator,
    target: VcfFleetTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``fleet.locker.certificate.import``: import a PEM chain, or generate one."""
    alias = _text(params, "alias") or ""
    mode = params.get("mode")
    if mode == "generate":
        body = _generate_body(params, alias)
        answer = await _send_secret_body(connector, operator, target, CERTIFICATES_PATH, body, ())
        return _certificate_answer(answer, alias, "generate")
    if mode != "import":
        raise ValueError("mode must be 'import' or 'generate'")
    chain = _text(params, "certificate_chain")
    key = await _read_secret(operator, target, params, "private_key_ref")
    body = {"alias": alias, "certificateChain": chain, "privateKey": key}
    secrets: tuple[str, ...] = (key,)
    if params.get("passphrase_ref") is not None:
        passphrase = await _read_secret(operator, target, params, "passphrase_ref")
        body["passphrase"] = passphrase
        secrets = (key, passphrase)
    answer = await _send_secret_body(
        connector, operator, target, CERTIFICATE_IMPORT_PATH, body, secrets
    )
    return _certificate_answer(answer, alias, "import")
