# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pure helpers of the Azure storage reads.

Nothing here calls Azure. Three kinds of functions:

* **Parameter checks.** :func:`account_name`, :func:`container_name`,
  :func:`resource_group_name`, :func:`blob_name`, :func:`blob_prefix` and
  :func:`max_results` check a caller's value before it goes into a request.
  A value that is not usable raises :class:`StorageParameterError` before
  any request is made. :func:`blob_host` builds the blob host only from a
  checked account name, so no parameter can change the host a token goes to.
* **Rows.** :func:`account_row`, :func:`container_row` and :func:`blob_row`
  turn one item of an Azure answer into one row of a MEHO answer. Only the
  listed fields are copied, each with a type check. An odd answer gives
  ``None`` in a field, never an error, and an account key, a SAS token or a
  connection string in an answer never reaches a result.
* **Data-plane answers.** :func:`blob_page` reads one List Blobs answer
  (XML, read with ``defusedxml``, which refuses entity and DTD tricks).
  :func:`file_properties` reads one file's properties from the answer
  headers, and :func:`text_content` decides whether a file's bytes are text.

Text from Azure (names, content types, file content) is copied as data. It
is never used to build a request.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import unquote

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import ParseError, fromstring

from meho_backplane.connectors.azure._paging import MAX_ROWS, PagedRows

if TYPE_CHECKING:
    from xml.etree.ElementTree import Element

    import httpx

__all__ = [
    "BLOB_HOST_SUFFIXES",
    "DEFAULT_MAX_RESULTS",
    "MAX_TEXT_BYTES",
    "FileProperties",
    "StorageAccount",
    "StorageParameterError",
    "StorageReadError",
    "account_name",
    "account_row",
    "binary_main_type",
    "blob_host",
    "blob_name",
    "blob_page",
    "blob_prefix",
    "blob_row",
    "container_name",
    "container_row",
    "file_properties",
    "http_date_to_iso",
    "list_answer",
    "max_results",
    "md5_hex",
    "resource_group_name",
    "resource_group_of",
    "storage_account",
    "text_content",
]

#: The most bytes ``azure.storage.blob.read`` returns: 1 MiB.
MAX_TEXT_BYTES: Final[int] = 1024 * 1024

#: The files ``azure.storage.blob.list`` returns when the caller gives no
#: ``max_results``: one full page of Azure's List Blobs.
DEFAULT_MAX_RESULTS: Final[int] = 5000

#: The longest blob name, and so the longest prefix, Azure allows.
_BLOB_NAME_MAX: Final[int] = 1024

#: The blob host suffix of each Azure cloud (Microsoft's documented
#: ``<account>.blob.<suffix>`` endpoints).
BLOB_HOST_SUFFIXES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "AzureCloud": "blob.core.windows.net",
        "AzureUSGovernment": "blob.core.usgovcloudapi.net",
        "AzureChinaCloud": "blob.core.chinacloudapi.cn",
    }
)

#: A storage account name, Azure's rule: 3 to 24 lower-case letters and
#: digits. Nothing else passes, so the name can go into a host name as it is.
_ACCOUNT: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]{3,24}")

#: A container name, Azure's rule: 3 to 63 lower-case letters, digits and
#: single hyphens, with a letter or digit first and last. Or one of Azure's
#: special containers ``$root``, ``$web`` and ``$logs``.
_CONTAINER: Final[re.Pattern[str]] = re.compile(
    r"\$root|\$web|\$logs|[a-z0-9](?:[a-z0-9]|-(?=[a-z0-9])){2,62}"
)

#: A resource group name, Azure's rule: 1 to 90 letters, digits, ``_``,
#: ``-``, ``.``, ``(`` and ``)``, not ending in ``.``. No slash, ``?``, ``#``,
#: ``%`` or space can pass, so the name cannot change the path.
_RESOURCE_GROUP: Final[re.Pattern[str]] = re.compile(r"[-\w.()]{0,89}[-\w()]")

#: Control characters a text file never holds: all below 0x20 except tab,
#: line feed, vertical tab, form feed, carriage return and escape (colour
#: codes in logs). NUL is one of them.
_NOT_TEXT: Final[re.Pattern[str]] = re.compile(r"[\x00-\x08\x0e-\x1a\x1c-\x1f]")

#: Main content types that are never text. A file with one is refused before
#: its bytes are read.
_BINARY_MAIN_TYPES: Final[frozenset[str]] = frozenset({"image", "audio", "video", "font", "model"})


