# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every Keycloak answer names the realm it acted on.

A Keycloak target works on one realm only: the managed realm it names. Each
answer of an operation (a read or a write) carries that realm in one fixed
field, :data:`REALM_FIELD`. Without it, a person can take an answer for the
realm in the name of the target when the two differ.

:func:`name_the_realm` wraps the operation handlers of the connector class.
A handler is a coroutine method with the parameters ``(operator, target,
params)``. The wrapper puts the field first in a dict answer. The
operations that read or change one realm use the managed realm, so the field
is always the managed realm. A target without a managed realm never gets
here: the handler refuses it before it returns.
"""

from __future__ import annotations

import functools
import inspect
from typing import Any, Final

from meho_backplane.connectors.keycloak.session import resolve_realm_config

__all__ = ["REALM_FIELD", "name_the_realm"]

#: The field every Keycloak answer carries: the realm the call acted on.
REALM_FIELD: Final = "managed_realm"

_HANDLER_PARAMS: Final = ["self", "operator", "target", "params"]


def _wrap(handler: Any) -> Any:
    @functools.wraps(handler)
    async def wrapper(self: Any, operator: Any, target: Any, params: Any) -> Any:
        answer = await handler(self, operator, target, params)
        if not isinstance(answer, dict):
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
