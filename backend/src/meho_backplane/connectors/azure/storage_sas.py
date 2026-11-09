# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Sign a read-only download link for one blob (#4169).

``azure.storage.blob.link.create`` asks Azure for a user delegation key and
signs the link itself, as Microsoft's "Create a user delegation SAS"
describes for Blob service version 2026-04-06 and later. Nothing here sends a
request.

* :func:`parse_user_delegation_key` reads Azure's ``UserDelegationKey`` answer
  (XML, read with ``defusedxml``).
* :func:`string_to_sign` builds the 28 lines Azure signs; the fields MEHO does
  not use are empty lines.
* :func:`sign_blob_read_sas` returns the query part of the link: read only
  (``sp=r``), one blob (``sr=b``), HTTPS only, one IPv4 address.
* :func:`blob_link` puts the account's blob host, the file and the query part
  together.

The key and the link are secrets. :class:`UserDelegationKey` hides its value
when it is printed, and no function here logs.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import quote, urlencode

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import ParseError, fromstring

from meho_backplane.connectors.azure.storage_parse import StorageReadError

__all__ = [
    "UserDelegationKey",
    "blob_link",
    "parse_user_delegation_key",
    "sign_blob_read_sas",
    "string_to_sign",
]

#: The elements of Azure's ``UserDelegationKey`` answer, by field of :class:`UserDelegationKey`.
_KEY_ELEMENTS: Final[dict[str, str]] = {
    "signed_oid": "SignedOid",
    "signed_tid": "SignedTid",
    "signed_start": "SignedStart",
    "signed_expiry": "SignedExpiry",
    "signed_service": "SignedService",
    "signed_version": "SignedVersion",
    "value": "Value",
}

_UNREADABLE_REMEDIATION: Final[str] = (
    "Try again later. If it keeps failing, check that the storage account allows "
    "Entra ID logins for the Blob service."
)


@dataclass(frozen=True, repr=False)
class UserDelegationKey:
    """Azure's user delegation key. ``value`` is the secret; it is never printed."""

    signed_oid: str
    signed_tid: str
    signed_start: str
    signed_expiry: str
    signed_service: str
    signed_version: str
    value: str

    def __repr__(self) -> str:
        return f"<UserDelegationKey oid={self.signed_oid} expiry={self.signed_expiry}>"

    __str__ = __repr__


def parse_user_delegation_key(xml_text: str) -> UserDelegationKey:
    """Read Azure's ``UserDelegationKey`` answer.

    Raises :class:`StorageReadError` when the answer is not XML or misses an
    element; the message names the element, never a value.
    """
    try:
        root = fromstring(xml_text, forbid_dtd=True)
    except (ParseError, DefusedXmlException) as exc:
        raise StorageReadError(
            "Azure's user delegation key answer is not the XML Azure documents.",
            remediation=_UNREADABLE_REMEDIATION,
        ) from exc
    found: dict[str, str] = {}
    for field, element in _KEY_ELEMENTS.items():
        node: Any = root.find(element)
        text = node.text.strip() if node is not None and node.text else ""
        if not text:
            raise StorageReadError(
                f"Azure's user delegation key answer has no {element}.",
                remediation=_UNREADABLE_REMEDIATION,
            )
        found[field] = text
    return UserDelegationKey(**found)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def string_to_sign(
    key: UserDelegationKey,
    *,
    account: str,
    container: str,
    blob: str,
    start: datetime,
    expiry: datetime,
    ip: str,
    version: str,
) -> str:
    """The 28 lines Azure signs for a read-only blob link (version 2026-04-06 and later)."""
    lines = [
        "r",  # signedPermissions
        _iso(start),
        _iso(expiry),
        f"/blob/{account}/{container}/{blob}",  # canonicalizedResource, not encoded
        key.signed_oid,
        key.signed_tid,
        key.signed_start,
        key.signed_expiry,
        key.signed_service,
        key.signed_version,
        "",  # signedAuthorizedUserObjectId
        "",  # signedUnauthorizedUserObjectId
        "",  # signedCorrelationId
        "",  # signedKeyDelegatedUserTenantId
        "",  # signedDelegatedUserObjectId
        ip,  # signedIP
        "https",  # signedProtocol
        version,  # signedVersion
        "b",  # signedResource
        "",  # signedSnapshotTime
        "",  # signedEncryptionScope
        "",  # canonicalizedSignedRequestHeaders
        "",  # canonicalizedSignedRequestQueryParameters
        "",  # rscc
        "",  # rscd
        "",  # rsce
        "",  # rscl
        "",  # rsct
    ]
    return "\n".join(lines)


def sign_blob_read_sas(
    key: UserDelegationKey,
    *,
    account: str,
    container: str,
    blob: str,
    start: datetime,
    expiry: datetime,
    ip: str,
    version: str,
) -> str:
    """The query part of a read-only link for one blob, signed with *key* (no leading ``?``)."""
    text = string_to_sign(
        key,
        account=account,
        container=container,
        blob=blob,
        start=start,
        expiry=expiry,
        ip=ip,
        version=version,
    )
    digest = hmac.new(base64.b64decode(key.value), text.encode("utf-8"), hashlib.sha256).digest()
    return urlencode(
        {
            "sp": "r",
            "st": _iso(start),
            "se": _iso(expiry),
            "skoid": key.signed_oid,
            "sktid": key.signed_tid,
            "skt": key.signed_start,
            "ske": key.signed_expiry,
            "sks": key.signed_service,
            "skv": key.signed_version,
            "sip": ip,
            "spr": "https",
            "sv": version,
            "sr": "b",
            "sig": base64.b64encode(digest).decode("ascii"),
        }
    )


def blob_link(blob_host: str, container: str, blob: str, sas: str) -> str:
    """The download link: the account's blob host, the encoded file path and *sas*."""
    return f"https://{blob_host}/{quote(container, safe='')}/{quote(blob, safe='/')}?{sas}"
