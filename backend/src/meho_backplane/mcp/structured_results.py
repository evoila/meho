# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Structured-only tool results for one MCP session (#4716).

A tool that declares an ``outputSchema`` sends its result twice: as JSON
text in ``content[0].text`` and as the ``structuredContent`` object (MCP
2025-06-18, Tools / Structured Content). The text copy is there for clients
that do not read ``structuredContent``.

A client that reads ``structuredContent`` can ask for the result once: it
adds ``?results=structured`` to the MCP URL (:func:`parse_tool_results`).
For that session, a successful result of a tool with an ``outputSchema``
carries one short line in ``content`` instead of the full JSON text
(:data:`~meho_backplane.operations.delivery_budget.STRUCTURED_ONLY_TEXT`).

The choice is off by default. Without the URL parameter, nothing changes.
These results keep the full text in every session:

* a tool without an ``outputSchema``: it has no ``structuredContent``;
* a result with ``isError: true``: some clients show the model only the
  text of an error result.

There is no server-wide switch on purpose. One MEHO serves many clients,
and a client that reads only the text would lose every result.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "STRUCTURED_RESULTS_VALUE",
    "UnknownToolResultsError",
    "parse_tool_results",
]

#: The one value of the ``results`` URL parameter.
STRUCTURED_RESULTS_VALUE: Final = "structured"

#: An error text repeats at most three values from the URL, each cut to
#: this many characters.
_MAX_SHOWN_CHARS: Final[int] = 64


class UnknownToolResultsError(ValueError):
    """The ``results`` URL parameter has a value other than ``structured``."""

    def __init__(self, values: list[str]) -> None:
        self.values = values
        shown = ", ".join(repr(value[:_MAX_SHOWN_CHARS]) for value in values[:3])
        super().__init__(
            f"unknown tool results {shown}. Use results={STRUCTURED_RESULTS_VALUE} to get "
            "each result once, in structuredContent, or leave it out to get the text too."
        )


def parse_tool_results(values: list[str]) -> bool:
    """Return True when the ``results`` URL parameter values ask for structured-only results.

    The value is not case-sensitive. No value, or only blank values, means
    the default (text and ``structuredContent``). Any other value raises
    :class:`UnknownToolResultsError`, so a typo is not quietly ignored.
    """
    names = sorted({value.strip().lower() for value in values if value.strip()})
    if not names:
        return False
    if names != [STRUCTURED_RESULTS_VALUE]:
        raise UnknownToolResultsError(names)
    return True
