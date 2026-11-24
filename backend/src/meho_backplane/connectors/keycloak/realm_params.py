# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Realm names that come as operation parameters: plain names, on one realm only.

Four ops take a realm name as a parameter: ``keycloak.realm.create`` and
``keycloak.realm.update`` (``realm`` and ``representation.realm``),
``keycloak.realm.delete`` and ``keycloak.user.check_login`` (``realm``).
Each value must pass
:func:`~meho_backplane.targets.required_extras.realm_name_problem`, the same
check a target's own realms pass. The name of a new realm
(``keycloak.realm.create``) must also be plain ASCII
(:func:`is_plain_realm_name`). The value is never trimmed.

:func:`realm_param` reads one value in a handler and raises
:class:`ValueError` for a bad one. The params check registered here
(:func:`~meho_backplane.operations._validate.register_params_check`) refuses
the same values earlier: before the policy gate, so before a call waits
for approval and before any call to Keycloak.

A target works on one realm only, the managed realm it names. All four ops
(:data:`FENCED_OPS`) refuse a realm that is not the managed realm: the
handler does (:func:`realm_param` with a *target*), and so does the check
before the wait for approval (:func:`realm_fence_park_check`). The text says
to use a target that names the other realm. It never repeats the value the
caller gave. So ``keycloak.realm.delete`` can only delete the managed realm
of its target, and its other guards (``check_realm_guards``) still refuse
master, the target's admin realm and MEHO's own login realm.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.keycloak.session import resolve_realm_config
from meho_backplane.operations._park_check import ParkCheck, register_park_check
from meho_backplane.operations._validate import (
    ParamsCheck,
    params_problem,
    register_params_check,
)
from meho_backplane.targets.required_extras import check_realm_name, realm_name_problem

if TYPE_CHECKING:
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "FENCED_OPS",
    "REALM_PARAM_OPS",
    "check_answered_realm",
    "check_realm_params",
    "is_plain_realm_name",
    "new_realm_problem",
    "realm_fence_park_check",
    "realm_fence_problem",
    "realm_param",
]

#: op_id -> the params (``a.b`` for a nested field) that hold a realm name.
REALM_PARAM_OPS: Final[dict[str, tuple[str, ...]]] = {
    "keycloak.realm.create": ("realm", "representation.realm"),
    "keycloak.realm.update": ("realm", "representation.realm"),
    "keycloak.realm.delete": ("realm",),
    "keycloak.user.check_login": ("realm",),
}


#: The ops that work on the managed realm only: all of the above.
FENCED_OPS: Final[frozenset[str]] = frozenset(REALM_PARAM_OPS)

_CREATE: Final[str] = "keycloak.realm.create"
_NOT_PLAIN: Final[str] = "a new realm's name holds only plain ASCII characters, no accents"


def _value(params: dict[str, Any], path: str) -> Any:
    current: Any = params
    for key in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _fence_text(op_id: str, managed: str) -> str:
    return (
        f"{op_id}: this target works on the realm {managed!r} only, and the call names "
        "another realm. Use a target that names that realm as its managed realm."
    )


def realm_fence_problem(
    op_id: str,
    params: dict[str, Any],
    target: KeycloakTargetLike | None,
    only: str | None = None,
) -> str | None:
    """The text that refuses a realm param other than the target's managed realm, or ``None``.

    Only the ops in :data:`FENCED_OPS` are fenced. A value that is not a
    plain name is left to :func:`check_realm_params`, and a target with no
    managed realm to the target check that refuses every call on it. With
    *only*, just that param is looked at.
    """
    if op_id not in FENCED_OPS or target is None:
        return None
    try:
        managed = resolve_realm_config(target).managed_realm
    except ValueError:
        return None
    for path in (only,) if only else REALM_PARAM_OPS[op_id]:
        value = _value(params, path)
        if isinstance(value, str) and value != "" and value != managed:
            return _fence_text(op_id, managed)
    return None


