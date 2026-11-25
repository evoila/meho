# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Target shape and secret shape of the Entra ID connector.

Target shape
============

One target is one Microsoft Entra ID tenant (directory).

* ``host`` is the Microsoft Graph host of the cloud: ``graph.microsoft.com``
  for the public cloud.
* ``extras.tenant_id`` (optional) is the tenant ID, a GUID. When it is set,
  it must equal the ``tenant_id`` in the secret.
* ``extras.cloud`` (optional) names the cloud: ``AzureCloud`` (the default),
  ``AzureUSGovernment`` or ``AzureChinaCloud``, the same names as on Azure
  targets. Only ``AzureCloud`` is planned for the first live proof.

MEHO sends the access token only to the Microsoft Graph host of the target's
cloud, over https on port 443, with the certificate checked. A target that
points anywhere else fails before any token is made.

Secret shape
============

The target's ``secret_ref`` names a secret with ``tenant_id``, ``client_id``
(the application ID of the login) and one of two ways to prove the app:

* ``client_secret``, or
* ``private_key`` and ``certificate`` (both PEM). MEHO signs a short proof with
  the key. This is the way Microsoft recommends for a program that signs in by
  itself. Only the public certificate is uploaded to the app.

Both ways in one secret, or neither, is refused. Other keys are ignored. The
client secret shape is the same as the secret of an Azure target, so one login
can serve both.

The checks here look like the ones of the Azure connector
(``connectors/azure/session.py``), but every message names Entra ID, so an
operator is never told to fix an "Azure target" that is not one.
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
    CertificateCredentials,
    CertificatePairError,
    ClientSecretCredentials,
    MicrosoftCredentials,
    check_certificate_pair,
)
from meho_backplane.connectors._shared.vault_creds import (
    load_vault_secret_data,
    strip_credential_value,
)

__all__ = [
    "DEFAULT_CLOUD",
    "ENTRA_CLOUDS",
    "EntraCloud",
    "EntraCredentials",
    "EntraCredentialsLoader",
    "EntraLogin",
    "EntraSecretShapeError",
    "EntraTargetConfig",
    "EntraTargetConfigError",
    "EntraTargetLike",
    "check_tenant_match",
    "is_guid",
    "load_credentials_from_vault",
    "parse_credentials",
    "resolve_target_config",
]

#: The cloud a target uses when ``extras.cloud`` is not set.
DEFAULT_CLOUD: Final[str] = "AzureCloud"

#: The keys the secret must always hold.
_SECRET_KEYS: Final[tuple[str, ...]] = ("tenant_id", "client_id")

#: The keys of the two ways to prove the app. The secret holds all keys of one
#: way and none of the other.
_SECRET_WAY: Final[tuple[str, ...]] = ("client_secret",)
_CERTIFICATE_WAY: Final[tuple[str, ...]] = ("private_key", "certificate")

_NEEDS: Final[str] = (
    "It needs tenant_id, client_id and either client_secret, or private_key and certificate."
)

_GUID: Final[re.Pattern[str]] = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


@dataclass(frozen=True)
class EntraCloud:
    """The Microsoft Graph host, the login host and the token scope of one cloud."""

    name: str
    graph_host: str
    login_host: str
    graph_scope: str


#: The Microsoft Graph host of each cloud (Microsoft's "national cloud
#: deployments" page). A client credentials token must name the Graph host of
#: its own cloud in the scope. US Government L5 (DoD), on its own host, is not
#: supported.
ENTRA_CLOUDS: Final[Mapping[str, EntraCloud]] = MappingProxyType(
    {
        "AzureCloud": EntraCloud(
            name="AzureCloud",
            graph_host="graph.microsoft.com",
            login_host=LOGIN_HOSTS["AzureCloud"],
            graph_scope="https://graph.microsoft.com/.default",
        ),
        "AzureUSGovernment": EntraCloud(
            name="AzureUSGovernment",
            graph_host="graph.microsoft.us",
            login_host=LOGIN_HOSTS["AzureUSGovernment"],
            graph_scope="https://graph.microsoft.us/.default",
        ),
        "AzureChinaCloud": EntraCloud(
            name="AzureChinaCloud",
            graph_host="microsoftgraph.chinacloudapi.cn",
            login_host=LOGIN_HOSTS["AzureChinaCloud"],
            graph_scope="https://microsoftgraph.chinacloudapi.cn/.default",
        ),
    }
)


