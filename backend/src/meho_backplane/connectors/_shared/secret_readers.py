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

A read counts for the exact secret reference the target held at the read
(``target.secret_ref``). When the target's reference changes, every caller
reads the new secret once, also a caller whose read of the old reference
ends after the change.

The audit row of a call names the login under which the secret this call
used was read from the store (``secret_read_as``, see
:mod:`meho_backplane.auth.secret_read_as`). There are three cases:

* The call reads the secret itself: the store read notes that login.
* The call uses a kept entry: :meth:`SecretReaders.has_read` notes the login
  that read the secret when the entry was kept (:meth:`SecretReaders.record`
  stores it). When that login is not known, it notes
  :data:`~meho_backplane.auth.secret_read_as.KEPT_LOGIN_READER_UNKNOWN`, never
  the present caller. A login that the call has already noted then stays.
* The call uses no secret: nothing is noted.

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
from meho_backplane.auth.secret_read_as import (
    KEPT_LOGIN_READER_UNKNOWN,
    current_secret_read_as,
    record_secret_read_as,
)
from meho_backplane.connectors._shared.cache_key import (
    TenantScopedTargetLike,
    target_cache_key,
)
from meho_backplane.connectors._shared.system_operator import is_system_operator

__all__ = ["SecretReaders", "caller_id"]

#: The kind of a caller: how it logs in to Vault.
SecretReaderKind = Literal["operator", "check-runner"]

#: A caller, and the secret reference their read passed for.
_ReadKey = tuple[str, tuple[SecretReaderKind, str]]


def caller_id(operator: Operator) -> tuple[SecretReaderKind, str]:
    """Return ``(kind, sub)``: who *operator* is for a secret read.

    The check runner (``check_runner_dispatch``) reads with its own token
    and role, also when it runs a Sensor under a person's ``sub``, so it is
    its own kind. Everything else is a person or machine login reading with
    its own token.
    """
    kind: SecretReaderKind = "check-runner" if operator.check_runner_dispatch else "operator"
    return (kind, operator.sub)


def _secret_ref(target: TenantScopedTargetLike) -> str:
    """The target's secret reference; ``""`` for a target that has none."""
    return str(getattr(target, "secret_ref", None) or "")


def _read_key(target: TenantScopedTargetLike, operator: Operator) -> _ReadKey:
    """Return the key of *operator*'s read: the target's secret reference and the caller."""
    return (_secret_ref(target), caller_id(operator))


class SecretReaders:
    """The callers who have read a target's secret in this process.

    Keyed on the tenant-unique target key
    (:func:`~meho_backplane.connectors._shared.cache_key.target_cache_key`),
    the target's secret reference and the caller (:func:`caller_id`: kind and
    ``sub``). For each read it keeps the login id that the caller's store read
    noted, or ``None``.
    """

    def __init__(self) -> None:
        self._callers: dict[tuple[str, str], dict[_ReadKey, str | None]] = {}

    def has_read(self, target: TenantScopedTargetLike, operator: Operator) -> bool:
        """Return whether *operator* has read the secret of *target* in this process.

        Only a read of the target's present secret reference counts.

        Ask it last, only when a kept entry is there and is used on yes: on
        yes it notes the login that read the secret when it was kept, for the
        audit row of the call
        (:data:`~meho_backplane.auth.secret_read_as.KEPT_LOGIN_READER_UNKNOWN`
        when that login is not known and the call has noted none). A store
        read later in the same call replaces the note. The system operator
        never has read: it runs the loader every time.
        """
        if is_system_operator(operator):
            return False
        readers = self._callers.get(target_cache_key(target), {})
        key = _read_key(target, operator)
        if key not in readers:
            return False
        record_secret_read_as(readers[key] or KEPT_LOGIN_READER_UNKNOWN)
        return True

    def record(self, target: TenantScopedTargetLike, operator: Operator) -> None:
        """Remember that *operator*'s read of the secret of *target* passed.

        Call it right after the loader returned: it keeps the login id that
        the store read noted in this call. The system operator is not
        recorded.
        """
        if is_system_operator(operator):
            return
        readers = self._callers.setdefault(target_cache_key(target), {})
        readers[_read_key(target, operator)] = current_secret_read_as()

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
        """Forget every reader of *target*, for every secret reference it had."""
        self._callers.pop(target_cache_key(target), None)

    def clear(self) -> None:
        """Forget every reader of every target."""
        self._callers.clear()

    def callers(self, target: TenantScopedTargetLike) -> frozenset[tuple[SecretReaderKind, str]]:
        """Who read the present secret of *target*, as ``(kind, sub)`` (for tests)."""
        ref = _secret_ref(target)
        reads = self._callers.get(target_cache_key(target), ())
        return frozenset(caller for read_ref, caller in reads if read_ref == ref)
