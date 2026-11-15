# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Vault KV-v2 adapter that acts through a registered Vault target.

Registered under kind ``"vault-target"``. The ref is::

    <target>/<path>#<field>

* ``<target>`` is the name (or alias) of a registered Vault target. The
  adapter logs in to Vault under that target's role
  (``extras["vault_role"]``), not under the calling operator's own Vault
  role. Source and sink each name their own target, so a move can read
  with one role and write with another.
* ``<path>#<field>`` is the same KV-v2 address the plain ``vault:`` kind
  takes.

The plain ``vault:`` kind is unchanged: it never has a target segment and
always uses the operator's own Vault identity. A separate kind keeps the
two grammars apart, so a path whose first segment equals a target name
cannot be read the wrong way.

The tenant-scope guard and the "no secret in logs" rules of
:mod:`meho_backplane.connectors.secret.vault_endpoint` apply unchanged.

Before any Vault login, each read or write checks, in this order:

1. the tenant path rule of the ``vault:`` kind (no I/O);
2. the target is a Vault target of the caller's tenant and names its own
   role (``extras["vault_role"]``). A target without a role is refused:
   the role is what does the read or the write;
3. the caller's MEHO policy for the matching ``vault.kv.*`` op on that
   target (:func:`~meho_backplane.operations.secondary_target_policy.secondary_target_refusal`):
   ``vault.kv.read`` for a source, ``vault.kv.put`` for a sink and
   ``vault.kv.patch`` for a sink with ``merge``. When a reviewer runs an
   approved move, the requester's policy is checked too.

:func:`vault_target_move_preflight` runs checks 2 and 3 when ``secret.move``
would wait for approval, so a move the caller may not make is refused
before a person is asked to approve it.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING, Any, Final, cast

import hvac

from meho_backplane.connectors._shared.vault_creds import DEFAULT_KV_MOUNT
from meho_backplane.connectors.base import ConnectorOwnMessage
from meho_backplane.connectors.secret.endpoints import parse_secret_ref, register_secret_endpoint
from meho_backplane.connectors.secret.vault_endpoint import (
    VaultKvSecretEndpoint,
    VaultSecretRefError,
)
from meho_backplane.connectors.vault.target_auth import (
    is_vault_target,
    resolve_vault_target_auth,
    vault_client_for_target,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations.secondary_target_policy import (
    SecondaryTargetWrite,
    requester_policy_preflight,
    secondary_target_refusal,
)
from meho_backplane.targets.resolver import (
    AmbiguousTargetError,
    TargetNotFoundError,
    resolve_target,
)
from meho_backplane.targets.schemas import Target

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from meho_backplane.auth.operator import Operator
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "VAULT_TARGET_KIND",
    "VaultTargetKvSecretEndpoint",
    "VaultTargetRefusedError",
    "check_vault_target_refs",
    "vault_target_move_preflight",
]

VAULT_TARGET_KIND = "vault-target"

_VAULT_CONNECTOR_ID: Final = "vault-1.x"
_DOC_REF: Final = "docs/codebase/connectors-secret-broker.md#vault-target-refs-and-the-merge-option"


class VaultTargetRefusedError(VaultSecretRefError, ConnectorOwnMessage):
    """The named Vault target cannot be used for this read or write.

    The target has no role of its own, or the caller's MEHO policy refuses
    the ``vault.kv.*`` op on it. Raised before any Vault login. MEHO writes
    the message (target name, op id, policy reason; never a value), so the
    caller sees it.
    """


def _policy_check(op_id: str) -> SecondaryTargetWrite:
    return SecondaryTargetWrite(
        connector_id=_VAULT_CONNECTOR_ID, op_ids=(op_id,), target_label="Vault target"
    )


