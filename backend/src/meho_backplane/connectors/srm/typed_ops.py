# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed-op metadata + registrar for :class:`SrmConnector`.

Two reads are defined here, on SRM 8.8, Live Site Recovery 9.0 and VCF
9.1 Protection and Recovery:

* ``srm.about`` -- ``GET /api/rest/srm/v2/server/info``: the server's
  name, status, version, build and paired vCenter;
* ``srm.api_file.get`` -- ``GET`` one of the two API files the appliance
  serves; the body lives in :mod:`meho_backplane.connectors.vmware_shared.dr_api_file`.

The pairing, recovery plan, protection group, issue and history reads are
defined in :mod:`meho_backplane.connectors.srm.recovery_ops` and joined
into :data:`SRM_TYPED_OPS` here.

The dataclass + tuple + registrar shape mirrors
:mod:`meho_backplane.connectors.hcx.typed_ops`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from meho_backplane.connectors.srm._op import SrmTypedOp
from meho_backplane.connectors.srm.recovery_ops import (
    RECOVERY_TYPED_OPS,
    RECOVERY_WHEN_TO_USE_BY_GROUP,
)
from meho_backplane.connectors.vmware_shared.dr_rest_schemas import (
    API_FILE_NEXT_STEP,
    API_FILE_OUTPUT_SHAPE,
    API_FILE_RESULT_SCALARS,
    SERVER_INFO_OUTPUT_SHAPE,
    api_file_response_schema,
    server_info_response_schema,
)

if TYPE_CHECKING:
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "SRM_TYPED_OPS",
    "SRM_TYPED_WHEN_TO_USE_BY_GROUP",
    "SrmTypedOp",
    "register_srm_typed_operations",
]

_GROUP_SERVER = "srm-server"
_GROUP_API_DOCS = "srm-api-docs"

#: Curated ``when_to_use`` text per group. Says what the group holds; it
#: makes no claim about the safety of the whole group.
SRM_TYPED_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    _GROUP_SERVER: (
        "Use to see which Site Recovery Manager server a target is: its name, "
        "status, version, build and the vCenter it is paired with, plus a short "
        "view of its certificates (srm.about). One read."
    ),
    _GROUP_API_DOCS: (
        "Use to read an API description file that a running Site Recovery Manager "
        "appliance serves: the Site Recovery API file or the appliance setup API "
        "file (srm.api_file.get). The read returns a checked copy of the file with "
        "its size, SHA-256 and the SRM version, with the text in parts behind a "
        "result handle, so a vendor API file can be pinned and compared. One read."
    ),
    **RECOVERY_WHEN_TO_USE_BY_GROUP,
}

_ABOUT = SrmTypedOp(
    op_id="srm.about",
    handler_attr="about",
    summary="Read the Site Recovery Manager server's name, status, version and build.",
    description=(
        "Reads GET /api/rest/srm/v2/server/info: server id, name, status, version, "
        "build, the paired vCenter and a short view of the certificates. MEHO uses "
        "the same call to detect the SRM version."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
        "examples": [{}],
    },
    response_schema=server_info_response_schema("SRM", "the vCenter the server is paired with"),
    group_key=_GROUP_SERVER,
    tags=("read-only", "srm", "live-recovery", "probe"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call first on a Site Recovery Manager target, to confirm which server it "
            "is and which version and build it runs (8.8, 9.0 or 9.1), and whether its "
            "status is OK."
        ),
        "output_shape": SERVER_INFO_OUTPUT_SHAPE,
        "next_step": (
            "If server_status is WARNING or ERROR, or certificate_warnings is not empty, "
            "report it. To read the vendor API file of this server, call srm.api_file.get."
        ),
    },
)

_API_FILE_GET = SrmTypedOp(
    op_id="srm.api_file.get",
    handler_attr="api_file_get",
    summary="Read one API description file the Site Recovery Manager appliance serves.",
    description=(
        "Reads the Site Recovery API file or the appliance setup API file from the "
        "appliance and returns a checked copy: secret values are replaced. The answer "
        "has the path, HTTP status, content type, size, SHA-256 and the SRM version. "
        "The text comes back in parts behind a result handle."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "file": {
                "type": "string",
                "enum": ["srm", "configure"],
                "description": (
                    "Which file to read. srm: the Site Recovery API "
                    "(/api/rest/srm/v2/open-api.yaml). configure: the appliance setup "
                    "API (/api/rest/configure/v2/open-api.yaml)."
                ),
            },
        },
        "required": ["file"],
        "additionalProperties": False,
        "examples": [{"file": "srm"}],
    },
    response_schema=api_file_response_schema("SRM"),
    group_key=_GROUP_API_DOCS,
    tags=("read-only", "srm", "live-recovery", "api-docs", "documentation"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to read the API file of a Site Recovery Manager appliance, for "
            "example to pin the vendor API file of an SRM version or to check which "
            "API calls a version serves."
        ),
        "output_shape": API_FILE_OUTPUT_SHAPE,
        "next_step": API_FILE_NEXT_STEP,
        "result_scalars": {"keys": list(API_FILE_RESULT_SCALARS)},
    },
)

SRM_TYPED_OPS: tuple[SrmTypedOp, ...] = (_ABOUT, _API_FILE_GET, *RECOVERY_TYPED_OPS)


async def register_srm_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert every op in :data:`SRM_TYPED_OPS` into ``endpoint_descriptor``.

    Queued onto the lifespan-driven registrar list in this package's
    ``__init__``; the runner passes the process-wide embedding service
    (or a test stub), which is forwarded to ``register_typed_operation``.
    """
    from meho_backplane.connectors.srm.connector import SrmConnector
    from meho_backplane.operations.typed_register import register_typed_operation

    for op in SRM_TYPED_OPS:
        handler = getattr(SrmConnector, op.handler_attr, None)
        if handler is None:
            raise AttributeError(
                f"SrmConnector typed op {op.op_id!r} declares "
                f"handler_attr={op.handler_attr!r} but the class has no such attribute"
            )
        when_to_use = (
            None if op.group_key is None else SRM_TYPED_WHEN_TO_USE_BY_GROUP.get(op.group_key)
        )
        if op.group_key is not None and when_to_use is None:
            raise ValueError(
                f"SrmConnector typed op {op.op_id!r} declares group_key={op.group_key!r} "
                "but no curated when_to_use exists for that key."
            )
        await register_typed_operation(
            product=SrmConnector.product,
            version=SrmConnector.version,
            impl_id=SrmConnector.impl_id,
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
