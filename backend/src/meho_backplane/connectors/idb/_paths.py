# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The REST paths the ``idb`` connector calls, and the references they come from.

Neither product publishes a downloadable API file for the versions MEHO
covers, so there is no spec to check these paths against. Each path is pinned
here to its reference; the decision record
``docs/decisions/2026-10-23-identity-broker-connector.md`` has the full list
of sources.

Workspace ONE Access 3.3 (``idb-wsa-3.3``):

* ``POST /SAAS/auth/oauthtoken`` -- OAuth 2.0 client-credentials login
  (``tokenUrl`` in the ``securityDefinitions`` of
  https://github.com/omnissa-archive/idm/blob/master/apidocs/swagger.json, and
  ``Connect-WorkspaceOneAccess`` in
  https://github.com/vmware/PowerCLI-Example-Scripts/blob/master/Modules/VMware.WorkspaceOneAccess/VMware.WorkspaceOneAccess.psm1).
* ``GET /SAAS/API/1.0/REST/system/health`` -- health; ``AllOk`` is the roll-up
  ("Workspace ONE Access URL Endpoints for Monitoring" and "Displaying
  Additional Information in the Health Check API", Identity Manager 3.3 docs
  on techdocs.broadcom.com).
* ``GET /SAAS/jersey/manager/api/identityProviders`` -- identity providers
  (``Get-IdentityProvider`` in the PowerCLI example module; media type
  ``application/vnd.vmware.horizon.manager.identityprovider.summary.list+json``).
* ``GET /SAAS/jersey/manager/api/connectormanagement/directoryconfigs`` --
  directories (``Get-Directory`` in the PowerCLI example module; media type
  ``application/vnd.vmware.horizon.manager.connector.management.directory.list+json``).
* ``POST`` on this path -- start a directory sync. Not in a vendor file;
  named by community automation write-ups. The lab proof confirms it:

  /SAAS/jersey/manager/api/connectormanagement/directoryconfigs/{directory_id}/syncprofile/sync

* ``POST /SAAS/jersey/manager/api/scim/Users/.search`` and
  ``POST /SAAS/jersey/manager/api/scim/Groups/.search`` -- SCIM search
  (the ``omnissa-archive/idm`` swagger file).

VCF Identity Broker 9.x (``idb-rest-9.0``), all under the tenant
(``/acs/t/{tenant}`` and ``/usergroup/t/{tenant}``), from the operation index
of https://developer.broadcom.com/xapis/vmware-identity-broker/latest/ :

* ``POST /acs/t/{tenant}/token`` -- OAuth 2.0 client-credentials login.
* ``GET /acs/t/{tenant}/properties`` -- ``version``, ``deployment_type``,
  ``instance_id`` (9.1 and newer).
* ``GET /acs/t/{tenant}/.well-known/openid-configuration`` -- OpenID Connect
  discovery; the fingerprint uses it, without a login, to tell that the
  target is an Identity Broker.
* ``POST /usergroup/t/{tenant}/scim/v2/Users/.search`` and
  ``POST /usergroup/t/{tenant}/scim/v2/Groups/.search`` -- SCIM search
  (9.0 and newer).

The spec check (``backend/tests/test_connectors_idb_spec_reconcile.py``) reads
the ``*_PATH`` constants of this module live and pins them.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import quote

__all__ = ["fill"]

# Workspace ONE Access 3.3
WSA_TOKEN_PATH = "/SAAS/auth/oauthtoken"
WSA_HEALTH_PATH = "/SAAS/API/1.0/REST/system/health"
WSA_IDENTITY_PROVIDERS_PATH = "/SAAS/jersey/manager/api/identityProviders"
WSA_DIRECTORIES_PATH = "/SAAS/jersey/manager/api/connectormanagement/directoryconfigs"
WSA_DIRECTORY_SYNC_PATH = (
    "/SAAS/jersey/manager/api/connectormanagement/directoryconfigs/{directory_id}/syncprofile/sync"
)
WSA_USERS_SEARCH_PATH = "/SAAS/jersey/manager/api/scim/Users/.search"
WSA_GROUPS_SEARCH_PATH = "/SAAS/jersey/manager/api/scim/Groups/.search"

# VCF Identity Broker 9.x
IDB_TOKEN_PATH = "/acs/t/{tenant}/token"
IDB_PROPERTIES_PATH = "/acs/t/{tenant}/properties"
IDB_OIDC_DISCOVERY_PATH = "/acs/t/{tenant}/.well-known/openid-configuration"
IDB_USERS_SEARCH_PATH = "/usergroup/t/{tenant}/scim/v2/Users/.search"
IDB_GROUPS_SEARCH_PATH = "/usergroup/t/{tenant}/scim/v2/Groups/.search"

_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")


def fill(template: str, values: Mapping[str, object] | None = None) -> str:
    """Return *template* with every placeholder filled as one encoded path segment.

    A missing or an extra value raises :exc:`ValueError`, so a typo never
    sends a wrong URL, and a value can never add a ``/``.
    """
    given = dict(values or {})
    placeholders = set(_PLACEHOLDER_RE.findall(template))
    if placeholders != set(given):
        raise ValueError(f"path {template!r} needs {sorted(placeholders)}, got {sorted(given)}")
    return _PLACEHOLDER_RE.sub(lambda m: quote(str(given[m.group(1)]), safe=""), template)
