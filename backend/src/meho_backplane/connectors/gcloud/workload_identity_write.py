# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Handlers that set up keyless Google access: pool, OIDC provider, binding.

The operation metadata is in
:mod:`meho_backplane.connectors.gcloud.ops_workload_identity_write`. The
reads, the delete and the binding removal are in
:mod:`meho_backplane.connectors.gcloud.workload_identity`; this module uses
its value checks, its policy read/write and its operation wait.

* Pool create and provider create send Google's ``create`` call and wait a
  short, bounded time for the operation. When it finishes, MEHO reads the
  new object, so the result shows its real state (``ACTIVE``). When it is
  still running, the result says ``CREATE_PENDING``. When the ID is already
  in use, MEHO changes nothing and shows the object that is there
  (``already_existed: true``).
* The uploaded public keys (``jwks``) are checked before any call: only RSA
  and EC public keys, only the fields Google accepts. A key with private
  parts is refused.
* The keys update reads the provider, sends ``PATCH`` with
  ``updateMask=oidc.jwksJson`` and changes nothing when the keys are the same.
* The binding add reads the service-account policy, adds one member to one
  role (in the binding without a condition) and writes the policy back with
  the ``etag`` it read. A member that is already there is kept; nothing is
  sent.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Final, cast

import httpx

from meho_backplane.connectors.base import ConnectorResourceNotFoundError
from meho_backplane.connectors.gcloud.session import GcloudTargetLike
from meho_backplane.connectors.gcloud.workload_identity import (
    _IAM_API,
    MEMBER_PATTERN,
    POOL_ID_PATTERN,
    ROLE_PATTERN,
    _checked,
    _get_sa_policy,
    _pool_id,
    _pool_row,
    _pools_name,
    _provider_row,
    _raise_if_operation_failed,
    _refuse_hidden_conditions,
    _send_json_abs,
    _service_account,
    _set_sa_policy,
    _wait_for_operation,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.gcloud.connector import GcloudConnector

__all__ = [
    "ISSUER_URI_PATTERN",
    "JWK_FIELDS",
    "JWK_TYPES",
    "PROVIDER_ID_PATTERN",
    "WorkloadIdentityWriteOps",
]

#: A provider ID follows the same rules as a pool ID.
PROVIDER_ID_PATTERN: Final[str] = POOL_ID_PATTERN

#: Google accepts only an HTTPS issuer URL.
ISSUER_URI_PATTERN: Final[str] = r"^https://[!-~]{1,2000}$"

#: The only fields Google accepts in an uploaded key (``oidc.jwksJson``).
JWK_FIELDS: Final[tuple[str, ...]] = ("kty", "alg", "use", "kid", "n", "e", "x", "y", "crv")

#: The key types Google accepts.
JWK_TYPES: Final[tuple[str, ...]] = ("RSA", "EC")

#: Fields that only a private (or symmetric) key has.
_PRIVATE_JWK_FIELDS: Final[frozenset[str]] = frozenset(
    {"d", "p", "q", "dp", "dq", "qi", "oth", "k"}
)

_PUBLIC_PARTS: Final[dict[str, tuple[str, ...]]] = {"RSA": ("n", "e"), "EC": ("crv", "x", "y")}

#: Google requires an OIDC provider to map ``google.subject``.
_DEFAULT_ATTRIBUTE_MAPPING: Final[dict[str, str]] = {"google.subject": "assertion.sub"}

#: Members that may never be added: everyone, or an account that is deleted.
_REFUSED_MEMBERS: Final[tuple[str, ...]] = ("allUsers", "allAuthenticatedUsers")

_NOT_CHANGED: Final[str] = "MEHO refused the value before calling Google; nothing was changed."

_PENDING_NOTE: Final[str] = (
    "Google accepted the request but its operation had not finished when MEHO stopped "
    "waiting. Check the state later with {check}."
)


# ---------------------------------------------------------------------------
# Value checks
# ---------------------------------------------------------------------------


def _conn(ops: WorkloadIdentityWriteOps) -> GcloudConnector:
    """The connector these methods run on. ``WorkloadIdentityWriteOps`` is only ever its base."""
    return cast("GcloudConnector", ops)


def _provider_id(params: dict[str, Any]) -> str:
    return _checked(
        params, "provider_id", PROVIDER_ID_PATTERN, "a provider ID (4-32 characters a-z, 0-9, '-')"
    )


def _jwks_json(params: dict[str, Any]) -> tuple[str, int]:
    """Check the uploaded public keys and return ``(jwksJson string, key count)``.

    Refuses a key with private parts, a key type Google does not accept, a
    field Google does not accept and a key without its public parts. The
    key values are never put into an error message.
    """
    jwks = params.get("jwks")
    keys = jwks.get("keys") if isinstance(jwks, dict) else None
    if (
        not isinstance(jwks, dict)
        or set(jwks) != {"keys"}
        or not isinstance(keys, list)
        or not keys
    ):
        raise ValueError(f"'jwks' must be a key set: {{\"keys\": [ ... ]}}. {_NOT_CHANGED}")
    for index, key in enumerate(keys):
        if not isinstance(key, dict):
            raise ValueError(f"'jwks' key {index} is not an object. {_NOT_CHANGED}")
        private = sorted(_PRIVATE_JWK_FIELDS & set(key))
        if private:
            raise ValueError(
                f"'jwks' key {index} has private key parts ({', '.join(private)}). Upload "
                f"only public keys. {_NOT_CHANGED}"
            )
        kty = key.get("kty")
        if kty not in JWK_TYPES:
            raise ValueError(
                f"'jwks' key {index} has key type {kty!r}; Google accepts only RSA and EC. "
                f"{_NOT_CHANGED}"
            )
        unknown = sorted(set(key) - set(JWK_FIELDS))
        if unknown:
            raise ValueError(
                f"'jwks' key {index} has fields Google does not accept ({', '.join(unknown)}). "
                f"Allowed: {', '.join(JWK_FIELDS)}. {_NOT_CHANGED}"
            )
        missing = [part for part in _PUBLIC_PARTS[kty] if not isinstance(key.get(part), str)]
        if missing or not all(isinstance(value, str) for value in key.values()):
            raise ValueError(
                f"'jwks' key {index} ({kty}) needs the text fields "
                f"{', '.join(_PUBLIC_PARTS[kty])}. {_NOT_CHANGED}"
            )
    return json.dumps({"keys": keys}, separators=(",", ":")), len(keys)


def _attribute_mapping(params: dict[str, Any]) -> dict[str, str]:
    mapping = params.get("attribute_mapping")
    if mapping is None:
        return dict(_DEFAULT_ATTRIBUTE_MAPPING)
    if (
        not isinstance(mapping, dict)
        or not all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items())
        or "google.subject" not in mapping
    ):
        raise ValueError(
            "'attribute_mapping' must map names to text and include 'google.subject'. "
            f"{_NOT_CHANGED}"
        )
    return dict(mapping)


