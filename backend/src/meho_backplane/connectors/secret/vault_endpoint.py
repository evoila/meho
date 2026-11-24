# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Vault KV-v2 :class:`SecretEndpoint` adapter — the first broker pair.

Registered under kind ``"vault"`` (see :func:`register_secret_endpoint`
at module import), this adapter is both a **source** (``read_secret``)
and a **sink** (``write_secret``) so the broker's first move pair is
vault-kv → vault-kv. Sibling tasks add further sink kinds (e.g. keycloak,
#1578) under the same :class:`SecretEndpoint` contract.

Ref grammar
===========

The store-specific ``ref`` addresses a KV-v2 secret as::

    [<mount>/]<path>#<field>

* ``<path>`` is the KV-v2 logical path (relative to the mount, the same
  shape hvac's ``read_secret_version`` / ``create_or_update_secret``
  ``path=`` expects — Vault inserts the ``/data/`` segment itself).
* ``#<field>`` (required) selects which field of the secret's data dict
  carries the value to move. The broker moves a single field, not a
  whole secret body, because the move is value-oriented (a password, a
  token) — naming the field is what keeps the SHA-256 / length in the
  response meaningful.
* A leading mount segment is **not** parsed out of the path here: the
  ``ref`` is forwarded to hvac's ``path=`` and the mount defaults to
  ``"secret"`` (the deployment KV-v2 mount, mirroring
  :data:`meho_backplane.connectors._shared.vault_creds.DEFAULT_KV_MOUNT`).
  A non-default mount is a per-adapter concern a sibling task can add via
  a richer ref grammar without touching the broker handler.

No secret in logs
=================

The adapter's structlog events carry only the ``path`` and the ``field``
*name* — never the field's value, and never the bytes carried by the
:class:`SecretMaterial`. The value lives only inside the
:class:`SecretMaterial` between the source read and the sink write.

Tenant scope
============

This adapter is a **second** KV-v2 read/write path (alongside the
``vault.kv.*`` handlers in :mod:`meho_backplane.connectors.vault.ops`), so
both methods run the same default-on defense-in-depth guard,
:func:`~meho_backplane.connectors.vault.tenant_scope.enforce_tenant_scope`,
**before** the Vault login. A ``secret.move`` whose source or sink ref
falls outside the operator's ``secret/tenants/{tenant_id}/`` subtree is
denied with :class:`~meho_backplane.connectors.vault.tenant_scope.VaultTenantScopeError`
at the app layer before any Vault round-trip — the broker and the KV-v2
handlers agree on the tenant subtree (S08, #296). The guard backstops the
per-tenant ``meho-mcp`` Vault ACL policy; it never grants access the
policy denies.
"""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Any, NamedTuple

import hvac
import structlog

import meho_backplane.auth.vault as _auth_vault
from meho_backplane.auth.secret_read_as import record_caller_kv_read, record_caller_secret_read
from meho_backplane.connectors._shared.vault_creds import (
    DEFAULT_KV_MOUNT,
    strip_credential_value,
)
from meho_backplane.connectors.base import ConnectorOwnMessage
from meho_backplane.connectors.secret.endpoints import (
    SecretMaterial,
    register_secret_endpoint,
)
from meho_backplane.connectors.vault.tenant_scope import enforce_tenant_scope
from meho_backplane.operations._preview import approved_preview

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator

__all__ = [
    "NOT_KNOWN",
    "SecretMergeChangedError",
    "SecretMergeDeletedVersionError",
    "SecretMergeRefusedError",
    "SecretSinkChangedError",
    "VaultKvSecretEndpoint",
    "VaultSecretRefError",
]

#: The approval card's answer when MEHO cannot tell whether a sink secret
#: (or its field) exists.
NOT_KNOWN = "not known"

_log = structlog.get_logger(__name__)


class VaultSecretRefError(ValueError):
    """A vault-kv ``ref`` is malformed or addresses a missing field.

    Raised for a ref without the required ``#<field>`` fragment, an
    empty path/field, a KV-v2 read whose data dict lacks the named
    field, or a malformed hvac payload. A :class:`ValueError` so the
    dispatcher's ``connector_error`` branch surfaces
    ``exception_class="VaultSecretRefError"``. The message names the
    path and field, never the value.
    """


class SecretMergeDeletedVersionError(VaultSecretRefError, ConnectorOwnMessage):
    """A merge was asked into a secret whose latest version is deleted or destroyed.

    Its other fields cannot be read, so the merge would lose them. Nothing
    is written. The message is a fixed MEHO sentence, so the caller sees it.
    """

    def __init__(self) -> None:
        super().__init__(
            "the latest version of the sink secret is deleted, so a merge cannot keep "
            "its other fields. Nothing was written. Restore the version (undelete) "
            "first, or move without merge to write a new version"
        )


class SecretMergeChangedError(VaultSecretRefError, ConnectorOwnMessage):
    """Vault refused the merge write because the secret changed after MEHO read it.

    The check-and-set guard did its job: nothing was written. Running the
    move again reads the new version. A fixed MEHO sentence, no Vault text.
    """

    def __init__(self) -> None:
        super().__init__(
            "the sink secret changed after MEHO read it, so Vault refused the merge "
            "write (check-and-set). Nothing was written. Run the move again"
        )


class SecretMergeRefusedError(VaultSecretRefError, ConnectorOwnMessage):
    """Vault refused the merge write, and the secret did not change since MEHO read it.

    Or MEHO could not read the secret again to tell (*known* is ``False``).
    Nothing was written. A fixed MEHO sentence, no Vault text.
    """

    def __init__(self, *, known: bool = True) -> None:
        if known:
            message = (
                "Vault refused the merge write. The sink secret did not change after "
                "MEHO read it, so running the move again does not help. Nothing was "
                "written. Check the Vault role and policy for this path"
            )
        else:
            message = (
                "Vault refused the merge write, and MEHO could not read the sink secret "
                "again to tell whether it changed. Nothing was written. Run the move "
                "again, or check the Vault role and policy for this path"
            )
        super().__init__(message)


class SecretSinkChangedError(VaultSecretRefError, ConnectorOwnMessage):
    """The approved run finds a sink secret or field the approval said does not exist.

    Nothing is written. A fixed MEHO sentence (the field *name* at most),
    no Vault text and no value.
    """

    def __init__(self, what: str) -> None:
        super().__init__(
            f"the sink changed since the approval: {what} exists now, but the approval "
            "showed it does not exist. Nothing was written. Send a new request"
        )


class _SinkExpectation(NamedTuple):
    """What the approval card of a ``secret.move`` said does not exist at the sink."""

    secret_missing: bool
    field_missing: bool


def _approved_sink_expectation() -> _SinkExpectation:
    """Read the sink side of the approval card, during the approved run of ``secret.move``.

    Only a ``"no"`` binds the run. ``"yes"``, ``"not known"`` and an
    ordinary run (no card) bind nothing.
    """
    card = approved_preview("secret.move")
    sink = card.get("sink") if card is not None else None
    if not isinstance(sink, dict):
        return _SinkExpectation(secret_missing=False, field_missing=False)
    return _SinkExpectation(
        secret_missing=sink.get("secret_exists") == "no",
        field_missing=sink.get("field_exists") == "no",
    )


def _parse_vault_ref(ref: str) -> tuple[str, str]:
    """Split ``"<path>#<field>"`` into ``(path, field)``.

    The split is on the **last** ``#`` so a path may itself contain a
    ``#`` (uncommon but legal in a KV-v2 key). Both halves must be
    non-empty after the split.
    """
    path, sep, field = ref.rpartition("#")
    if not sep or not path or not field:
        raise VaultSecretRefError(
            f"malformed vault secret ref {ref!r}: expected '<path>#<field>' "
            "selecting one KV-v2 field (e.g. 'secret/db/prod#password')"
        )
    return path.strip(), field.strip()


class VaultKvSecretEndpoint:
    """A vault-kv source+sink endpoint addressing one KV-v2 field.

    Constructed per move from the parsed ``ref``. Both methods open an
    operator-scoped Vault client via
    :func:`~meho_backplane.auth.vault.vault_client_for_operator` (JWT/OIDC
    login under the operator's own token, revoked on exit), so the move
    runs inside the operator's authorization envelope.
    """

    def __init__(self, ref: str, *, mount: str = DEFAULT_KV_MOUNT) -> None:
        self._path, self._field = _parse_vault_ref(ref)
        self._mount = mount

    def _client(self, operator: Operator, op_id: str) -> AbstractAsyncContextManager[hvac.Client]:
        """Open the Vault client this endpoint talks through.

        Here: the calling operator's own Vault identity; *op_id* (the
        ``vault.kv.*`` op this read or write matches) is not used. The
        ``vault-target`` kind overrides it to check the caller's policy for
        *op_id* on a registered Vault target and log in under that
        target's role instead.
        """
        del op_id
        return _auth_vault.vault_client_for_operator(operator)

    async def read_secret(self, operator: Operator) -> SecretMaterial:
        """Read the addressed field and wrap its value in a :class:`SecretMaterial`.

        Reads via ``client.secrets.kv.v2.read_secret_version``, performs
        the KV-v2 ``data.data`` double-unwrap (the same shape
        ``vault/ops.py`` and ``vault_creds._structural_unwrap`` use), and
        selects the named field. The value is coerced + whitespace-
        stripped by :func:`strip_credential_value` (a trailing newline is
        the single most common secret-storage artifact) before it is
        hashed and forwarded, so source and sink agree byte-for-byte.
        """
        _log.debug("secret_broker.vault.read", path=self._path, field=self._field)
        # Defense-in-depth tenant-scope check (#1643 / S08 #296): deny a path
        # outside the operator's tenant namespace BEFORE the Vault login, so
        # this second KV-v2 read path enforces the same per-tenant subtree as
        # the ``vault.kv.*`` handlers. No-op unless
        # ``vault_kv_tenant_scope_prefix`` is configured; behind the Vault
        # ``meho-mcp`` ACL policy, never a replacement for it. ``read_only``
        # so the platform-path allow-list applies on the read side.
        enforce_tenant_scope(operator, mount=self._mount, path=self._path, read_only=True)
        async with self._client(operator, "vault.kv.read") as client:
            payload = await asyncio.to_thread(
                client.secrets.kv.v2.read_secret_version,
                path=self._path,
                mount_point=self._mount,
                raise_on_deleted_version=False,
            )
        record_caller_kv_read(operator, payload)
        secret_data = _structural_unwrap(payload, path=self._path)
        if self._field not in secret_data:
            raise VaultSecretRefError(
                f"vault KV-v2 secret at {self._path!r} has no field {self._field!r}"
            )
        return SecretMaterial(strip_credential_value(secret_data[self._field]))

    def check_write_allowed(self, operator: Operator) -> None:
        """Deny a write outside the operator's tenant path, before any Vault call.

        Defense-in-depth tenant-scope check (#1643 / S08 #296), run before
        the Vault login. ``read_only=False`` — a write to the shared platform
        path under a non-owning operator stays tenant-scoped (#1725 M1).
        Public so ``secret.handoff.import`` can run it *before* it uses the
        one-time handoff, so a wrong sink path does not burn the handoff.
        """
        enforce_tenant_scope(operator, mount=self._mount, path=self._path, read_only=False)

    async def write_secret(self, operator: Operator, material: SecretMaterial) -> None:
        """Write *material* into the addressed field as a new KV-v2 version.

        KV-v2 replaces the latest version wholesale (no server-side
        merge), so the write sends a single-field secret body
        ``{<field>: <value>}`` (other fields are dropped; use
        :meth:`write_secret_merged` to keep them). The value is decoded back to ``str`` from
        the material's bytes — KV-v2 stores JSON string values, and the
        source read coerced via :func:`strip_credential_value`, so the
        round-trip is lossless for the credential strings the broker
        moves. The write is unconditional, except in the approved run of a
        ``secret.move`` whose approval said the sink secret does not exist:
        then it writes with ``cas=0``, so Vault refuses it if the secret
        exists by then (:class:`SecretSinkChangedError`, nothing written).
        """
        _log.debug("secret_broker.vault.write", path=self._path, field=self._field)
        self.check_write_allowed(operator)
        cas = 0 if _approved_sink_expectation().secret_missing else None
        body: dict[str, str] = {self._field: material.value.decode("utf-8")}
        async with self._client(operator, "vault.kv.put") as client:
            try:
                await asyncio.to_thread(
                    client.secrets.kv.v2.create_or_update_secret,
                    path=self._path,
                    secret=body,
                    cas=cas,
                    mount_point=self._mount,
                )
            except hvac.exceptions.InvalidRequest:
                if cas is None:
                    raise
                # Vault's answer to a cas=0 write on a secret that exists.
                raise SecretSinkChangedError("the sink secret") from None

    async def write_secret_merged(self, operator: Operator, material: SecretMaterial) -> None:
        """Write *material* into the addressed field and keep the other fields.

        The merge option of ``secret.move``. Reads the current secret,
        sets the one field, and writes the whole body back with a
        check-and-set (CAS) guard on the version that was read. If another
        writer changed the secret in between, Vault refuses the write and
        nothing is lost. A secret that does not exist yet is created with
        ``cas=0`` (Vault: "only if it does not exist"). A secret whose
        latest version is deleted or destroyed is refused and nothing is
        written: its other fields cannot be read, so a merge would lose them.

        In the approved run of a ``secret.move``, the read holds the sink to
        the approval: when the approval said the secret does not exist, a
        secret found now refuses the write, and when it said the field does
        not exist, the field found now refuses it
        (:class:`SecretSinkChangedError`, nothing written). The
        check-and-set on the read version covers the time up to the write.
        """
        _log.debug("secret_broker.vault.write_merged", path=self._path, field=self._field)
        self.check_write_allowed(operator)
        value = material.value.decode("utf-8")
        expect = _approved_sink_expectation()
        async with self._client(operator, "vault.kv.patch") as client:
            await asyncio.to_thread(self._merge_write, client, value, expect, operator)

    def _merge_write(
        self, client: hvac.Client, value: str, expect: _SinkExpectation, operator: Operator
    ) -> None:
        """Blocking read-modify-write with CAS (runs in a worker thread)."""
        kv = client.secrets.kv.v2
        body, cas = self._merge_read(kv, expect, operator)
        body[self._field] = value
        try:
            kv.create_or_update_secret(
                path=self._path, secret=body, cas=cas, mount_point=self._mount
            )
        except (hvac.exceptions.InvalidRequest, hvac.exceptions.Forbidden) as exc:
            # A failed check-and-set (400) or a policy that allows the read
            # but not the write (403).
            refusal = self._merge_refusal(kv, cas, operator)
            if expect.secret_missing and isinstance(refusal, SecretMergeChangedError):
                refusal = SecretSinkChangedError("the sink secret")
            raise refusal from exc

    def _merge_read(
        self, kv: Any, expect: _SinkExpectation, operator: Operator
    ) -> tuple[dict[str, Any], int]:
        """Read the sink secret for a merge: its data and the version to guard with.

        ``({}, 0)`` when the secret does not exist. Refuses a deleted latest
        version and a sink that differs from what the approval said. A read
        that returns the secret notes the caller's login for the audit row,
        also when the write after it fails or is refused.
        """
        try:
            payload = kv.read_secret_version(
                path=self._path, mount_point=self._mount, raise_on_deleted_version=False
            )
        except hvac.exceptions.InvalidPath as exc:
            # A destroyed latest version also answers 404, but with metadata.
            if _version_metadata(exc.json) is None:
                return {}, 0
            if expect.secret_missing:
                raise SecretSinkChangedError("the sink secret") from None
            raise SecretMergeDeletedVersionError from None
        metadata = _version_metadata(payload) or {}
        deleted = bool(metadata.get("deletion_time")) or metadata.get("destroyed") is True
        if not deleted:
            record_caller_secret_read(operator)
        if expect.secret_missing:
            raise SecretSinkChangedError("the sink secret")
        if deleted:
            raise SecretMergeDeletedVersionError
        body = dict(_structural_unwrap(payload, path=self._path))
        version = metadata.get("version")
        if not isinstance(version, int) or isinstance(version, bool):
            raise VaultSecretRefError(
                f"vault KV-v2 read for {self._path!r} returned no version, "
                "so the merge cannot be guarded with check-and-set"
            )
        if expect.field_missing and self._field in body:
            raise SecretSinkChangedError(f"the field {self._field!r} of the sink secret")
        return body, version

    def _merge_refusal(self, kv: Any, cas: int, operator: Operator) -> VaultSecretRefError:
        """Tell a changed secret from another refusal of the merge write.

        Reads the secret's version again. A version other than the one the
        write was guarded with means the secret changed after MEHO read it.
        Only the version is used; the read data is dropped at once. A read
        that returns the secret notes the caller's login for the audit row.
        """
        try:
            payload = kv.read_secret_version(
                path=self._path, mount_point=self._mount, raise_on_deleted_version=False
            )
        except hvac.exceptions.InvalidPath as exc:
            metadata = _version_metadata(exc.json)
            current = metadata.get("version") if metadata is not None else 0
        except Exception:
            return SecretMergeRefusedError(known=False)
        else:
            metadata = _version_metadata(payload) or {}
            if not metadata.get("deletion_time") and metadata.get("destroyed") is not True:
                record_caller_secret_read(operator)
            current = metadata.get("version")
        if not isinstance(current, int) or isinstance(current, bool):
            return SecretMergeRefusedError(known=False)  # no version: MEHO cannot tell
        if current != cas:
            return SecretMergeChangedError()
        return SecretMergeRefusedError()

    async def sink_state(self, operator: Operator, *, merge: bool) -> dict[str, str]:
        """Whether the sink secret exists and, with *merge*, whether its field exists.

        For the approval card. Each answer is ``"yes"``, ``"no"`` or
        :data:`NOT_KNOWN` (the tenant path rule or the target check refuses,
        Vault refuses the read, or Vault cannot be reached). Logs in the
        same way the write does. Only the field *name* is looked up; the
        read data is dropped at once and never logged.
        """
        op_id = "vault.kv.patch" if merge else "vault.kv.put"
        try:
            self.check_write_allowed(operator)
            async with self._client(operator, op_id) as client:
                exists, field = await asyncio.to_thread(self._read_state, client, operator)
        except Exception as exc:
            _log.info(
                "secret_broker.vault.sink_state_unknown",
                path=self._path,
                error_class=type(exc).__name__,
            )
            exists, field = NOT_KNOWN, NOT_KNOWN
        state = {"secret_exists": exists}
        if merge:
            state["field_exists"] = field
        return state

    def _read_state(self, client: hvac.Client, operator: Operator) -> tuple[str, str]:
        """Blocking read of ``(secret exists, field exists)`` (runs in a worker thread).

        A read that returns a secret that is not deleted notes the caller's
        login for the audit row.
        """
        kv = client.secrets.kv.v2
        try:
            payload = kv.read_secret_version(
                path=self._path, mount_point=self._mount, raise_on_deleted_version=False
            )
        except hvac.exceptions.InvalidPath as exc:
            # A destroyed latest version also answers 404, but with metadata.
            if _version_metadata(exc.json) is not None:
                return "yes", NOT_KNOWN
            return "no", "no"
        metadata = _version_metadata(payload) or {}
        if metadata.get("deletion_time") or metadata.get("destroyed") is True:
            return "yes", NOT_KNOWN
        record_caller_kv_read(operator, payload)
        data = _structural_unwrap(payload, path=self._path)
        return "yes", "yes" if self._field in data else "no"


def _version_metadata(payload: object) -> dict[str, Any] | None:
    """The ``data.metadata`` dict of a KV-v2 read answer, or ``None``."""
    outer = payload.get("data") if isinstance(payload, dict) else None
    metadata = outer.get("metadata") if isinstance(outer, dict) else None
    return metadata if isinstance(metadata, dict) else None


def _structural_unwrap(payload: object, *, path: str) -> dict[str, Any]:
    """Unwrap hvac's ``read_secret_version`` payload to the secret data dict.

    KV-v2's GET returns ``{"data": {"data": {<secret kv>}, "metadata":
    {...}}}``; the secret content is the nested ``data["data"]``. A
    malformed payload (missing either level) raises
    :class:`VaultSecretRefError` naming the path rather than a bare
    ``KeyError`` deep inside hvac's response — mirroring
    ``vault_creds._structural_unwrap``.
    """
    outer = payload.get("data") if isinstance(payload, dict) else None
    secret_data = outer.get("data") if isinstance(outer, dict) else None
    if not isinstance(secret_data, dict):
        raise VaultSecretRefError(
            f"vault KV-v2 read for {path!r} returned a malformed payload: "
            "expected a nested 'data.data' object holding the secret fields"
        )
    return secret_data


# Register the vault-kv adapter under kind ``"vault"`` at import time. The
# package ``__init__`` imports this module so the registration lands
# before the lifespan runs the move op's registrar.
register_secret_endpoint("vault", VaultKvSecretEndpoint)
