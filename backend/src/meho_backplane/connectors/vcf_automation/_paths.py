# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Request paths for the VCFA provisioning ops (evoila/meho#3890).

Split out of :mod:`.typed_ops` (which keeps the read-op paths) so the
provisioning surface -- org / role / user / API token / project writes
plus the right + role reads they need -- has one module of path
constants the spec-reconcile lane
(``tests/test_connectors_vcf_automation_spec_reconcile.py``) introspects
by name. Every constant ends in ``_PATH``; the lane maps each one to its
HTTP method(s) explicitly, so a new constant fails the sweep until its
method is pinned.

Endpoint facts are pinned to the vendor SDK the VCF Automation
terraform provider builds on (``vmware/go-vcloud-director`` v3:
``govcd/tm_org.go``, ``global_role.go``, ``rights.go``,
``openapi_user.go``, ``api_token.go``) and, for the tenant plane, to the
OpenAPI 3.0.1 IaaS document a 9.1 appliance serves at
``/iaas-api/swagger/v3/api-docs/2021-07-15``.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "OAUTH_REGISTER_PATH",
    "OAUTH_TOKEN_PATH",
    "ORG_SESSION_CURRENT_PATH",
    "ORG_SESSION_PATH",
    "PROVIDER_GLOBAL_ROLES_PATH",
    "PROVIDER_GLOBAL_ROLE_PATH",
    "PROVIDER_GLOBAL_ROLE_PUBLISH_ALL_PATH",
    "PROVIDER_GLOBAL_ROLE_PUBLISH_PATH",
    "PROVIDER_GLOBAL_ROLE_RIGHTS_PATH",
    "PROVIDER_GLOBAL_ROLE_TENANTS_PATH",
    "PROVIDER_RIGHTS_PATH",
    "PROVIDER_ROLES_PATH",
    "PROVIDER_TOKENS_PATH",
    "PROVIDER_TOKEN_PATH",
    "PROVIDER_USERS_PATH",
    "TENANT_IAAS_API_VERSION",
]

#: Every right the appliance knows (``GET``, FIQL ``filter``).
PROVIDER_RIGHTS_PATH: Final[str] = "/cloudapi/1.0.0/rights"
#: Global roles -- the role surface tenants consume under
#: ``ADVANCED_RIGHTS_BUNDLE_MODE`` (``GET`` list, ``POST`` create).
PROVIDER_GLOBAL_ROLES_PATH: Final[str] = "/cloudapi/1.0.0/globalRoles"
#: One global role (``DELETE`` -- rollback of a create whose rights PUT failed).
PROVIDER_GLOBAL_ROLE_PATH: Final[str] = "/cloudapi/1.0.0/globalRoles/{id}"
#: The orgs a global role is published to (``GET``).
PROVIDER_GLOBAL_ROLE_TENANTS_PATH: Final[str] = "/cloudapi/1.0.0/globalRoles/{id}/tenants"
#: A global role's rights (``GET`` list, ``PUT`` replace).
PROVIDER_GLOBAL_ROLE_RIGHTS_PATH: Final[str] = "/cloudapi/1.0.0/globalRoles/{id}/rights"
#: Publish a global role to named tenants (``POST`` ``{"values": [{name, id}]}``).
PROVIDER_GLOBAL_ROLE_PUBLISH_PATH: Final[str] = "/cloudapi/1.0.0/globalRoles/{id}/tenants/publish"
#: Publish a global role to every tenant (``POST`` ``{}``).
PROVIDER_GLOBAL_ROLE_PUBLISH_ALL_PATH: Final[str] = (
    "/cloudapi/1.0.0/globalRoles/{id}/tenants/publishAll"
)
#: An org's roles, read with the org's tenant-context headers (``GET``).
PROVIDER_ROLES_PATH: Final[str] = "/cloudapi/1.0.0/roles"
#: Org users, read/written with the org's tenant-context headers
#: (``GET`` list, ``POST`` create).
PROVIDER_USERS_PATH: Final[str] = "/cloudapi/1.0.0/users"
#: API tokens visible to the session's user (``GET``).
PROVIDER_TOKENS_PATH: Final[str] = "/cloudapi/1.0.0/tokens"
#: One API token; ``DELETE`` revokes it (``urn:vcloud:token:<client_id>``).
PROVIDER_TOKEN_PATH: Final[str] = "/cloudapi/1.0.0/tokens/{id}"
#: Session create for a tenant-org user (HTTP Basic ``<user>@<org>``). The
#: System org uses :data:`._routing.PROVIDER_SESSION_PATH` instead.
ORG_SESSION_PATH: Final[str] = "/cloudapi/1.0.0/sessions"
#: Log out the calling session (``DELETE``) -- closes the org-user session
#: the API-token ops open.
ORG_SESSION_CURRENT_PATH: Final[str] = "/cloudapi/1.0.0/sessions/current"
#: OAuth client registration (``POST {"client_name"}``); ``{context}`` is
#: ``provider`` for the System org or ``tenant/<org>`` for a tenant org.
OAUTH_REGISTER_PATH: Final[str] = "/oauth/{context}/register"
#: OAuth token grant (form-encoded ``jwt-bearer`` → ``refresh_token``).
OAUTH_TOKEN_PATH: Final[str] = "/oauth/{context}/token"

#: The IaaS API version the tenant writes pin (``?apiVersion=``) -- the
#: ``latestApiVersion`` VCFA 9.0 and 9.1 report from ``/iaas/api/about``.
TENANT_IAAS_API_VERSION: Final[str] = "2021-07-15"
