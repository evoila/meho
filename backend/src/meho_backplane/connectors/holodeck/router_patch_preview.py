# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approval preview for ``holodeck.router.patch`` (#4850).

The op is pinned ``credential_write`` as a safety net
(:data:`~meho_backplane.broadcast.events._CREDENTIAL_WRITE_OPS`), and it takes
no params. So without this builder the approver saw only the op id and the
target, not what the call changes on the HoloRouter.

The preview is ``{hr_version, verify_only, patches}``. ``patches`` lists each
fixed patch the call checks and, unless ``verify_only`` is true, may apply:
its id and the module file it edits. Both come from constants in code
(:data:`~meho_backplane.connectors.holodeck.ops_deploy.ROUTER_PATCHES`), and
``hr_version`` comes from the target, so the preview holds no secret. On a
9.1 HoloRouter the call only checks and changes nothing. The builder does no
network calls.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.operations._preview import PreviewContext, register_preview_builder


async def _router_patch_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``holodeck.router.patch`` — which patches, which files, check-only or not."""
    # Imported here: ``ops_deploy`` and ``ops`` import each other, and the
    # package imports this module first.
    from meho_backplane.connectors.holodeck.ops_deploy import (
        ROUTER_PATCHES,
        _is_ninety_one,
        target_hr_version,
    )

    hr_version = target_hr_version(ctx.target)
    return {
        "resource": "holorouter_modules",
        "hr_version": hr_version,
        "verify_only": _is_ninety_one(hr_version),
        "patches": [
            {"patch_id": patch.patch_id, "file_path": patch.file_path} for patch in ROUTER_PATCHES
        ],
    }


register_preview_builder("holodeck.router.patch", _router_patch_preview)
