# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Central-side request/response envelopes for the gateway checks API (#2499).

The *runner-facing* wire shapes (``RunnerAssignment`` / ``RunnerWorkItem`` /
``ResolvedTargetDescriptor`` / ``RunnerResultBatch``) live in
:mod:`meho_backplane.runner.wire` and are shared by both ends of the wire.
This module holds only the shapes the *operator* authoring surface needs —
the ``PUT`` document and the result-ingest accounting response — following
``docs/codebase/api-shape-conventions.md``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AssignmentDocument",
    "AssignmentDocumentResponse",
    "AuthoredCheckItem",
    "ResultIngestResponse",
]


class AuthoredCheckItem(BaseModel):
    """One operator-authored check in a runner's assignment document.

    The runner-facing ``GET`` materialises each of these into a wire
    :class:`~meho_backplane.runner.wire.RunnerWorkItem` at request time:
    ``target_name`` is resolved to a live target descriptor, ``op`` to the
    resolved connector's enabled descriptor (yielding ``handler_ref`` +
    ``safety_level`` + the ``(product, version, impl_id)`` key). ``op`` is
    the connector-side op id (e.g. ``"vsphere.host.list"``); the connector
    triple is derived from the resolved target, not authored here.
    """

    model_config = ConfigDict(frozen=True)

    check_ref: str = Field(min_length=1)
    target_name: str = Field(min_length=1)
    op: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    # Runner-side poll cadence in seconds; ``ge=1`` (not ``gt=0``) so the
    # constraint renders as ``minimum`` — identical in OpenAPI 3.0/3.1 —
    # rather than a 3.1-only numeric ``exclusiveMinimum`` the CLI codegen
    # (OpenAPI 3.0) cannot parse.
    cadence_seconds: int = Field(ge=1)


class AssignmentDocument(BaseModel):
    """Full-document ``PUT`` body: replaces a runner's assignment wholesale."""

    model_config = ConfigDict(frozen=True)

    items: list[AuthoredCheckItem] = Field(default_factory=list)


class AssignmentDocumentResponse(BaseModel):
    """Echo returned by ``PUT``: the stored authored document + runner name."""

    model_config = ConfigDict(frozen=True)

    runner: str
    items: list[AuthoredCheckItem]


class ResultIngestResponse(BaseModel):
    """``POST /checks/results`` response — idempotency accounting.

    ``accepted`` counts rows newly persisted; ``duplicates`` counts rows
    whose ``result_uid`` was already ingested for this runner (a re-posted
    spool batch). ``accepted + duplicates`` equals the batch length.
    """

    model_config = ConfigDict(frozen=True)

    accepted: int
    duplicates: int
