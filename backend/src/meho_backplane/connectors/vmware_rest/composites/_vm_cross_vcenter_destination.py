# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The other vCenter of ``vm.migrate_cross_vcenter``: a pinned, checked login.

Before MEHO puts the other vCenter's login into the move request, it must
know two things: that it talks to the vCenter the ``ssl_thumbprint`` of the
target names, and that the login from Vault works there. Both are checked on
one short-lived HTTP client of their own:

* **Every TLS connection is checked.** The client's network backend wraps
  each new connection; right after the TLS handshake, and before any byte
  of a request goes out, the SHA-1 hash of the certificate the server
  showed is compared with the thumbprint. A different certificate closes
  the connection and raises :class:`DestinationCertificateMismatchError`. So a
  second connection (a new one after a keep-alive ended, a retry) cannot
  show another certificate and still get the login. The target's own TLS
  settings (``verify_tls``, ``tls_ca_pin``, ``tls_server_name``) and the
  address pin of the connector's transport stay on; the thumbprint check
  comes on top.
* **The login is tested as such.** ``POST /api/session`` with the exact
  username and password from Vault, never a cached session of the
  connector. A refused login is :class:`DestinationLoginFailedError`.

The session then serves the reads on the other vCenter (``instanceUuid`` of
its ``ServiceContent``, the target objects of the move). The connector's
pooled client and cached session of that target are not used at all.
"""

from __future__ import annotations

import hashlib
import ssl
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Final

import httpcore
import httpx
import structlog

from meho_backplane.connectors.vmware_rest._mount import (
    SESSION_PATH_MODERN,
    vmomi_mounted_path,
    vmomi_release_from_version,
)
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import (
    IncompleteAnswerError,
    _json_fault_type,
    _objects,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _VMOMI_RETRIEVE_PROPERTIES_PATH,
)
from meho_backplane.connectors.vmware_rest.vim_body import (
    retrieve_properties_body,
    unwrap_vim_value,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "DestinationCertificateMismatchError",
    "DestinationLoginFailedError",
    "DestinationSession",
    "certificate_thumbprint",
    "open_destination",
    "pin_client_to_thumbprint",
]

_log = structlog.get_logger(__name__)

_SERVICE_CONTENT_PATH: Final = "/ServiceInstance/ServiceInstance/RetrieveServiceContent"
#: Served without a login on every vCenter; the first request on the pinned client.
_SERVICE_VERSIONS_PATH: Final = "/sdk/vimServiceVersions.xml"
_SESSION_HEADER: Final = "vmware-api-session-id"
_MO_NOT_FOUND_FAULT: Final = "ManagedObjectNotFound"
_LOGIN_REFUSED_STATUSES: Final = frozenset({401, 403})


class DestinationCertificateMismatchError(Exception):
    """A TLS connection to the other vCenter showed a certificate with another hash."""

    def __init__(self, expected: str, shown: str | None) -> None:
        super().__init__(f"certificate hash {shown or 'none'} is not the thumbprint {expected}")
        self.expected = expected
        self.shown = shown


class DestinationLoginFailedError(Exception):
    """The login from Vault was refused, incomplete, or gave no ``instanceUuid``."""


def certificate_thumbprint(der: bytes) -> str:
    """The SHA-1 hash of a DER certificate as 40 lower-case hex digits.

    SHA-1 is what vCenter's ``sslThumbprint`` is; it names the certificate,
    it does not protect anything.
    """
    return hashlib.new("sha1", der, usedforsecurity=False).hexdigest()


class _ThumbprintStream(httpcore.AsyncNetworkStream):
    """A connection whose TLS handshake must show the expected certificate."""

    def __init__(self, inner: httpcore.AsyncNetworkStream, thumbprint: str) -> None:
        self._inner = inner
        self._thumbprint = thumbprint

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return await self._inner.read(max_bytes, timeout=timeout)

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        await self._inner.write(buffer, timeout=timeout)

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        tls = await self._inner.start_tls(
            ssl_context, server_hostname=server_hostname, timeout=timeout
        )
        ssl_object = tls.get_extra_info("ssl_object")
        der = ssl_object.getpeercert(binary_form=True) if ssl_object is not None else None
        shown = certificate_thumbprint(der) if der else None
        if shown != self._thumbprint:
            await tls.aclose()
            raise DestinationCertificateMismatchError(self._thumbprint, shown)
        return tls

    def get_extra_info(self, info: str) -> Any:
        return self._inner.get_extra_info(info)


class _ThumbprintBackend(httpcore.AsyncNetworkBackend):
    """Network backend whose every new connection is a :class:`_ThumbprintStream`."""

    def __init__(self, inner: httpcore.AsyncNetworkBackend, thumbprint: str) -> None:
        self._inner = inner
        self._thumbprint = thumbprint

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        stream = await self._inner.connect_tcp(
            host, port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )
        return _ThumbprintStream(stream, self._thumbprint)

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        stream = await self._inner.connect_unix_socket(
            path, timeout=timeout, socket_options=socket_options
        )
        return _ThumbprintStream(stream, self._thumbprint)

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def pin_client_to_thumbprint(client: httpx.AsyncClient, thumbprint: str) -> None:
    """Make every new connection of *client* check the server certificate's hash.

    *client* must run on an :class:`httpx.AsyncHTTPTransport` (the connector's
    pinned transport is one). The transport's connection pool keeps its
    settings; only its network backend is wrapped, the way the connector's address pin wraps it.
    """
    transport = client._transport
    if not isinstance(transport, httpx.AsyncHTTPTransport):
        raise TypeError(f"cannot pin a client on a {type(transport).__name__}")
    pool = transport._pool
    pool._network_backend = _ThumbprintBackend(pool._network_backend, thumbprint)


class DestinationSession:
    """A logged-in, certificate-pinned session on the other vCenter."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        name: str,
        extensions: dict[str, Any],
        release: str | None,
    ) -> None:
        self._client = client
        self._name = name
        self._extensions = extensions
        self._release = release
        self._headers: dict[str, str] = {}
        self.login: dict[str, str] = {}
        self.instance_uuid: str = ""

    async def log_in(self, login: dict[str, str]) -> None:
        """``POST /api/session`` with exactly *login*; then read ``instanceUuid``."""
        username, password = login.get("username"), login.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            raise DestinationLoginFailedError(
                f"the Vault login of target {self._name!r} is incomplete"
            )
        response = await self._client.post(
            SESSION_PATH_MODERN, auth=(username, password), extensions=self._extensions
        )
        if response.status_code in _LOGIN_REFUSED_STATUSES:
            raise DestinationLoginFailedError(
                f"target {self._name!r} refused the login from Vault (HTTP {response.status_code})"
            )
        response.raise_for_status()
        token = response.json()
        if isinstance(token, dict):
            token = token.get("value")
        if not isinstance(token, str) or not token:
            raise DestinationLoginFailedError(f"target {self._name!r} gave no session token")
        self._headers = {_SESSION_HEADER: token}
        self.login = {"username": username, "password": password}
        content = unwrap_vim_value(await self.post(_SERVICE_CONTENT_PATH, {}))
        about = content.get("about") if isinstance(content, dict) else None
        uuid = about.get("instanceUuid") if isinstance(about, dict) else None
        if not isinstance(uuid, str) or not uuid:
            raise DestinationLoginFailedError(f"target {self._name!r} gave no instanceUuid")
        self.instance_uuid = uuid

    async def post(self, vmomi_path: str, body: dict[str, Any]) -> Any:
        """One VI-JSON method on the pinned session; HTTP errors propagate."""
        path = (
            vmomi_mounted_path(self._release, vmomi_path)
            if self._release is not None
            else f"/api{vmomi_path}"
        )
        response = await self._client.post(
            path, json=body, headers=self._headers, extensions=self._extensions
        )
        response.raise_for_status()
        return response.json()

    async def read_object(
        self, *, mo_type: str, moid: str, props: list[str]
    ) -> dict[str, Any] | None:
        """*props* of one object in the other vCenter; ``None`` when it does not exist.

        The same contract as the shared ``read_object`` of the composites: a
        ``ManagedObjectNotFound`` fault or an empty answer is absent, and a
        property vCenter could not read raises :class:`IncompleteAnswerError`.
        """
        try:
            result = await self.post(
                _VMOMI_RETRIEVE_PROPERTIES_PATH, retrieve_properties_body(mo_type, [moid], props)
            )
        except httpx.HTTPStatusError as exc:
            if _json_fault_type(exc) == _MO_NOT_FOUND_FAULT:
                return None
            raise
        found = _objects(result).get(moid)
        if found is None:
            return None
        values, missing = found
        unread = sorted(missing & set(props))
        if unread:
            raise IncompleteAnswerError(
                f"the other vCenter could not read {', '.join(unread)} of {mo_type} {moid!r}"
            )
        return values

    async def aclose(self) -> None:
        """Log out (best effort) and close the client."""
        try:
            if self._headers:
                await self._client.delete(
                    SESSION_PATH_MODERN, headers=self._headers, extensions=self._extensions
                )
        except httpx.HTTPError:
            _log.info("vm_migrate_cross_vcenter_destination_logout_failed", target=self._name)
        finally:
            await self._client.aclose()


