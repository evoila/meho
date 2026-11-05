# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What the flight recorder keeps of an Azure answer (#5212).

When the flight recorder is on, :func:`~meho_backplane.connectors.azure._http.send`
keeps each answer from Azure in the trace (the record of one call, for
debugging). The connector's ``trace_response_body`` hands the trace Azure's
JSON with the value of every field in :data:`SECRET_FIELDS` replaced by
:data:`LEFT_OUT`, at any depth, for every family. The shared filter then
still runs on the result: it finds secrets by their shape and by the shared
secret-name rule (:func:`~meho_backplane.broadcast.events.is_secret_param_name`).

:data:`SECRET_FIELDS` holds only the names that rule does not match, so the
two lists never repeat each other. Names the rule already matches, such as
``sharedKey``, ``adminPassword``, ``serverAppSecret``, ``secret`` and
``connectionString``, are not listed here.
"""

from __future__ import annotations

from typing import Any, Final

__all__ = ["LEFT_OUT", "SECRET_FIELDS", "mask_answer"]

#: The fields of an Azure answer whose value the trace never keeps:
#:
#: * ``serviceKey``, ``authorizationKey``: the keys of an ExpressRoute circuit
#:   and its authorizations; ``activationKey``: the key of an ExpressRoute
#:   Direct port or a VPN site;
#: * ``keys``, ``primaryKey``, ``secondaryKey``: the access keys a ``listKeys``
#:   answer holds (storage accounts, Automation accounts);
#: * ``parameters`` (job schedules, runbooks, deployments), ``publishContentLink``
#:   and ``draft`` (runbooks): values of any shape and signed download links.
SECRET_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "serviceKey",
        "authorizationKey",
        "activationKey",
        "keys",
        "primaryKey",
        "secondaryKey",
        "parameters",
        "publishContentLink",
        "draft",
    }
)

#: What the trace keeps in place of such a field.
LEFT_OUT: Final[str] = "<left out by MEHO: can hold a secret>"

_SECRET_FIELDS_FOLDED: Final[frozenset[str]] = frozenset(name.casefold() for name in SECRET_FIELDS)


def mask_answer(body: Any) -> Any:
    """*body* with the value of each :data:`SECRET_FIELDS` name replaced by :data:`LEFT_OUT`.

    Walks objects and lists at any depth. A name matches in any case, and its
    whole value is replaced, an object or a list too. The walk changes *body*
    in place (a freshly parsed answer) and returns it.
    """
    if isinstance(body, dict):
        for name, value in body.items():
            if isinstance(name, str) and name.casefold() in _SECRET_FIELDS_FOLDED:
                body[name] = LEFT_OUT
            else:
                mask_answer(value)
    elif isinstance(body, list):
        for item in body:
            mask_answer(item)
    return body
