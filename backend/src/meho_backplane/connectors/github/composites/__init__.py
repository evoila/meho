# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.github.composites -- gh-rest composites.

Side-effect import: this package's ``__init__`` queues
:func:`register_github_composite_operations` onto the lifespan-driven
registrar list via
:func:`~meho_backplane.operations.typed_register.register_typed_op_registrar`.

The chassis lifespan's
:func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
invokes every registered registrar in registration order after
:func:`~meho_backplane.connectors.registry._eager_import_connectors`
has walked every ``connectors/<product>/`` subpackage, so the
``endpoint_descriptor`` upsert for the T4 composite lands before any
dispatch can fire.

Layout mirrors the vmware-rest composites package: ``__init__`` wires
the registrar; ``_register.py`` carries per-composite registration
metadata; ``_read.py`` / ``_board.py`` / ``_sub_issues.py`` carry handler
implementations; ``_graphql.py`` carries the shared GraphQL transport
helper; ``schemas.py`` carries the JSON Schema 2020-12 parameter +
response contracts.

Scope (5 composites): ``gh.composite.pr_status_summary`` (T4 #1224,
read); and the #2081 board + sub-issue set -- ``gh.composite.project_view``
(read), ``gh.composite.project_item_add`` /
``gh.composite.project_item_set_field`` (Projects-v2 GraphQL writes), and
``gh.composite.sub_issue_add`` (sub-issue linkage REST write). Together
the #2081 set makes a board-complete ticket filable through MEHO without
a ``gh`` CLI fallback.
"""

from meho_backplane.connectors.github.composites._board import (
    project_item_add_composite,
    project_item_set_field_composite,
    project_view_composite,
)
from meho_backplane.connectors.github.composites._read import (
    pr_status_summary_composite,
)
from meho_backplane.connectors.github.composites._register import (
    register_github_composite_operations,
)
from meho_backplane.connectors.github.composites._sub_issues import (
    sub_issue_add_composite,
)
from meho_backplane.operations.typed_register import register_typed_op_registrar

# Queue the composite-op upsert onto the lifespan-driven registrar list.
# The lifespan calls ``run_typed_op_registrars`` after
# ``_eager_import_connectors`` so every connector subpackage has self-
# registered by the time the runner iterates.
register_typed_op_registrar(register_github_composite_operations)

__all__ = [
    "pr_status_summary_composite",
    "project_item_add_composite",
    "project_item_set_field_composite",
    "project_view_composite",
    "register_github_composite_operations",
    "sub_issue_add_composite",
]