class StorageParameterError(ValueError):
    """A parameter of an Azure storage read is not usable. Raised before any request.

    The message names the target and the parameter, never the value. The
    dispatcher returns it as ``connector_error`` with ``remediation``.
    """

    def __init__(self, message: str, *, remediation: str) -> None:
        super().__init__(message)
        self.remediation = remediation


class StorageReadError(ValueError):
    """MEHO refused a file or a storage account, or could not read an answer.

    For example a file over :data:`MAX_TEXT_BYTES`, a file that is not text,
    an account without the standard blob endpoint, or a List Blobs answer
    that is not the XML Azure documents. The message is MEHO's own text and
    never repeats file content.
    """

    def __init__(self, message: str, *, remediation: str) -> None:
        super().__init__(message)
        self.remediation = remediation


# ----------------------------------------------------------------------
# Parameter checks
# ----------------------------------------------------------------------


def _refused(
    target_name: str, parameter: str, rule: str, remediation: str
) -> StorageParameterError:
    return StorageParameterError(
        f"Azure target {target_name!r}: the parameter {parameter!r} {rule}. MEHO sent no request.",
        remediation=remediation,
    )


def account_name(value: object, target_name: str) -> str:
    """Return *value* when it is a storage account name Azure allows, or fail."""
    if isinstance(value, str) and _ACCOUNT.fullmatch(value):
        return value
    raise _refused(
        target_name,
        "account",
        "must be a storage account name: 3 to 24 lower-case letters and digits",
        "Use the name field of azure.storage.account.list.",
    )


def container_name(value: object, target_name: str) -> str:
    """Return *value* when it is a container name Azure allows, or fail."""
    if isinstance(value, str) and _CONTAINER.fullmatch(value):
        return value
    raise _refused(
        target_name,
        "container",
        "must be a container name: 3 to 63 lower-case letters, digits and single "
        "hyphens, or $root, $web or $logs",
        "Use the name field of azure.storage.container.list.",
    )


def resource_group_name(value: object, target_name: str) -> str:
    """Return *value* when it is a resource group name Azure allows, or fail."""
    if isinstance(value, str) and _RESOURCE_GROUP.fullmatch(value):
        return value
    raise _refused(
        target_name,
        "resource_group",
        "must be a resource group name: 1 to 90 letters, digits, '_', '-', '.', '(' "
        "and ')', not ending in '.'",
        "Use the resource_group field of azure.storage.account.list.",
    )


def blob_name(value: object, target_name: str) -> str:
    """Return *value* when it can name one file (blob) in a request, or fail.

    1 to 1,024 characters, and no part between slashes may be ``.`` or
    ``..``: an HTTP client removes such parts from a path, so the request
    would read another file, maybe in another container.
    """
    if (
        isinstance(value, str)
        and 0 < len(value) <= _BLOB_NAME_MAX
        and not any(part in (".", "..") for part in value.split("/"))
    ):
        return value
    raise _refused(
        target_name,
        "name",
        "must be a file name of 1 to 1,024 characters with no '.' or '..' part between slashes",
        "Use the name field of azure.storage.blob.list.",
    )


def blob_prefix(value: object, target_name: str) -> str | None:
    """Return *value* as the start of the file names to list, or ``None`` for all."""
    if value is None or value == "":
        return None
    if isinstance(value, str) and len(value) <= _BLOB_NAME_MAX:
        return value
    raise _refused(
        target_name,
        "prefix",
        "must be text of at most 1,024 characters",
        "Give the start of the file names, for example backups/2026-10/.",
    )


def max_results(value: object, target_name: str) -> int:
    """Return the most files to list: *value*, or :data:`DEFAULT_MAX_RESULTS`."""
    if value is None:
        return DEFAULT_MAX_RESULTS
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_ROWS:
        return value
    raise _refused(
        target_name,
        "max_results",
        f"must be a whole number from 1 to {MAX_ROWS:,}",
        f"Leave it out for {DEFAULT_MAX_RESULTS:,} files, or give a smaller number.",
    )


def blob_host(account: str, cloud_name: str) -> str:
    """Return the blob host of *account* in the cloud *cloud_name*.

    The host is built only from a name that passes Azure's account rule
    (checked here again), so a parameter can never change the host the
    storage token goes to.
    """
    if not _ACCOUNT.fullmatch(account):
        raise ValueError("blob_host needs a checked storage account name")
    return f"{account}.{BLOB_HOST_SUFFIXES[cloud_name]}"


