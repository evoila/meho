# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed-op metadata + registrar for :class:`SaltConnector`.

Six reads on Aria Automation Config 8.18 and VCF 9.1 Salt, each one or
more RaaS RPC calls (bodies in :mod:`meho_backplane.connectors.salt.reads`):

* ``salt.about`` -- ``api.get_versions`` and ``master.get_master_presence``;
* ``salt.minion.list`` -- ``minions.get_minion_key_state``,
  ``minions.get_minion_presence`` and ``minions.get_minion_cache``;
* ``salt.key.list`` -- ``minions.get_minion_key_state``;
* ``salt.job.list`` -- ``cmd.get_cmds`` (job runs with state, user, start);
* ``salt.job.result`` -- ``ret.get_returns`` for one ``jid``;
* ``salt.target.list`` -- ``tgt.get_target_group``.

The dataclass + tuple + registrar shape mirrors
:mod:`meho_backplane.connectors.srm.typed_ops`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from meho_backplane.connectors.salt._op_common import (
    GROUP_JOBS,
    GROUP_MINIONS,
    GROUP_SERVER,
    JOB_STATES,
    KEY_STATES,
    SaltTypedOp,
)
from meho_backplane.connectors.salt._ops_jobs import JOB_LIST_OP, JOB_RESULT_OP
from meho_backplane.connectors.salt._ops_minions import (
    ABOUT_OP,
    KEY_LIST_OP,
    MINION_LIST_OP,
    TARGET_LIST_OP,
)

if TYPE_CHECKING:
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "JOB_STATES",
    "KEY_STATES",
    "SALT_TYPED_OPS",
    "SALT_TYPED_WHEN_TO_USE_BY_GROUP",
    "SaltTypedOp",
    "register_salt_typed_operations",
]

#: Curated ``when_to_use`` text per group. Says what the group holds; it
#: makes no claim about the safety of the whole group.
SALT_TYPED_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    GROUP_SERVER: (
        "Use to see which Salt server (RaaS of Aria Automation Config or VCF Salt) a "
        "target is: its version, the versions of its parts, and the Salt masters it "
        "knows with their status (salt.about). One read."
    ),
    GROUP_MINIONS: (
        "Use to see the Salt minions (the managed machines): each minion's key state, "
        "master, presence, last contact and OS (salt.minion.list), the minion keys by "
        "state (salt.key.list), and the saved targets, the minion groups that jobs run "
        "on (salt.target.list). Three reads."
    ),
    GROUP_JOBS: (
        "Use to see Salt job runs: recent runs with function, target, state, user and "
        "start time (salt.job.list), and the result of one run per minion, with the "
        "failed states named (salt.job.result). Two reads."
    ),
}

SALT_TYPED_OPS: tuple[SaltTypedOp, ...] = (
    ABOUT_OP,
    MINION_LIST_OP,
    KEY_LIST_OP,
    JOB_LIST_OP,
    JOB_RESULT_OP,
    TARGET_LIST_OP,
)


async def register_salt_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert every op in :data:`SALT_TYPED_OPS` into ``endpoint_descriptor``."""
    from meho_backplane.connectors.salt.connector import SaltConnector
    from meho_backplane.operations.typed_register import register_typed_operation

    for op in SALT_TYPED_OPS:
        handler = getattr(SaltConnector, op.handler_attr, None)
        if handler is None:
            raise AttributeError(
                f"SaltConnector typed op {op.op_id!r} declares "
                f"handler_attr={op.handler_attr!r} but the class has no such attribute"
            )
        when_to_use = (
            None if op.group_key is None else SALT_TYPED_WHEN_TO_USE_BY_GROUP.get(op.group_key)
        )
        if op.group_key is not None and when_to_use is None:
            raise ValueError(
                f"SaltConnector typed op {op.op_id!r} declares group_key={op.group_key!r} "
                "but no curated when_to_use exists for that key."
            )
        await register_typed_operation(
            product=SaltConnector.product,
            version=SaltConnector.version,
            impl_id=SaltConnector.impl_id,
            op_id=op.op_id,
            handler=handler,
            summary=op.summary,
            description=op.description,
            parameter_schema=op.parameter_schema,
            response_schema=op.response_schema,
            group_key=op.group_key,
            when_to_use=when_to_use,
            tags=list(op.tags),
            safety_level=op.safety_level,
            requires_approval=op.requires_approval,
            llm_instructions=op.llm_instructions,
            embedding_service=embedding_service,
        )
