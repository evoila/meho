# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The op classes the GitHub connector declares for its own operations.

See :mod:`meho_backplane.op_classes`. The five ticket writes are ``safe``
(they need no approval), but they are writes, not reads. Their class is
``write`` (for the audit row and the feed), and they are marked as safe
writes, which keeps them out of every place that treats ``safe`` as a read:
health checks refuse them, and a satellite runner never takes them as a
read. See ``docs/decisions/2026-11-02-github-ticket-writes-are-safe.md``.
"""

from meho_backplane.op_classes import declare_op_classes, declare_safe_writes

#: The five GitHub ticket writes that are ``safe`` and need no approval.
TICKET_WRITE_OP_IDS: tuple[str, ...] = (
    "gh.composite.issue_create",
    "gh.composite.issue_comment",
    "gh.composite.sub_issue_add",
    "gh.composite.project_item_add",
    "gh.composite.project_item_set_field",
)

declare_op_classes(write=TICKET_WRITE_OP_IDS)
declare_safe_writes(TICKET_WRITE_OP_IDS)
