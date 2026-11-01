# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The registration row of one vmware-rest composite.

A composite module exports its rows as ``COMPOSITES``, a tuple of
:class:`CompositeSpec`. ``_register.py`` collects the rows of every module
in the package at start-up, sorts them by op id, registers each row's
preview builder, publishes the child calls each composite may make, and
derives the list of composites that stay off the vmware-rest-8.0 catalog.
So a new composite needs a new module, and no shared list changes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal, NamedTuple

from meho_backplane.connectors import OperationResult
from meho_backplane.operations._preview import PreviewBuilder

__all__ = ["CompositeSpec"]


class CompositeSpec(NamedTuple):
    """Per-composite registration arguments.

    Each row carries its own ``safety_level`` and ``requires_approval``,
    so the policy posture is implied by the row, not by global defaults:
    reads ship ``"safe"`` / ``False``; writes ship ``"dangerous"`` (or
    ``"caution"`` / ``"destructive"``) / ``True``. The common fields
    (product, version, implementation id) live on the registrar.
    """

    op_id: str
    # Read composites return a plain aggregation dict; write composites may
    # instead return an ``OperationResult`` verbatim when the direct-session
    # governance seam parks or denies an internal write, so the handler
    # contract widens to that union.
    handler: Callable[..., Awaitable[dict[str, Any] | OperationResult]]
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any]
    group_key: str
    tags: list[str]
    safety_level: Literal["safe", "caution", "dangerous", "destructive"]
    requires_approval: bool
    # Optional per-op agent-facing usage instructions. ``None`` when the
    # ``description`` already carries the guidance.
    llm_instructions: dict[str, Any] | None = None
    # What the approver sees while the call waits for approval. ``None``:
    # the generic echo of the parameters.
    preview: PreviewBuilder | None = None
    # Why the composite is not on the vmware-rest-8.0 catalog. ``None``: it
    # is on both catalogs.
    not_on_8_0: str | None = None
    # The vendor calls (child op ids) this composite may make, REST and VI-JSON.
    # ``_register.py`` publishes them, so an operator can see which grants a
    # service principal needs to run the composite. Empty: the composite
    # declares no child calls (reads, and the guest-ops composites).
    governed_subops: tuple[str, ...] = ()
