# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Secret-broker adapter protocol + kind-keyed endpoint registry.

The secret broker (Initiative #581) moves credential material from one
store to another **server-side**, so the value never enters the agent's
op params, the op response, a log event, or an audit row. This module
owns the abstraction the move handler dispatches through:

* :class:`SecretMaterial` — an in-memory wrapper over the moved value.
  Its ``__repr__`` / ``__str__`` redact the value; it exposes only the
  value's SHA-256 digest and byte length to the response path. The
  wrapped value is read back exactly once by a sink's
  :meth:`SecretEndpoint.write_secret` and is otherwise inert.
* :class:`SecretEndpoint` — a structural :class:`typing.Protocol` every
  store adapter satisfies. A source adapter implements
  :meth:`~SecretEndpoint.read_secret`; a sink adapter implements
  :meth:`~SecretEndpoint.write_secret`. A vault-kv adapter (the first
  pair, #1577) implements both. Sibling tasks add further kinds (e.g. a
  keycloak sink, #1578) under the **same** contract.
* :data:`SECRET_ENDPOINT_REGISTRY` + :func:`register_secret_endpoint` —
  a kind-string → endpoint-factory registry. The move handler resolves
  each side's ``kind`` (``vault``, …) to a factory and constructs the
  per-move endpoint from the parsed ``ref``. This is the extension seam
  sibling adapter tasks register into; they import this module, never
  the handler.
* :func:`parse_secret_ref` — splits a ``"<kind>:<ref>"`` intent string
  into its kind and store-specific reference.

No secret in logs / responses / audit
======================================

The broker's core invariant (Initiative #581): the value never appears
in op args, the transcript, logs, the op response, a broadcast payload,
or the audit row — only the move's status, the value's SHA-256, and its
length. :class:`SecretMaterial` enforces the representation half of that
invariant (a stray ``logger.info("...", material=m)`` or f-string renders
the redacted form, not the value); the handler enforces the response
half by returning only ``status`` + ``value_sha256`` + ``length`` and
never threading the value into params. Adapters keep only field *names*
(never values) in their structlog events, mirroring the discipline in
:mod:`meho_backplane.connectors._shared.vault_creds`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator

__all__ = [
    "SECRET_ENDPOINT_REGISTRY",
    "SecretEndpoint",
    "SecretEndpointFactory",
    "SecretMaterial",
    "SecretRef",
    "UnknownSecretKindError",
    "parse_secret_ref",
    "register_secret_endpoint",
]


class SecretMaterial:
    """An in-memory carrier for a moved secret value.

    The broker reads a value out of a source store and writes it into a
    sink store entirely server-side; this wrapper is the only thing that
    ever holds the value in the move handler's address space. It is
    deliberately **not** a dataclass / pydantic model and overrides
    ``__repr__`` / ``__str__`` so the value cannot leak through the
    string-coercion paths that secret leaks usually travel:

    * a structlog event whose JSON renderer calls ``repr()`` on a bound
      non-primitive value,
    * an f-string / ``str()`` in an exception message,
    * a ``print`` left in during debugging.

    The wrapped value is stored as ``bytes`` (a ``str`` value is encoded
    UTF-8 on construction) so the SHA-256 digest and the length are over
    the exact byte sequence that will be written to the sink. The plain
    value is reachable only through :attr:`value` — the single accessor a
    sink adapter calls inside :meth:`SecretEndpoint.write_secret`. No
    callers outside an adapter's write path should touch it.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str | bytes) -> None:
        self._value: bytes = value.encode("utf-8") if isinstance(value, str) else value

    @property
    def value(self) -> bytes:
        """The raw secret bytes. Read only by a sink's ``write_secret``."""
        return self._value

    @property
    def length(self) -> int:
        """Byte length of the wrapped value — safe to surface in the response."""
        return len(self._value)

    @property
    def value_sha256(self) -> str:
        """Hex SHA-256 of the wrapped value — the provenance hash for the response/audit."""
        return hashlib.sha256(self._value).hexdigest()

    def __repr__(self) -> str:
        # Redacted: length + digest only, never the value. The digest is
        # the provenance signal an auditor reading a log line wants; the
        # value never appears.
        return f"<SecretMaterial len={self.length} sha256={self.value_sha256}>"

    __str__ = __repr__


class SecretRef:
    """A parsed ``"<kind>:<ref>"`` secret-move intent reference.

    ``kind`` selects the adapter in :data:`SECRET_ENDPOINT_REGISTRY`
    (``vault`` → the vault-kv adapter); ``ref`` is the store-specific
    address the adapter interprets (for vault-kv: a KV-v2 path with an
    optional ``#<field>`` fragment selecting one field).
    """

    __slots__ = ("kind", "ref")

    def __init__(self, kind: str, ref: str) -> None:
        self.kind = kind
        self.ref = ref

    def __repr__(self) -> str:
        return f"SecretRef(kind={self.kind!r}, ref={self.ref!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SecretRef):
            return NotImplemented
        return self.kind == other.kind and self.ref == other.ref

    def __hash__(self) -> int:
        return hash((self.kind, self.ref))


def parse_secret_ref(raw: str) -> SecretRef:
    """Split a ``"<kind>:<ref>"`` intent string into a :class:`SecretRef`.

    The split is on the **first** colon only, so the store-specific
    reference may itself contain colons (a port, a nested path):

    * ``"vault:secret/db/prod#password"`` → kind ``"vault"``, ref
      ``"secret/db/prod#password"``.

    Both halves must be non-empty after the split. A string with no
    colon, an empty kind (``":ref"``), or an empty ref (``"vault:"``)
    raises :class:`ValueError` — the move intent is malformed and there
    is no defaulting that would be safe for a credential move.
    """
    kind, sep, ref = raw.partition(":")
    if not sep or not kind or not ref:
        raise ValueError(
            f"malformed secret ref {raw!r}: expected '<kind>:<ref>' with a "
            "non-empty kind and ref (e.g. 'vault:secret/db/prod#password')"
        )
    return SecretRef(kind=kind, ref=ref)


@runtime_checkable
class SecretEndpoint(Protocol):
    """Structural contract for a secret store the broker reads from / writes to.

    A concrete adapter is constructed by a
    :data:`SecretEndpointFactory` from the parsed store-specific
    ``ref``. A **source** endpoint implements :meth:`read_secret`; a
    **sink** endpoint implements :meth:`write_secret`. An adapter that is
    both (the vault-kv pair, #1577) implements both. Because this is a
    :func:`~typing.runtime_checkable` :class:`~typing.Protocol`, an
    adapter need only define the methods — no base class, no
    registration ceremony beyond :func:`register_secret_endpoint`.

    Both methods take the request-scoped :class:`Operator` so the
    adapter performs its store access under the operator's own
    credentials (e.g. a Vault JWT/OIDC login via
    :func:`~meho_backplane.auth.vault.vault_client_for_operator`),
    keeping the move inside the operator's existing authorization
    envelope. Neither method accepts or returns the raw value as a bare
    string: the read produces a :class:`SecretMaterial`, the write
    consumes one.
    """

    async def read_secret(self, operator: Operator) -> SecretMaterial:
        """Read the addressed value from the store and wrap it.

        Raises a store-specific error (subclass of :class:`Exception`)
        when the address is missing, malformed, or unreadable; the
        dispatcher maps the exception to a structured ``connector_error``
        result. The error message names the field/path, never the value.
        """
        ...

    async def write_secret(self, operator: Operator, material: SecretMaterial) -> None:
        """Write *material* into the store at the addressed location.

        Reads ``material.value`` exactly once to obtain the bytes to
        store. Raises a store-specific error on a write failure.
        """
        ...


#: Builds a per-move endpoint from the store-specific ``ref`` string an
#: adapter is registered for. A PEP 695 ``type`` alias, whose value is
#: lazily evaluated, so the ``Callable`` / :class:`SecretEndpoint`
#: references resolve at type-check time without a runtime cost.
type SecretEndpointFactory = Callable[[str], SecretEndpoint]


class UnknownSecretKindError(KeyError):
    """No adapter is registered for a move's ``<kind>``.

    Raised by the move handler when :func:`parse_secret_ref` yields a
    kind absent from :data:`SECRET_ENDPOINT_REGISTRY`. A
    :class:`KeyError` subclass so the dispatcher's ``connector_error``
    branch reports ``exception_class="UnknownSecretKindError"``; the
    message names the unknown kind and the registered kinds, never a
    ref value.
    """


#: Kind-string → endpoint-factory registry. Populated at connector
#: import time: each adapter module calls :func:`register_secret_endpoint`
#: for the kind(s) it serves. The move handler reads it to resolve a
#: parsed ``<kind>`` to the factory that builds the per-move endpoint
#: from the ``ref``. Sibling adapter tasks (#1578 keycloak sink, …)
#: register additional kinds here under the same contract; no handler
#: change is needed to add a kind.
SECRET_ENDPOINT_REGISTRY: dict[str, SecretEndpointFactory] = {}


def register_secret_endpoint(kind: str, factory: SecretEndpointFactory) -> None:
    """Register *factory* as the endpoint builder for move ``kind``.

    Called once per adapter at import time. Re-registering the same
    kind raises :class:`ValueError` — two adapters claiming one kind is
    a wiring bug that should fail the import (and therefore the lifespan
    eager-import pass) loudly rather than silently shadowing one with
    the other.
    """
    if kind in SECRET_ENDPOINT_REGISTRY:
        raise ValueError(
            f"secret endpoint kind {kind!r} is already registered "
            f"(by {SECRET_ENDPOINT_REGISTRY[kind]!r}); kinds must be unique"
        )
    SECRET_ENDPOINT_REGISTRY[kind] = factory
