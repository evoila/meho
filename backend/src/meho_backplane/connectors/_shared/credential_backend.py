# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Backend-agnostic target-credential resolution seam.

Every REST connector resolves a target's ``secret_ref`` to vendor
credentials through :mod:`meho_backplane.connectors._shared.vault_creds`.
That path was hard-wired to a single store (Vault KV-v2). This module
adds the **dispatch seam** so a second credential store (GCP Secret
Manager, #2230) can be added without touching a single connector: a
kind-keyed registry maps a ``secret_ref`` scheme to a
:class:`CredentialBackend`, and the loaders in ``vault_creds`` dispatch on
that scheme instead of always reading Vault.

It is the direct analogue — on the **target credential-read** path — of
the :data:`~meho_backplane.connectors.secret.endpoints.SECRET_ENDPOINT_REGISTRY`
seam the ``secret.move`` broker already uses. The two serve different
contracts (the broker moves one opaque value and reports only its
SHA-256; a credential backend returns a named-field secret payload a
connector session builder consumes), so they are deliberately separate
registries — but the shape is intentionally the same: a
:func:`~typing.runtime_checkable` :class:`~typing.Protocol`, a
kind-string → implementation registry, a duplicate-kind guard, and a
scheme-splitting helper.

Scheme dispatch, zero migration
===============================

A ``secret_ref`` is either **schemeless** (today's bare KV-v2 logical
path, ``targets/<id>``) or **scheme-prefixed** (``<kind>:<ref>``). A
schemeless ref resolves via the deployment's *default* backend
(``config.credentialBackend`` / ``CREDENTIAL_BACKEND``, default
``"vault"``), so every existing install — which stores bare paths and
runs Vault — is unaffected. An explicit ``vault:targets/<id>`` resolves
identically to the schemeless form. An unknown scheme (``gsm:...`` before
#2230 registers it) raises :class:`UnknownCredentialBackendError` naming
the kind and the registered kinds, rather than silently attempting a
Vault read of a nonsensical path.

The scheme split is intentionally conservative: a colon is treated as a
scheme separator **only** when the segment before the first colon is a
bare scheme token (a leading letter followed by letters / digits /
``+`` / ``.`` / ``-``, no slash). A logical KV-v2 path never carries such
a prefix, so a path that happens to contain a colon deeper in a segment
is left schemeless rather than mis-split.

No secret in this module
========================

Nothing here reads or holds a credential value — this is pure routing.
The concrete backend (:class:`~...vault_creds.VaultCredentialBackend`)
owns the read and the same no-secret-in-logs discipline the rest of the
credential path enforces.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator

__all__ = [
    "CREDENTIAL_BACKEND_REGISTRY",
    "DEFAULT_CREDENTIAL_BACKEND",
    "CredentialBackend",
    "CredentialsReadError",
    "UnknownCredentialBackendError",
    "register_credential_backend",
    "resolve_credential_backend",
    "split_credential_ref",
]


class CredentialsReadError(Exception):
    """Backend-neutral read-phase failure resolving a target credential.

    The common base every backend's read error derives from
    (``VaultCredentialsReadError``, ``GcpSecretManagerReadError``), so a
    caller that wants "the credential could not be read, whatever the store
    is" catches one class. That matters beyond tidiness: the dispatcher
    renders a handler exception as ``connector_error: <class name>``
    (``operations/_errors.py``), so a Vault-named class surfacing on a
    ``credentialBackend=gsm`` deploy — which runs no Vault at all — sends the
    operator hunting a component that isn't installed (#2642). A backend
    raises **its own** subclass; only genuinely store-agnostic failures raise
    this base directly.
    """


#: The backend kind schemeless ``secret_ref`` values resolve through when
#: ``config.credentialBackend`` / ``CREDENTIAL_BACKEND`` is unset. ``vault``
#: keeps every existing install on today's Vault KV-v2 read with no config
#: change — the zero-migration default.
DEFAULT_CREDENTIAL_BACKEND: str = "vault"

#: A bare scheme token: a leading letter then letters / digits / ``+`` /
#: ``.`` / ``-`` (loosely RFC 3986 scheme shape, minus the ``:``). A
#: ``secret_ref`` prefix matching this before the first colon is treated
#: as a backend kind; anything else (notably a path segment with a slash)
#: leaves the ref schemeless.
_SCHEME_TOKEN = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*$")


@runtime_checkable
class CredentialBackend(Protocol):
    """Structural contract a credential store adapter satisfies.

    A backend resolves a store-specific ``secret_ref`` (the part *after*
    the ``<kind>:`` scheme, or the whole schemeless ref) plus the
    request-scoped :class:`~meho_backplane.auth.operator.Operator` to the
    secret's raw field payload — a plain ``dict[str, object]`` of the
    named fields stored for the target. Field extraction, whitespace
    normalisation, and the no-secret structlog event stay in the shared
    loader, so a new backend only implements the store read.

    Because this is a :func:`~typing.runtime_checkable`
    :class:`~typing.Protocol`, an adapter need only define
    :meth:`load_secret_data` and register itself via
    :func:`register_credential_backend` — no base class.
    """

    async def load_secret_data(
        self,
        secret_ref: str,
        operator: Operator,
        *,
        target_name: str,
        mount: str,
    ) -> dict[str, object]:
        """Read the store secret at *secret_ref* and return its field dict.

        Parameters
        ----------
        secret_ref
            The store-specific reference (scheme already stripped by the
            dispatcher). For the Vault backend this is the logical KV-v2
            path; for a future backend it is that store's address form.
        operator
            The request-scoped operator. A backend that performs an
            operator-context read (Vault JWT/OIDC) forwards
            ``operator.raw_jwt``; a backend that reads under a
            deployment identity (GSM SA-direct, #2230) may ignore it.
            **The backend owns the empty-``raw_jwt`` precondition** (#2642):
            a store that can only read under an operator identity rejects a
            system-initiated call itself, while one that has a deployment
            identity to fall back on serves it. The shared loader no longer
            pre-empts that decision, so background dispatch works on the
            backends that can support it and still fails closed on the ones
            that cannot.
        target_name
            Names the target in error messages — never a credential value.
        mount
            A Vault-KV concept threaded through the shared loader's
            existing ``mount=`` parameter. Backends that have no mount
            concept (GSM) ignore it.

        Returns a flat ``{field: value}`` dict. Raises a store-specific
        error (the Vault backend raises ``VaultCredentialsReadError`` /
        ``VaultClientError``) when the ref is missing, malformed, or
        unreadable — never a bare ``KeyError``, never echoing a value.
        """
        ...


class UnknownCredentialBackendError(Exception):
    """No backend is registered for a ``secret_ref``'s resolved kind.

    Raised by :func:`resolve_credential_backend` when a scheme
    (``gsm:`` before #2230) — or a ``config.credentialBackend`` value —
    names a kind absent from :data:`CREDENTIAL_BACKEND_REGISTRY`. Mirrors
    the actionable, distinct-from-read-phase contract of
    ``VaultCredentialsReadError``: the message names the unknown kind and
    the registered kinds so the misconfiguration is fixable at a glance,
    and the dispatch fails loudly instead of falling through to a silent
    Vault read.
    """


#: Kind-string → :class:`CredentialBackend` registry. Populated at import
#: time: each backend module calls :func:`register_credential_backend` for
#: the kind it serves (``vault_creds`` registers ``"vault"`` at import; a
#: future GSM module registers ``"gsm"``). The shared loader reads it to
#: resolve a parsed kind to the backend that performs the store read. No
#: loader change is needed to add a backend.
CREDENTIAL_BACKEND_REGISTRY: dict[str, CredentialBackend] = {}


def register_credential_backend(kind: str, backend: CredentialBackend) -> None:
    """Register *backend* as the resolver for ``secret_ref`` kind *kind*.

    Called once per backend at import time. Re-registering an existing
    kind raises :class:`ValueError` — two backends claiming one kind is a
    wiring bug that should fail the eager-import pass loudly rather than
    silently shadowing one with the other (same posture as
    ``register_secret_endpoint``).
    """
    if kind in CREDENTIAL_BACKEND_REGISTRY:
        raise ValueError(
            f"credential backend kind {kind!r} is already registered "
            f"(by {CREDENTIAL_BACKEND_REGISTRY[kind]!r}); kinds must be unique"
        )
    CREDENTIAL_BACKEND_REGISTRY[kind] = backend


def resolve_credential_backend(kind: str) -> CredentialBackend:
    """Return the backend registered for *kind* or raise a clear error.

    Raises :class:`UnknownCredentialBackendError` naming the unknown kind
    and the registered kinds when no backend serves *kind*.
    """
    backend = CREDENTIAL_BACKEND_REGISTRY.get(kind)
    if backend is None:
        known = ", ".join(sorted(CREDENTIAL_BACKEND_REGISTRY)) or "(none)"
        raise UnknownCredentialBackendError(
            f"no credential backend registered for kind {kind!r}; registered kinds: {known}"
        )
    return backend


def split_credential_ref(secret_ref: str, *, default_backend: str) -> tuple[str, str]:
    """Split *secret_ref* into ``(kind, store_ref)``.

    A ``<kind>:<ref>`` value with a bare scheme token before the first
    colon (and a non-empty remainder) splits into that kind and the
    remainder. Everything else — no colon, an empty remainder, or a
    prefix that is not a bare scheme token (e.g. a path segment with a
    slash) — is schemeless and resolves through *default_backend* with the
    ref passed through verbatim.

    * ``"targets/vc-01"`` → ``(default_backend, "targets/vc-01")``
    * ``"vault:targets/vc-01"`` → ``("vault", "targets/vc-01")``
    * ``"gsm:proj/secret#pw"`` → ``("gsm", "proj/secret#pw")``
    """
    kind, sep, rest = secret_ref.partition(":")
    if sep and rest and _SCHEME_TOKEN.match(kind):
        return kind, rest
    return default_backend, secret_ref
