# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What the flight recorder may store of a Salt ``/rpc`` answer.

The shared redaction of the flight recorder knows secrets by shape (a
token, a key) and by op family (a login). A pillar value in a job return,
or a password a job read from a file, is neither. So
:meth:`SaltConnector.trace_response_body` runs every answer through
:func:`trace_payload` before the trace stores it: the recorded answer
passes the same rules as the read that made the call, and a value a read
withholds never reaches the stored trace.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors._shared.api_file_values import Scrubber
from meho_backplane.connectors.salt.connector import (
    GET_VERSIONS_RPC,
    RETURNS_RPC,
    TARGET_GROUP_RPC,
)
from meho_backplane.connectors.salt.reads import _result_row, _results

__all__ = ["trace_payload"]

_TARGET_SECRET_KEYS: Final[frozenset[str]] = frozenset({"pillars", "metadata"})


def _with_results(ret: Any, rows: list[dict[str, Any]]) -> Any:
    """*ret* with its ``results`` replaced by *rows*; its other keys (count, paging) scrubbed."""
    if not isinstance(ret, dict):
        return rows
    rest = Scrubber().tree({key: value for key, value in ret.items() if key != "results"}, {})
    return {**rest, "results": rows}


def _trace_ret(call: tuple[Any, Any], ret: Any) -> Any:
    if call == RETURNS_RPC:
        rows, _ = _results(ret)
        return _with_results(ret, [_result_row(row) for row in rows])
    if call == TARGET_GROUP_RPC:
        rows, _ = _results(ret)
        kept = [
            {key: value for key, value in row.items() if key not in _TARGET_SECRET_KEYS}
            for row in rows
        ]
        return _with_results(ret, kept)
    if call == GET_VERSIONS_RPC and isinstance(ret, dict):
        ret = {key: value for key, value in ret.items() if key != "opts"}
    return Scrubber().tree(ret, {})


def trace_payload(request: Any, payload: Any) -> Any:
    """The answer of one ``/rpc`` call as the flight recorder may record it.

    The recorded answer holds nothing a read withholds from its result: job
    returns and arguments pass the rules of ``salt.job.result``, saved
    targets lose their pillar data and metadata, and the versions answer
    loses the server settings (``opts``). Every other value is scrubbed of
    secret-named values. *request* is the parsed request body, which names
    the RPC ``resource`` and ``method``.
    """
    if not isinstance(payload, dict) or not isinstance(request, dict):
        return Scrubber().tree(payload, {})
    call = (request.get("resource"), request.get("method"))
    rest = Scrubber().tree({key: value for key, value in payload.items() if key != "ret"}, {})
    return {**rest, "ret": _trace_ret(call, payload.get("ret"))}
