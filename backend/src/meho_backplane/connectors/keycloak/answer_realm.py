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
default. Off: every answer is as before.

:func:`name_the_realm` wraps the operation handlers of the connector class.
A handler is a coroutine method with the parameters ``(operator, target,
params)``. The wrapper puts the field first in a dict answer. The
operations that read or change one realm use the managed realm, so the field
is always the managed realm. A target without a managed realm never gets
here: the handler refuses it before it returns.

A long list answer is reduced to a short summary and a link to the stored
rows. :func:`with_realm_scalar` adds the field to the op's
``result_scalars`` hint, so the summary keeps it whole, also when the name
is long. With the switch off the answer has no such field, and the hint
keeps nothing.
"""

from __future__ import annotations

import functools
import inspect
import os
from typing import Any, Final

from meho_backplane.connectors.keycloak.session import resolve_realm_config
from meho_backplane.settings import parse_bool_env

__all__ = ["REALM_FIELD", "answer_realm_on", "name_the_realm", "with_realm_scalar"]

#: The field every Keycloak answer carries: the realm the call acted on.
REALM_FIELD: Final = "managed_realm"

_HANDLER_PARAMS: Final = ["self", "operator", "target", "params"]


def answer_realm_on() -> bool:
    """Return the state of the ``KEYCLOAK_ANSWER_REALM`` switch.

    Read from the env name, with the same parser as
    :attr:`~meho_backplane.settings.Settings.keycloak_answer_realm`.
    """
    return parse_bool_env(os.environ.get("KEYCLOAK_ANSWER_REALM"))


def _wrap(handler: Any) -> Any:
    @functools.wraps(handler)
    async def wrapper(self: Any, operator: Any, target: Any, params: Any) -> Any:
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


def with_realm_scalar(llm_instructions: dict[str, Any] | None) -> dict[str, Any]:
    """Return *llm_instructions* with :data:`REALM_FIELD` in its ``result_scalars`` keys.

    The result reducer keeps the keys of this hint in the summary of a
    reduced answer, outside the size limit of the other plain values. The
    input is not changed.
    """
    instructions = dict(llm_instructions or {})
    hint = dict(instructions.get("result_scalars") or {})
    keys = list(hint.get("keys") or [])
    if REALM_FIELD not in keys:
        keys.append(REALM_FIELD)
    hint["keys"] = keys
    instructions["result_scalars"] = hint
    return instructions
