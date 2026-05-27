# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Dotted-path glob matcher -- Initiative #805, Task #1072.

Tier-2 redaction rules (:class:`~meho_backplane.redaction.policy.Tier2Rule`)
declare which payload paths are free-text via glob patterns. The
engine walks the payload and asks "does this dotted path match any
of the rule's globs?"; this module is the matcher.

Extracted out of :mod:`.presidio` so the latter stays under the
code-quality file-size limit. The matcher is **pure and side-effect-
free** (one ``lru_cache`` on the regex compile is the only state),
which lets it be unit-tested in isolation and reused later if a
non-Presidio surface needs the same glob semantics.

Glob grammar (intentionally minimal -- two metacharacters cover the
operator-facing cases the parent initiative needs):

* ``*`` -- exactly one path segment. ``items.*.message`` matches
  ``items.0.message`` and ``items.abc.message``, but not
  ``items.0.nested.message`` (two segments) or ``items.message``
  (zero segments).
* ``**`` -- any depth, including zero segments. ``**.error.body``
  matches ``error.body``, ``a.error.body``, and ``a.b.c.error.body``.

Everything else (literal segment text, dots) is matched verbatim.
The compiled regex is anchored (``^`` ... ``$``) so a glob never
matches a prefix or suffix of a longer path.

A note on the tokenise step: we scan for ``**`` *before* ``*`` so
the longest-match rule applies; otherwise a glob ``"**"`` would be
parsed as two ``*`` tokens and the trailing-double-star case would
never fire.
"""

from __future__ import annotations

import functools
import re

__all__ = [
    "glob_to_regex",
    "path_matches",
]


@functools.lru_cache(maxsize=256)
def glob_to_regex(glob: str) -> re.Pattern[str]:
    """Compile a dotted-path glob to an anchored regex.

    Result is cached at module scope so repeated calls on the same
    glob (the common case -- one rule's ``fields`` is fixed for the
    lifetime of the policy) amortise the compile cost.
    """
    tokens = _tokenise(glob)
    parts: list[str] = []
    pending_double = False
    for tok in tokens:
        if tok == "**":
            pending_double = True
            continue
        if pending_double:
            if tok == ".":
                # ``**.`` -- consume but emit nothing; the next tok
                # decides what comes before.
                continue
            # ``**`` followed by a literal becomes ``(?:.*\.)?<lit>``
            # so ``**.foo`` matches ``foo`` and ``a.b.foo``.
            parts.append("(?:.*\\.)?")
            pending_double = False
        if tok == "*":
            parts.append("[^.]+")
        elif tok == ".":
            parts.append("\\.")
        else:
            parts.append(re.escape(tok))
    if pending_double:
        # Trailing ``**`` matches anything (including the empty
        # remainder if the path already ended).
        parts.append(".*")
    pattern = "^" + "".join(parts) + "$"
    return re.compile(pattern)


def path_matches(globs: tuple[str, ...], path: str) -> bool:
    """Return ``True`` when *path* matches any glob in *globs*."""
    return any(glob_to_regex(glob).match(path) for glob in globs)


def _tokenise(glob: str) -> list[str]:
    """Tokenise *glob* into ``**`` / ``*`` / ``.`` / literal segments.

    Hot path: ~50 ns per glob in practice; cached by
    :func:`glob_to_regex` so each glob is tokenised exactly once per
    process. The tokeniser handles ``**`` before ``*`` to keep the
    longest-match rule.
    """
    tokens: list[str] = []
    i = 0
    while i < len(glob):
        if glob.startswith("**", i):
            tokens.append("**")
            i += 2
            # Skip a following dot so ``**.foo`` doesn't compile to
            # ``(?:.*)\.foo`` requiring at least one dot upstream
            # (which would reject the bare leaf ``foo``).
            if i < len(glob) and glob[i] == ".":
                i += 1
                tokens.append(".")
        elif glob[i] == "*":
            tokens.append("*")
            i += 1
        elif glob[i] == ".":
            tokens.append(".")
            i += 1
        else:
            j = i
            while j < len(glob) and glob[j] not in "*.":
                j += 1
            tokens.append(glob[i:j])
            i = j
    return tokens
