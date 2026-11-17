# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Is the realm's ``browser`` flow still Keycloak's default login?

Level 1 of the touch flow (``meho approval touch``) is a fixed copy of the
login that Keycloak puts into every new realm as ``browser``. An admin can
change that flow, for example make the one-time code required for
everyone. Binding the touch flow to the console would then drop the change
from the console login. So the touch flow op binds only when ``browser``
still has the default steps, requirements and settings.

The default shapes come from Keycloak's ``DefaultAuthenticationFlows.
browserFlow`` (26.0 to 26.8) and the migration that adds the organization
step (26.6.1):

* 26.0 to 26.2: the one-time code sub-flow is ``Browser - Conditional OTP``
  with the code required.
* 26.3 and newer: it is ``Browser - Conditional 2FA``, the code is an
  alternative, and WebAuthn and recovery codes are there but turned off
  (each only when its feature is on).
* 26.4 and newer, with the passkeys feature on: a ``conditional-credential``
  step skips the code after a passkey.
* With the organization feature on (not in realm ``master``): an
  ``Organization`` sub-flow after the company login step. Level 1 of the
  touch flow has no such step, so these shapes count only while the
  realm's ``organizationsEnabled`` is false or missing.

Allowed settings: the company login step may have a default company login
(``defaultProvider``; MEHO's company login setup sets it, and the touch flow
copies it). The ``conditional-credential`` step has the passkey settings.
No other step has settings.
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.keycloak._paths import (
    _AUTH_CONFIG_PATH,
    _AUTH_FLOW_EXECUTIONS_PATH,
    fill_path,
)
from meho_backplane.connectors.keycloak.ops_authflow import get_admin_list_strict

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike

__all__ = [
    "DEFAULT_LOGIN_FLOW",
    "ORGANIZATIONS_ON_REASON",
    "default_login_difference",
    "default_login_shapes",
    "organizations_difference",
    "read_default_login_difference",
]

DEFAULT_LOGIN_FLOW = "browser"
ORGANIZATIONS_ON_REASON = (
    "the realm has organizations turned on; level 1 of the touch flow has no organization step"
)
_REDIRECTOR = "identity-provider-redirector"
_CONDITION = "conditional-credential"
_PASSKEY = "webauthn-passwordless"

#: One step as Keycloak lists it: level, name, requirement.
_Row = tuple[int, str, str]
#: A listed step and its settings (``None`` when it has none).
_Step = tuple[dict[str, Any], dict[str, Any] | None]

_HEAD: tuple[_Row, ...] = (
    (0, "auth-cookie", "ALTERNATIVE"),
    (0, "auth-spnego", "DISABLED"),
    (0, _REDIRECTOR, "ALTERNATIVE"),
)
_ORGANIZATION: tuple[_Row, ...] = (
    (0, "Organization", "ALTERNATIVE"),
    (1, "Browser - Conditional Organization", "CONDITIONAL"),
    (2, "conditional-user-configured", "REQUIRED"),
    (2, "organization", "ALTERNATIVE"),
)
_FORMS: tuple[_Row, ...] = (
    (0, "forms", "ALTERNATIVE"),
    (1, "auth-username-password-form", "REQUIRED"),
)
_OTP_26_0: tuple[_Row, ...] = (
    (1, "Browser - Conditional OTP", "CONDITIONAL"),
    (2, "conditional-user-configured", "REQUIRED"),
    (2, "auth-otp-form", "REQUIRED"),
)


def _two_factor(passkey: bool, webauthn: bool, recovery: bool) -> tuple[_Row, ...]:
    """The one-time code sub-flow of Keycloak 26.3 and newer."""
    rows: list[_Row] = [
        (1, "Browser - Conditional 2FA", "CONDITIONAL"),
        (2, "conditional-user-configured", "REQUIRED"),
    ]
    if passkey:
        rows.append((2, _CONDITION, "REQUIRED"))
    rows.append((2, "auth-otp-form", "ALTERNATIVE"))
    if webauthn:
        rows.append((2, "webauthn-authenticator", "DISABLED"))
    if recovery:
        rows.append((2, "auth-recovery-authn-code-form", "DISABLED"))
    return tuple(rows)


