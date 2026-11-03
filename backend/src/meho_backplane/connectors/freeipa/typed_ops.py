# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Operation table, group texts and registrar of the FreeIPA connector.

The connector has typed operations only: FreeIPA has no OpenAPI description.
All seventeen operations read (``safe``). They are registered at start-up with
``source_kind="typed"``, so they work on a fresh install with nothing to
ingest. Their bodies live in :mod:`meho_backplane.connectors.freeipa.reads`,
their metadata in :mod:`.ops_directory` and :mod:`.ops_access`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from meho_backplane.connectors.freeipa._op_common import (
    GROUP_ACCESS,
    GROUP_HOSTS,
    GROUP_IDENTITY,
    GROUP_SERVER,
    FreeipaTypedOp,
)
from meho_backplane.connectors.freeipa.ops_access import ACCESS_OPS
from meho_backplane.connectors.freeipa.ops_directory import DIRECTORY_OPS

if TYPE_CHECKING:
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "FREEIPA_TYPED_OPS",
    "FREEIPA_TYPED_WHEN_TO_USE_BY_GROUP",
    "FreeipaTypedOp",
    "register_freeipa_typed_operations",
]

_log = structlog.get_logger(__name__)

#: The text of each group. The AI reads it through ``list_operation_groups``
#: to choose where to search.
FREEIPA_TYPED_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    GROUP_SERVER: (
        "Use to check a Red Hat Identity Management (FreeIPA) server: that MEHO can log in, "
        "the server version and the API version (freeipa.about). One read."
    ),
    GROUP_HOSTS: (
        "Use to read the hosts and host groups of a FreeIPA domain: find hosts, list the hosts "
        "of one host group (freeipa.host.list with hostgroup), and read one host or host group "
        "with its members. Four reads."
    ),
    GROUP_IDENTITY: (
        "Use to read the users, user groups and roles of a FreeIPA domain, and its password "
        "policies: find users, read one user with its groups, read a group's or a role's "
        "members, read a group's or the global password policy. Seven reads."
    ),
    GROUP_ACCESS: (
        "Use to read who may do what on which host in a FreeIPA domain: sudo rules, access "
        "rules (HBAC, including the default rule allow_all), and freeipa.hbac.test, which "
        "answers whether one user may use one service (for example sshd) on one host. "
        "Five reads."
    ),
}

#: Every FreeIPA typed operation. All read; none needs approval.
FREEIPA_TYPED_OPS: tuple[FreeipaTypedOp, ...] = (*DIRECTORY_OPS, *ACCESS_OPS)


async def register_freeipa_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Store every operation of :data:`FREEIPA_TYPED_OPS` in ``endpoint_descriptor``.

    Runs at start-up after every connector package was imported. Safe to run
    twice: an unchanged operation is not embedded again.
    """
    # Imported here: the operations package loads the embedding model, which
    # connector unit tests should not pay for.
    from meho_backplane.connectors.freeipa.connector import FreeipaConnector
    from meho_backplane.operations.typed_register import register_typed_operation

    for op in FREEIPA_TYPED_OPS:
        handler = getattr(FreeipaConnector, op.handler_attr, None)
        if handler is None:
            raise AttributeError(
                f"FreeIPA typed op {op.op_id!r} names handler_attr={op.handler_attr!r}, "
                "but FreeipaConnector has no such method"
            )
        when_to_use = FREEIPA_TYPED_WHEN_TO_USE_BY_GROUP.get(op.group_key)
        if when_to_use is None:
            raise ValueError(
                f"FreeIPA typed op {op.op_id!r} names group_key={op.group_key!r}, which has "
                "no text in FREEIPA_TYPED_WHEN_TO_USE_BY_GROUP"
            )
        await register_typed_operation(
            product=FreeipaConnector.product,
            version=FreeipaConnector.version,
            impl_id=FreeipaConnector.impl_id,
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
    _log.info(
        "freeipa_typed_operations_registered",
        count=len(FREEIPA_TYPED_OPS),
        product=FreeipaConnector.product,
        version=FreeipaConnector.version,
        impl_id=FreeipaConnector.impl_id,
    )
