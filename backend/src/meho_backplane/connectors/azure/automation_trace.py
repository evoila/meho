# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What the flight recorder keeps of an Azure Automation answer.

When the flight recorder is on, :func:`~meho_backplane.connectors.azure._http.send`
keeps each answer from Azure in the trace (the record of one call, for
debugging). Its shared filter finds secrets by their shape, such as a token or
a key. Some fields of Automation answers can hold a secret of any shape:

* ``parameters`` of a job schedule: the values a schedule passes to its
  runbook;
* ``parameters`` of a runbook, with their default values;
* ``publishContentLink`` and ``draft`` (with ``draftContentLink``) of a
  runbook: links to the runbook's code, which can be signed download links.

:class:`TraceMaskingCaller` is the connector as ``send`` sees it, except that
the trace gets the answer with these fields replaced by a fixed text. The
Azure connector has no way yet to mask fields per family; this object is the
bridge until it has one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.azure.session import AzureTargetLike

if TYPE_CHECKING:
    from meho_backplane.connectors.azure.connector import AzureConnector

__all__ = ["LEFT_OUT", "SECRET_FIELDS", "TraceMaskingCaller", "masked_answer"]

#: The fields of a row's ``properties`` that the trace never keeps.
SECRET_FIELDS: Final[frozenset[str]] = frozenset({"parameters", "publishContentLink", "draft"})

#: What the trace keeps in place of such a field.
LEFT_OUT: Final[str] = "<left out by MEHO: can hold a secret>"


def masked_answer(response: httpx.Response) -> Any:
    """The answer for the trace: Azure's JSON with :data:`SECRET_FIELDS` replaced.

    Each row in ``value`` keeps every field except these, which hold
    :data:`LEFT_OUT`. The shared filter still runs on the result. ``None``
    for an answer that is not JSON: the shared filter then sees the raw
    answer, which has no such fields.
    """
    try:
        body = response.json()
    except ValueError:
        return None
    rows = body.get("value") if isinstance(body, dict) else None
    for row in rows if isinstance(rows, list) else []:
        properties = row.get("properties") if isinstance(row, dict) else None
        if isinstance(properties, dict):
            for field in SECRET_FIELDS & properties.keys():
                properties[field] = LEFT_OUT
    return body


class TraceMaskingCaller:
    """The connector as ``send`` sees it, but its trace leaves out secret-bearing fields.

    ``send`` asks its connector for the request headers, the HTTP client,
    the request extensions and, while a trace is kept, the answer for the
    trace. This object answers with the connector's own, except for the
    answer for the trace: :func:`masked_answer`. The 401 handling stays the
    connector's own.
    """

    def __init__(self, connector: AzureConnector) -> None:
        self._connector = connector

    async def authorize(
        self, target: AzureTargetLike, operator: Operator
    ) -> tuple[dict[str, str], tuple[str, str]]:
        return await self._connector.authorize(target, operator)

    async def invalidate_session(self, target: AzureTargetLike) -> None:
        await self._connector.invalidate_session(target)

    async def _http_client(self, target: AzureTargetLike) -> httpx.AsyncClient:
        return await self._connector._http_client(target)

    def _request_extensions(self, target: AzureTargetLike) -> dict[str, Any]:
        return self._connector._request_extensions(target)

    def _trace_response_body(self, start: Any, response: httpx.Response) -> Any:
        if start is None:
            return None
        return masked_answer(response)
