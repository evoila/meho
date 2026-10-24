# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approval preview for ``vmware.composite.vm.guest.file.read`` (#4850).

The op logs into the guest OS, so it is pinned ``credential_write``
(:data:`~meho_backplane.broadcast.events._CREDENTIAL_WRITE_OPS`) and the
generic echo of params is turned off. It is a ``caution`` read: an agent's
call waits for approval, a person's call runs at once. Without this builder
the approver of an agent's call saw only the op id and the target.

The preview is ``{vm, guest_path, fetch_content, max_inline_bytes?,
guest_secret_ref?}``: which file in which VM, and whether MEHO downloads the
file bytes. ``guest_secret_ref`` is the store path of the guest login, shown
so the approver knows which login is used; the login itself is read only
when the approved call runs. The builder reads only the params and does no
network calls.

It lives here, not in :mod:`._write_preview`, because that module's map holds
the write composites only (a test pins it to them).
"""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.vmware_rest.composites._guest_secret_ref import (
    GUEST_SECRET_REF_PARAM,
)
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

OP_ID_GUEST_FILE_READ = "vmware.composite.vm.guest.file.read"


async def guest_file_read_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``vm.guest.file.read`` — which file in which VM, and if bytes come back."""
    fetch_content = bool(ctx.params.get("fetch_content", False))
    preview: dict[str, Any] = {
        "vm": ctx.params.get("vm"),
        "guest_path": ctx.params.get("guest_path"),
        "fetch_content": fetch_content,
    }
    max_inline_bytes = ctx.params.get("max_inline_bytes")
    if fetch_content and isinstance(max_inline_bytes, int):
        preview["max_inline_bytes"] = max_inline_bytes
    guest_secret_ref = ctx.params.get(GUEST_SECRET_REF_PARAM)
    if isinstance(guest_secret_ref, str) and guest_secret_ref:
        preview[GUEST_SECRET_REF_PARAM] = guest_secret_ref
    return preview


register_preview_builder(OP_ID_GUEST_FILE_READ, guest_file_read_preview)
