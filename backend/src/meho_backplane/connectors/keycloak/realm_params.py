# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Realm names that come as operation parameters must be plain names.

Four ops take a realm name as a parameter: ``keycloak.realm.create`` and
``keycloak.realm.update`` (``realm`` and ``representation.realm``),
``keycloak.realm.delete`` and ``keycloak.user.check_login`` (``realm``).
Each value must pass
:func:`~meho_backplane.targets.required_extras.realm_name_problem`, the same
check a target's own realms pass. The value is never trimmed.

:func:`realm_param` reads one value in a handler and raises
:class:`ValueError` for a bad one. The params check registered here
(:func:`~meho_backplane.operations._validate.register_params_check`) refuses
the same values earlier: before the policy gate, so before a call waits
for approval and before any call to Keycloak.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.operations._validate import (
    ParamsCheck,
    params_problem,
    register_params_check,
)
from meho_backplane.targets.required_extras import check_realm_name, realm_name_problem

__all__ = ["REALM_PARAM_OPS", "check_realm_params", "realm_param"]

#: op_id -> the params (``a.b`` for a nested field) that hold a realm name.
REALM_PARAM_OPS: Final[dict[str, tuple[str, ...]]] = {
    "keycloak.realm.create": ("realm", "representation.realm"),
    "keycloak.realm.update": ("realm", "representation.realm"),
    "keycloak.realm.delete": ("realm",),
    "keycloak.user.check_login": ("realm",),
}


def _value(params: dict[str, Any], path: str) -> Any:
    current: Any = params
    for key in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def realm_param(params: dict[str, Any], path: str, op_id: str) -> str | None:
    """Return the realm name at *path* in *params*, or ``None`` when it is not given.

    An empty string counts as not given. Any other value must be a plain
    realm name, else :class:`ValueError` names the op and the param.
    """
    value = _value(params, path)
    if value is None or value == "":
        return None
    return check_realm_name(value, f"{op_id}: {path}")


def check_realm_params(op_id: str) -> ParamsCheck:
    """The params check of *op_id*: the first realm param that is not a plain name."""

    def check(params: dict[str, Any]) -> dict[str, Any] | None:
        for path in REALM_PARAM_OPS[op_id]:
            value = _value(params, path)
            if value is None or value == "":
                continue
            problem = realm_name_problem(value)
            if problem is not None:
                return params_problem(
                    path,
                    "a plain realm name",
                    f"Give the realm name as it is, without spaces around it; {problem}.",
                )
        return None

    return check


for _op_id in REALM_PARAM_OPS:
    register_params_check(_op_id, check_realm_params(_op_id))
