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

Layout: ``__init__`` wires
the registrar; ``_register.py`` carries per-composite registration
metadata; ``_read.py`` / ``_board.py`` / ``_sub_issues.py`` carry handler
implementations; ``_repos.py`` carries the repository write;
``_graphql.py`` carries the shared GraphQL transport helper;
``schemas.py`` carries the JSON Schema 2020-12 parameter + response
contracts.

Scope (10 composites): ``gh.composite.pr_status_summary`` (T4 #1224,
read); ``gh.composite.repo_view`` (one repository, read);
``gh.composite.repo_create`` (one repository in an organization, write,
waits for approval); and the #2081
board + sub-issue set -- ``gh.composite.project_view``
(read), ``gh.composite.project_item_add`` /
``gh.composite.project_item_set_field`` (Projects-v2 GraphQL writes), and
``gh.composite.sub_issue_add`` (sub-issue linkage REST write). Together
the #2081 set makes a board-complete ticket filable through MEHO without
a ``gh`` CLI fallback. The ``git`` group (``gh.composite.git_ref_view``,
``gh.composite.git_tree_view``, ``gh.composite.branch_commit_files``)
reads a branch and a file tree and commits files to a branch in one
approved step; see :mod:`._git`.
"""

from meho_backplane.connectors.github.composites._board import (
    project_item_add_composite,
    project_item_set_field_composite,
    project_view_composite,
)
from meho_backplane.connectors.github.composites._git import (
    branch_commit_files_composite,
    git_ref_view_composite,
    git_tree_view_composite,
)
from meho_backplane.connectors.github.composites._read import (
    pr_status_summary_composite,
    repo_view_composite,
)
from meho_backplane.connectors.github.composites._register import (
    register_github_composite_operations,
)
from meho_backplane.connectors.github.composites._repos import (
    repo_create_composite,
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

# Side-effect import: registers the park-time preview of the git write.
import meho_backplane.connectors.github.composites._git_preview  # noqa: E402,F401

__all__ = [
    "branch_commit_files_composite",
    "git_ref_view_composite",
    "git_tree_view_composite",
    "pr_status_summary_composite",
    "project_item_add_composite",
    "project_item_set_field_composite",
    "project_view_composite",
    "register_github_composite_operations",
    "repo_create_composite",
    "repo_view_composite",
    "sub_issue_add_composite",
]
