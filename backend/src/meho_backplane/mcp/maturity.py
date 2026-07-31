# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Feature-maturity band for ``initialize.instructions`` (#2675).

The MCP ``initialize`` response is the fresh agent session's first —
and sometimes only — guidance surface (2026-07-21 grounding review §4).
This module renders the one-paragraph maturity summary the #2664
program requires there: which features are beta, which are
experimental, and how the per-tool ``[beta]`` / ``[experimental]``
description prefixes relate to it.

The band is derived **entirely** from
:data:`~meho_backplane.features.FEATURE_MATURITY` at import time —
the registry is static module-level data, so the text never changes
within a process lifetime and building it once is free. Reclassifying
a feature (the v0.28 post-eval round) changes this band with zero
edits here.

Rendered as a delimited band in the same shape as
:data:`~meho_backplane.conventions.preamble.BROADCAST_DISCIPLINE_BAND`
(MEHO-authored trusted guidance — no guard prefix needed; delimiters
exist for band separation and grep-friendliness). It is appended by
:func:`meho_backplane.mcp.server._initialize` *after* the assembled
tenant preamble rather than inside
:func:`~meho_backplane.conventions.preamble.assemble_preamble` because
the label contract is an MCP-surface concern (#2675), not a tenant-
conventions concern — the conventions write-path feedback consumers of
the preamble assembler must not see it.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.features import FEATURE_MATURITY

__all__ = [
    "FEATURE_MATURITY_BAND",
    "MATURITY_BLOCK_END",
    "MATURITY_BLOCK_START",
]

#: Opening delimiter for the maturity band. Same positional-envelope
#: shape as the broadcast-discipline band's delimiters.
MATURITY_BLOCK_START: Final[str] = "<<FEATURE_MATURITY>>"

#: Closing delimiter for the maturity band. Pairs with
#: :data:`MATURITY_BLOCK_START`.
MATURITY_BLOCK_END: Final[str] = "<<END_FEATURE_MATURITY>>"


def _build_band() -> str:
    """Render the maturity band from the registry.

    Lists exactly the registry's non-GA features, tier by tier (sorted
    for deterministic output — the registry is a plain dict and its
    ordering is an editing artifact, not a contract). Returns ``""``
    when every feature is GA so the ``initialize`` band-join drops the
    band cleanly — the same empty-band convention the preamble
    assembler uses.

    Deliberately renders feature *keys only* — no ``target_ga`` and no
    ``tracking`` URLs. Beyond compactness (the band rides in every
    session's token budget), the tracking URLs name the
    ``evoila/meho`` tracker, and ``initialize.instructions`` must stay
    free of repo-identity tokens per the G0.13-T7 (#1137) forbidden-
    token contract pinned in
    :mod:`tests.test_mcp_initialize_instructions`.
    """
    beta = sorted(key for key, info in FEATURE_MATURITY.items() if info["maturity"] == "beta")
    experimental = sorted(
        key for key, info in FEATURE_MATURITY.items() if info["maturity"] == "experimental"
    )
    if not beta and not experimental:
        return ""
    lines = [
        MATURITY_BLOCK_START,
        "## Feature maturity",
        "",
        "MEHO features carry maturity tiers. Tools whose description "
        "starts with [beta] or [experimental] belong to a pre-GA "
        "feature; unprefixed tools are GA and carry the stability "
        "promise.",
    ]
    if beta:
        lines += [
            "",
            "Beta (works end-to-end, gaps known and tracked; may change "
            "with deprecation notice): " + ", ".join(beta) + ".",
        ]
    if experimental:
        lines += [
            "",
            "Experimental (may change or vanish; outside the 1.0 "
            "stability promise): " + ", ".join(experimental) + ".",
        ]
    lines.append(MATURITY_BLOCK_END)
    return "\n".join(lines)


#: The maturity band appended to every ``initialize.instructions``
#: payload. Static per process — see module docstring.
FEATURE_MATURITY_BAND: Final[str] = _build_band()
