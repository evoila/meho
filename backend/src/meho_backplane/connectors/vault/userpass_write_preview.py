# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approval previews for the two Vault userpass writes that carry a password.

``vault.auth.userpass.write`` and ``vault.auth.userpass.update_password``
take the new password as a param, so they are pinned ``credential_write``
(:data:`~meho_backplane.broadcast.events._CREDENTIAL_WRITE_OPS`). For such an
op the generic echo of params is turned off, so without these builders the
approver saw only the op id and the target (#4850).

============================================  ==========================================
op_id                                         preview
============================================  ==========================================
``vault.auth.userpass.write``                 ``{mount, username, sets_password,
                                              token_policies, token_policies_change}``
``vault.auth.userpass.update_password``       ``{mount, username, sets_password}``
============================================  ==========================================

Each builder reads only ``mount``, ``username`` and ``token_policies`` from
the params. It never reads ``password``, so the value cannot reach the
stored approval request. ``sets_password`` only says that the call sets a
new password. The builders do no network calls.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.operations._preview import PreviewContext, register_preview_builder


def _mount_and_username(ctx: PreviewContext) -> tuple[str, str]:
    """Return the userpass mount (default ``userpass``) and the username."""
    mount = str(ctx.params.get("mount", "userpass")).strip()
    username = str(ctx.params.get("username", "")).strip()
    return mount, username


async def _userpass_write_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``vault.auth.userpass.write`` — user, mount and policies, no password.

    The token policies are what the user can do after login, so the approver
    must see them. When ``token_policies`` is absent, Vault keeps the user's
    current policies; ``token_policies_change`` says which case applies.
    """
    mount, username = _mount_and_username(ctx)
    raw_policies = ctx.params.get("token_policies")
    preview: dict[str, Any] = {
        "resource": "userpass_user",
        "mount": mount,
        "username": username,
        "sets_password": True,
    }
    if isinstance(raw_policies, list):
        preview["token_policies"] = [str(policy) for policy in raw_policies]
        preview["token_policies_change"] = "replace"
    else:
        preview["token_policies_change"] = "unchanged"
    return preview


async def _userpass_update_password_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``vault.auth.userpass.update_password`` — which user, no password."""
    mount, username = _mount_and_username(ctx)
    return {
        "resource": "userpass_user",
        "mount": mount,
        "username": username,
        "sets_password": True,
    }


def _register_vault_userpass_preview_builders() -> None:
    """Wire the two userpass preview builders. Called at import time."""
    register_preview_builder("vault.auth.userpass.write", _userpass_write_preview)
    register_preview_builder(
        "vault.auth.userpass.update_password", _userpass_update_password_preview
    )


_register_vault_userpass_preview_builders()
