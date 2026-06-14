# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``register_github_composite_operations`` -- registrar for gh-rest composites.

Module-level async function called from the lifespan-driven
:func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
after the registrar list is populated by the
:mod:`meho_backplane.connectors.github.composites` package's
``__init__`` (which appends this function via
:func:`register_typed_op_registrar`).

Mirrors the vmware-rest precedent
(:mod:`meho_backplane.connectors.vmware_rest.composites._register`):
per-composite arguments (summary / description / group_key / tags /
``parameter_schema`` / ``safety_level`` / ``requires_approval``) live
in this module so a future shape change touches one file. The helper
:func:`~meho_backplane.operations.typed_register.register_composite_operation`
handles upsert, body-hash dedupe, embedding pipeline, and
``source_kind="composite"`` persistence.

Scope at T4 (#1224)
-------------------

One composite ships: ``gh.composite.pr_status_summary``. It is read-
only (``safety_level="read"`` / ``requires_approval=False``) -- the
issue body's mandatory posture for the trigger use case (the operator
asks "is PR #N ready to merge?" and gets a structured answer without
mutating anything).

The composite-helper's defaults are ``safety_level="dangerous"`` +
``requires_approval=True`` (suited to write composites); the read
composite explicitly overrides both. Future T7+ write composites will
omit the override and inherit the helper's safe-by-default policy.

``safety_level`` value note
---------------------------

The issue body specifies ``safety_level="read"``. The
:func:`~meho_backplane.operations.typed_register.register_composite_operation`
helper validates against the enum ``{"safe", "caution", "dangerous"}``
(see the ``Literal`` annotation on its signature). ``"safe"`` is the
register-time equivalent of the operator-visible "read" label -- the
descriptor row stores ``safety_level="safe"`` and ``op_class="read"``
is computed elsewhere from method semantics. The registrar passes
``"safe"``; the issue body's "read" wording maps to this verbatim.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal, NamedTuple

from meho_backplane.connectors.github._catalog_command import (
    catalog_command_for_github_rest,
)
from meho_backplane.connectors.github.composites._read import (
    _CONNECTOR_ID,
    _SUB_OPS_PR_STATUS_SUMMARY,
    pr_status_summary_composite,
)
from meho_backplane.connectors.github.composites.schemas import (
    PR_STATUS_SUMMARY_PARAMETER_SCHEMA,
    PR_STATUS_SUMMARY_RESPONSE_SCHEMA,
)
from meho_backplane.operations.composite_backing import register_composite_backing
from meho_backplane.operations.typed_register import register_composite_operation
from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = ["register_github_composite_operations"]


# Natural-key shorthand. Every gh-rest composite registers against the
# same triple the connector advertises -- ``register_connector_v2(
# product="gh", version="3", impl_id="gh-rest", ...)`` in the package
# ``__init__`` -- so the dispatcher's ``connector_id="gh-rest-3"``
# lookup resolves the composite alongside the ingested L2 ops.
_PRODUCT = "gh"
_VERSION = "3"
_IMPL_ID = "gh-rest"


#: Curated agent-actionable group selectors for the gh-rest composite
#: surface, surfaced verbatim by ``list_operation_groups`` so the LLM
#: client picks the right composite group before drilling into
#: ``search_operations``. T4 ships exactly one composite (the ``pulls``
#: group); future T7+ composites populate ``release``, ``board``, etc.
_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    "pulls": (
        "Use for one-call PR-status questions: 'is PR #N ready to "
        "merge?' / 'what is the CI state on the head commit?' / 'who "
        "approved this PR?'. The composite gathers PR metadata, "
        "head-commit check runs, reviews, and the mergeable state in "
        "a single envelope so the LLM client does not have to "
        "orchestrate three separate L2 calls. Read-only. Pair with the "
        "ingested L2 ops (gh.pr.get_files, gh.pr.get_commits, etc.) "
        "when drill-in beyond the summary is needed."
    ),
}


class _CompositeSpec(NamedTuple):
    """Per-composite registration arguments.

    Field-table form rather than per-composite kwargs blocks: keeps the
    op_id / handler / schemas / group / tags / policy posture adjacent
    so a future maintainer reading the registrar sees the whole row at
    a glance. Common fields (``product`` / ``version`` / ``impl_id``)
    live on the call site below.
    """

    op_id: str
    handler: Callable[..., Awaitable[dict[str, Any]]]
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any]
    group_key: str
    tags: list[str]
    safety_level: Literal["safe", "caution", "dangerous"]
    requires_approval: bool
    #: The L2 sub-op-ids the handler dispatches into -- the SAME tuple the
    #: handler hands its preflight (``_read.py``'s ``_SUB_OPS_*``). Carried
    #: on the spec so the import-time backing registration (below) and the
    #: dispatch-time preflight read one source of truth, and the listing's
    #: ``unbacked`` marker can never drift from what the composite hits.
    sub_op_ids: tuple[str, ...]


