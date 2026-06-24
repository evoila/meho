# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared RFC6570 expression-operator vocabulary for ingested-op paths.

A single source of truth for the one RFC6570 (URI Template) detail two
otherwise-decoupled stages of the ingested-op pipeline must agree on:
the set of leading **expression operators** a path-template variable may
carry, and how the *bare* variable name is recovered from an
operator-bearing one.

* The **ingest parser** (:mod:`meho_backplane.operations.ingest.openapi`)
  reads a vendor spec's ``parameters`` and keys each one's JSON-Schema
  property on its name. For an ``in: path`` parameter whose declared
  ``name`` carries an operator -- e.g. VCF Operations-for-Logs declares
  ``{"name": "+path", "in": "path"}`` for the template ``/events/{+path}``
  -- the property must be keyed on the **bare** name (``path``).
* The **path renderer** (:func:`meho_backplane.operations._branches._substitute_path`)
  expands the template at dispatch time, stripping the operator to look
  the value up by the same bare name and choosing the encoding safe-set
  from the operator (reserved expansion for ``+`` / ``#``).

If those two stages used different operator sets the property key and the
lookup key would disagree and the op would be undispatchable (#2003 /
#2066). Sharing this leaf module -- which imports nothing beyond the
standard library, so neither the heavyweight dispatch branch nor the
dependency-free parser is coupled to the other -- makes that drift
impossible by construction.

The operator set is RFC6570 §2.2's expression operators restricted to the
ones that can sensibly head a single-segment path variable:
``+`` (reserved expansion, §3.2.3), ``#`` (fragment), ``.`` (label),
``/`` (path segment), ``;`` (path-style param), ``?`` / ``&`` (form-style
query). The reserved/query operators are not *expanded* specially by the
single-segment substituter, but they are recognised so the operator is
never mistaken for part of the variable name.
"""

from __future__ import annotations

__all__ = [
    "RFC6570_PATH_OPERATORS",
    "split_path_operator",
]

# The RFC6570 §2.2 expression operators recognised at the head of a
# single-segment path-template variable. Mirrored verbatim by the
# renderer's ``_PATH_VAR_RE`` character class so the parser and the
# renderer can never disagree on what counts as an operator.
RFC6570_PATH_OPERATORS = "+#./;?&"


def split_path_operator(name: str) -> tuple[str, str]:
    """Split a single leading RFC6570 operator off a path-variable name.

    Mirrors the renderer's ``_PATH_VAR_RE`` capture exactly: at most one
    leading character is consumed as an operator, and only when it is one
    of :data:`RFC6570_PATH_OPERATORS`. A second operator char, or an
    operator anywhere but the first position, stays part of the name.

    >>> split_path_operator("+path")
    ('+', 'path')
    >>> split_path_operator("path")
    ('', 'path')
    >>> split_path_operator("++weird")
    ('+', '+weird')
    >>> split_path_operator("path+")
    ('', 'path+')

    Returns ``(operator, bare_name)`` where ``operator`` is ``""`` when the
    name carries none.
    """
    if name and name[0] in RFC6570_PATH_OPERATORS:
        return name[0], name[1:]
    return "", name