def default_login_shapes() -> list[tuple[_Row, ...]]:
    """Every shape Keycloak 26 gives a new or upgraded realm's ``browser`` flow."""
    codes = [_OTP_26_0] + [
        _two_factor(*flags) for flags in itertools.product((True, False), repeat=3)
    ]
    return [
        _HEAD + organization + _FORMS + code
        for organization in (_ORGANIZATION, ())
        for code in codes
    ]


def _name(row: dict[str, Any]) -> str:
    """A sub-flow is named by its alias (``displayName``), a step by its provider."""
    value = (
        row.get("displayName") if row.get("authenticationFlow") is True else row.get("providerId")
    )
    return value if isinstance(value, str) else ""


def _settings_ok(name: str, config: dict[str, Any] | None) -> bool:
    if name == _REDIRECTOR:
        return config is None or set(config) <= {"defaultProvider"}
    if name == _CONDITION:
        if config is None or config.get("credentials") != _PASSKEY:
            return False
        return set(config) <= {"credentials", "included"} and (
            str(config.get("included", "false")).strip().lower() == "false"
        )
    return config is None


def _first_difference(steps: list[_Step], shape: tuple[_Row, ...]) -> tuple[int, str | None]:
    """How far *steps* match *shape*, and how they differ (``None`` when they match)."""
    for index, want in enumerate(shape):
        if index >= len(steps):
            return index, f"step {index + 1} ({want[1]!r}) is missing"
        row, config = steps[index]
        have = (row.get("level"), _name(row), row.get("requirement"))
        if have != want:
            return index, (
                f"step {index + 1} is {have[1]!r} ({have[2]}) at level {have[0]}; "
                f"wanted {want[1]!r} ({want[2]}) at level {want[0]}"
            )
        if not _settings_ok(want[1], config):
            return index, f"step {index + 1} ({want[1]!r}) has other settings"
    if len(steps) > len(shape):
        return len(shape), f"it has {len(steps)} steps; wanted {len(shape)}"
    return len(shape), None


def default_login_difference(steps: list[_Step]) -> str | None:
    """How *steps* differ from Keycloak's default login, or ``None`` when they match.

    The difference named is the one against the default shape that matches
    the most steps from the top.
    """
    best: tuple[int, str | None] = (-1, None)
    for shape in default_login_shapes():
        reached, difference = _first_difference(steps, shape)
        if difference is None:
            return None
        if reached > best[0]:
            best = (reached, difference)
    return best[1]


def organizations_difference(steps: list[_Step], organizations: bool) -> str | None:
    """:data:`ORGANIZATIONS_ON_REASON` when *organizations* is on and *steps*
    have the ``Organization`` sub-flow, else ``None``."""
    if organizations and any(
        row.get("level") == 0 and _name(row) == _ORGANIZATION[0][1] for row, _ in steps
    ):
        return ORGANIZATIONS_ON_REASON
    return None


async def read_default_login_difference(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    realm: str,
    *,
    organizations: bool,
) -> str | None:
    """Read flow ``browser`` and its settings, then :func:`default_login_difference`
    and :func:`organizations_difference`.

    *organizations* is the realm's ``organizationsEnabled``.
    """
    path = fill_path(
        _AUTH_FLOW_EXECUTIONS_PATH,
        {"realm": realm, "flowAlias": DEFAULT_LOGIN_FLOW},
    )
    steps: list[_Step] = []
    for row in await get_admin_list_strict(connector, target, path, operator=operator):
        if not isinstance(row, dict):
            return "a step of the flow is not a JSON object"
        config_id = row.get("authenticationConfig")
        config: dict[str, Any] | None = None
        if isinstance(config_id, str) and config_id:
            config_path = fill_path(_AUTH_CONFIG_PATH, {"realm": realm, "id": config_id})
            body = await connector._get_admin_json(target, config_path, operator=operator)
            raw = body.get("config")
            config = raw if isinstance(raw, dict) else {}
        steps.append((row, config))
    return default_login_difference(steps) or organizations_difference(steps, organizations)