class VaultTargetKvSecretEndpoint(VaultKvSecretEndpoint):
    """A vault-kv endpoint that logs in under a registered Vault target's role."""

    def __init__(self, ref: str, *, mount: str = DEFAULT_KV_MOUNT) -> None:
        target_name, sep, rest = ref.partition("/")
        if not sep or not target_name.strip() or not rest.strip():
            raise VaultSecretRefError(
                f"malformed vault-target secret ref {ref!r}: expected "
                "'<target>/<path>#<field>' (e.g. 'vault-prod/secret/db/prod#password')"
            )
        super().__init__(rest, mount=mount)
        self._target_name = target_name.strip()

    @property
    def target_name(self) -> str:
        """The target name (or alias) the ref names."""
        return self._target_name

    async def resolve_vault_target(self, operator: Operator) -> TargetORM:
        """Find the named target in the caller's tenant; refuse one MEHO cannot use.

        Refused (:class:`VaultTargetRefusedError`, before any Vault login):
        a target that is not a Vault target, and a Vault target without its
        own ``vault_role``. An unknown target raises the resolver's error.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            row = await resolve_target(session, operator.tenant_id, self._target_name)
        if not is_vault_target(row):
            raise VaultTargetRefusedError(
                f"target {self._target_name!r} is not a Vault target "
                f"(product {getattr(row, 'product', None)!r})"
            )
        if resolve_vault_target_auth(cast(Target, row)).role is None:
            raise VaultTargetRefusedError(
                f"Vault target {self._target_name!r} has no vault_role, so it cannot "
                "be used in a vault-target ref: its own role does the read or the write. "
                "Set extras.vault_role on the target, or use a plain 'vault:' ref"
            )
        return row

    async def check_access(self, operator: Operator, op_id: str) -> TargetORM:
        """Find the target and check the caller's policy for *op_id* on it. No Vault call."""
        row = await self.resolve_vault_target(operator)
        refusal = await secondary_target_refusal(operator, _policy_check(op_id), row)
        if refusal is not None:
            raise VaultTargetRefusedError(f"{refusal}. Nothing was read or written.")
        return row

    @asynccontextmanager
    async def _client(self, operator: Operator, op_id: str) -> AsyncIterator[hvac.Client]:
        """Check the target and the caller's policy for *op_id*, then log in under its role."""
        row = await self.check_access(operator, op_id)
        # The login reads only product and extras, which the ORM row has;
        # the same narrow the keycloak adapter makes for its target.
        target = cast(Target, row)
        client_cm: AbstractAsyncContextManager[hvac.Client] = vault_client_for_target(
            operator, target
        )
        async with client_cm as client:
            yield client


def _move_sides(params: dict[str, Any]) -> list[tuple[VaultTargetKvSecretEndpoint, str]]:
    """The ``vault-target`` sides of a move, each with the op it runs on its target."""
    sink_op = "vault.kv.patch" if params.get("merge") is True else "vault.kv.put"
    sides: list[tuple[VaultTargetKvSecretEndpoint, str]] = []
    for key, op_id in (("from", "vault.kv.read"), ("to", sink_op)):
        raw = params.get(key)
        if not isinstance(raw, str):
            continue
        try:
            spec = parse_secret_ref(raw)
            if spec.kind == VAULT_TARGET_KIND:
                sides.append((VaultTargetKvSecretEndpoint(spec.ref), op_id))
        except ValueError:
            continue  # the run reports a malformed ref itself
    return sides


async def check_vault_target_refs(operator: Operator, params: dict[str, Any]) -> None:
    """Check every ``vault-target`` side of a move before the move reads anything.

    So a sink the caller may not use stops the move before the source is
    read. Raises :class:`VaultTargetRefusedError` (or the resolver's error
    for an unknown target). Each read or write checks again at its login.
    """
    for endpoint, op_id in _move_sides(params):
        await endpoint.check_access(operator, op_id)


def _refused(reason: str, record: dict[str, Any]) -> dict[str, Any]:
    """A preflight answer that refuses the park (``will_be_denied``)."""
    return {
        **record,
        "will_be_denied": True,
        "reason": reason,
        "remediation": (
            "Name a Vault target that has its own vault_role and that you may use "
            "for this read or write, or ask an admin for that grant, then call again."
        ),
        "doc_ref": _DOC_REF,
    }


async def vault_target_move_preflight(ctx: PreviewContext) -> dict[str, Any] | None:
    """Park-time check of the ``vault-target`` sides of ``secret.move``.

    Runs as the caller, when the move would wait for approval. For each
    ``vault-target`` side it finds the target, checks that it has its own
    role, and checks the caller's MEHO policy for the matching
    ``vault.kv.*`` op on it. A problem returns ``will_be_denied: true``:
    MEHO then refuses the call and does not ask a person to approve it.
    Otherwise returns the requester record, so a reviewer's run checks the
    requester again. ``None`` when the move has no ``vault-target`` side.
    """
    record: dict[str, Any] | None = None
    for endpoint, op_id in _move_sides(ctx.params):
        try:
            row = await endpoint.resolve_vault_target(ctx.operator)
        except VaultTargetRefusedError as exc:
            return _refused(str(exc), record or {})
        except (TargetNotFoundError, AmbiguousTargetError):
            return _refused(
                f"no single Vault target {endpoint.target_name!r} in your tenant", record or {}
            )
        verdict = await requester_policy_preflight(ctx.operator, _policy_check(op_id), row)
        if verdict["requester_denied"]:
            return _refused(str(verdict["requester_reason"]), verdict)
        record = verdict
    return record


register_secret_endpoint(VAULT_TARGET_KIND, VaultTargetKvSecretEndpoint)
