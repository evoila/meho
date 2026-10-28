# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Target shape and secret shape of the Azure connector.

Target shape
============

One target is one Azure subscription.

* ``host`` is the Azure Resource Manager host of the cloud:
  ``management.azure.com`` for the public cloud.
* ``extras.subscription_id`` (required) is the subscription ID, a GUID.
* ``extras.tenant_id`` (optional) is the tenant (directory) ID, a GUID. When it
  is set, it must equal the ``tenant_id`` in the secret.
* ``extras.cloud`` (optional) names the Azure cloud: ``AzureCloud`` (the
  default), ``AzureUSGovernment`` or ``AzureChinaCloud``. Only ``AzureCloud``
  is proven on a live tenant.

MEHO sends the access token only to the Resource Manager host of the target's
cloud, over https on port 443, with the certificate checked. A target that
points anywhere else fails before any token is made.

Secret shape
============

The target's ``secret_ref`` names a secret with three keys: ``tenant_id``,
``client_id`` (the application ID of the login) and ``client_secret``. Other
keys are ignored. Several targets may share one secret.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Protocol, runtime_checkable

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.credential_backend import CredentialsReadError
from meho_backplane.connectors._shared.microsoft_login import (
    LOGIN_HOSTS,
    ClientSecretCredentials,
)
from meho_backplane.connectors._shared.vault_creds import (
    load_vault_secret_data,
    strip_credential_value,
)

__all__ = [
    "AZURE_CLOUDS",
    "DEFAULT_CLOUD",
    "AzureCloud",
    "AzureCredentials",
    "AzureCredentialsLoader",
    "AzureSecretShapeError",
    "AzureTargetConfig",
    "AzureTargetConfigError",
    "AzureTargetLike",
    "check_tenant_match",
    "load_credentials_from_vault",
    "parse_credentials",
    "resolve_target_config",
]

#: The cloud a target uses when ``extras.cloud`` is not set.
DEFAULT_CLOUD: Final[str] = "AzureCloud"

#: The keys the secret must hold.
_SECRET_KEYS: Final[tuple[str, ...]] = ("tenant_id", "client_id", "client_secret")

_GUID: Final[re.Pattern[str]] = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


@dataclass(frozen=True)
class AzureCloud:
    """The hosts and the token scope of one Azure cloud."""

    name: str
    management_host: str
    login_host: str
    arm_scope: str


#: The three Azure clouds. The Resource Manager hosts and scopes are the ones
#: Microsoft's own Python SDK uses (``azure.mgmt.core.tools.get_arm_endpoints``).
AZURE_CLOUDS: Final[Mapping[str, AzureCloud]] = MappingProxyType(
    {
        "AzureCloud": AzureCloud(
            name="AzureCloud",
            management_host="management.azure.com",
            login_host=LOGIN_HOSTS["AzureCloud"],
            arm_scope="https://management.azure.com/.default",
        ),
        "AzureUSGovernment": AzureCloud(
            name="AzureUSGovernment",
            management_host="management.usgovcloudapi.net",
            login_host=LOGIN_HOSTS["AzureUSGovernment"],
            arm_scope="https://management.core.usgovcloudapi.net/.default",
        ),
        "AzureChinaCloud": AzureCloud(
            name="AzureChinaCloud",
            management_host="management.chinacloudapi.cn",
            login_host=LOGIN_HOSTS["AzureChinaCloud"],
            arm_scope="https://management.chinacloudapi.cn/.default",
        ),
    }
)


@runtime_checkable
class AzureTargetLike(Protocol):
    """The target fields the Azure connector reads.

    The ``Target`` model fits this shape as it is. ``id`` and ``tenant_id``
    form the cache key, so two targets never share a cached secret or token.
    ``verify_tls`` is read with ``getattr`` and defaults to ``True``.
    """

    id: object
    tenant_id: object
    name: str
    host: str
    port: int | None
    secret_ref: str | None
    auth_model: str | None
    extras: Mapping[str, Any]


@dataclass(frozen=True)
class AzureTargetConfig:
    """The checked Azure settings of one target."""

    subscription_id: str
    tenant_id: str | None
    cloud: AzureCloud


#: The login of a target, read from its secret. ``tenant_id`` and
#: ``client_id`` are lower-case GUIDs.
AzureCredentials = ClientSecretCredentials


#: Reads a target's secret and returns its login. Injected into the connector,
#: so tests can pass a stub.
AzureCredentialsLoader = Callable[[AzureTargetLike, Operator], Awaitable[AzureCredentials]]


class AzureTargetConfigError(ValueError):
    """The target is not a usable Azure target.

    The message names the target and the field to fix. It never repeats a
    value.
    """


class AzureSecretShapeError(CredentialsReadError):
    """The target's secret is missing a key, or a key is not usable.

    The message names the target, the secret reference and the key. It never
    repeats a value from the secret.
    """


