# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The written guidance block of an exact-op contract (#4757).

Connector authors write advice for the AI in an operation's
``llm_instructions``: when to call it, what must be true first, hints
for the parameters, what comes back, and what to do next. This module
picks those prose keys out and keeps them small, so the exact-op
contract of ``search_operations`` can carry them.

* Only the keys in :data:`GUIDANCE_KEYS` are read. Machine keys
  (``pagination_hint``, ``result_*``, ...) are not guidance and stay out.
* Each text is cut to :data:`GUIDANCE_VALUE_MAX_CHARS`, and the whole
  block to :data:`GUIDANCE_MAX_CHARS`. A cut text ends with
  :data:`CUT_MARK`. When the total is used up, the later keys are left
  out.
* ``parameter_hints`` is a mapping of parameter name to hint text. Each
  hint is cut the same way. The hint and the parameter name both count
  toward the total.
* A value of the wrong type or an empty text is skipped. When nothing is
  left, the result is ``None`` and the contract has no block.

The switch ``OPERATION_CONTRACT_GUIDANCE`` (off by default) decides
whether the contract carries the block at all; see
:func:`~meho_backplane.operations._contract.build_operation_contract`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

__all__ = [
    "CUT_MARK",
    "GUIDANCE_KEYS",
    "GUIDANCE_MAX_CHARS",
    "GUIDANCE_VALUE_MAX_CHARS",
    "bounded_guidance",
]

#: The prose keys of ``llm_instructions``, in the order they are kept
#: when the total size runs out. ``when_to_use`` is the older name of
#: ``when_to_call``; both are passed on as written.
GUIDANCE_KEYS: Final[tuple[str, ...]] = (
    "when_to_call",
    "when_to_use",
    "preconditions",
    "parameter_hints",
    "output_shape",
    "next_step",
)

#: One text (or one parameter hint) is cut to at most this many characters.
GUIDANCE_VALUE_MAX_CHARS: Final[int] = 1000

#: All texts of one block together are at most this many characters.
GUIDANCE_MAX_CHARS: Final[int] = 3000

#: The last character of a text that was cut.
CUT_MARK: Final[str] = "…"


def _cut(text: str, limit: int) -> str:
    """Return *text* cut to *limit* characters, ending with :data:`CUT_MARK` when cut."""
    if len(text) <= limit:
        return text
    return text[: limit - len(CUT_MARK)] + CUT_MARK


class _Budget:
    """The characters still free in one guidance block."""

    def __init__(self) -> None:
        self.left = GUIDANCE_MAX_CHARS

    def take(self, value: object, *, label: str = "") -> str | None:
        """Return *value* cut to fit, or ``None`` when it is not usable text or no room is left.

        *label* (a parameter name) counts toward the total too.
        """
        if not isinstance(value, str):
            return None
        text = value.strip()
        room = self.left - len(label)
        if not text or room <= len(CUT_MARK):
            return None
        text = _cut(text, min(GUIDANCE_VALUE_MAX_CHARS, room))
        self.left = room - len(text)
        return text


def _hints(value: object, budget: _Budget) -> dict[str, str] | None:
    """Return the usable parameter hints of *value*, cut to fit."""
    if not isinstance(value, Mapping):
        return None
    hints: dict[str, str] = {}
    for name, hint in value.items():
        if not isinstance(name, str):
            continue
        text = budget.take(hint, label=name)
        if text is not None:
            hints[name] = text
    return hints or None


def bounded_guidance(llm_instructions: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return the size-limited prose guidance of one operation, or ``None``.

    See the module docstring for which keys are read and how they are cut.
    """
    if not llm_instructions:
        return None
    budget = _Budget()
    guidance: dict[str, Any] = {}
    for key in GUIDANCE_KEYS:
        value = llm_instructions.get(key)
        kept = _hints(value, budget) if key == "parameter_hints" else budget.take(value)
        if kept is not None:
            guidance[key] = kept
    return guidance or None
