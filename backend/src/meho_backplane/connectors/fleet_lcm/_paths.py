# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Hand-coded Fleet LCM Service ``/v1/*`` path templates — the reconcile-lane surface.

Every request path the modern ``fleet-lcm`` typed read core dispatches is
declared here as a module-level ``_*_PATH`` template constant, and every
handler in :mod:`~meho_backplane.connectors.fleet_lcm.typed_reads` builds
its concrete path from one of these templates via :func:`fill_path` (or
uses a placeholder-free constant directly) — never from an inline
f-string. Placeholder names are byte-for-byte the pinned spec's own
path-parameter names (``docs/fleet-lcm-9.0/fleet-lcm-openapi.yaml`` on the
operator's spec shelf): the by-id reads take ``{sddcLcmId}`` /
``{componentId}`` / ``{taskId}`` / ``{planId}``.

This lets the spec-reconcile lane
(:mod:`tests.test_connectors_fleet_lcm_spec_reconcile`) introspect these
live constants and assert each declared ``GET:/path`` is served by the
pinned Apache-2.0 spec — the #2944 introspect-live-constants pattern
(never a hardcoded mirror that can drift), the same shape
:mod:`~meho_backplane.connectors.harbor._paths` and
:mod:`~meho_backplane.connectors.keycloak._paths` established.

All templates are the raw ``/v1/*`` form. The pinned spec's server url
``https://vcf.broadcom.com/fleet-lcm`` is **absolute**, so ``parse_openapi``
does not fold the ``/fleet-lcm`` base onto path keys (only *relative*
server bases are folded, #1796) — the served op_ids and these constants
are both the raw ``/v1/*`` path, and the ``/fleet-lcm`` base is an operator
target-configuration concern (the connector's per-target client carries it
in ``host``).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import quote

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["encode_segment", "fill_path"]

# -- templates reconciled against the pinned Fleet LCM 9.0 spec -------------
# System / service surface
_HEALTH_PATH = "/v1/health"
_SYSTEM_PATH = "/v1/system"
_CONFIG_PATH = "/v1/config"
# SDDC LCM inventory
_SDDC_LCMS_PATH = "/v1/sddc-lcms"
_SDDC_LCM_PATH = "/v1/sddc-lcms/{sddcLcmId}"
# Managed components
_COMPONENTS_PATH = "/v1/components"
_COMPONENT_PATH = "/v1/components/{componentId}"
_COMPONENT_STATUS_PATH = "/v1/components/{componentId}/status"
# Async tasks
_TASKS_PATH = "/v1/tasks"
_TASK_PATH = "/v1/tasks/{taskId}"
# Lifecycle — upgrade plans + release versions
_UPGRADE_PLANS_PATH = "/v1/upgrade-plans"
_UPGRADE_PLAN_PATH = "/v1/upgrade-plans/{planId}"
_RELEASE_VERSIONS_PATH = "/v1/release-versions"


_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")


def encode_segment(value: object) -> str:
    """Percent-encode one URL path segment (OpenAPI ``style: simple``).

    Empty safe set, so a reserved char in a caller-controlled value can
    never leak path structure. The Fleet LCM by-id segments
    (``sddcLcmId`` / ``componentId`` / ``taskId`` / ``planId``) are
    server-issued UUIDs in practice, but encoding them is the safe default
    the harbor / keycloak path helpers established — a defensive encode
    costs nothing on an opaque id and forecloses path injection if a caller
    ever passes a non-UUID value.
    """
    return quote(str(value), safe="")


def fill_path(template: str, values: Mapping[str, str]) -> str:
    """Substitute *values* into *template*'s ``{placeholder}`` segments.

    Fail-loud contract, both directions: every placeholder in *template*
    must have a value and every provided key must name a placeholder — a
    typo'd key or a missing segment raises :exc:`ValueError` at the call
    site instead of dispatching a malformed URL. Values are inserted
    verbatim; callers pre-encode caller-controlled segments with
    :func:`encode_segment`. Verbatim mirror of
    :func:`meho_backplane.connectors.harbor._paths.fill_path` (per-connector
    copy is the established convention — harbor and keycloak each ship
    their own, with no shared module and no cross-connector import).
    """
    placeholders = set(_PLACEHOLDER_RE.findall(template))
    provided = set(values)
    if placeholders != provided:
        raise ValueError(
            f"fill_path template {template!r} declares placeholders "
            f"{sorted(placeholders)} but got values for {sorted(provided)}"
        )
    return _PLACEHOLDER_RE.sub(lambda match: values[match.group(1)], template)