def _guid(value: object) -> str | None:
    """Return *value* as a lower-case GUID, or ``None`` when it is not one."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text.lower() if _GUID.fullmatch(text) else None


def resolve_target_config(target: AzureTargetLike) -> AzureTargetConfig:
    """Check *target* and return its Azure settings.

    Raises :class:`AzureTargetConfigError` when ``extras.subscription_id`` is
    missing or not a GUID, ``extras.tenant_id`` is set but not a GUID,
    ``extras.cloud`` is unknown, or the target does not point at the Resource
    Manager host of its cloud over https on port 443 with the certificate
    checked.
    """
    extras: Mapping[str, Any] = target.extras if isinstance(target.extras, Mapping) else {}
    cloud_name = extras.get("cloud") or DEFAULT_CLOUD
    cloud = AZURE_CLOUDS.get(cloud_name) if isinstance(cloud_name, str) else None
    if cloud is None:
        raise AzureTargetConfigError(
            f"Azure target {target.name!r}: extras.cloud must be one of "
            f"{sorted(AZURE_CLOUDS)} (or not set, which means {DEFAULT_CLOUD!r})."
        )
    subscription_id = _guid(extras.get("subscription_id"))
    if subscription_id is None:
        raise AzureTargetConfigError(
            f"Azure target {target.name!r}: extras.subscription_id must be the "
            "subscription ID, a GUID such as 00000000-0000-0000-0000-000000000000."
        )
    tenant_value = extras.get("tenant_id")
    tenant_id = _guid(tenant_value)
    if tenant_value not in (None, "") and tenant_id is None:
        raise AzureTargetConfigError(
            f"Azure target {target.name!r}: extras.tenant_id must be the tenant "
            "(directory) ID, a GUID, or not set."
        )
    _check_endpoint(target, cloud, extras)
    return AzureTargetConfig(subscription_id=subscription_id, tenant_id=tenant_id, cloud=cloud)


def _check_endpoint(target: AzureTargetLike, cloud: AzureCloud, extras: Mapping[str, Any]) -> None:
    """Make sure the access token can only go to the cloud's Resource Manager host."""
    host = str(target.host).strip().lower()
    scheme = extras.get("scheme") or "https"
    if host != cloud.management_host or scheme != "https" or target.port not in (None, 443):
        raise AzureTargetConfigError(
            f"Azure target {target.name!r}: host must be {cloud.management_host!r} "
            f"(cloud {cloud.name!r}) over https on port 443. MEHO sends the Azure "
            "access token to no other address."
        )
    if getattr(target, "verify_tls", True) is False:
        raise AzureTargetConfigError(
            f"Azure target {target.name!r}: verify_tls must stay on. MEHO sends the "
            "Azure access token only over a connection with a checked certificate."
        )


def check_tenant_match(
    target: AzureTargetLike, config: AzureTargetConfig, credentials: AzureCredentials
) -> None:
    """Fail when the target's tenant is not the tenant of its login.

    A login is valid in one tenant only. When ``extras.tenant_id`` names
    another tenant than the secret, every call would fail later with a less
    clear error, so MEHO stops here.
    """
    if config.tenant_id is not None and config.tenant_id != credentials.tenant_id:
        raise AzureTargetConfigError(
            f"Azure target {target.name!r}: extras.tenant_id is not the tenant_id in "
            f"the secret at {target.secret_ref!r}. Make both name the same tenant."
        )


def parse_credentials(
    secret_data: Mapping[str, object], *, target_name: str, secret_ref: str | None
) -> AzureCredentials:
    """Return the login stored in *secret_data*.

    Every value is stripped of surrounding white space (a trailing newline is
    the most common storage mistake). Raises :class:`AzureSecretShapeError`
    naming the first missing or unusable key.
    """
    values: dict[str, str] = {}
    for key in _SECRET_KEYS:
        raw = secret_data.get(key)
        value = strip_credential_value(raw) if raw is not None else ""
        if not value:
            raise AzureSecretShapeError(
                f"Azure target {target_name!r}: the secret at {secret_ref!r} has no "
                f"{key!r} key. It needs tenant_id, client_id and client_secret."
            )
        values[key] = value
    return AzureCredentials(
        tenant_id=_secret_guid(values, "tenant_id", target_name=target_name, secret_ref=secret_ref),
        client_id=_secret_guid(values, "client_id", target_name=target_name, secret_ref=secret_ref),
        client_secret=values["client_secret"],
    )


def _secret_guid(
    values: Mapping[str, str], key: str, *, target_name: str, secret_ref: str | None
) -> str:
    """Return the secret's *key* as a lower-case GUID, or fail naming the key."""
    checked = _guid(values[key])
    if checked is None:
        raise AzureSecretShapeError(
            f"Azure target {target_name!r}: {key!r} in the secret at {secret_ref!r} must be a GUID."
        )
    return checked


async def load_credentials_from_vault(
    target: AzureTargetLike, operator: Operator
) -> AzureCredentials:
    """Read the target's secret as *operator* and return its login.

    Uses the shared credential reader, so the read runs under the operator's
    own identity and works for every secret store MEHO supports. Its errors
    pass through unchanged.
    """
    secret_data = await load_vault_secret_data(target, operator)
    return parse_credentials(secret_data, target_name=target.name, secret_ref=target.secret_ref)
