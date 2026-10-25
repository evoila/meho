# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What the flight recorder may store of a Salt ``/rpc`` answer.

The shared redaction of the flight recorder knows secrets by shape (a
token, a key) and by op family (a login). A pillar value in a job return,
a custom grain, or a password a job read from a file, is neither. So
:meth:`SaltConnector.trace_response_body` runs every answer through
:func:`trace_payload` before the trace stores it.

The stored answer keeps only the fields that the read which made the call
uses, under their vendor names. Every other field is left out, so a value
a read does not show never reaches the stored trace. Job returns pass the
rules of ``salt.job.result``. The answer of a method that no read calls
keeps no ``ret`` at all.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors._shared.api_file_values import Scrubber
from meho_backplane.connectors.salt.connector import (
    CMDS_RPC,
    GET_VERSIONS_RPC,
    MASTER_PRESENCE_RPC,
    MINION_CACHE_RPC,
    MINION_KEY_STATE_RPC,
    MINION_PRESENCE_RPC,
    RETURNS_RPC,
    TARGET_GROUP_RPC,
)
from meho_backplane.connectors.salt.reads import JOB_FIELDS, _results
from meho_backplane.connectors.salt.returns import result_row

__all__ = ["trace_payload"]

#: Paging fields of a paged answer.
_PAGE_FIELDS: Final[frozenset[str]] = frozenset({"count", "limit", "page"})

_KEY_FIELDS: Final[frozenset[str]] = frozenset(
    {"minion", "minion_id", "master", "master_id", "key_state"}
)

#: Per paged RPC: the row fields its read uses.
_ROW_FIELDS: Final[dict[tuple[str, str], frozenset[str]]] = {
    MASTER_PRESENCE_RPC: frozenset({"master_id", "cluster_id", "status", "last_seen"}),
    MINION_KEY_STATE_RPC: _KEY_FIELDS,
    MINION_PRESENCE_RPC: _KEY_FIELDS | {"status", "timestamp"},
    CMDS_RPC: frozenset(JOB_FIELDS),
    TARGET_GROUP_RPC: frozenset({"uuid", "name", "desc", "tgt"}),
}

#: The fields of one saved-target entry (per master) that ``salt.target.list`` reads.
_TARGET_SPEC_FIELDS: Final[frozenset[str]] = frozenset({"tgt", "tgt_type"})

#: The grains of the minion cache that ``salt.minion.list`` reads.
_GRAIN_FIELDS: Final[frozenset[str]] = frozenset({"os", "osrelease", "kernel", "saltversion"})

#: The parts of the ``api.get_versions`` answer that ``salt.about`` reads.
_VERSION_FIELDS: Final[frozenset[str]] = frozenset({"raas", "python"})


def _pick(value: Any, fields: frozenset[str]) -> dict[str, Any]:
    """The entries of the dict *value* whose key is in *fields*; ``{}`` for a non-dict."""
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if key in fields}


def _paged(ret: Any, rows: list[dict[str, Any]]) -> Any:
    """A paged answer with only its paging fields and *rows*."""
    if not isinstance(ret, dict):
        return rows
    return {**_pick(ret, _PAGE_FIELDS), "results": rows}


def _target_row(row: dict[str, Any]) -> dict[str, Any]:
    kept = _pick(row, _ROW_FIELDS[TARGET_GROUP_RPC])
    tgt = kept.get("tgt")
    if isinstance(tgt, dict):
        kept["tgt"] = {master: _pick(spec, _TARGET_SPEC_FIELDS) for master, spec in tgt.items()}
    return kept


def _grains(entry: Any) -> Any:
    return entry.get("grains") if isinstance(entry, dict) else None


def _minion_cache(ret: Any) -> dict[str, Any]:
    """The minion cache (``{master: {minion: {"grains": ...}}}``) with only the read grains."""
    if not isinstance(ret, dict):
        return {}
    return {
        master: {
            minion: {"grains": _pick(_grains(entry), _GRAIN_FIELDS)}
            for minion, entry in minions.items()
        }
        for master, minions in ret.items()
        if isinstance(minions, dict)
    }


def _trace_ret(call: tuple[Any, Any], ret: Any) -> Any:
    if call == RETURNS_RPC:
        rows, _ = _results(ret)
        return _paged(ret, [result_row(row) for row in rows])
    if call == TARGET_GROUP_RPC:
        rows, _ = _results(ret)
        return _paged(ret, [_target_row(row) for row in rows])
    fields = _ROW_FIELDS.get(call)
    if fields is not None:
        rows, _ = _results(ret)
        return _paged(ret, [_pick(row, fields) for row in rows])
    if call == MINION_CACHE_RPC:
        return _minion_cache(ret)
    if call == GET_VERSIONS_RPC:
        return _pick(ret, _VERSION_FIELDS)
    return None


def trace_payload(request: Any, payload: Any) -> Any:
    """The answer of one ``/rpc`` call as the flight recorder may record it.

    ``ret`` keeps only the fields the read that made the call uses: job
    returns pass the rules of ``salt.job.result``, the minion cache keeps
    the four grains ``salt.minion.list`` shows, the versions answer keeps
    the RaaS and Python versions. The rest of the answer (``riq``,
    ``error``, ``warnings``) and every kept value are scrubbed of
    secret-named values. *request* is the parsed request body, which names
    the RPC ``resource`` and ``method``.
    """
    if not isinstance(payload, dict) or not isinstance(request, dict):
        return Scrubber().tree(payload, {})
    call = (request.get("resource"), request.get("method"))
    rest = {key: value for key, value in payload.items() if key != "ret"}
    return Scrubber().tree({**rest, "ret": _trace_ret(call, payload.get("ret"))}, {})
