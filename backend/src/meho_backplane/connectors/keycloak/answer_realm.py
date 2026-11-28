# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every Keycloak answer names the realm it acted on.

A Keycloak target works on one realm only: the managed realm it names. Each
answer of an operation (a read or a write) carries that realm in one fixed
field, :data:`REALM_FIELD`. Without it, a person can take an answer for the
realm in the name of the target when the two differ.

The field changes what an AI agent reads, so it is behind the switch
``KEYCLOAK_ANSWER_REALM``
(:attr:`~meho_backplane.settings.Settings.keycloak_answer_realm`), off by
default. Off: no answer has the ``managed_realm`` field.

:func:`name_the_realm` wraps the operation handlers of the connector class.
A handler is a coroutine method with the parameters ``(operator, target,
params)``. The wrapper puts the field first in a dict answer. The
operations that read or change one realm use the managed realm, so the field
is always the managed realm. A target without a managed realm never gets
here: the handler refuses it before it returns.

A long answer is reduced to a short summary and a link to the stored result.
The summary keeps the field whole, also when the name is long
(:mod:`~meho_backplane.operations.required_fields`). With the switch off the
summary has no ``managed_realm`` field.

The wrapper also opens one run of the realm check
(:mod:`~meho_backplane.connectors.keycloak.managed_realm_check`), in both
states of the switch: every run checks the realm's name anew, and a check
passed in it does not count after it.
"""

from __future__ import annotations

import functools
import inspect
from typing import Any, Final

from meho_backplane.connectors.keycloak.session import resolve_realm_config
from meho_backplane.operations.required_fields import register_required_fields
from meho_backplane.targets.keycloak_realm_warnings import answer_realm_on

__all__ = ["REALM_FIELD", "answer_realm_on", "name_the_realm"]

#: The field every Keycloak answer carries: the realm the call acted on.
REALM_FIELD: Final = "managed_realm"

_HANDLER_PARAMS: Final = ["self", "operator", "target", "params"]


def _wrap(handler: Any) -> Any:
    @functools.wraps(handler)
    async def wrapper(self: Any, operator: Any, target: Any, params: Any) -> Any:
        with self._realm_checks.run():
            answer = await handler(self, operator, target, params)
        if not isinstance(answer, dict) or not answer_realm_on():
            return answer
        return {REALM_FIELD: resolve_realm_config(target).managed_realm, **answer}

    return wrapper


def name_the_realm[C: type](cls: C) -> C:
    """Wrap every operation handler of *cls* so its answer names the realm."""
    for name, attr in list(vars(cls).items()):
        if name.startswith("_") or not inspect.iscoroutinefunction(attr):
            continue
        if list(inspect.signature(attr).parameters) == _HANDLER_PARAMS:
            setattr(cls, name, _wrap(attr))
    return cls


def _required_fields() -> tuple[str, ...]:
    """The field every shortened answer keeps while the switch is on."""
    return (REALM_FIELD,) if answer_realm_on() else ()


register_required_fields("keycloak", _required_fields)
