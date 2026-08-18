# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Hand-coded Keycloak Admin REST path templates — the reconcile-lane surface.

Every request path the keycloak connector dispatches is declared here as
a module-level ``_*_PATH`` template constant, and every handler builds
its concrete path from one of these templates via :func:`fill_path` —
never from an inline f-string. Placeholder names are byte-for-byte the
pinned spec's own path-parameter names (``{realm}``, ``{client-uuid}``,
``{user-id}``, ``{role-name}`` — ``docs/keycloak-26.3/`` on the
operator's spec shelf), so the spec-reconcile lane
(:mod:`tests.test_connectors_keycloak_spec_reconcile`, #2988) can
introspect these live constants and assert each declared
``METHOD:/path`` is served by the pinned spec verbatim — the #2944
introspect-live-constants pattern (never a hardcoded mirror that can
drift). Before #2988 the paths lived as inline f-strings across four
modules; hoisting them here is what makes the lane's enumeration
introspectable.

Two constants are dispatched but deliberately **outside** the pinned
Admin REST spec's scope (the spec's only top-level path family is
``/admin/realms``); the lane pins them as evidenced exclusions rather
than reconciling them:

* :data:`_TOKEN_PATH` — the OIDC token endpoint the admin-token mint
  POSTs. Specified by OAuth 2.0 (RFC 6749 §3.2) / OIDC discovery
  (``/realms/{realm}/.well-known/openid-configuration``), not by the
  Admin REST OpenAPI document.
* :data:`_SERVERINFO_PATH` — the admin console's server-info endpoint
  (best-effort version read in
  :meth:`~meho_backplane.connectors.keycloak.connector.KeycloakConnector._server_version`).
  Stable in practice but not modeled by the published Admin REST
  OpenAPI document.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["fill_path"]

# -- templates reconciled against the pinned Admin REST spec ----------------

_ADMIN_REALMS_PATH = "/admin/realms"
_ADMIN_REALM_PATH = "/admin/realms/{realm}"
_CLIENTS_PATH = "/admin/realms/{realm}/clients"
_CLIENT_PATH = "/admin/realms/{realm}/clients/{client-uuid}"
_CLIENT_PROTOCOL_MAPPERS_PATH = (
    "/admin/realms/{realm}/clients/{client-uuid}/protocol-mappers/models"
)
_CLIENT_SCOPES_PATH = "/admin/realms/{realm}/client-scopes"
_USERS_PATH = "/admin/realms/{realm}/users"
_USER_ROLE_MAPPINGS_PATH = "/admin/realms/{realm}/users/{user-id}/role-mappings"
_USER_ROLE_MAPPINGS_REALM_PATH = "/admin/realms/{realm}/users/{user-id}/role-mappings/realm"
_USER_RESET_PASSWORD_PATH = "/admin/realms/{realm}/users/{user-id}/reset-password"
_ROLES_PATH = "/admin/realms/{realm}/roles"
_ROLE_PATH = "/admin/realms/{realm}/roles/{role-name}"
_ROLE_USERS_PATH = "/admin/realms/{realm}/roles/{role-name}/users"

# -- dispatched, but outside the Admin REST spec's scope (see docstring) ----

_TOKEN_PATH = "/realms/{realm}/protocol/openid-connect/token"
_SERVERINFO_PATH = "/admin/serverinfo"


_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")


def fill_path(template: str, values: Mapping[str, str]) -> str:
    """Substitute *values* into *template*'s ``{placeholder}`` segments.

    Fail-loud contract, both directions: every placeholder in *template*
    must have a value and every provided key must name a placeholder — a
    typo'd key or a missing segment raises :exc:`ValueError` at the call
    site instead of dispatching a malformed URL. Values are inserted
    verbatim; callers pre-encode caller-controlled segments with
    :func:`~meho_backplane.connectors.keycloak.session.quote_segment`
    exactly as the pre-#2988 f-strings did.
    """
    placeholders = set(_PLACEHOLDER_RE.findall(template))
    provided = set(values)
    if placeholders != provided:
        raise ValueError(
            f"fill_path template {template!r} declares placeholders "
            f"{sorted(placeholders)} but got values for {sorted(provided)}"
        )
    return _PLACEHOLDER_RE.sub(lambda match: values[match.group(1)], template)
