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
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING, cast

import hvac

from meho_backplane.connectors._shared.vault_creds import DEFAULT_KV_MOUNT
from meho_backplane.connectors.secret.endpoints import register_secret_endpoint
from meho_backplane.connectors.secret.vault_endpoint import (
    VaultKvSecretEndpoint,
    VaultSecretRefError,
)
from meho_backplane.connectors.vault.target_auth import is_vault_target, vault_client_for_target
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.targets.resolver import resolve_target
from meho_backplane.targets.schemas import Target

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from meho_backplane.auth.operator import Operator

__all__ = ["VAULT_TARGET_KIND", "VaultTargetKvSecretEndpoint"]

VAULT_TARGET_KIND = "vault-target"


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

    @asynccontextmanager
    async def _client(self, operator: Operator) -> AsyncIterator[hvac.Client]:
        """Resolve the target (tenant-scoped, by name), then log in under its role."""
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            row = await resolve_target(session, operator.tenant_id, self._target_name)
        if not is_vault_target(row):
            raise VaultSecretRefError(
                f"target {self._target_name!r} is not a Vault target "
                f"(product {getattr(row, 'product', None)!r})"
            )
        # The login reads only product and extras, which the ORM row has;
        # the same narrow the keycloak adapter makes for its target.
        target = cast(Target, row)
        client_cm: AbstractAsyncContextManager[hvac.Client] = vault_client_for_target(
            operator, target
        )
        async with client_cm as client:
            yield client


register_secret_endpoint(VAULT_TARGET_KIND, VaultTargetKvSecretEndpoint)
