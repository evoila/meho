# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Safety floor for the Hetzner Robot vSwitch writes (#3973).

The generic ingest rule gives a POST ``caution`` and a DELETE ``dangerous``,
both without an approval. A human login would run either at once. Every
vSwitch write changes a private network that several servers share, so this
floor makes each one wait for a human approval:

* ``POST /vswitch/{vswitch-id}/server`` (add servers) -> ``dangerous``.
* ``DELETE /vswitch/{vswitch-id}/server`` (remove servers) -> ``dangerous``.
* ``POST /vswitch/{vswitch-id}`` (rename, or change the VLAN of every member)
  -> ``dangerous``.
* ``DELETE /vswitch/{vswitch-id}`` (cancel the whole vSwitch) ->
  ``destructive``, the strongest level.

All four also get ``requires_approval=True``. What that means per login:

* A human waits for approval. The requester can never approve their own
  ``dangerous`` or ``destructive`` request, not even with
  ``APPROVAL_ALLOW_SELF_APPROVAL``.
* An agent is refused by default. An explicit agent permission lifts a
  ``dangerous`` op only to "waits for approval"; a ``destructive`` op is
  always refused for agents.
* A service login waits for approval. A standing grant cannot skip the wait:
  the default grant patterns refuse ``DELETE:*`` and ``POST:/vswitch/*``.

The floor only raises a level, never lowers it, and it applies to every
version of the ``hetzner-rest`` connector, because these routes mean the same
thing in every Robot release. It survives a re-ingest: the ingest merge in
``operations/ingest/_upsert.py`` keeps the stricter level and the approval
flag.

Registered as an import side effect (see the package ``__init__``), keyed by
the dispatch-canonical ``(product="hetzner", impl_id="hetzner-rest")``.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.operations.ingest.safety_floors import register_ingest_safety_floor
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto, SafetyLevel

#: The minimum safety level per vSwitch write, keyed by ``(METHOD, path)``.
VSWITCH_WRITE_FLOORS: Final[dict[tuple[str, str], SafetyLevel]] = {
    ("POST", "/vswitch/{vswitch-id}/server"): "dangerous",
    ("DELETE", "/vswitch/{vswitch-id}/server"): "dangerous",
    ("POST", "/vswitch/{vswitch-id}"): "dangerous",
    ("DELETE", "/vswitch/{vswitch-id}"): "destructive",
}

_RANK: Final[dict[str, int]] = {"safe": 0, "caution": 1, "dangerous": 2, "destructive": 3}


def hetzner_robot_safety_floor(
    version: str, proto: EndpointDescriptorProto
) -> EndpointDescriptorProto:
    """Raise a vSwitch write to its floor level and require approval."""
    del version  # the routes mean the same thing in every Robot release
    floor = VSWITCH_WRITE_FLOORS.get((proto.method.upper(), proto.path.split("?", 1)[0]))
    if floor is None:
        return proto
    level = proto.safety_level if _RANK[proto.safety_level] > _RANK[floor] else floor
    return proto.model_copy(update={"safety_level": level, "requires_approval": True})


def register_safety_floor() -> None:
    """Register this floor in the process-wide registry. Safe to call twice.

    Called once at import time below. A test calls it again so its check
    does not depend on import order (another test may clear the registry).
    """
    register_ingest_safety_floor(
        product="hetzner",
        impl_id="hetzner-rest",
        floor=hetzner_robot_safety_floor,
    )


register_safety_floor()