@runtime_checkable
class EntraTargetLike(Protocol):
    """The target fields the Entra ID connector reads.

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
class EntraTargetConfig:
    """The checked Entra ID settings of one target."""

    tenant_id: str | None
    cloud: EntraCloud


#: The client secret login of a target. ``tenant_id`` and ``client_id`` are
#: lower-case GUIDs.
EntraCredentials = ClientSecretCredentials

#: The login of a target as read from its secret: a client secret or a certificate.
EntraLogin = MicrosoftCredentials


#: Reads a target's secret and returns its login. Injected into the connector,
#: so tests can pass a stub.
EntraCredentialsLoader = Callable[[EntraTargetLike, Operator], Awaitable[EntraLogin]]


class EntraTargetConfigError(ValueError):
    """The target is not a usable Entra ID target.

    The message names the target and the field to fix. It never repeats a
    value.
    """


class EntraSecretShapeError(CredentialsReadError):
    """The target's secret is missing a key, or a key is not usable.

    The message names the target, the secret reference and the key. It never
    repeats a value from the secret.
    """


def is_guid(value: object) -> bool:
    """Return ``True`` when *value* is a GUID such as an object ID from Graph."""
    return isinstance(value, str) and _GUID.fullmatch(value) is not None


def _guid(value: object) -> str | None:
    """Return *value* as a lower-case GUID, or ``None`` when it is not one."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text.lower() if _GUID.fullmatch(text) else None


def resolve_target_config(target: EntraTargetLike) -> EntraTargetConfig:
    """Check *target* and return its Entra ID settings.

    Raises :class:`EntraTargetConfigError` when ``extras.tenant_id`` is set
    but not a GUID, ``extras.cloud`` is unknown, or the target does not
    point at the Microsoft Graph host of its cloud over https on port 443
    with the certificate checked.
    """
    extras: Mapping[str, Any] = target.extras if isinstance(target.extras, Mapping) else {}
    cloud_name = extras.get("cloud") or DEFAULT_CLOUD
    cloud = ENTRA_CLOUDS.get(cloud_name) if isinstance(cloud_name, str) else None
    if cloud is None:
        raise EntraTargetConfigError(
            f"Entra ID target {target.name!r}: extras.cloud must be one of "
            f"{sorted(ENTRA_CLOUDS)} (or not set, which means {DEFAULT_CLOUD!r})."
        )
    tenant_value = extras.get("tenant_id")
    tenant_id = _guid(tenant_value)
    if tenant_value not in (None, "") and tenant_id is None:
        raise EntraTargetConfigError(
            f"Entra ID target {target.name!r}: extras.tenant_id must be the tenant "
            "(directory) ID, a GUID, or not set."
        )
    _check_endpoint(target, cloud, extras)
    return EntraTargetConfig(tenant_id=tenant_id, cloud=cloud)


def _check_endpoint(target: EntraTargetLike, cloud: EntraCloud, extras: Mapping[str, Any]) -> None:
    """Make sure the access token can only go to the cloud's Microsoft Graph host."""
    host = str(target.host).strip().lower()
    scheme = extras.get("scheme") or "https"
    if host != cloud.graph_host or scheme != "https" or target.port not in (None, 443):
        raise EntraTargetConfigError(
            f"Entra ID target {target.name!r}: host must be {cloud.graph_host!r} "
            f"(cloud {cloud.name!r}) over https on port 443. MEHO sends the Microsoft "
            "Graph access token to no other address."
        )
    if getattr(target, "verify_tls", True) is False:
        raise EntraTargetConfigError(
            f"Entra ID target {target.name!r}: verify_tls must stay on. MEHO sends the "
            "access token only over a connection with a checked certificate."
        )


