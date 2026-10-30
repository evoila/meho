# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Move a secret to another MEHO instance in two approved steps.

``secret.move`` works inside one MEHO: both sides use this instance's own
Vault. To move a target's login from instance A to instance B, the value
must cross from A's store into B's store. These two handlers do that
without a person or an agent ever seeing the value:

* :func:`secret_handoff_export` runs on **A**. It reads the value from a
  source (any registered source kind, so ``vault:`` keeps its tenant
  check) and puts it into a Vault **response-wrapping token** on A's own
  store (``sys/wrapping/wrap``). That token is the *handoff*: it can be
  used once, it has a short life, and it does not hold the value — the
  value stays in A's store until someone unwraps it. The result is the
  handoff, the value's SHA-256 and its length.
* :func:`secret_handoff_import` runs on **B**. It uses the handoff once
  against A's store (``sys/wrapping/unwrap``), reached through a target
  registered on B whose host is A's store. It writes the value into B's
  store through a normal sink (``vault:`` writes only under B's tenant
  path) and returns the SHA-256 and the length. A handoff that was
  already used, or has expired, fails with a plain error.

Why the unwrap needs no login on A's store: the handoff token *is* the
login for that one call. B never needs a long-lived credential for A's
store, and A never needs one for B's store.

**How B reaches A's store.** The unwrap is one HTTP ``POST``. It goes
over a plain ``httpx`` client on the shared address-pinning transport
(:mod:`meho_backplane.connectors._shared.pinned_transport`) with the
target destination guard (:mod:`meho_backplane.targets.ssrf_guard`) as
its resolver — the same screen every other target dial gets. The host is
screened before the request and again when the socket opens, and the
socket dials only an address that passed the screen. So a store target
whose DNS answer changed after it was registered cannot steer the
handoff token at a blocked address. The client follows no redirect: a
store that answers with one is reported, not followed.

The value is held only in a :class:`~.endpoints.SecretMaterial` and never
enters params, results, log events, or exception messages. The handoff
token is in A's result and in B's params; both ops are pinned as
credential classes in :mod:`meho_backplane.broadcast.events`, so the
approval row keeps B's params encrypted and the approval preview shows
only addresses (:mod:`.handoff_preview`).

**How errors reach the caller.** Both ops send a secret, so the
dispatcher withholds the raised message (``extras.exception_message``)
from the error result and keeps only ``extras.remediation``
(:mod:`meho_backplane.operations._vendor_text`). Every
:class:`SecretHandoffError` therefore carries a ``remediation`` written
by MEHO: the store, the Vault path, and what to do next. It never holds
text from the store, the sink, or a client library. The source and sink
adapters raise their own errors, which carry no remediation, so each
step maps what the adapter raises before the first store call and on
the source read (:func:`_read_source`, :func:`_check_sink`) into a
handoff error too.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Final, NoReturn

import httpx
import hvac
import hvac.exceptions
import requests.exceptions
import structlog

