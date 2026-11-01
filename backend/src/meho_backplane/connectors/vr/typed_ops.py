# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed-op metadata + registrar for :class:`VrConnector`.

Seven reads, on vSphere Replication 8.8 and 9.0 and VCF 9.1 Protection
and Recovery:

* ``vr.about`` -- ``GET /api/rest/vr/v2/info``: the server's name,
  status, version, build and local vCenter;
* ``vr.api_file.get`` -- ``GET`` one of the two API files the appliance
  serves; the body lives in :mod:`meho_backplane.connectors.vmware_shared.dr_api_file`;
* ``vr.pairing.list``, ``vr.replication.list``, ``vr.replication.issues``,
  ``vr.vm.check`` and ``vr.task.get`` -- metadata in
  :mod:`meho_backplane.connectors.vr.read_ops`, bodies in
  :mod:`meho_backplane.connectors.vr.reads`.

The dataclass + tuple + registrar shape mirrors
:mod:`meho_backplane.connectors.srm.typed_ops`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from meho_backplane.connectors.vmware_shared.dr_rest_schemas import (
    API_FILE_NEXT_STEP,
    API_FILE_OUTPUT_SHAPE,
    API_FILE_RESULT_SCALARS,
    SERVER_INFO_OUTPUT_SHAPE,
    api_file_response_schema,
    server_info_response_schema,
)
from meho_backplane.connectors.vr.op_types import GROUP_API_DOCS, GROUP_SERVER, VrTypedOp
from meho_backplane.connectors.vr.read_ops import VR_READ_OPS, VR_READ_WHEN_TO_USE_BY_GROUP

if TYPE_CHECKING:
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "VR_TYPED_OPS",
    "VR_TYPED_WHEN_TO_USE_BY_GROUP",
    "VrTypedOp",
    "register_vr_typed_operations",
]

#: Curated ``when_to_use`` text per group. Says what the group holds; it
#: makes no claim about the safety of the whole group.
VR_TYPED_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    GROUP_SERVER: (
        "Use to see which vSphere Replication server a target is: its name, status, "
        "version, build and the vCenter it is registered with, plus a short view of "
        "its certificates (vr.about). One read."
    ),
    GROUP_API_DOCS: (
        "Use to read an API description file that a running vSphere Replication "
        "appliance serves: the vSphere Replication API file or the appliance setup "
        "API file (vr.api_file.get). The read returns a checked copy of the file with "
        "its size, SHA-256 and the vSphere Replication version, with the text in parts "
        "behind a result handle, so a vendor API file can be pinned and compared. "
        "One read."
    ),
    **VR_READ_WHEN_TO_USE_BY_GROUP,
}

_ABOUT = VrTypedOp(
    op_id="vr.about",
    handler_attr="about",
    summary="Read the vSphere Replication server's name, status, version and build.",
    description=(
        "Reads GET /api/rest/vr/v2/info: server id, name, status, version, build, the "
        "local vCenter and a short view of the certificates. MEHO uses the same call "
        "to detect the vSphere Replication version."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
        "examples": [{}],
    },
    response_schema=server_info_response_schema(
        "vSphere Replication", "the local vCenter the server is registered with"
    ),
    group_key=GROUP_SERVER,
    tags=("read-only", "vr", "vsphere-replication", "live-recovery", "probe"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call first on a vSphere Replication target, to confirm which server it is "
            "and which version and build it runs (8.8, 9.0 or 9.1), and whether its "
            "status is OK."
        ),
        "output_shape": SERVER_INFO_OUTPUT_SHAPE,
        "next_step": (
            "If server_status is WARNING or ERROR, or certificate_warnings is not empty, "
            "report it. To read the vendor API file of this server, call vr.api_file.get."
        ),
    },
)

_API_FILE_GET = VrTypedOp(
    op_id="vr.api_file.get",
    handler_attr="api_file_get",
    summary="Read one API description file the vSphere Replication appliance serves.",
    description=(
        "Reads the vSphere Replication API file or the appliance setup API file from "
        "the appliance and returns a checked copy: secret values are replaced. The "
        "answer has the path, HTTP status, content type, size, SHA-256 and the version. "
        "The text comes back in parts behind a result handle."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "file": {
                "type": "string",
                "enum": ["vr", "configure"],
                "description": (
                    "Which file to read. vr: the vSphere Replication API "
                    "(/api/rest/vr/v2/open-api.yaml). configure: the appliance setup "
                    "API (/api/rest/configure/v2/open-api.yaml)."
                ),
            },
        },
        "required": ["file"],
        "additionalProperties": False,
        "examples": [{"file": "vr"}],
    },
    response_schema=api_file_response_schema("vSphere Replication"),
    group_key=GROUP_API_DOCS,
    tags=("read-only", "vr", "vsphere-replication", "live-recovery", "api-docs", "documentation"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to read the API file of a vSphere Replication appliance, for example "
            "to pin the vendor API file of a vSphere Replication version or to check "
            "which API calls a version serves."
        ),
        "output_shape": API_FILE_OUTPUT_SHAPE,
        "next_step": API_FILE_NEXT_STEP,
        "result_scalars": {"keys": list(API_FILE_RESULT_SCALARS)},
    },
)

VR_TYPED_OPS: tuple[VrTypedOp, ...] = (_ABOUT, _API_FILE_GET, *VR_READ_OPS)


async def register_vr_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert every op in :data:`VR_TYPED_OPS` into ``endpoint_descriptor``.

    Queued onto the lifespan-driven registrar list in this package's
    ``__init__``; the runner passes the process-wide embedding service
    (or a test stub), which is forwarded to ``register_typed_operation``.
    """
    from meho_backplane.connectors.vr.connector import VrConnector
    from meho_backplane.operations.typed_register import register_typed_operation

    for op in VR_TYPED_OPS:
        handler = getattr(VrConnector, op.handler_attr, None)
        if handler is None:
            raise AttributeError(
                f"VrConnector typed op {op.op_id!r} declares "
                f"handler_attr={op.handler_attr!r} but the class has no such attribute"
            )
        when_to_use = (
            None if op.group_key is None else VR_TYPED_WHEN_TO_USE_BY_GROUP.get(op.group_key)
        )
        if op.group_key is not None and when_to_use is None:
            raise ValueError(
                f"VrConnector typed op {op.op_id!r} declares group_key={op.group_key!r} "
                "but no curated when_to_use exists for that key."
            )
        await register_typed_operation(
            product=VrConnector.product,
            version=VrConnector.version,
            impl_id=VrConnector.impl_id,
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
