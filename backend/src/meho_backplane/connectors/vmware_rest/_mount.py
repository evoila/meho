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

from collections.abc import Mapping
from typing import Any

SESSION_PATH_MODERN = "/api/session"
SESSION_PATH_LEGACY = "/rest/com/vmware/cis/session"

API_MOUNT_MODERN = "/api"
API_MOUNT_LEGACY = "/rest"
_KNOWN_API_MOUNT_PREFIXES = (f"{API_MOUNT_MODERN}/", f"{API_MOUNT_LEGACY}/")

# Broadcom documents the VI-JSON (vmomi-over-JSON) wire base as
# ``/sdk/vim25/{release}/{MoType}/{moId}/{method}`` — release-versioned,
# available since vCenter 8.0U1 and unchanged on 9.x (vSphere Web
# Services SDK programming guide, "Building JSON Request URLs"). This is
# a *different* base from the vSphere Automation API mounts above: the
# typed vmomi reads (``RetrievePropertiesEx``,
# ``VsanQueryVcClusterHealthSummary``, ``QueryEvents``, ``QueryPerf`` …)
# are served here, **not** under ``/api`` — an ``/api``-mounted vmomi
# method 404s on vCenter 8.0.x (the ``/api`` form is an undocumented 9.x
# accommodation, kept only as a fallback). See :func:`vmomi_mounted_path`.
VI_JSON_MOUNT_PREFIX = "/sdk/vim25"

# vSphere's list FilterSpec query params carry a ``filter.`` prefix on the
# legacy ``/rest`` mount (``filter.datastores``, ``filter.hosts``, ...) but
# are addressed by their bare name on the modern ``/api`` mount
# (``datastores``, ``hosts``, ...). See :func:`adapt_filter_params`.
_FILTER_PREFIX = "filter."

__all__ = [
    "API_MOUNT_LEGACY",
    "API_MOUNT_MODERN",
    "SESSION_PATH_LEGACY",
    "SESSION_PATH_MODERN",
    "VI_JSON_MOUNT_PREFIX",
    "adapt_filter_params",
    "api_mount_for_session_path",
    "mounted_path",
    "vmomi_mounted_path",
    "vmomi_release_from_version",
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


def adapt_filter_params(api_mount: str, query: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Key vSphere ``filter.*`` query params off the target's API mount.

    Composite sub-calls and typed-op listing legs author their query
    buckets in the legacy ``/rest`` style — ``filter.datastores``,
    ``filter.hosts``, ``filter.names``, ``filter.types`` and friends.
    The modern ``/api`` mount (real vCenter 8.x) addresses the same
    FilterSpec fields by their *bare* name (``datastores``, ``hosts``,
    ...) and returns HTTP 400 for the ``filter.``-prefixed form; the
    legacy ``/rest`` mount — and the ``vmware/vcsim`` simulator CI runs
    against — requires the prefix. Encode that protocol-flavor split
    once, here at the transport seam, rather than per call site.

    On any mount that is not the explicit legacy ``/rest`` mount (i.e.
    the modern mount, or an unknown one that
    :func:`api_mount_for_session_path` already resolves toward modern),
    strip the ``filter.`` prefix from every key; on the legacy mount
    return the params unchanged. Keys without the prefix pass through
    untouched on both mounts. Empty / ``None`` in → ``None`` out, so the
    result drops straight into ``params=`` at a seam that previously used
    the ``params=... or None`` idiom.
    """
    if not query:
        return None
    if api_mount == API_MOUNT_LEGACY:
        return dict(query)
    return {
        (key[len(_FILTER_PREFIX) :] if key.startswith(_FILTER_PREFIX) else key): value
        for key, value in query.items()
    }


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


def vmomi_release_from_version(version: str | None) -> str | None:
    """Normalise a vCenter ``about.version`` to the VI-JSON ``{release}`` segment.

    Broadcom's ``/sdk/vim25/{release}/...`` base takes a four-part
    dotted-decimal release (``8.0.2.0``, ``9.0.0.0``); ``GET /api/about``
    reports the three-part ``version`` (``8.0.3``). Pad the leading
    numeric components to four with ``.0`` so ``8.0.3`` → ``8.0.3.0`` (an
    already-four-part value passes through, a longer one is truncated).

    Returns ``None`` when *version* is empty or has no leading numeric
    component, so the caller falls back to the ``/api``-mounted vmomi form
    rather than emitting a malformed ``/sdk/vim25//...`` path.
    """
    if not version:
        return None
    numeric: list[str] = []
    for part in version.strip().split("."):
        if part.isdigit():
            numeric.append(part)
        else:
            break
    if not numeric:
        return None
    while len(numeric) < 4:
        numeric.append("0")
    return ".".join(numeric[:4])


def vmomi_mounted_path(release: str, descriptor_path: str) -> str:
    """Mount a spec-relative vmomi method *descriptor_path* on the VI-JSON base.

    Prefixes ``/{MoType}/{moId}/{method}`` with
    ``/sdk/vim25/{release}`` — the documented VI-JSON wire base
    (:data:`VI_JSON_MOUNT_PREFIX`) — so a typed vmomi read reaches the
    endpoint vCenter 8.0.x actually serves instead of 404ing on the
    Automation ``/api`` mount. *release* is the four-part segment from
    :func:`vmomi_release_from_version`; *descriptor_path* is normalised to
    a leading slash the same way :func:`mounted_path` normalises the
    Automation form.
    """
    normalised = descriptor_path if descriptor_path.startswith("/") else f"/{descriptor_path}"
    return f"{VI_JSON_MOUNT_PREFIX}/{release}{normalised}"