async def open_destination(
    connector: VmwareRestConnector, dest: Any, operator: Operator, thumbprint: str
) -> DestinationSession:
    """A :class:`DestinationSession` on *dest*, pinned to *thumbprint* and logged in.

    Order: the pinned client, one read of the version file (the first TLS
    connection; its certificate is checked before the request goes out),
    the Vault read, the login, the ``instanceUuid`` read. Raises
    :class:`DestinationCertificateMismatchError`, :class:`DestinationLoginFailedError`,
    the Vault reader's error, or :class:`httpx.HTTPError`. The caller owns
    the session and closes it.
    """
    client = await connector._ephemeral_http_client(dest)
    try:
        pin_client_to_thumbprint(client, thumbprint)
        extensions = connector._request_extensions(dest)
        response = await client.get(_SERVICE_VERSIONS_PATH, extensions=extensions)
        response.raise_for_status()
        from meho_backplane.connectors.vmware_rest.connector import service_versions_api_version

        release = vmomi_release_from_version(service_versions_api_version(response.text))
        session = DestinationSession(
            client, name=str(getattr(dest, "name", "")), extensions=extensions, release=release
        )
    except BaseException:
        await client.aclose()
        raise
    try:
        await session.log_in(await connector._session_loader(dest, operator))
    except BaseException:
        await session.aclose()
        raise
    return session
