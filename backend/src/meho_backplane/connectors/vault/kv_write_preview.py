# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Park-time ``proposed_effect`` preview builders for the Vault KV-v2 write ops.

G0.31 follow-up (#2332). Wires redaction-safe preview builders for the
three ``vault.kv.*`` write ops onto the per-op builder hook shipped by
#1437 (:mod:`meho_backplane.operations._preview`):

================  ===================================================
op_id             preview stored in ``ApprovalRequest.proposed_effect``
================  ===================================================
``vault.kv.put``    ``{mount, path, kv_version, semantics: replace, key_names, cas?}``
``vault.kv.patch``  ``{mount, path, kv_version, semantics: merge, key_names}``
``vault.kv.delete`` ``{mount, path, kv_version, semantics: soft_delete, versions}``
================  ===================================================

Why bespoke builders are required here
======================================

``vault.kv.put`` and ``vault.kv.patch`` classify as ``credential_write``
(:data:`~meho_backplane.broadcast.events._CREDENTIAL_WRITE_OPS`): the
KV-v2 secret ``data`` rides in the *request params*. The generic
params-echo default (#1856) is therefore suppressed for them
(:func:`~meho_backplane.operations._preview.build_proposed_effect` step 3),
so before this module the parked request carried only the op-identity
default ``{op_id, connector_id, target_id}``. The approver could see
*that* a ``vault.kv.put`` against a target was pending, but not WHICH KV
path or WHAT keys would be written — the four-eyes decision was made
blind (#2332).

A *bespoke* builder is the deliberate exception to the credential-class
suppression: like the permission-preflight hook (G0.20-T4 #1504) and the
Keycloak user-create preview (#1857), it is trusted to own its own field
discipline and runs even for a credential-class op. These builders emit
only the KV **path**, the **mount**, the KV **version**, the write
**semantics** (put = wholesale replace vs patch = merge vs delete =
version soft-delete), and — critically — the set of **key names** being
written, never their **values**. That restores the approver's ability to
distinguish "rotate the throwaway probe key" from "clobber the production
database password" while keeping the value-redaction promise intact.

``vault.kv.delete`` classifies as plain ``write`` (its params carry only
the path + version numbers, no secret), so it would already get the
generic params-echo default. A bespoke builder gives it the same
resource-centric shape as its siblings for a consistent approval surface.

Redaction discipline
====================

The preview reads only ``ctx.params`` and echoes:

* ``mount`` / ``path`` — non-secret addressing. The path is what the
  approver needs to reason about the blast radius.
* ``key_names`` — the *top-level* keys of the ``data`` object being
  written (``put`` / ``patch``), sorted, **names only**. A KV key name
  (``db_password``, ``api_token``) is descriptive metadata, not secret
  material; its *value* never leaves the params dict. Nested values are
  never walked, so no secret rides along even from a nested structure.
* ``versions`` — the integer version numbers a ``delete`` soft-deletes.
* ``cas`` — the optional Check-And-Set version guard on a ``put`` (an
  integer, not secret).

No ``data`` *value* is ever read or echoed.

Fail-soft
=========

Every builder is pure (no connector I/O) — it reads only ``ctx.params``.
Should a malformed param shape raise anyway,
:func:`~meho_backplane.operations._preview.build_proposed_effect` swallows
it into the explicit ``preview_unavailable`` marker (#1628) rather than
blocking the park, matching the existing builder contract.

References
----------

* Task: https://github.com/evoila/meho/issues/2332
* Parent goal: https://github.com/evoila/meho/issues/221
* Parent initiative: https://github.com/evoila/meho/issues/2364
* Builder seam: G11.7 #1437; credential-class suppression + bespoke
  exception: #1856 / #1857.
* KV-v2 write ops: G3.3-T1 #545; write-capability preflight: G0.20-T4 #1504.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.vault.ops import _DEFAULT_KV_MOUNT
from meho_backplane.operations._preview import (
    PreviewContext,
    register_preview_builder,
)

#: KV-v2 is the only secret engine version these ops address (hvac's
#: ``secrets.kv.v2`` client). Surfaced so the reviewer reads the engine
#: version explicitly rather than inferring it from the op id.
_KV_VERSION = 2


def _mount_and_path(ctx: PreviewContext) -> tuple[str, str]:
    """Resolve the ``(mount, path)`` the parked write addresses.

    Mirrors the handler's mount defaulting (:data:`_DEFAULT_KV_MOUNT`)
    without the raising path-shape pre-flight
    (:func:`~meho_backplane.connectors.vault.ops._extract_mount_and_path`):
    a preview must never fault on a formatting quirk that the write itself
    would reject downstream. Both values are non-secret addressing.
    """
    mount = str(ctx.params.get("mount", _DEFAULT_KV_MOUNT)).strip()
    path = str(ctx.params.get("path", "")).strip()
    return mount, path


def _data_key_names(ctx: PreviewContext) -> list[str]:
    """Return the sorted top-level key names of the ``data`` param.

    Names only — the values (the secret material) are never read. A
    non-dict / absent ``data`` yields an empty list so a malformed param
    shape previews no keys rather than raising.
    """
    data = ctx.params.get("data")
    if not isinstance(data, dict):
        return []
    return sorted(str(key) for key in data)


async def _kv_put_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``vault.kv.put`` — path + key names visible, values never.

    ``put`` replaces the latest version wholesale (KV-v2 does not merge),
    so the semantics label is ``replace``. The set of key names that will
    survive the write is exactly ``data``'s top-level keys.
    """
    mount, path = _mount_and_path(ctx)
    preview: dict[str, Any] = {
        "resource": "kv_secret",
        "mount": mount,
        "path": path,
        "kv_version": _KV_VERSION,
        "semantics": "replace",
        "key_names": _data_key_names(ctx),
    }
    cas = ctx.params.get("cas")
    if isinstance(cas, int) and not isinstance(cas, bool):
        # A version guard, not a secret — surface so the reviewer sees a
        # create-only (cas=0) or optimistic-lock (cas=N) write.
        preview["cas"] = cas
    return preview


async def _kv_patch_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``vault.kv.patch`` — merged key names visible, values never.

    ``patch`` merges the supplied fields onto the current version, so the
    semantics label is ``merge``; the key names are the fields being
    added or overwritten (keys absent from ``data`` are preserved).
    """
    mount, path = _mount_and_path(ctx)
    return {
        "resource": "kv_secret",
        "mount": mount,
        "path": path,
        "kv_version": _KV_VERSION,
        "semantics": "merge",
        "key_names": _data_key_names(ctx),
    }


async def _kv_delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``vault.kv.delete`` — path + soft-deleted versions.

    A version soft-delete carries no secret in its params; the blast
    radius is *which versions* of *which path* stop being readable.
    """
    mount, path = _mount_and_path(ctx)
    raw_versions = ctx.params.get("versions")
    versions = (
        [int(v) for v in raw_versions if isinstance(v, int) and not isinstance(v, bool)]
        if isinstance(raw_versions, list)
        else []
    )
    return {
        "resource": "kv_secret",
        "mount": mount,
        "path": path,
        "kv_version": _KV_VERSION,
        "semantics": "soft_delete",
        "versions": versions,
    }


def _register_vault_kv_write_preview_builders() -> None:
    """Wire the Vault KV-v2 write preview builders. Called at import time."""
    register_preview_builder("vault.kv.put", _kv_put_preview)
    register_preview_builder("vault.kv.patch", _kv_patch_preview)
    register_preview_builder("vault.kv.delete", _kv_delete_preview)


_register_vault_kv_write_preview_builders()
