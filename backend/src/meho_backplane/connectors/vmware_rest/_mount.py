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
from dataclasses import dataclass
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

# vSAN methods (#4441). vCenter 9.x serves them through VI-JSON under a
# ``/vsan`` service segment: ``/sdk/vim25/{release}/vsan/{MoType}/{moId}/{method}``.
# Source: the vendor VI/JSON files of 9.0, 9.1.0 and 9.1.1 (``servers`` url
# ``/sdk/vim25/{release}``, path ``/vsan/VsanVcClusterHealthSystem/{moId}/
# VsanQueryVcClusterHealthSummary``). The vendor VI/JSON files of 8.0 U1, U2
# and U3 have no ``/vsan`` paths, so on vCenter 8.0 the connector sends vSAN
# methods as SOAP to ``/vsanHealth`` instead (see :func:`vsan_needs_soap`).
VSAN_PATH_PREFIX = "/vsan"
#: The first vCenter major version whose VI/JSON API has the vSAN methods.
VSAN_VI_JSON_MIN_MAJOR = 9

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
    "VSAN_PATH_PREFIX",
    "VSAN_VI_JSON_MIN_MAJOR",
    "VsanMethod",
    "adapt_filter_params",
    "api_mount_for_session_path",
    "is_vmomi_path",
    "mounted_path",
    "vmomi_mounted_path",
    "vmomi_release_from_version",
    "vsan_method_path",
    "vsan_needs_soap",
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


def is_vmomi_path(descriptor_path: str) -> bool:
    """Return ``True`` when *descriptor_path* is a spec-relative VI-JSON path.

    ``vi-json.yaml`` paths start with a PascalCase managed-object type
    (``/DistributedVirtualPortgroup/{moId}/config``,
    ``/PropertyCollector/{moId}/RetrievePropertiesEx``). Every vSphere
    Automation path in ``vcenter.yaml`` starts with a lower-case segment
    (``/vcenter``, ``/appliance``, ``/cis`` ...), and an already-mounted
    path starts with ``/api``, ``/rest`` or ``/sdk``. So the case of the
    first character tells the two spec families apart without a lookup.
    """
    first = descriptor_path.lstrip("/")[:1]
    return first.isascii() and first.isupper()


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


@dataclass(frozen=True, slots=True)
class VsanMethod:
    """One vSAN method on a vSAN object that vCenter serves for all clusters.

    vCenter gives each of these objects a fixed id, for example
    ``vsan-cluster-health-system`` for ``VsanVcClusterHealthSystem`` (vendor
    sample ``vsanapiutils.py`` of the vSAN Management SDK). Every vSAN
    operation declares the method it calls as one module-level constant of
    this type and sends it with ``VmwareRestConnector._post_vsan``. The
    reconcile lanes collect these constants and check each method against
    the vendor files.
    """

    mo_type: str
    moid: str
    method: str


def vsan_method_path(mo_type: str, moid: str, method: str) -> str:
    """The VI-JSON path of one vSAN method: ``/vsan/{MoType}/{moId}/{method}``.

    The one helper that builds every vSAN path. The result is spec-relative:
    :func:`vmomi_mounted_path` puts ``/sdk/vim25/{release}`` in front of it,
    the same as for any other VI-JSON method. With ``moid="{moId}"`` it gives
    the path as the vendor VI/JSON file writes it.
    """
    return f"{VSAN_PATH_PREFIX}/{mo_type}/{moid}/{method}"


def vsan_needs_soap(version: str | None) -> bool:
    """``True`` when a vCenter of *version* has no VI-JSON vSAN methods (8.0 and older).

    *version* is the vCenter version the connector detected (``8.0.3.0``,
    ``9.1.0.0``, ...). An unknown or unreadable version gives ``False``: the
    call then stays on VI-JSON, the path for current vCenter releases.
    """
    release = vmomi_release_from_version(version)
    if release is None:
        return False
    return int(release.split(".", 1)[0]) < VSAN_VI_JSON_MIN_MAJOR
