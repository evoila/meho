# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Only a platform admin may choose the Vault login role of a target.

A vault target may name the Vault login role and the auth mount it logs in
under in its ``extras`` (``vault_role`` and ``vault_mount``, see
:mod:`meho_backplane.connectors.vault.target_auth`). Which Vault role a
tenant's calls log in under is a decision for the whole install, not for
one tenant. So every write path that stores ``extras`` — ``POST`` and
``PATCH`` on ``/api/v1/targets``, and the ``targets.register`` operation,
which calls the same create handler — runs
:func:`enforce_vault_login_extras_platform_admin` first.

The check compares the stored value of each key with the value the request
would store. A caller without ``platform_admin`` may send ``extras`` as long
as neither key changes: adding, changing or removing one is refused with
HTTP 403 ``vault_login_role_requires_platform_admin``. A target that
already carries a role keeps it, and a ``PATCH`` that re-sends the same
value (the UI import does that) passes. Values are compared as stored; a
blank string is a change like any other, even though the login treats it
as "no role".
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import structlog
from fastapi import HTTPException, status

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vault.target_auth import (
    VAULT_MOUNT_EXTRAS_KEY,
    VAULT_ROLE_EXTRAS_KEY,
)

_log = structlog.get_logger(__name__)

#: The ``extras`` keys that pick the Vault login role and auth mount.
VAULT_LOGIN_EXTRAS_KEYS: tuple[str, ...] = (VAULT_ROLE_EXTRAS_KEY, VAULT_MOUNT_EXTRAS_KEY)

#: The 403 ``detail`` when a caller without ``platform_admin`` changes a key.
VAULT_LOGIN_ROLE_REQUIRES_PLATFORM_ADMIN = "vault_login_role_requires_platform_admin"


def changed_vault_login_keys(
    before: Mapping[str, Any] | None, after: Mapping[str, Any] | None
) -> list[str]:
    """Return the Vault login keys whose stored value *after* differs from *before*.

    ``None`` stands for "no extras" (a new target, a SQL ``NULL`` row, or a
    ``PATCH`` that clears ``extras``). A key that is absent on one side and
    present on the other counts as changed.
    """
    old = before or {}
    new = after or {}
    return [key for key in VAULT_LOGIN_EXTRAS_KEYS if old.get(key) != new.get(key)]


def enforce_vault_login_extras_platform_admin(
    operator: Operator,
    *,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    target_name: str,
) -> None:
    """Refuse a change of ``vault_role`` / ``vault_mount`` by a non-platform-admin.

    Raises HTTP 403 ``vault_login_role_requires_platform_admin`` when a key
    changes and ``operator.platform_admin`` is false. Returns when nothing
    changed or the operator is a platform admin, so a PATCH that touches
    other fields of a target that already has a role passes.
    """
    changed = changed_vault_login_keys(before, after)
    if not changed or operator.platform_admin:
        return
    _log.warning(
        "vault_login_extras_denied",
        operator_sub=operator.sub,
        tenant_id=str(operator.tenant_id),
        target_name=target_name,
        keys=changed,
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=VAULT_LOGIN_ROLE_REQUIRES_PLATFORM_ADMIN,
    )
