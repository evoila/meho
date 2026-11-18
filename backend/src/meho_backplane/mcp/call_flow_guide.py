# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A short guide to the call flow in the session-start text (#4715).

When a session starts, MEHO sends the MCP ``initialize.instructions``
text. With the ``MCP_CALL_FLOW_GUIDE`` switch on, that text starts with
this guide. It explains once, in order, how to find and call an
operation: connectors and targets, groups, search and the exact
contract, preview, the call, result handles, and approval. An approved
call with ``reveal_secret: true`` does not run by itself; the approval
step says how the person who asked gets the value, by the
``MCP_REVEAL_FETCH`` switch.

The switch is off by default. With it off, the session-start text does
not change.

The text is the same for every client. It names only tools the session
can see: a step whose tools are not on the session's tool list is left
out. A session without ``call_operation`` gets no guide, because it
cannot call an operation.

A client may ignore ``instructions``. So each tool description still
carries the rules that a correct call needs. This guide adds the
overview; it does not replace those rules.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Final

__all__ = [
    "CALL_FLOW_BLOCK_END",
    "CALL_FLOW_BLOCK_START",
    "CALL_FLOW_STEPS",
    "call_flow_guide",
]

#: Opening delimiter of the guide. Same shape as the other MEHO bands.
CALL_FLOW_BLOCK_START: Final[str] = "<<CALL_FLOW>>"

#: Closing delimiter. Pairs with :data:`CALL_FLOW_BLOCK_START`.
CALL_FLOW_BLOCK_END: Final[str] = "<<END_CALL_FLOW>>"

#: The tool without which the guide is left out.
_CALL_TOOL: Final[str] = "call_operation"

#: The approval step. :func:`call_flow_guide` adds the reveal note to it.
_APPROVAL_STEP: Final[str] = (
    "Some calls wait for a person's approval. Then `status` is "
    "`awaiting_approval`: nothing has run yet. Tell the user and give "
    "them `extras.approval_request_id`. Do not call again or try another "
    "way. MEHO runs the call once a person approves it."
)

#: The steps, in order. Each step names the tools it needs; a step is left
#: out when one of them is not on the session's tool list.
CALL_FLOW_STEPS: Final[tuple[tuple[frozenset[str], str], ...]] = (
    (
        frozenset({"meho_connector_list", "list_targets"}),
        "`meho_connector_list` shows the connectors. `list_targets` shows the "
        "targets: the systems you can reach. Use a target's name, not its id.",
    ),
    (
        frozenset({"list_operation_groups"}),
        "`list_operation_groups` shows the groups of a connector and when to use each one.",
    ),
    (
        frozenset({"search_operations"}),
        "`search_operations` with a short `query` finds operations; add "
        "`group` to search one group. Then call it again with the `op_id` you "
        "picked to get the exact contract: the parameters and whether a "
        "target is needed. Read it before you call.",
    ),
    (
        frozenset({"preview_operation"}),
        "Before a change, call `preview_operation` with the same arguments. "
        "It shows what the call would do. Nothing runs.",
    ),
    (
        frozenset({_CALL_TOOL}),
        "`call_operation` takes `connector_id`, `op_id`, `target` and "
        "`params`. Use only the parameters of the contract. If the answer "
        "says `invalid_params`, fix `params` before you call again.",
    ),
    (
        frozenset({"result_query"}),
        "A large result comes back as a `handle`, a link to the stored "
        "result. Read it with `result_query`. Do not run the call again for "
        "more rows.",
    ),
    (frozenset({_CALL_TOOL}), _APPROVAL_STEP),
)

#: Added to the approval step: an approved ``reveal_secret`` call does not
#: run by itself. With ``MCP_REVEAL_FETCH`` on, the person who asked gets the
#: value with one more ``call_operation``; with it off, outside MCP.
_REVEAL_WITH_FETCH: Final[str] = (
    " A `reveal_secret: true` call differs: approval runs nothing. Then "
    "call `call_operation` again with only `connector_id`, `op_id` and "
    "`approval_request_id` to get the value."
)
_REVEAL_WITHOUT_FETCH: Final[str] = (
    " A `reveal_secret: true` call differs: approval runs nothing. The "
    "person who asked gets the value in the MEHO console or command line."
)


def call_flow_guide(tool_names: Collection[str], *, reveal_fetch: bool = False) -> str:
    """Return the guide for a session that sees *tool_names*.

    *reveal_fetch* is the ``MCP_REVEAL_FETCH`` switch. It picks how the
    approval step says to get the value of an approved ``reveal_secret``
    call.

    Returns ``""`` when ``call_operation`` is not in *tool_names*, so the
    band join in ``_session_instructions`` drops it cleanly.
    """
    if _CALL_TOOL not in tool_names:
        return ""
    reveal_note = _REVEAL_WITH_FETCH if reveal_fetch else _REVEAL_WITHOUT_FETCH
    steps = [
        text + reveal_note if text == _APPROVAL_STEP else text
        for tools, text in CALL_FLOW_STEPS
        if tools <= set(tool_names)
    ]
    lines = [
        CALL_FLOW_BLOCK_START,
        "## How to find and call an operation",
        "",
        "Do these steps in order. Each tool description has the full rules.",
        "",
    ]
    lines += [f"{number}. {text}" for number, text in enumerate(steps, start=1)]
    lines.append(CALL_FLOW_BLOCK_END)
    return "\n".join(lines)
