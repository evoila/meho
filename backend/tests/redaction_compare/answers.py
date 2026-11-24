# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Run the fixed inputs through each secret-hiding step.

Two outputs come from the same answers:

* ``snapshot_lines()`` gives one line per input: the input id and one
  sha256 checksum (``sum``) over the full answers of all steps the input
  runs on. ``scripts/ci/redaction_compare.py --write`` writes these lines to
  ``tests/snapshots/redaction_compare.jsonl``, and the snapshot test fails
  when the code's answers no longer match them. The file holds no lists of
  hidden and shown parts.
* ``records()`` gives one record per input and step: the planted parts that
  the answer no longer holds (``hidden``) and the ones it still holds
  (``shown``), a hash of the whole answer (``sha``), and a hash of the
  answer without its planted parts (``rest``). The CI job builds these
  records with ``redaction_compare.py --records``, for the base branch and
  for the pull request, each from its own code, and judges them against
  each other. They are never committed.

For ``rest``, every planted part (secret or plain) and every hiding marker
becomes one ``<hidden>`` token. So a change that only hides or shows a
planted part keeps ``rest`` the same, and any other change of the answer
does not. A plain part counts as shown only where it stands outside a hiding
marker. So ``--[REDACTED:password]`` in place of ``--password <value>``
hides the label ``password`` too, and the judge sees that the label is gone.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterator
from typing import Any

from meho_backplane.broadcast.events import scrub_broadcast_params
from meho_backplane.operations._secret_values import hide_secret_values
from meho_backplane.operations._vendor_text import redact_error_payload
from meho_backplane.redaction.command_line import hide_command_text, hide_shell_script
from meho_backplane.redaction.engine import redact
from meho_backplane.redaction.flight_recorder._content import scrub_content
from meho_backplane.redaction.resolver import get_default_policy
from tests.redaction_compare.inputs import CompareInput, Part, secret_inputs

_OP_ID = "compare.read"

#: The markers the steps put in place of a hidden value.
_MARKER = re.compile(r"\*\*\*REDACTED\*\*\*|\[(?:MEHO-)?REDACTED:[^\]]*\]")
_HIDDEN = "<hidden>"
#: Two or more hidden tokens split only by spaces: one value with a space may become one marker.
_HIDDEN_RUN = re.compile(rf"{_HIDDEN}(?:\s+{_HIDDEN})+")


def _dict_step(step: Callable[[dict[str, Any]], Any]) -> Callable[[Any], Any]:
    """A step that takes a dict: a bare value goes in as ``{"value": ...}`` and comes out bare."""

    def run(payload: Any) -> Any:
        if isinstance(payload, dict):
            return step(payload)
        return step({"value": payload})["value"]

    return run


STEPS: dict[str, Callable[[Any], Any]] = {
    "redact": lambda payload: redact(payload, get_default_policy()).redacted,
    "scrub_content": lambda payload: scrub_content(payload)[0],
    "scrub_broadcast_params": _dict_step(lambda params: scrub_broadcast_params(params)[0]),
    "redact_error_payload": redact_error_payload,
    "default_view": _dict_step(lambda params: hide_secret_values(_OP_ID, params)),
    "hide_command_text": lambda payload: hide_command_text(payload, None, shell=True)[0],
    "hide_shell_script": hide_shell_script,
}


def answer_text(answer: Any) -> str:
    """The answer as one stable JSON text."""
    return json.dumps(answer, sort_keys=True, ensure_ascii=False, default=str)


def rest_text(text: str, parts: list[str]) -> str:
    """*text* with each planted part value and each hiding marker as one ``<hidden>`` token."""
    for value in sorted(parts, key=len, reverse=True):
        text = text.replace(value, _HIDDEN)
    return _HIDDEN_RUN.sub(_HIDDEN, _MARKER.sub(_HIDDEN, text))


def _is_shown(part: Part, text: str, outside: str) -> bool:
    """A secret counts as shown anywhere in *text*, a plain part only outside a marker."""
    return part.value in (outside if part.pid.startswith("p") else text)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _fresh(payload: Any) -> Any:
    """A copy of the payload, so a step that changes it in place cannot touch the next step."""
    return payload if isinstance(payload, str) else json.loads(json.dumps(payload))


def record(item: CompareInput, path: str, text: str) -> dict[str, Any]:
    """The record of *item* on *path* when the step answered *text*."""
    outside = _MARKER.sub(_HIDDEN, text)
    return {
        "id": item.id,
        "path": path,
        "hidden": sorted(p.pid for p in item.parts if not _is_shown(p, text, outside)),
        "shown": sorted(p.pid for p in item.parts if _is_shown(p, text, outside)),
        "sha": _sha(text),
        "rest": _sha(rest_text(text, [p.value for p in item.parts if p.value])),
    }


def _answers(item: CompareInput) -> dict[str, str]:
    """The full answer text of each step that *item* runs on."""
    return {path: answer_text(STEPS[path](_fresh(item.payload))) for path in sorted(item.paths)}


def records(items: list[CompareInput] | None = None) -> Iterator[dict[str, Any]]:
    """One record per input and step, sorted by id and step."""
    for item in items if items is not None else secret_inputs():
        for path, text in _answers(item).items():
            yield record(item, path, text)


def checksum(item: CompareInput) -> str:
    """One sha256 over the full answers of all steps of *item* (a JSON object, sorted keys)."""
    return _sha(json.dumps(_answers(item), sort_keys=True, separators=(",", ":")))


def snapshot_lines(items: list[CompareInput] | None = None) -> list[str]:
    """The snapshot file as lines, sorted by input id: ``{"id": ..., "sum": ...}``."""
    chosen = items if items is not None else secret_inputs()
    return [
        json.dumps({"id": item.id, "sum": checksum(item)}, sort_keys=True, separators=(",", ":"))
        for item in sorted(chosen, key=lambda item: item.id)
    ]
