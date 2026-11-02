# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Who has read a target's secret: the check each connector cache runs on a hit.

A connector keeps a credential, a login token or a client per target, so
the target's secret is read from Vault once and not on every call. The
read is what checks the caller: Vault answers under the caller's own
login. :class:`SecretReaders` keeps that check for every caller when the
cache is warm. A cached entry is handed to a caller only after that
caller's own Vault read of the target's secret passed once in this
process. The first caller's read fills the cache. Every later caller
reads the secret once too, under their own login, and then uses the
cached entry. So Vault's answer reaches every caller, and the vendor
still sees one login per credential (some vendors allow only one active
session per user, see :func:`~meho_backplane.connectors._shared.cache_key.credential_cache_key`).

Callers are told apart by their kind and their ``operator.sub``
(:func:`caller_id`). A person is one caller. The check runner (the
background job that runs Sensors) has its own token and Vault role, but it
may run a Sensor under the ``sub`` of the person who made it, so its kind
(``check_runner_dispatch``) is part of the key: it never counts as that
person and reads the secret with its own login. The synthesised system
operator (operator-less probes) is never recorded: it runs the loader
every time, as before, and the loader fails closed on a live Vault.

A target change forgets the readers of that target
(:meth:`SecretReaders.forget`), together with the cached entries, so the
next call of every caller reads the new secret.

Use::

    self._readers = SecretReaders()

    # A credential cache: a hit counts only for a caller who has read.
    cached = self._creds_cache.get(cache_key)
    if cached is not None and self._readers.has_read(target, operator):
        return cached
    raw = await self._credentials_loader(target, operator)
    self._creds_cache[cache_key] = raw
    self._readers.record(target, operator)

    # A token cache: a hit is used after the caller's own read.
    cached = self._session_tokens.get(cache_key)
    if cached is not None:
        await self._readers.ensure_read(
            target, operator, lambda: self._credentials_loader(target, operator)
        )
        return cached

Every method is synchronous apart from :meth:`ensure_read`, and none of
them awaits while it changes state, so a connector calls them under its
own cache lock without a second lock.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.cache_key import (
    TenantScopedTargetLike,
    target_cache_key,
)
from meho_backplane.connectors._shared.system_operator import is_system_operator

__all__ = ["SecretReaders", "caller_id"]

#: The kind of a caller: how it logs in to Vault.
CallerKind = Literal["operator", "check-runner"]


def caller_id(operator: Operator) -> tuple[CallerKind, str]:
    """Return ``(kind, sub)``: who *operator* is for a secret read.

    The check runner (``check_runner_dispatch``) reads with its own token
    and role, also when it runs a Sensor under a person's ``sub``, so it is
    its own kind. Everything else is a person or machine login reading with
    its own token.
    """
    kind: CallerKind = "check-runner" if operator.check_runner_dispatch else "operator"
    return (kind, operator.sub)


class SecretReaders:
    """The callers who have read a target's secret in this process.

    Keyed on the tenant-unique target key
    (:func:`~meho_backplane.connectors._shared.cache_key.target_cache_key`)
    and the caller (:func:`caller_id`: kind and ``sub``).
    """

    def __init__(self) -> None:
        self._callers: dict[tuple[str, str], set[tuple[CallerKind, str]]] = {}

    def has_read(self, target: TenantScopedTargetLike, operator: Operator) -> bool:
        """Return whether *operator* has read the secret of *target* in this process.

        The system operator never has: it runs the loader every time.
        """
        if is_system_operator(operator):
            return False
        return caller_id(operator) in self._callers.get(target_cache_key(target), ())

    def record(self, target: TenantScopedTargetLike, operator: Operator) -> None:
        """Remember that *operator*'s read of the secret of *target* passed.

        Call it right after the loader returned. The system operator is not
        recorded.
        """
        if is_system_operator(operator):
            return
        self._callers.setdefault(target_cache_key(target), set()).add(caller_id(operator))

    async def ensure_read(
        self,
        target: TenantScopedTargetLike,
        operator: Operator,
        read: Callable[[], Awaitable[object]],
    ) -> None:
        """Run *read* once for a caller who has not read the secret of *target* yet.

        *read* is the connector's credential loader, bound to *target* and
        *operator*. Its result is not needed: the cache already holds the
        entry the caller may now use. A refused read raises, and the caller
        is not recorded.
        """
        if self.has_read(target, operator):
            return
        await read()
        self.record(target, operator)

    def forget(self, target: TenantScopedTargetLike) -> None:
        """Forget every reader of *target*; the next call of each one reads again."""
        self._callers.pop(target_cache_key(target), None)

    def clear(self) -> None:
        """Forget every reader of every target."""
        self._callers.clear()

    def callers(self, target: TenantScopedTargetLike) -> frozenset[tuple[CallerKind, str]]:
        """The recorded callers of *target*, as ``(kind, sub)`` (read-only, for tests)."""
        return frozenset(self._callers.get(target_cache_key(target), ()))
