# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared standalone-ESXi vs vCenter host-target classification (#3332).

The host-domain write composites (``vmware.composite.host.*`` in
:mod:`~meho_backplane.connectors.vmware_rest.composites._host`) and the
``vmware.host.storage_devices`` typed read
(:mod:`~meho_backplane.connectors.vmware_rest.typed_ops_host_storage_devices`)
both resolve a host, and both must take the **same** branch:

* a managing-**vCenter** target resolves the host through the vCenter
  Automation REST listing ``GET:/vcenter/host`` (a display-name /
  moref lookup) -- the pre-#3332 behaviour, unchanged; while
* a standalone **ESXi** target -- a host no vCenter manages yet, e.g.
  a freshly-provisioned nested host during a management-domain
  bring-up before the SDDC vCenter exists -- serves no
  ``GET:/vcenter/host`` at all. It has exactly one host, the well-known
  singleton ``ha-host`` MoRef, reached through the VI-JSON seam. On such
  a target the ``host`` parameter is ignored: *the host is the target*.

The distinguisher is the target's cached **probe fingerprint**: the
``vmware-rest`` connector registers under ``product="vmware"`` (that is
the resolver key), but its :meth:`fingerprint` stamps
``product_from_line_id`` -> ``"vcenter"`` / ``"esxi"`` into
``Target.fingerprint``. So the fingerprint's ``product`` -- not the
``Target.product`` column -- tells the two apart.

Keeping the classifier here (dependency-light: no ``operations`` /
connector imports) lets the composite dispatch path, its park-time
``proposed_effect`` preview builder, and the typed read all agree by
construction -- the #3312 preview/call parity rule (a preview that
passes on an ESXi target must not be denied at call, because both run
this one function).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

__all__ = [
    "HOST_FLAVOR_ESXI",
    "HOST_FLAVOR_VCENTER",
    "STANDALONE_ESXI_HOST_MOID",
    "classify_host_target",
]

#: The well-known singleton ``HostSystem`` MoRef on a standalone ESXi
#: host (the host a vCenter has not inventoried). Every ESXi host serves
#: exactly one, addressable through the VI-JSON seam without any
#: ``GET:/vcenter/host`` listing.
STANDALONE_ESXI_HOST_MOID: Final = "ha-host"

#: Classification results (also the ``fingerprint.product`` slugs
#: :func:`~meho_backplane.connectors.vmware_rest.connector.product_from_line_id`
#: stamps).
HOST_FLAVOR_VCENTER: Final = "vcenter"
HOST_FLAVOR_ESXI: Final = "esxi"


def _fingerprint_field(fingerprint: Any, name: str) -> Any:
    """Read *name* off a fingerprint that may be a ``Mapping`` or an object.

    A probed target hands the ORM a JSON ``Mapping`` (the probe route
    persists ``FingerprintResult.model_dump(mode="json")``); a duck-typed
    test target may instead expose attributes. Read both shapes -- mirrors
    :func:`~meho_backplane.connectors.resolver.resolve_target_version`.
    """
    if isinstance(fingerprint, Mapping):
        return fingerprint.get(name)
    return getattr(fingerprint, name, None)


def classify_host_target(target: Any) -> tuple[str | None, dict[str, Any] | None]:
    """Classify *target* as a vCenter or a standalone ESXi for host resolution.

    Returns one of:

    * ``("esxi", None)`` -- the probe fingerprint names ``product="esxi"``:
      resolve the host as the well-known :data:`STANDALONE_ESXI_HOST_MOID`
      via VI-JSON (the ``host`` parameter is ignored -- the host is the
      target).
    * ``("vcenter", None)`` -- a vCenter fingerprint, an **absent**
      fingerprint (never probed), or an **unreachable** one (a failed /
      transient probe): resolve through ``GET:/vcenter/host``. This is the
      pre-#3332 default, so a managing-vCenter target -- probed or not --
      keeps resolving exactly as before (no regression), and the existing
      host-composite callers that pass no fingerprint at all stay on the
      vCenter path.
    * ``(None, refusal)`` -- a **reachable** fingerprint that names some
      *other* product (an unrecognised ``product_line_id``): fail closed
      with a typed ``unsupported_host_target`` envelope rather than guess
      which resolution path applies. The envelope carries ``status`` /
      ``target_product`` / ``guidance``; a handler merges it into its own
      refusal shape.

    ``target`` may be ``None`` (a unit-test handler call with no resolved
    target) -- that reads as an absent fingerprint and classifies as
    ``vcenter``.
    """
    fingerprint = getattr(target, "fingerprint", None)
    if fingerprint is None:
        return HOST_FLAVOR_VCENTER, None
    product = _fingerprint_field(fingerprint, "product")
    if product == HOST_FLAVOR_ESXI:
        return HOST_FLAVOR_ESXI, None
    if product in (None, HOST_FLAVOR_VCENTER) or not bool(
        _fingerprint_field(fingerprint, "reachable")
    ):
        return HOST_FLAVOR_VCENTER, None
    return None, {
        "status": "unsupported_host_target",
        "target_product": product,
        "guidance": (
            "host-domain operations resolve a host only on a vCenter target "
            "(via GET:/vcenter/host) or a standalone ESXi target (the "
            "well-known ha-host via VI-JSON); this target's probe fingerprint "
            f"names product={product!r}, which is neither"
        ),
    }
