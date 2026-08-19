# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Hand-coded Harbor REST path templates — the reconcile-lane surface.

Every request path the harbor connector dispatches is declared here as a
module-level ``_*_PATH`` template constant, and every handler builds its
concrete path from one of these templates via :func:`fill_path` (or uses
a placeholder-free constant directly) — never from an inline f-string.
Placeholder names are byte-for-byte the pinned spec's own path-parameter
names (``docs/harbor-2.12/`` on the operator's spec shelf): the
project-detail and ``/summary`` endpoints take Harbor's name-or-id
segment ``{project_name_or_id}``, while the repository sub-tree uses
``{project_name}`` / ``{repository_name}`` / ``{reference}`` and the
robot-delete uses ``{robot_id}``. This lets the spec-reconcile lane
(:mod:`tests.test_connectors_harbor_spec_reconcile`, #2990) introspect
these live constants and assert each declared ``METHOD:/path`` is served
by the pinned spec — the #2944 introspect-live-constants pattern (never a
hardcoded mirror that can drift). Before #2990 the paths lived as inline
f-strings across :mod:`~meho_backplane.connectors.harbor.connector` and
:mod:`~meho_backplane.connectors.harbor.typed_reads`; hoisting them here
is what makes the lane's enumeration introspectable.

All templates are the folded ``/api/v2.0`` form the connector's pooled
per-target client dispatches. The pinned Swagger 2.0 document declares
its path keys relative to ``basePath: /api/v2.0``, which the lane
prepends before comparing.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import quote

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["encode_segment", "fill_path"]

# -- templates reconciled against the pinned Harbor 2.12 spec ---------------

_SYSTEMINFO_PATH = "/api/v2.0/systeminfo"
_HEALTH_PATH = "/api/v2.0/health"
_PROJECTS_PATH = "/api/v2.0/projects"
_PROJECT_PATH = "/api/v2.0/projects/{project_name_or_id}"
_PROJECT_SUMMARY_PATH = "/api/v2.0/projects/{project_name_or_id}/summary"
_PROJECT_REPOSITORIES_PATH = "/api/v2.0/projects/{project_name}/repositories"
_REPOSITORY_PATH = "/api/v2.0/projects/{project_name}/repositories/{repository_name}"
_ARTIFACTS_PATH = "/api/v2.0/projects/{project_name}/repositories/{repository_name}/artifacts"
_ARTIFACT_PATH = (
    "/api/v2.0/projects/{project_name}/repositories/{repository_name}/artifacts/{reference}"
)
_ARTIFACT_VULNERABILITIES_PATH = (
    "/api/v2.0/projects/{project_name}/repositories/{repository_name}"
    "/artifacts/{reference}/additions/vulnerabilities"
)
_ROBOTS_PATH = "/api/v2.0/robots"
_ROBOT_PATH = "/api/v2.0/robots/{robot_id}"
_QUOTAS_PATH = "/api/v2.0/quotas"


_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")


def encode_segment(value: object) -> str:
    """Percent-encode one URL path segment (OpenAPI ``style: simple``).

    Empty safe set, so a reserved char in a caller-controlled value can
    never leak path structure: a nested ``repository_name``
    (``team/nginx`` -> ``team%2Fnginx``) keeps its separator encoded and a
    ``sha256:`` digest ``reference`` has its ``:`` encoded to ``%3A``. The
    single encoder both :mod:`~meho_backplane.connectors.harbor.connector`
    and :mod:`~meho_backplane.connectors.harbor.typed_reads` share (was the
    duplicated ``_encode_segment`` / ``_seg`` before #2990); matches the
    retired generic dispatcher's ``_substitute_path`` RFC6570 expansion.
    """
    return quote(str(value), safe="")


def fill_path(template: str, values: Mapping[str, str]) -> str:
    """Substitute *values* into *template*'s ``{placeholder}`` segments.

    Fail-loud contract, both directions: every placeholder in *template*
    must have a value and every provided key must name a placeholder — a
    typo'd key or a missing segment raises :exc:`ValueError` at the call
    site instead of dispatching a malformed URL. Values are inserted
    verbatim; callers pre-encode caller-controlled segments with
    :func:`encode_segment` exactly as the pre-#2990 f-strings did.
    """
    placeholders = set(_PLACEHOLDER_RE.findall(template))
    provided = set(values)
    if placeholders != provided:
        raise ValueError(
            f"fill_path template {template!r} declares placeholders "
            f"{sorted(placeholders)} but got values for {sorted(provided)}"
        )
    return _PLACEHOLDER_RE.sub(lambda match: values[match.group(1)], template)
