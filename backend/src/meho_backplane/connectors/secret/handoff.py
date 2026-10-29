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

The value is held only in a :class:`~.endpoints.SecretMaterial` and never
enters params, results, log events, or exception messages. The handoff
token is in A's result and in B's params; both ops are pinned as
credential classes in :mod:`meho_backplane.broadcast.events`, so the
approval row keeps B's params encrypted and the approval preview shows
only addresses (:mod:`.handoff_preview`).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Final, NoReturn

import hvac
import hvac.exceptions
import requests.exceptions
import structlog

import meho_backplane.auth.vault as _auth_vault
from meho_backplane.connectors._shared.cache_key import DEFAULT_PORTS, effective_scheme
from meho_backplane.connectors.secret.endpoints import (
    SecretMaterial,
    parse_secret_ref,
    resolve_secret_endpoint,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import resolve_target

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator

__all__ = [
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


class SecretHandoffError(Exception):
    """Base class for a failed handoff step. Messages name store and path, never the value."""


class SecretHandoffUnreachableError(SecretHandoffError):
    """The store could not be reached (DNS, TCP, TLS or timeout)."""


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


def _raise_store_error(exc: Exception, *, store: str, path: str, action: str) -> NoReturn:
    """Map an hvac / requests / Vault-client failure to a plain handoff error.

    Only the store name, the path and the exception class appear in the
    message — never the payload or the token.
    """
    if isinstance(
        exc,
        requests.exceptions.ConnectionError
        | requests.exceptions.Timeout
        | _auth_vault.VaultUnreachableError,
    ):
        raise SecretHandoffUnreachableError(
            f"could not reach {store} at path {path} to {action} "
            f"({type(exc).__name__}); check the address and the network"
        ) from exc
    if isinstance(exc, _auth_vault.VaultRoleDeniedError | hvac.exceptions.Forbidden):
        raise SecretHandoffRefusedError(
            f"{store} refused the login or the call at path {path}, so it could not {action}"
        ) from exc
    raise SecretHandoffStoreError(
        f"{store} failed at path {path} while trying to {action} ({type(exc).__name__})"
    ) from exc


def _build_store_client(url: str, *, token: str, verify: bool) -> hvac.Client:
    """Build an hvac client for another instance's store, bound to the handoff token.

    The single seam tests replace to point the import at a fake store.
    """
    return hvac.Client(
        url=url,
        token=token,
        verify=verify,
        timeout=get_settings().vault_timeout_seconds,
    )


# ---------------------------------------------------------------------------
# Step 1 on instance A — export
# ---------------------------------------------------------------------------


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
        _raise_store_error(exc, store=_LOCAL_STORE, path=WRAP_PATH, action="make a handoff")
    wrap_info = response.get("wrap_info") if isinstance(response, dict) else None
    token = wrap_info.get("token") if isinstance(wrap_info, dict) else None
    if not isinstance(token, str) or not token:
        raise SecretHandoffStoreError(
            f"{_LOCAL_STORE} returned no handoff token at path {WRAP_PATH}"
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
    material = await source.read_secret(operator)
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

    __slots__ = ("label", "url", "verify")

    def __init__(self, label: str, url: str, verify: bool) -> None:
        self.label = label
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
            f"'{_STORE_PRODUCT}' target (Vault or OpenBao)"
        )
    if row.tls_ca_pin:
        raise SecretHandoffStoreError(
            f"{label} sets a pinned CA (tls_ca_pin), which the handoff import does not "
            "support yet; use a certificate the system trusts"
        )
    try:
        scheme = effective_scheme(row)
    except ValueError as exc:
        raise SecretHandoffStoreError(str(exc)) from exc
    port = f":{row.port}" if row.port and row.port != DEFAULT_PORTS[scheme] else ""
    url = f"{scheme}://{row.host}{port}"
    return _RemoteStore(label=f"{label} ({url})", url=url, verify=bool(row.verify_tls))


async def _unwrap(store: _RemoteStore, handoff_token: str) -> SecretMaterial:
    """Use the handoff once at the other store and wrap the value it returns."""
    client = _build_store_client(store.url, token=handoff_token, verify=store.verify)
    try:
        response = await asyncio.to_thread(client.sys.unwrap)
    except (hvac.exceptions.InvalidRequest, hvac.exceptions.Forbidden) as exc:
        # Vault answers 400 for a used or expired wrapping token, and 403
        # when the token is unknown to this store. Both mean the same thing
        # to the operator: this handoff cannot be used here any more.
        raise SecretHandoffInvalidError(
            f"{store.label} did not accept the handoff at path {UNWRAP_PATH}: it was "
            "already used, it expired, or it was made by another store. Make a new "
            "handoff on the first instance and import that one"
        ) from exc
    except (hvac.exceptions.VaultError, requests.exceptions.RequestException) as exc:
        _raise_store_error(exc, store=store.label, path=UNWRAP_PATH, action="use the handoff")
    data = response.get("data") if isinstance(response, dict) else None
    value = data.get(_WRAP_FIELD) if isinstance(data, dict) else None
    if not isinstance(value, str):
        raise SecretHandoffStoreError(
            f"{store.label} returned no '{_WRAP_FIELD}' field at path {UNWRAP_PATH}; "
            "the handoff was not made by secret.handoff.export"
        )
    return SecretMaterial(value)


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
    check_write = getattr(sink, "check_write_allowed", None)
    if check_write is not None:
        check_write(operator)
    store = await _resolve_remote_store(operator, str(params["store"]))

    material = await _unwrap(store, str(params["handoff_token"]))

    expected = params.get("expect_sha256")
    if isinstance(expected, str) and expected.lower() != material.value_sha256:
        raise SecretHandoffDigestMismatchError(
            f"the value from {store.label} has SHA-256 {material.value_sha256}, not the "
            f"expected {expected.lower()}; nothing was written to {sink_ref.kind}:{sink_ref.ref}"
        )
    try:
        await sink.write_secret(operator, material)
    except Exception as exc:
        raise SecretHandoffSinkWriteError(
            f"the handoff was used, but writing to {sink_ref.kind}:{sink_ref.ref} failed "
            f"({type(exc).__name__}: {exc}). The handoff cannot be used again: make a new "
            "handoff on the first instance and import that one"
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