def realm_fence_park_check(op_id: str, inner: ParkCheck | None = None) -> ParkCheck:
    """The check before the wait for approval: the fence first, then *inner* when given."""

    async def check(ctx: PreviewContext) -> str | None:
        problem = realm_fence_problem(op_id, ctx.params, ctx.target)
        if problem is not None:
            return problem
        return await inner(ctx) if inner is not None else None

    return check


def realm_param(
    params: dict[str, Any],
    path: str,
    op_id: str,
    target: KeycloakTargetLike | None = None,
) -> str | None:
    """Return the realm name at *path* in *params*, or ``None`` when it is not given.

    An empty string counts as not given. Any other value must be a plain
    realm name, else :class:`ValueError` names the op and the param. With a
    *target*, a fenced op also refuses a realm that is not the target's
    managed realm.
    """
    value = _value(params, path)
    if value is None or value == "":
        return None
    name = check_realm_name(value, f"{op_id}: {path}")
    problem = realm_fence_problem(op_id, params, target, only=path)
    if problem is not None:
        raise ValueError(problem)
    return name


def check_answered_realm(op_id: str, answer: Any, target: KeycloakTargetLike) -> None:
    """Refuse a realm that Keycloak answers with another name than the managed realm.

    A Keycloak database can match a realm name without letter case or
    accents, so ``GET /admin/realms/{realm}`` can answer with another realm.
    The answer's ``realm`` must be exactly the target's managed realm, with
    the same letters, case and accents. Any other name, no name, or an
    answer that is not an object raises :class:`ValueError`. The text names
    the managed realm only. ``keycloak.realm.get`` and
    ``keycloak.realm.update`` call it before they return or change the realm.
    """
    managed = resolve_realm_config(target).managed_realm
    if not isinstance(answer, dict) or answer.get("realm") != managed:
        raise ValueError(
            f"{op_id}: Keycloak did not answer with the realm {managed!r}, so MEHO "
            "returns and changes nothing. Check the managed realm of the target."
        )


def is_plain_realm_name(name: str) -> bool:
    """True when *name* holds only plain ASCII characters (``" "`` to ``"~"``).

    The name of a realm that ``keycloak.realm.create`` makes must be such a
    name: letters ``a`` to ``z`` and ``A`` to ``Z``, digits, the space and the
    other ASCII signs that the realm name check allows (all but ``/``,
    ``\\``, ``?``, ``#`` and ``%``). No letter with an accent: a Keycloak
    database can match such a letter with the plain one.
    """
    return all(" " <= ch <= "~" for ch in name)


def new_realm_problem(target: KeycloakTargetLike | None) -> str | None:
    """The text that refuses to create the target's managed realm, or ``None``.

    ``keycloak.realm.create`` makes the managed realm, also when the call
    names none, so that name must be plain ASCII (:func:`is_plain_realm_name`).
    A target with no managed realm is left to the target check.
    """
    if target is None:
        return None
    try:
        managed = resolve_realm_config(target).managed_realm
    except ValueError:
        return None
    if is_plain_realm_name(managed):
        return None
    return f"{_CREATE}: this target works on the realm {managed!r} only; {_NOT_PLAIN}."


async def _new_realm_park_check(ctx: PreviewContext) -> str | None:
    return new_realm_problem(ctx.target)


def check_realm_params(op_id: str) -> ParamsCheck:
    """The params check of *op_id*: the first realm param that is not a plain name."""

    def check(params: dict[str, Any]) -> dict[str, Any] | None:
        for path in REALM_PARAM_OPS[op_id]:
            value = _value(params, path)
            if value is None or value == "":
                continue
            problem = realm_name_problem(value)
            if problem is None and op_id == _CREATE and not is_plain_realm_name(value):
                problem = _NOT_PLAIN
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


for _op_id in ("keycloak.realm.delete", "keycloak.user.check_login"):
    register_park_check(_op_id, realm_fence_park_check(_op_id))
register_park_check(_CREATE, realm_fence_park_check(_CREATE, _new_realm_park_check))