# ----------------------------------------------------------------------
# Resource Manager rows
# ----------------------------------------------------------------------


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _properties(item: Mapping[str, Any]) -> dict[str, Any]:
    properties = item.get("properties")
    return properties if isinstance(properties, dict) else {}


def resource_group_of(resource_id: object) -> str | None:
    """The resource group named in an Azure resource id, or ``None``."""
    if not isinstance(resource_id, str):
        return None
    parts = resource_id.split("/")
    for index, part in enumerate(parts[:-1]):
        if part.lower() == "resourcegroups":
            return parts[index + 1] or None
    return None


def list_answer(paged: PagedRows, rows: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    """The answer of a list read.

    ``value`` holds *rows* and ``count`` their number; then come the paging
    fields of :meth:`PagedRows.as_dict` and *extra*.
    """
    paging = {key: value for key, value in paged.as_dict().items() if key != "value"}
    return {"value": rows, "count": len(rows), **paging, **extra}


def account_row(item: Mapping[str, Any]) -> dict[str, Any]:
    """One row of ``azure.storage.account.list`` from one ``StorageAccount``.

    Azure documents an empty ``allowSharedKeyAccess`` as ``true``, so a
    missing value is shown as ``True``: the account accepts its keys.
    """
    properties = _properties(item)
    sku = item.get("sku")
    acls = properties.get("networkAcls")
    shared_key = properties.get("allowSharedKeyAccess")
    return {
        "name": _text(item.get("name")),
        "resource_group": resource_group_of(item.get("id")),
        "location": _text(item.get("location")),
        "kind": _text(item.get("kind")),
        "sku": _text(sku.get("name")) if isinstance(sku, dict) else None,
        "access_tier": _text(properties.get("accessTier")),
        "shared_key_access": True if shared_key is None else _bool(shared_key),
        "public_network_access": _text(properties.get("publicNetworkAccess")),
        "network_default_action": (
            _text(acls.get("defaultAction")) if isinstance(acls, dict) else None
        ),
    }


def container_row(item: Mapping[str, Any]) -> dict[str, Any]:
    """One row of ``azure.storage.container.list`` from one ``ListContainerItem``."""
    properties = _properties(item)
    versions = properties.get("immutableStorageWithVersioning")
    return {
        "name": _text(item.get("name")),
        "public_access": _text(properties.get("publicAccess")),
        "last_modified": _text(properties.get("lastModifiedTime")),
        "lease_state": _text(properties.get("leaseState")),
        "has_immutability_policy": _bool(properties.get("hasImmutabilityPolicy")),
        "has_legal_hold": _bool(properties.get("hasLegalHold")),
        "version_immutability": (
            _bool(versions.get("enabled")) if isinstance(versions, dict) else None
        ),
    }


@dataclass(frozen=True)
class StorageAccount:
    """A storage account found in the target's subscription, with its blob host."""

    name: str
    resource_group: str | None
    blob_host: str

    def as_dict(self) -> dict[str, Any]:
        """The ``account`` object of an answer: the name and the resource group."""
        return {"name": self.name, "resource_group": self.resource_group}


def storage_account(
    item: Mapping[str, Any], name: str, cloud_name: str, target_name: str
) -> StorageAccount:
    """The account *name* from its ``Storage Accounts - List`` item.

    The blob host is built from the checked *name* (:func:`blob_host`). It
    must also be the blob endpoint Azure reports for the account; an account
    with another endpoint (for example an Azure DNS zone endpoint, where the
    standard host may belong to another account) is refused.
    """
    host = blob_host(name, cloud_name)
    endpoints = _properties(item).get("primaryEndpoints")
    reported = endpoints.get("blob") if isinstance(endpoints, dict) else None
    if not isinstance(reported, str) or reported.strip().lower().rstrip("/") != f"https://{host}":
        raise StorageReadError(
            f"Azure target {target_name!r}: Azure reports no standard blob endpoint "
            f"({host}) for storage account {name!r}. MEHO reads files only at that "
            "endpoint.",
            remediation=(
                "The account may still be being created, or it uses another kind of "
                "endpoint (for example an Azure DNS zone endpoint), which MEHO does not "
                "read. Check the account in the Azure portal."
            ),
        )
    return StorageAccount(
        name=name, resource_group=resource_group_of(item.get("id")), blob_host=host
    )


# ----------------------------------------------------------------------
# Blob data plane
# ----------------------------------------------------------------------


def md5_hex(value: str | None) -> str | None:
    """Azure's base64 MD5 (``Content-MD5``) as 32 hex digits, or ``None``."""
    if not value:
        return None
    try:
        digest = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None
    return digest.hex() if len(digest) == 16 else None


def http_date_to_iso(value: str | None) -> str | None:
    """An HTTP date (RFC 1123, as Azure Storage writes it) in ISO 8601 UTC, or ``None``."""
    if not value:
        return None
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _int_text(value: str | None) -> int | None:
    if value is None or not value.isdigit():
        return None
    return int(value)


def _xml_text(element: Element | None) -> str | None:
    if element is None or element.text is None:
        return None
    return element.text.strip() or None


def _listed_name(element: Element | None) -> str | None:
    """A blob name of a List Blobs answer.

    Azure percent-encodes a name that holds a character XML cannot carry and
    marks it with ``Encoded="true"``; such a name is decoded here.
    """
    name = _xml_text(element)
    if name is not None and element is not None and element.get("Encoded") == "true":
        return unquote(name)
    return name


def blob_row(blob: Element) -> dict[str, Any]:
    """One row of ``azure.storage.blob.list`` from one ``<Blob>`` element."""
    properties = blob.find("Properties")

    def prop(tag: str) -> str | None:
        return _xml_text(properties.find(tag)) if properties is not None else None

    return {
        "name": _listed_name(blob.find("Name")),
        "size": _int_text(prop("Content-Length")),
        "last_modified": http_date_to_iso(prop("Last-Modified")),
        "content_md5": md5_hex(prop("Content-MD5")),
        "content_type": prop("Content-Type"),
        "access_tier": prop("AccessTier"),
    }


def blob_page(body: bytes, target_name: str) -> tuple[list[dict[str, Any]], str | None]:
    """The rows and the next marker of one List Blobs answer.

    The marker is ``None`` when the list is complete. Raises
    :class:`StorageReadError` when the answer is not the documented XML.
    """
    try:
        root = fromstring(body, forbid_dtd=True)
    except (ParseError, DefusedXmlException) as exc:
        raise _unreadable_list(target_name) from exc
    if root.tag != "EnumerationResults":
        raise _unreadable_list(target_name)
    rows = [blob_row(blob) for blob in root.iterfind("Blobs/Blob")]
    return rows, _xml_text(root.find("NextMarker"))


def _unreadable_list(target_name: str) -> StorageReadError:
    return StorageReadError(
        f"Azure target {target_name!r}: Azure Storage answered the blob list with "
        "something that is not the XML list Azure documents. MEHO returned nothing.",
        remediation="Try again. If it fails again, check the storage account in the Azure portal.",
    )


@dataclass(frozen=True)
class FileProperties:
    """One file's properties, read from the headers of a Get Blob Properties answer."""

    size: int | None
    content_type: str | None
    content_encoding: str | None
    content_md5: str | None
    last_modified: str | None
    access_tier: str | None
    etag: str | None

    def as_dict(self) -> dict[str, Any]:
        """The fields of the file a read returns."""
        return {
            "size": self.size,
            "content_type": self.content_type,
            "content_md5": self.content_md5,
            "last_modified": self.last_modified,
        }


def file_properties(headers: httpx.Headers) -> FileProperties:
    """The properties of one file from the headers Azure answered with.

    Only these headers are read. A header such as ``x-ms-copy-source``,
    which can hold a SAS token, is never copied.
    """
    return FileProperties(
        size=_int_text(headers.get("content-length")),
        content_type=_text(headers.get("content-type")),
        content_encoding=_text(headers.get("content-encoding")),
        content_md5=md5_hex(headers.get("content-md5")),
        last_modified=http_date_to_iso(headers.get("last-modified")),
        access_tier=_text(headers.get("x-ms-access-tier")),
        etag=_text(headers.get("etag")),
    )


def binary_main_type(content_type: str | None) -> str | None:
    """The main content type when it is never text (image, audio, ...), else ``None``."""
    if not content_type:
        return None
    main = content_type.split("/", 1)[0].strip().lower()
    return main if main in _BINARY_MAIN_TYPES else None


def text_content(data: bytes) -> str | None:
    """*data* as text when it is UTF-8 text, else ``None``.

    A leading byte order mark is dropped. Bytes that are not valid UTF-8, or
    that hold a NUL or another control character text never has, are not
    text: binary formats (images, archives, programs) almost always hold
    them.
    """
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    return None if _NOT_TEXT.search(text) else text