import meho_backplane.auth.vault as _auth_vault
from meho_backplane.connectors._shared.cache_key import DEFAULT_PORTS, effective_scheme
from meho_backplane.connectors._shared.pinned_transport import build_pinned_async_transport
from meho_backplane.connectors.secret.endpoints import (
    SecretEndpoint,
    SecretMaterial,
    parse_secret_ref,
    resolve_secret_endpoint,
)
from meho_backplane.connectors.vault.tenant_scope import VaultTenantScopeError
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import resolve_target
from meho_backplane.targets.ssrf_guard import (
    TargetDestinationBlockedError,
    assert_public_destination_async,
    screen_and_resolve,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator

__all__ = [
    "SecretHandoffDestinationBlockedError",
    "SecretHandoffDigestMismatchError",
    "SecretHandoffError",
    "SecretHandoffInvalidError",
    "SecretHandoffRefusedError",
    "SecretHandoffSinkWriteError",
    "SecretHandoffStoreError",
    "SecretHandoffUnreachableError",
    "secret_handoff_export",
    "secret_handoff_import",
]

_log = structlog.get_logger(__name__)

#: Vault API paths the two steps call. Named in every error message.
WRAP_PATH: Final[str] = "sys/wrapping/wrap"
UNWRAP_PATH: Final[str] = "sys/wrapping/unwrap"

#: The one key the wrapped payload carries.
_WRAP_FIELD: Final[str] = "value"

#: Handoff life when the caller does not set ``ttl_seconds``.
DEFAULT_HANDOFF_TTL_SECONDS: Final[int] = 3600

#: Name used for A's own store in error messages.
_LOCAL_STORE: Final[str] = "this instance's secret store"

#: Only a Vault-API target can be the store a handoff is unwrapped at.
#: OpenBao speaks the same API and registers under the same product.
_STORE_PRODUCT: Final[str] = "vault"

#: The header Vault reads the token from, and the marker hvac also sends.
_TOKEN_HEADER: Final[str] = "X-Vault-Token"
_REQUEST_HEADER: Final[str] = "X-Vault-Request"

#: What to do after a failed export, and after a failed import.
_RETRY_EXPORT: Final[str] = "then run secret.handoff.export again"
_NEW_HANDOFF: Final[str] = (
    "make a new handoff with secret.handoff.export on the first instance and import that one"
)


class SecretHandoffError(Exception):
    """Base class for a failed handoff step.

    The message names the store and the Vault path, never the value, the
    token, or text from a store, a sink, or a client library. Because
    every word is MEHO's own, the same text is the ``remediation`` the
    dispatcher keeps when it withholds the message of an operation that
    sends a secret.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.remediation = message


class SecretHandoffUnreachableError(SecretHandoffError):
    """The store could not be reached (DNS, TCP, TLS or timeout)."""


class SecretHandoffDestinationBlockedError(SecretHandoffError):
    """The store target is, or now resolves to, an address this MEHO does not dial."""


class SecretHandoffRefusedError(SecretHandoffError):
    """The store refused the login or the call (no right on the path)."""


class SecretHandoffInvalidError(SecretHandoffError):
    """The handoff is not valid: already used, expired, or made by another store."""


class SecretHandoffStoreError(SecretHandoffError):
    """The store answered with another error, a bad target, or a malformed reply."""


class SecretHandoffDigestMismatchError(SecretHandoffError):
    """The unwrapped value's SHA-256 is not the one the export reported."""


class SecretHandoffSinkWriteError(SecretHandoffError):
    """The handoff was used, but writing the value into the sink failed."""


def _raise_store_error(
    exc: Exception, *, store: str, path: str, action: str, retry: str
) -> NoReturn:
    """Map an hvac / requests / httpx failure to a plain handoff error.

    Only the store name, the path and the exception class appear in the
    message — never the payload, the token, or the library's own text.
    *retry* says what the caller does once the cause is fixed.
    """
    if isinstance(
        exc,
        requests.exceptions.ConnectionError
        | requests.exceptions.Timeout
        | httpx.TransportError
        | _auth_vault.VaultUnreachableError,
    ):
        raise SecretHandoffUnreachableError(
            f"could not reach {store} at path {path} to {action} ({type(exc).__name__}); "
            f"check the address and the network, {retry}"
        ) from exc
    if isinstance(exc, _auth_vault.VaultRoleDeniedError | hvac.exceptions.Forbidden):
        raise SecretHandoffRefusedError(
            f"{store} refused the login or the call at path {path}, so it could not {action}; "
            f"check the Vault role and the policy for that path, {retry}"
        ) from exc
    raise SecretHandoffStoreError(
        f"{store} failed at path {path} while trying to {action} ({type(exc).__name__}); "
        f"check the store, {retry}"
    ) from exc


# ---------------------------------------------------------------------------
# Step 1 on instance A — export
# ---------------------------------------------------------------------------


async def _read_source(
    operator: Operator, source: SecretEndpoint, source_name: str
) -> SecretMaterial:
    """Read the value from the source, in handoff-error terms.

    The source adapter logs in to this instance's store and reads the
    path. Its own errors carry no remediation, so a refused login, an
    unreachable store, a denied tenant path or a missing field is mapped
    here. *source_name* is the ``<kind>:<ref>`` address, never the value.
    """
    try:
        return await source.read_secret(operator)
    except VaultTenantScopeError as exc:
        raise SecretHandoffRefusedError(
            f"the source {source_name} is outside this operator's tenant path, so "
            f"{_LOCAL_STORE} was not asked; use a path under the tenant, {_RETRY_EXPORT}"
        ) from exc
    except (
        _auth_vault.VaultClientError,
        hvac.exceptions.VaultError,
        requests.exceptions.RequestException,
    ) as exc:
        _raise_store_error(
            exc, store=_LOCAL_STORE, path=source_name, action="read the value", retry=_RETRY_EXPORT
        )
    except (ValueError, NotImplementedError) as exc:
        raise SecretHandoffStoreError(
            f"the source {source_name} could not be read ({type(exc).__name__}); check that "
            f"the kind can be read from and that the path and the field exist, {_RETRY_EXPORT}"
        ) from exc


async def _wrap_on_local_store(
    operator: Operator, material: SecretMaterial, ttl_seconds: int
) -> tuple[str, int]:
    """Put *material* into a response-wrapping token on this instance's store.

    Logs in under the operator's own Vault identity (the same login every
    broker read uses). Returns ``(token, ttl_seconds)``.
    """
    payload = {_WRAP_FIELD: material.value.decode("utf-8")}
    try:
        async with _auth_vault.vault_client_for_operator(operator) as client:
            response = await asyncio.to_thread(client.sys.wrap, payload=payload, ttl=ttl_seconds)
    except (
        _auth_vault.VaultClientError,
        hvac.exceptions.VaultError,
        requests.exceptions.RequestException,
    ) as exc:
        _raise_store_error(
            exc, store=_LOCAL_STORE, path=WRAP_PATH, action="make a handoff", retry=_RETRY_EXPORT
        )
    wrap_info = response.get("wrap_info") if isinstance(response, dict) else None
    token = wrap_info.get("token") if isinstance(wrap_info, dict) else None
    if not isinstance(token, str) or not token:
        raise SecretHandoffStoreError(
            f"{_LOCAL_STORE} returned no handoff token at path {WRAP_PATH}; "
            f"check that the store allows response wrapping, {_RETRY_EXPORT}"
        )
    ttl = wrap_info.get("ttl") if isinstance(wrap_info, dict) else None
    return token, ttl if isinstance(ttl, int) else ttl_seconds


async def secret_handoff_export(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Read a secret and put it into a one-time handoff on this instance's store.

    Op-id: ``secret.handoff.export``. Returns ``status``, ``value_sha256``,
    ``length``, ``handoff_token`` and ``handoff_ttl_seconds``. The value
    itself is never returned.
    """
    del target
    source_ref = parse_secret_ref(str(params["from"]))
    source = resolve_secret_endpoint(source_ref)
    material = await _read_source(operator, source, f"{source_ref.kind}:{source_ref.ref}")
    ttl_seconds = int(params.get("ttl_seconds", DEFAULT_HANDOFF_TTL_SECONDS))
    token, ttl = await _wrap_on_local_store(operator, material, ttl_seconds)
    _log.info(
        "secret_broker.handoff.export",
        from_kind=source_ref.kind,
        value_sha256=material.value_sha256,
        length=material.length,
        handoff_ttl_seconds=ttl,
    )
    return {
        "status": "exported",
        "value_sha256": material.value_sha256,
        "length": material.length,
        "handoff_token": token,
        "handoff_ttl_seconds": ttl,
    }


# ---------------------------------------------------------------------------
# Step 2 on instance B — import
# ---------------------------------------------------------------------------


class _RemoteStore:
    """The other instance's store, as resolved from a target on this instance."""

    __slots__ = ("host", "label", "url", "verify")

    def __init__(self, label: str, host: str, url: str, verify: bool) -> None:
        self.label = label
        self.host = host
        self.url = url
        self.verify = verify


async def _resolve_remote_store(operator: Operator, name: str) -> _RemoteStore:
    """Resolve the ``store`` target (tenant-scoped) to the other store's address."""
    async with get_sessionmaker()() as session:
        row = await resolve_target(session, operator.tenant_id, name)
    label = f"store target {name!r}"
    if row.product != _STORE_PRODUCT:
        raise SecretHandoffStoreError(
            f"{label} has product {row.product!r}; a handoff can only be used at a "
            f"'{_STORE_PRODUCT}' target (Vault or OpenBao). Point 'store' at such a target"
        )
    if row.tls_ca_pin:
        raise SecretHandoffStoreError(
            f"{label} sets a pinned CA (tls_ca_pin), which the handoff import does not "
            "support yet; use a certificate the system trusts"
        )
    try:
        scheme = effective_scheme(row)
    except ValueError as exc:
        raise SecretHandoffStoreError(f"{label}: {exc}") from exc
    port = f":{row.port}" if row.port and row.port != DEFAULT_PORTS[scheme] else ""
    url = f"{scheme}://{row.host}{port}"
    return _RemoteStore(
        label=f"{label} ({url})", host=str(row.host), url=url, verify=bool(row.verify_tls)
    )


def _blocked(store_label: str, exc: TargetDestinationBlockedError) -> SecretHandoffError:
    """The destination guard refused the store's address; nothing was sent."""
    return SecretHandoffDestinationBlockedError(
        f"{store_label} is, or now resolves to, an address this MEHO does not dial, so the "
        f"handoff was not sent to path {UNWRAP_PATH} and is still usable: {exc}. Fix the "
        "store target's host, then run secret.handoff.import again"
    )


def _pin_store_addresses(host: str) -> list[str] | None:
    """Connect-time screen for the pinning transport, in handoff-error terms.

    Runs inside the transport's ``connect_tcp``. httpx hands an exception
    it does not map back unchanged, so :func:`_unwrap` can catch it.
    """
    try:
        return screen_and_resolve(host)
    except TargetDestinationBlockedError as exc:
        raise _blocked("the store target", exc) from exc


def _build_store_client(store: _RemoteStore) -> httpx.AsyncClient:
    """Build the HTTP client that reaches the other instance's store.

    Pinned to the screened address, no redirects, the store's own TLS
    setting. The single seam tests replace to point the import at a fake
    store.
    """
    return httpx.AsyncClient(
        base_url=store.url,
        transport=build_pinned_async_transport(_pin_store_addresses, verify=store.verify),
        timeout=get_settings().vault_timeout_seconds,
        follow_redirects=False,
    )


def _check_unwrap_status(store: _RemoteStore, response: httpx.Response) -> None:
    """Turn a non-2xx unwrap answer into a plain handoff error. Keeps no body text."""
    status = response.status_code
    if 200 <= status < 300:
        return
    if 300 <= status < 400:
        raise SecretHandoffStoreError(
            f"{store.label} answered path {UNWRAP_PATH} with a redirect (HTTP {status}), which "
            "the handoff import does not follow; the handoff was not used. Register the store "
            "target with the address the store itself listens on, then run "
            "secret.handoff.import again"
        )
    if status in (400, 403):
        # Vault answers 400 for a used or expired wrapping token, and 403
        # when the token is unknown to this store. Both mean the same thing
        # to the operator: this handoff cannot be used here any more.
        raise SecretHandoffInvalidError(
            f"{store.label} did not accept the handoff at path {UNWRAP_PATH} (HTTP {status}): "
            f"it was already used, it expired, or it was made by another store; {_NEW_HANDOFF}"
        )
    raise SecretHandoffStoreError(
        f"{store.label} answered path {UNWRAP_PATH} with HTTP {status} while trying to use the "
        f"handoff; check the store, then run secret.handoff.import again or, if the handoff "
        f"is gone, {_NEW_HANDOFF}"
    )


async def _unwrap(store: _RemoteStore, handoff_token: str) -> SecretMaterial:
    """Use the handoff once at the other store and wrap the value it returns."""
    try:
        await assert_public_destination_async(store.host)
    except TargetDestinationBlockedError as exc:
        raise _blocked(store.label, exc) from exc
    headers = {_TOKEN_HEADER: handoff_token, _REQUEST_HEADER: "true"}
    try:
        async with _build_store_client(store) as client:
            response = await client.post(f"/v1/{UNWRAP_PATH}", json={}, headers=headers)
    except SecretHandoffError:
        raise
    except httpx.HTTPError as exc:
        _raise_store_error(
            exc,
            store=store.label,
            path=UNWRAP_PATH,
            action="use the handoff",
            retry="then run secret.handoff.import again with the same handoff",
        )
    _check_unwrap_status(store, response)
    try:
        body = response.json()
    except ValueError:
        body = None
    data = body.get("data") if isinstance(body, dict) else None
    value = data.get(_WRAP_FIELD) if isinstance(data, dict) else None
    if not isinstance(value, str):
        raise SecretHandoffStoreError(
            f"{store.label} returned no '{_WRAP_FIELD}' field at path {UNWRAP_PATH}; "
            f"the handoff was not made by secret.handoff.export. {_NEW_HANDOFF}"
        )
    return SecretMaterial(value)


def _check_sink(operator: Operator, sink: SecretEndpoint, sink_name: str) -> None:
    """Run the sink's own write check before the handoff is used, in handoff-error terms.

    Only a sink with ``check_write_allowed`` (the ``vault:`` kind) has a
    check to run. A denied tenant path is reported with the sink address
    and the fact that the handoff is still usable.
    """
    check_write = getattr(sink, "check_write_allowed", None)
    if check_write is None:
        return
    try:
        check_write(operator)
    except VaultTenantScopeError as exc:
        raise SecretHandoffRefusedError(
            f"the sink {sink_name} is outside this operator's tenant path, so the handoff "
            "was not used and is still usable; use a path under the tenant, then run "
            "secret.handoff.import again with the same handoff"
        ) from exc


async def secret_handoff_import(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Use a handoff from another instance once and write the value into a sink.

    Op-id: ``secret.handoff.import``. Checks the sink before the handoff is
    used, so a wrong sink path does not burn the handoff. Returns only
    ``status``, ``value_sha256`` and ``length``.
    """
    del target
    sink_ref = parse_secret_ref(str(params["to"]))
    sink = resolve_secret_endpoint(sink_ref)
    sink_name = f"{sink_ref.kind}:{sink_ref.ref}"
    _check_sink(operator, sink, sink_name)
    store = await _resolve_remote_store(operator, str(params["store"]))

    material = await _unwrap(store, str(params["handoff_token"]))

    expected = params.get("expect_sha256")
    if isinstance(expected, str) and expected.lower() != material.value_sha256:
        raise SecretHandoffDigestMismatchError(
            f"the value from {store.label} has SHA-256 {material.value_sha256}, not the "
            f"expected {expected.lower()}; nothing was written to {sink_name}. The handoff "
            f"is used now: {_NEW_HANDOFF}, and pass its value_sha256 as expect_sha256"
        )
    try:
        await sink.write_secret(operator, material)
    except Exception as exc:
        raise SecretHandoffSinkWriteError(
            f"the handoff was used, but writing to {sink_name} failed "
            f"({type(exc).__name__}). The handoff cannot be used again: fix the sink, then "
            f"{_NEW_HANDOFF}"
        ) from exc

    _log.info(
        "secret_broker.handoff.import",
        to_kind=sink_ref.kind,
        store=store.label,
        value_sha256=material.value_sha256,
        length=material.length,
    )
    return {
        "status": "imported",
        "value_sha256": material.value_sha256,
        "length": material.length,
    }