_COMPOSITES: tuple[_CompositeSpec, ...] = (
    _CompositeSpec(
        op_id="gh.composite.pr_status_summary",
        handler=pr_status_summary_composite,
        summary="Return PR metadata + checks + reviews + mergeable in one call.",
        description=(
            "Composes three L2 sub-ops -- GET:/repos/{owner}/{repo}/"
            "pulls/{pull_number}, GET:/repos/{owner}/{repo}/commits/"
            "{ref}/check-runs (against the PR's head SHA), and "
            "GET:/repos/{owner}/{repo}/pulls/{pull_number}/reviews -- "
            "into a single envelope. Answers 'is this PR ready to "
            "merge?' without three separate L2 calls. Read-only; "
            "degrades gracefully when the checks or reviews sub-call "
            "fails (the failed field surfaces as null + a "
            "checks_status / review_status of 'unknown'). Equivalent "
            "of 'gh pr view <n> --json ...' for an LLM client that "
            "needs the answer in one round-trip through MEHO. The "
            "primary PR sub-call is non-optional -- a 404 / 401 on "
            "that call propagates to the operator as a connector "
            "error."
        ),
        parameter_schema=PR_STATUS_SUMMARY_PARAMETER_SCHEMA,
        response_schema=PR_STATUS_SUMMARY_RESPONSE_SCHEMA,
        group_key="pulls",
        tags=["composite", "read-only", "pulls", "status"],
        safety_level="safe",
        requires_approval=False,
        sub_op_ids=_SUB_OPS_PR_STATUS_SUMMARY,
    ),
)


# Import-time backing registration (G0.25-T6 #1757). A pure in-process
# dict write -- no DB / embedding work -- so it runs at import (when this
# module is first loaded, alongside the package ``__init__``'s
# lifespan-registrar queueing), well before any request reaches the op
# listing. This lets ``search_operations`` mark a composite ``unbacked``
# with the catalog-ingest ``next_step`` while its L2 sub-ops are absent,
# instead of advertising it as enabled-but-broken until the first
# dispatch trips ``composite_l2_missing``. The ``connector_id`` +
# ``catalog_command`` are the same values the preflight's
# ``CompositeL2DependencyMissing`` carries, and ``sub_op_ids`` is the same
# tuple the handler hands the preflight -- one source of truth across the
# listing and the dispatch.
for _spec in _COMPOSITES:
    register_composite_backing(
        composite_op_id=_spec.op_id,
        connector_id=_CONNECTOR_ID,
        sub_op_ids=_spec.sub_op_ids,
        catalog_command=catalog_command_for_github_rest(),
    )


async def register_github_composite_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert every gh-rest composite into ``endpoint_descriptor``.

    Idempotent: a second invocation against unchanged descriptions is a
    no-op for the embedding pipeline (the body-hash skip path in
    :func:`_register_in_session`). The runner
    (:func:`run_typed_op_registrars`) calls every registered registrar
    on every lifespan startup; the skip-re-embed branch keeps that
    cheap.

    Scope at T4: one composite -- ``gh.composite.pr_status_summary`` --
    with ``safety_level="safe"`` (the register-time equivalent of the
    operator-visible "read" label per the issue body) and
    ``requires_approval=False``. Future T7+ Tasks add write composites
    that inherit the helper's safe-by-default ``dangerous`` /
    ``requires_approval=True`` posture.

    Test seam: ``embedding_service`` lets test fixtures inject a stub
    so unit tests don't load the ONNX model. Production callers leave
    it ``None`` and each registration resolves the process-wide
    singleton.
    """
    for spec in _COMPOSITES:
        await register_composite_operation(
            product=_PRODUCT,
            version=_VERSION,
            impl_id=_IMPL_ID,
            op_id=spec.op_id,
            handler=spec.handler,
            summary=spec.summary,
            description=spec.description,
            parameter_schema=spec.parameter_schema,
            response_schema=spec.response_schema,
            group_key=spec.group_key,
            when_to_use=_WHEN_TO_USE_BY_GROUP[spec.group_key],
            tags=spec.tags,
            safety_level=spec.safety_level,
            requires_approval=spec.requires_approval,
            embedding_service=embedding_service,
        )