def check_tenant_match(
    target: EntraTargetLike, config: EntraTargetConfig, credentials: EntraLogin
) -> None:
    """Fail when the target's tenant is not the tenant of its login.

    A login is valid in one tenant only. When ``extras.tenant_id`` names
    another tenant than the secret, MEHO stops here with a clear message.
    """
    if config.tenant_id is not None and config.tenant_id != credentials.tenant_id:
        raise EntraTargetConfigError(
            f"Entra ID target {target.name!r}: extras.tenant_id is not the tenant_id in "
            f"the secret at {target.secret_ref!r}. Make both name the same tenant."
        )


def parse_credentials(
    secret_data: Mapping[str, object], *, target_name: str, secret_ref: str | None
) -> EntraLogin:
    """Return the login stored in *secret_data*.

    Every value is stripped of surrounding white space (a trailing newline is
    the most common storage mistake). Raises :class:`EntraSecretShapeError`
    naming the first missing or unusable key. A certificate login is checked
    here too (readable PEM, RSA, key belongs to the certificate), so a secret
    with a bad pair is never kept in the connector's cache.
    """
    values: dict[str, str] = {}
    for key in (*_SECRET_KEYS, *_SECRET_WAY, *_CERTIFICATE_WAY):
        raw = secret_data.get(key)
        values[key] = strip_credential_value(raw) if raw is not None else ""
    for key in _SECRET_KEYS:
        if not values[key]:
            raise EntraSecretShapeError(
                f"Entra ID target {target_name!r}: the secret at {secret_ref!r} has no "
                f"{key!r} key. {_NEEDS}"
            )
    has_secret = bool(values["client_secret"])
    present_certificate = [key for key in _CERTIFICATE_WAY if values[key]]
    if has_secret and present_certificate:
        raise EntraSecretShapeError(
            f"Entra ID target {target_name!r}: the secret at {secret_ref!r} holds both a "
            f"client_secret and certificate keys. Keep one way only. {_NEEDS}"
        )
    if not has_secret and not present_certificate:
        raise EntraSecretShapeError(
            f"Entra ID target {target_name!r}: the secret at {secret_ref!r} has no "
            f"client_secret and no certificate keys. {_NEEDS}"
        )
    if present_certificate:
        _check_certificate_way(values, target_name=target_name, secret_ref=secret_ref)
    guids: dict[str, str] = {}
    for key in _SECRET_KEYS:
        checked = _guid(values[key])
        if checked is None:
            raise EntraSecretShapeError(
                f"Entra ID target {target_name!r}: {key!r} in the secret at "
                f"{secret_ref!r} must be a GUID."
            )
        guids[key] = checked
    if present_certificate:
        return CertificateCredentials(
            tenant_id=guids["tenant_id"],
            client_id=guids["client_id"],
            private_key=values["private_key"],
            certificate=values["certificate"],
        )
    return ClientSecretCredentials(
        tenant_id=guids["tenant_id"],
        client_id=guids["client_id"],
        client_secret=values["client_secret"],
    )


def _check_certificate_way(
    values: Mapping[str, str], *, target_name: str, secret_ref: str | None
) -> None:
    """Fail unless the secret holds a usable private key and certificate pair."""
    for key in _CERTIFICATE_WAY:
        if not values[key]:
            raise EntraSecretShapeError(
                f"Entra ID target {target_name!r}: the secret at {secret_ref!r} has no "
                f"{key!r} key. A certificate login needs private_key and certificate."
            )
    try:
        check_certificate_pair(values["private_key"], values["certificate"])
    except CertificatePairError as exc:
        raise EntraSecretShapeError(
            f"Entra ID target {target_name!r}: the secret at {secret_ref!r} has an "
            f"unusable certificate login. {exc}"
        ) from None


async def load_credentials_from_vault(target: EntraTargetLike, operator: Operator) -> EntraLogin:
    """Read the target's secret as *operator* and return its login.

    Uses the shared credential reader, so the read runs under the operator's
    own identity and works for every secret store MEHO supports. Its errors
    pass through unchanged.
    """
    secret_data = await load_vault_secret_data(target, operator)
    return parse_credentials(secret_data, target_name=target.name, secret_ref=target.secret_ref)