def _key_count(provider: dict[str, Any]) -> int:
    """How many keys are uploaded on a provider; 0 when Google reads them from the issuer."""
    raw = (provider.get("oidc") or {}).get("jwksJson")
    if not isinstance(raw, str) or not raw:
        return 0
    try:
        keys = json.loads(raw).get("keys")
    except (ValueError, AttributeError):
        return 0
    return len(keys) if isinstance(keys, list) else 0


def _uploaded_keys(provider: dict[str, Any]) -> Any:
    raw = (provider.get("oidc") or {}).get("jwksJson")
    try:
        return json.loads(raw).get("keys") if isinstance(raw, str) and raw else None
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Create with wait
# ---------------------------------------------------------------------------


async def _get_or_not_found(
    connector: GcloudConnector,
    target: GcloudTargetLike,
    name: str,
    what: str,
    *,
    operator: Operator,
) -> dict[str, Any]:
    try:
        return await connector._get_json_abs(target, f"{_IAM_API}/{name}", operator=operator)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise ConnectorResourceNotFoundError(
                [name], f"{what} does not exist in this project."
            ) from exc
        raise


async def _create(
    connector: GcloudConnector,
    target: GcloudTargetLike,
    *,
    operator: Operator,
    parent: str,
    id_query: str,
    name: str,
    body: dict[str, Any],
    what: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, bool]:
    """Create one object and wait for Google's operation.

    Returns ``(object, operation, already_existed)``. ``object`` is the
    object read after the operation finished, or the object that already
    used the ID; it is ``None`` when the operation was still running.
    """
    object_id = name.rsplit("/", 1)[-1]
    url = f"{_IAM_API}/{parent}?{id_query}={object_id}"
    try:
        operation = await connector._post_json_abs(target, url, operator=operator, json_body=body)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 409:
            raise
        existing = await connector._get_json_abs(target, f"{_IAM_API}/{name}", operator=operator)
        if existing.get("state") == "DELETED":
            raise ValueError(
                f"The ID of {what} is used by a deleted object. Google keeps it for 30 days and "
                "it can be restored (undelete) in that time; until then the ID cannot be used "
                "again. Nothing was changed."
            ) from exc
        return existing, None, True
    operation = await _wait_for_operation(connector, target, operation, operator=operator)
    _raise_if_operation_failed(operation, f"create {what}")
    if operation.get("done") is not True:
        return None, operation, False
    created = await connector._get_json_abs(target, f"{_IAM_API}/{name}", operator=operator)
    return created, operation, False


