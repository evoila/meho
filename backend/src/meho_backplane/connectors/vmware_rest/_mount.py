# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""vCenter REST endpoint topology — session paths + op mount mapping.

Extracted from ``connector.py`` to keep that module within the
code-quality size budget. This module owns one cohesive concern:
*where vCenter exposes its REST surface*, and how a spec-relative
ingested-descriptor path maps onto the mount a given target actually
serves.

Modern vCenter (8.0+) serves the automation API under ``/api`` and
mints sessions at ``POST /api/session``. Older vCenter and the
``vmware/vcsim`` simulator serve the legacy ``/rest`` mount; vcsim
only registers ``POST /rest/com/vmware/cis/session`` (per
``govmomi/vapi/simulator``). The connector discovers which mount is
live per-target during session establishment (the modern→legacy 404
fallback in ``VmwareRestConnector._session_token``) and uses
:func:`mounted_path` to route every subsequent ingested op to the
same mount.

Ingested descriptors carry *spec-relative* paths — the G0.7 pipeline
strips the OpenAPI server base, so the canary's op_ids are
``GET:/vcenter/vm`` (not ``GET:/api/vcenter/vm``). Mapping that onto
``/api`` vs ``/rest`` is connector-owned vendor knowledge; the
generic dispatcher stays vendor-neutral (CLAUDE.md postulate 5).
"""

from __future__ import annotations

SESSION_PATH_MODERN = "/api/session"
SESSION_PATH_LEGACY = "/rest/com/vmware/cis/session"

API_MOUNT_MODERN = "/api"
API_MOUNT_LEGACY = "/rest"
_KNOWN_API_MOUNT_PREFIXES = (f"{API_MOUNT_MODERN}/", f"{API_MOUNT_LEGACY}/")

__all__ = [
    "API_MOUNT_LEGACY",
    "API_MOUNT_MODERN",
    "SESSION_PATH_LEGACY",
    "SESSION_PATH_MODERN",
    "api_mount_for_session_path",
    "mounted_path",
]


def api_mount_for_session_path(session_path: str) -> str:
    """Map an established session path to its REST API mount prefix.

    ``/api/session`` → ``/api`` (modern); the legacy
    ``/rest/com/vmware/cis/session`` → ``/rest``. Defaults to the
    modern mount when the recorded path is neither known constant so a
    future session-path addition fails toward the production-correct
    mount rather than silently misrouting every op to ``/rest``.
    """
    if session_path == SESSION_PATH_LEGACY:
        return API_MOUNT_LEGACY
    return API_MOUNT_MODERN


def mounted_path(session_path: str, descriptor_path: str) -> str:
    """Return *descriptor_path* prefixed with the mount *session_path* implies.

    A path already carrying a known mount prefix (``/api/...`` /
    ``/rest/...``) is returned unchanged so an explicitly-mounted
    descriptor isn't double-prefixed. Otherwise the spec-relative path
    is normalised to a leading slash and prefixed with the mount the
    target's established session path selects.
    """
    if descriptor_path.startswith(_KNOWN_API_MOUNT_PREFIXES):
        return descriptor_path
    mount = api_mount_for_session_path(session_path)
    normalised = descriptor_path if descriptor_path.startswith("/") else f"/{descriptor_path}"
    return f"{mount}{normalised}"
