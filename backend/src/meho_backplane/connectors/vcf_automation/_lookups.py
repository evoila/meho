# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Lookup + secret helpers shared by the VCFA provisioning ops (evoila/meho#3890).

Used by the handlers (:mod:`._provisioning`, :mod:`._role_user`,
:mod:`._api_token`) and the park-time preview builders
(:mod:`._provisioning_preview`).

Name lookups use the cloudapi FIQL ``filter`` (``name==<value>``). FIQL
cannot carry its reserved characters (``,;()*=!<>`` and quotes) in a
value, so a name containing any of them is matched by paging the full
list instead -- the same fallback
``go-vcloud-director``'s ``getRightByName`` uses. Matches compare
case-insensitively (VCFA object names are case-insensitively unique).

The user password for a create / token op is read from a **per-op**
Vault secret ref under the operator's identity -- the Keycloak
``password_secret_ref`` seam (#1406) -- so it is never an op param.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

from meho_backplane.connectors._shared.vault_creds import (
    load_vault_secret_data,
    strip_credential_value,
)
from meho_backplane.connectors.vcf_automation._paths import (
    PROVIDER_GLOBAL_ROLES_PATH,
    PROVIDER_USERS_PATH,
)
from meho_backplane.connectors.vcf_automation.typed_ops import (
    PROVIDER_ORGS_PATH,
    TENANT_PROJECTS_PATH,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vcf_automation.connector import VcfAutomationConnector
    from meho_backplane.connectors.vcf_automation.session import VcfAutomationTargetLike

__all__ = [
    "DEFAULT_PASSWORD_KEY",
    "DEFAULT_PASSWORD_MOUNT",
    "entity_ref",
    "find_by_field",
    "find_global_role",
    "find_org",
    "find_org_user",
    "find_project",
    "fiql_safe",
    "list_all",
    "password_missing_guidance",
    "password_ref",
    "quote_segment",
    "read_password",
    "tenant_context_headers",
]

#: Page size for full-list walks (the cloudapi maximum).
_PAGE_SIZE: Final = 128
#: Upper bound on pages walked by :func:`list_all` -- 128 x 50 = 6400 rows,
#: far above any right / role / org count; a runaway ``pageCount`` stops here.
_MAX_PAGES: Final = 50
#: Tenant-context headers that scope a provider session to one org.
_TENANT_CONTEXT_HEADER: Final = "X-VMWARE-VCLOUD-TENANT-CONTEXT"
_AUTH_CONTEXT_HEADER: Final = "X-VMWARE-VCLOUD-AUTH-CONTEXT"
#: KV-v2 mount / field defaults for the password secret ref.
DEFAULT_PASSWORD_MOUNT: Final = "secret"
DEFAULT_PASSWORD_KEY: Final = "password"


def entity_ref(row: Mapping[str, Any]) -> dict[str, Any]:
    """The cloudapi ``{name, id}`` entity reference for a row."""
    return {"name": row.get("name"), "id": row.get("id")}


def quote_segment(value: str) -> str:
    """Percent-encode one path segment (empty safe set, OpenAPI ``style: simple``)."""
    return quote(str(value), safe="")


def _uuid_of(urn: str) -> str:
    """``urn:vcloud:org:<uuid>`` → ``<uuid>`` (a bare uuid passes through)."""
    return urn.rsplit(":", 1)[-1]


#: Characters FIQL reserves (``,`` / ``;`` combine, ``(`` / ``)`` group, ``*``
#: wildcards, ``=`` / ``!`` / ``<`` / ``>`` compare) or that would need quoting.
#: A lookup value holding any of them is matched by a full-list scan instead
#: of a ``filter`` -- e.g. a role named "Org Admin (API token)".
_FIQL_RESERVED: Final = frozenset(",;()*=!<>'\"")


def fiql_safe(value: str) -> bool:
    """True when *value* can ride a FIQL ``filter`` literally (see module docstring)."""
    return not _FIQL_RESERVED.intersection(value)


def _values(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("values")
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


async def list_all(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    path: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Walk every page of a cloudapi collection and return the merged ``values``."""
    rows: list[dict[str, Any]] = []
    for page in range(1, _MAX_PAGES + 1):
        payload = await connector._request_json(
            target,
            "GET",
            path,
            operator=operator,
            params={**(params or {}), "page": page, "pageSize": _PAGE_SIZE},
            extra_headers=headers,
        )
        values = _values(payload)
        rows.extend(values)
        page_count = payload.get("pageCount")
        if isinstance(page_count, int):
            if page >= page_count:
                break
        elif len(values) < _PAGE_SIZE:
            break
    return rows


async def find_by_field(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    path: str,
    value: str,
    *,
    field: str = "name",
    headers: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Return the one row of *path* whose *field* equals *value* (case-insensitive)."""
    params = {"filter": f"{field}=={value}"} if fiql_safe(value) else None
    wanted = value.casefold()
    for row in await list_all(connector, target, operator, path, params=params, headers=headers):
        candidate = row.get(field)
        if isinstance(candidate, str) and candidate.casefold() == wanted:
            return row
    return None


async def find_org(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    name: str,
) -> dict[str, Any] | None:
    """The org named *name*, or ``None``."""
    return await find_by_field(connector, target, operator, PROVIDER_ORGS_PATH, name)


async def find_global_role(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    name: str,
) -> dict[str, Any] | None:
    """The global role named *name*, or ``None``."""
    return await find_by_field(connector, target, operator, PROVIDER_GLOBAL_ROLES_PATH, name)


def tenant_context_headers(org: Mapping[str, Any]) -> dict[str, str]:
    """The tenant-context header pair that scopes a provider request to *org*.

    Same pair ``go-vcloud-director``'s ``getTenantContextHeader`` sends: the
    org uuid (not the urn) plus the org name.
    """
    return {
        _TENANT_CONTEXT_HEADER: _uuid_of(str(org.get("id") or "")),
        _AUTH_CONTEXT_HEADER: str(org.get("name") or ""),
    }


async def find_org_user(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    org: Mapping[str, Any],
    username: str,
) -> dict[str, Any] | None:
    """The user *username* in *org*, or ``None``."""
    return await find_by_field(
        connector,
        target,
        operator,
        PROVIDER_USERS_PATH,
        username,
        field="username",
        headers=tenant_context_headers(org),
    )


def _odata_literal(value: str) -> str:
    """An OData string literal (single quotes doubled)."""
    return "'" + value.replace("'", "''") + "'"


async def find_project(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    name: str,
) -> dict[str, Any] | None:
    """The tenant project named *name*, or ``None`` (tenant plane)."""
    payload = await connector._request_json(
        target,
        "GET",
        TENANT_PROJECTS_PATH,
        operator=operator,
        params={"$filter": f"name eq {_odata_literal(name)}"},
    )
    content = payload.get("content")
    wanted = name.casefold()
    for row in content if isinstance(content, list) else []:
        if (
            isinstance(row, dict)
            and isinstance(row.get("name"), str)
            and row["name"].casefold() == wanted
        ):
            return row
    return None


# ---------------------------------------------------------------------------
# Password secret (per-op ref, operator-context read)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _SecretRefTarget:
    """Adapter pointing the credential-backend seam at a per-op secret ref.

    :func:`~meho_backplane.connectors._shared.vault_creds.load_vault_secret_data`
    reads ``secret_ref`` off a target-like object; the user password lives
    at the op's ``password_secret_ref``, not the target's own secret, so
    this carries the target's ``name`` / ``host`` (log attribution) with
    the op's ref. Same adapter the Keycloak user writes use (#1406).
    """

    name: str
    host: str
    secret_ref: str | None


def password_ref(params: Mapping[str, Any]) -> tuple[str, str, str]:
    """``(secret_ref, mount, key)`` for the op's password secret ref."""
    return (
        str(params["password_secret_ref"]).strip(),
        str(params.get("password_secret_mount") or DEFAULT_PASSWORD_MOUNT).strip(),
        str(params.get("password_secret_key") or DEFAULT_PASSWORD_KEY).strip(),
    )


async def read_password(
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: Mapping[str, Any],
) -> str | None:
    """Read the user password from ``password_secret_ref`` under the operator's identity.

    Returns ``None`` when the secret carries no usable string under the
    key (the caller answers ``invalid_request``). Vault access failures
    propagate. The value never enters a log event, result or error.
    """
    secret_ref, mount, key = password_ref(params)
    data = await load_vault_secret_data(
        _SecretRefTarget(name=target.name, host=target.host, secret_ref=secret_ref),
        operator,
        mount=mount,
    )
    value = data.get(key) if isinstance(data, dict) else None
    stripped = strip_credential_value(value) if isinstance(value, str) else None
    return stripped or None


def password_missing_guidance(params: Mapping[str, Any]) -> str:
    secret_ref, mount, key = password_ref(params)
    return (
        f"the password secret at password_secret_ref={secret_ref!r} (mount={mount!r}) "
        f"carries no usable string under key {key!r}; store the password there "
        "(e.g. with vault.kv.put) or set password_secret_key"
    )