def _operation_fields(operation: dict[str, Any] | None) -> dict[str, Any]:
    if operation is None:
        return {"operation_name": None, "operation_done": None}
    return {
        "operation_name": operation.get("name"),
        "operation_done": operation.get("done") is True,
    }


# ---------------------------------------------------------------------------
# Operation bodies
# ---------------------------------------------------------------------------


async def pool_create(
    connector: GcloudConnector,
    operator: Operator,
    target: GcloudTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``gcloud.iam.workload_identity_pools.create``."""
    pool_id = _pool_id(params)
    name = f"{_pools_name(target)}/{pool_id}"
    body = {
        k: params[p]
        for p, k in (("display_name", "displayName"), ("description", "description"))
        if params.get(p)
    }
    pool, operation, existed = await _create(
        connector,
        target,
        operator=operator,
        parent=_pools_name(target),
        id_query="workloadIdentityPoolId",
        name=name,
        body=body,
        what=f"workload identity pool {pool_id!r}",
    )
    row = _pool_row(pool) if pool is not None else {}
    if pool is None:
        note = _PENDING_NOTE.format(check="gcloud.iam.workload_identity_pools.list")
    elif existed:
        note = "A pool with this ID already exists. MEHO changed nothing."
    else:
        note = f"Google created the pool; its state is {row.get('state')}."
    return {
        "pool_id": pool_id,
        "name": name,
        "state": row.get("state") or "CREATE_PENDING",
        "already_existed": existed,
        "display_name": row.get("display_name"),
        "description": row.get("description"),
        **_operation_fields(operation),
        "note": note,
    }


async def provider_create(
    connector: GcloudConnector,
    operator: Operator,
    target: GcloudTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``gcloud.iam.workload_identity_pools.providers.create``: an OIDC provider."""
    pool_id = _pool_id(params)
    provider_id = _provider_id(params)
    issuer = _checked(params, "issuer_uri", ISSUER_URI_PATTERN, "an https:// issuer URL")
    oidc: dict[str, Any] = {"issuerUri": issuer}
    key_count = 0
    if params.get("jwks") is not None:
        oidc["jwksJson"], key_count = _jwks_json(params)
    if params.get("allowed_audiences"):
        oidc["allowedAudiences"] = list(params["allowed_audiences"])
    body: dict[str, Any] = {"oidc": oidc, "attributeMapping": _attribute_mapping(params)}
    for param, key in (
        ("attribute_condition", "attributeCondition"),
        ("display_name", "displayName"),
        ("description", "description"),
    ):
        if params.get(param):
            body[key] = params[param]

    pool_name = f"{_pools_name(target)}/{pool_id}"
    name = f"{pool_name}/providers/{provider_id}"
    provider, operation, existed = await _create(
        connector,
        target,
        operator=operator,
        parent=f"{pool_name}/providers",
        id_query="workloadIdentityPoolProviderId",
        name=name,
        body=body,
        what=f"provider {provider_id!r} in pool {pool_id!r}",
    )
    result: dict[str, Any] = {
        "pool_id": pool_id,
        "provider_id": provider_id,
        "name": name,
        "already_existed": existed,
        **_operation_fields(operation),
    }
    if provider is None:
        return {
            **result,
            "state": "CREATE_PENDING",
            "issuer_uri": issuer,
            "key_count": key_count,
            "attribute_condition": body.get("attributeCondition"),
            "note": _PENDING_NOTE.format(check="gcloud.iam.workload_identity_pools.providers.list"),
        }
    row = _provider_row(provider)
    note = f"Google created the provider; its state is {row['state']}."
    if existed:
        note = (
            "A provider with this ID already exists. MEHO changed nothing; the fields show "
            "the provider that is there. To change its keys, use "
            "gcloud.iam.workload_identity_pools.providers.update_keys."
        )
    return {
        **result,
        "state": row["state"],
        "issuer_uri": row["issuer_uri"],
        "key_count": _key_count(provider),
        "attribute_condition": row["attribute_condition"],
        "note": note,
    }


async def provider_update_keys(
    connector: GcloudConnector,
    operator: Operator,
    target: GcloudTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``gcloud.iam.workload_identity_pools.providers.update_keys``: replace the uploaded keys."""
    pool_id = _pool_id(params)
    provider_id = _provider_id(params)
    jwks_json, key_count = _jwks_json(params)
    name = f"{_pools_name(target)}/{pool_id}/providers/{provider_id}"
    provider = await _get_or_not_found(
        connector,
        target,
        name,
        f"Provider {provider_id!r} in pool {pool_id!r}",
        operator=operator,
    )
    if "oidc" not in provider:
        raise ValueError(
            f"Provider {provider_id!r} is not an OIDC provider, so it has no uploaded keys. "
            "Nothing was changed."
        )
    if provider.get("state") == "DELETED":
        raise ValueError(
            f"Provider {provider_id!r} is marked deleted. Restore it first. Nothing was changed."
        )
    result: dict[str, Any] = {
        "pool_id": pool_id,
        "provider_id": provider_id,
        "name": name,
        "issuer_uri": (provider.get("oidc") or {}).get("issuerUri"),
        "keys_before": _key_count(provider),
        "key_count": key_count,
        "changed": False,
        "operation_name": None,
        "operation_done": None,
        "note": "The provider already has exactly these keys. MEHO changed nothing.",
    }
    if _uploaded_keys(provider) == json.loads(jwks_json)["keys"]:
        return result
    operation = await _send_json_abs(
        connector,
        target,
        "PATCH",
        f"{_IAM_API}/{name}?updateMask=oidc.jwksJson",
        operator=operator,
        json_body={"oidc": {"jwksJson": jwks_json}},
    )
    operation = await _wait_for_operation(connector, target, operation, operator=operator)
    _raise_if_operation_failed(operation, f"update the keys of provider {provider_id!r}")
    result.update(changed=True, **_operation_fields(operation))
    result["note"] = (
        "Google now trusts tokens signed by the uploaded keys."
        if result["operation_done"]
        else _PENDING_NOTE.format(check="gcloud.iam.workload_identity_pools.providers.list")
    )
    return result


async def sa_binding_add(
    connector: GcloudConnector,
    operator: Operator,
    target: GcloudTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``gcloud.iam.service_accounts.binding.add``: read, add one member, write with etag."""
    account = _service_account(params)
    role = _checked(params, "role", ROLE_PATTERN, "a role such as roles/iam.workloadIdentityUser")
    member = _checked(params, "member", MEMBER_PATTERN, "an IAM member as written in the policy")
    if member in _REFUSED_MEMBERS or member.startswith("deleted:"):
        raise ValueError(
            f"MEHO does not add the member {member!r}: it means everyone, or an account that "
            "is deleted. Nothing was changed."
        )

    policy = await _get_sa_policy(connector, target, account, operator=operator)
    _refuse_hidden_conditions(policy)
    bindings = list(policy.get("bindings") or [])
    plain = [b for b in bindings if b.get("role") == role and not b.get("condition")]
    present = any(member in (b.get("members") or []) for b in plain)
    result: dict[str, Any] = {
        "service_account_email": account,
        "role": role,
        "member": member,
        "added": False,
        "members_with_role": sum(len(b.get("members") or []) for b in plain),
        "etag": policy.get("etag"),
    }
    if present:
        return result

    new_bindings: list[dict[str, Any]] = []
    done = False
    for binding in bindings:
        if not done and binding in plain:
            binding = {**binding, "members": [*(binding.get("members") or []), member]}
            done = True
        new_bindings.append(binding)
    if not done:
        new_bindings.append({"role": role, "members": [member]})
    written = await _set_sa_policy(
        connector, target, account, policy, new_bindings, operator=operator
    )
    result.update(
        added=True, members_with_role=result["members_with_role"] + 1, etag=written.get("etag")
    )
    return result


class WorkloadIdentityWriteOps:
    """The methods of ``GcloudConnector`` that set up keyless Google access."""

    async def gcloud_wif_pool_create(
        self, operator: Operator, target: GcloudTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``gcloud.iam.workload_identity_pools.create``: make one pool."""
        return await pool_create(_conn(self), operator, target, params)

    async def gcloud_wif_provider_create(
        self, operator: Operator, target: GcloudTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``gcloud.iam.workload_identity_pools.providers.create``: make one OIDC provider."""
        return await provider_create(_conn(self), operator, target, params)

    async def gcloud_wif_provider_update_keys(
        self, operator: Operator, target: GcloudTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``gcloud.iam.workload_identity_pools.providers.update_keys``: replace the keys."""
        return await provider_update_keys(_conn(self), operator, target, params)

    async def gcloud_sa_binding_add(
        self, operator: Operator, target: GcloudTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``gcloud.iam.service_accounts.binding.add``: add one member to one role."""
        return await sa_binding_add(_conn(self), operator, target, params)
