# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Ingested operations run only on a connector class that accepts them.

An ingested operation sends the method and path of its API file with the
login of the connector class the target resolves to. It runs none of the
checks that the hand-written operations of that class run. So a class
says whether ingested operations may run on it, with
:attr:`~meho_backplane.connectors.base.Connector.accepts_ingested_operations`
(default ``False``). The generic and profiled classes accept them, and so
do the hand-written classes built to serve an ingested catalog.

MEHO checks this in two places:

* **At ingest.** :meth:`~meho_backplane.operations.ingest.IngestionPipelineService.ingest`
  refuses an API file under the connector name of a class that does not
  accept ingested operations (:func:`assert_ingest_target_accepts`), before
  it reads the file or writes a row. REST answers 422, MCP ``-32602``.
* **At run time.** The dispatcher picks the class from the target. When the
  class does not accept ingested operations, the call is refused with
  ``ingested_ops_not_accepted``: before it waits for approval, again when
  an approved call runs, and in ``preview_operation``. No request reaches
  the vendor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors import OperationResult

if TYPE_CHECKING:
    from meho_backplane.connectors.base import Connector

__all__ = [
    "INGESTED_OPS_NOT_ACCEPTED",
    "IngestedOperationsNotAccepted",
    "accepts_ingested_operations",
    "assert_ingest_target_accepts",
    "build_ingested_ops_not_accepted_detail",
    "ingested_ops_refusal_message",
    "result_ingested_ops_not_accepted",
]

#: The ``error_code`` of a refused ingested operation.
INGESTED_OPS_NOT_ACCEPTED: Final = "ingested_ops_not_accepted"


def accepts_ingested_operations(cls: type[Connector]) -> bool:
    """True when ingested operations may run on *cls*."""
    return bool(cls.accepts_ingested_operations)


def ingested_ops_refusal_message(cls: type[Connector]) -> str:
    """The reason shown to the caller. It names the class, never a param."""
    return (
        f"connector {cls.__name__} runs only its own hand-written operations; "
        f"operations ingested from an API file do not run on it. Use the "
        f"connector's hand-written operations instead"
    )


def result_ingested_ops_not_accepted(
    op_id: str, message: str, duration_ms: float
) -> OperationResult:
    """The ``ingested_ops_not_accepted`` result: nothing was sent to the vendor."""
    return OperationResult(
        status="error",
        op_id=op_id,
        error=f"{INGESTED_OPS_NOT_ACCEPTED}: {message}",
        duration_ms=duration_ms,
        extras={"error_code": INGESTED_OPS_NOT_ACCEPTED, "op_id": op_id},
    )


class IngestedOperationsNotAccepted(ValueError):  # noqa: N818 -- the name states the refusal
    """An ingest names a connector whose class does not accept ingested operations.

    Raised by :func:`assert_ingest_target_accepts` before any spec fetch or
    DB write. Inherits from :class:`ValueError`, like the other caller-input
    ingest errors.
    """

    def __init__(self, *, product: str, version: str, impl_id: str, class_name: str) -> None:
        self.product = product
        self.version = version
        self.impl_id = impl_id
        self.class_name = class_name
        super().__init__(
            f"{INGESTED_OPS_NOT_ACCEPTED}: connector {product}/{version}/{impl_id} is "
            f"served by {class_name}, which runs only its own hand-written "
            f"operations. MEHO does not ingest an API file under its name"
        )


def build_ingested_ops_not_accepted_detail(exc: IngestedOperationsNotAccepted) -> dict[str, Any]:
    """Structured detail of the refusal, shared by the REST 422 and MCP ``-32602``."""
    return {
        "kind": INGESTED_OPS_NOT_ACCEPTED,
        "product": exc.product,
        "version": exc.version,
        "impl_id": exc.impl_id,
        "connector_class": exc.class_name,
        "message": str(exc),
    }


def assert_ingest_target_accepts(*, product: str, version: str, impl_id: str) -> None:
    """Refuse an ingest whose rows would run on a class that does not accept them.

    The rows of an ingest run on the class registered for the triple, or
    on the hand-written class that serves its ``(version, impl_id)`` (the
    ingest registers no shim then). When neither exists, the ingest
    registers a shim, which accepts ingested operations.
    """
    from meho_backplane.connectors.registry import all_connectors_v2
    from meho_backplane.operations.ingest.connector_registration import (
        handrolled_class_for_impl_id,
    )

    cls = all_connectors_v2().get((product, version, impl_id))
    if cls is None:
        cls = handrolled_class_for_impl_id(version=version, impl_id=impl_id)
    if cls is None or accepts_ingested_operations(cls):
        return
    raise IngestedOperationsNotAccepted(
        product=product, version=version, impl_id=impl_id, class_name=cls.__name__
    )
