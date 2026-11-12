# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""REST routes to read and edit the classification of operations.

A tenant admin gives connectors, operation groups and operations a
category, labels and the "always wait" mark (see
``docs/codebase/policy-classification.md``). The routes:

* ``GET /api/v1/policy/classification`` -- the taxonomy and the stored
  entries that apply to the caller's tenant.
* ``POST /api/v1/policy/classification/preview`` -- the dry run of one
  edit: every operation it matches, with the values before and after.
  Nothing is written.
* ``POST /api/v1/policy/classification`` -- ask for the edit. It is a
  change of rights, so the answer is ``202`` with the id of the approval
  request: a second tenant admin must approve it.
* ``PATCH /api/v1/connectors/{connector_id}/classification`` -- the same
  request for one whole connector.

The operation and group PATCH routes in
:mod:`meho_backplane.api.v1.connectors_ingest` take ``category`` and
``labels`` too and answer with :func:`classification_request_response`.
All of them use :mod:`meho_backplane.policy.bulk`, like the CLI, the
console and the admin MCP tool. Every route answers ``409`` while the
``POLICY_CLASSIFICATION`` switch is off.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.policy.bulk import (
    ClassificationEditError,
    ClassificationOffError,
    preview_classification_edit,
    request_classification_edit,
)
from meho_backplane.policy.classification import DEFAULT_TAXONOMY
from meho_backplane.policy.store import (
    ClassificationEdit,
    classification_enabled,
    load_classification,
)

__all__ = [
    "CLASSIFICATION_PATCH_RESPONSES",
    "ClassificationEditBody",
    "ClassificationRequestResponse",
    "ConnectorClassificationBody",
    "classification_patch",
    "classification_request_response",
    "connector_router",
    "router",
]

router = APIRouter(prefix="/api/v1/policy/classification", tags=["policy"])
connector_router = APIRouter(prefix="/api/v1/connectors", tags=["connectors"])

_require_operator = Depends(require_role(TenantRole.OPERATOR))
_require_admin = Depends(require_role(TenantRole.TENANT_ADMIN))

_PATTERN_HINT = "`*` is any text, `?` is one character, no wildcard is an exact match."
_CATEGORY_FIELD = Field(
    default=None,
    pattern="^[a-z][a-z0-9_-]{0,62}$",
    description="A category of the taxonomy, for example network or storage.",
)
_LABELS_FIELD = Field(
    default=None,
    max_length=32,
    description="Free key=value text. Replaces the labels of this entry.",
)


class ClassificationMatch(BaseModel):
    """What an entry matches. Every field given must match."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    connector: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description=f"Connector id pattern, for example vault-1.x. {_PATTERN_HINT}",
    )
    group: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description=f"Operation group key pattern. {_PATTERN_HINT}",
    )
    op: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description=f"Operation id pattern, for example k8s.deployment.*. {_PATTERN_HINT}",
    )


class ClassificationEditBody(BaseModel):
    """One edit: the match and the values it sets. Set at least one value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    match: ClassificationMatch
    category: str | None = _CATEGORY_FIELD
    labels: dict[str, str] | None = _LABELS_FIELD
    always_wait: bool | None = Field(
        default=None,
        description=(
            "true: every call of a matched operation waits for approval. false: back to "
            "the operation's own value, never below it."
        ),
    )

    def to_edit(self) -> ClassificationEdit:
        """The edit this body asks for."""
        return ClassificationEdit(
            match=self.match.model_dump(exclude_none=True),
            category=self.category,
            labels=self.labels,
            always_wait=self.always_wait,
        )


class ConnectorClassificationBody(BaseModel):
    """The values for one whole connector. Set at least one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    category: str | None = _CATEGORY_FIELD
    labels: dict[str, str] | None = _LABELS_FIELD
    always_wait: bool | None = None


class ClassificationValues(BaseModel):
    """The classified values of one operation, before the floors."""

    category: str | None
    labels: dict[str, str]
    risk: str | None
    always_wait: bool
    rule_ref: str


class ClassificationPreviewRow(BaseModel):
    """One matched operation, with its values before and after the edit."""

    connector_id: str
    group_key: str | None
    op_id: str
    before: ClassificationValues
    after: ClassificationValues
    changes: bool


class ClassificationPreviewResponse(BaseModel):
    """The dry run of one edit. Lists every matched operation; nothing is written.

    ``conflict`` is the reason the edit would be refused, or ``null``.
    """

    match: dict[str, str]
    rule_ref: str
    entry: dict[str, Any]
    matched_operations: int
    changed_operations: int
    conflict: str | None
    operations: list[ClassificationPreviewRow]


class ClassificationRequestResponse(BaseModel):
    """The answer to an edit request: it waits until a second tenant admin approves it."""

    status: str
    approval_request_id: str | None
    message: str
    matched_operations: int
    changed_operations: int


class ClassificationEntryOut(BaseModel):
    """One stored entry. ``ref`` names it, for example ``tenant:classifications[0]``."""

    ref: str
    scope: str
    match: dict[str, str]
    category: str | None
    labels: dict[str, str]
    risk: str | None
    always_wait: bool | None


class ClassificationTaxonomyOut(BaseModel):
    """The categories an entry may use, and the category of an unclassified operation."""

    categories: list[str]
    default: str


class ClassificationListResponse(BaseModel):
    """The taxonomy and the stored entries of the caller's tenant and of the install.

    ``enabled`` is ``false`` while the ``POLICY_CLASSIFICATION`` switch is
    off; then nothing is read and ``entries`` is empty.
    """

    enabled: bool
    taxonomy: ClassificationTaxonomyOut
    entries: list[ClassificationEntryOut]


def _off_409() -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_409_CONFLICT,
        detail=f"classification_off: {ClassificationOffError()}",
    )


def _refused_400(exc: ClassificationEditError) -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_400_BAD_REQUEST,
        detail=f"classification_refused: {exc}",
    )


_STATUS_CODES = {"awaiting_approval": 202, "ok": 200, "denied": 403}


async def classification_request_response(
    operator: Operator, edit: ClassificationEdit
) -> JSONResponse:
    """Ask for *edit* and map the answer to HTTP.

    ``202`` with the approval request id when it waits (the normal case),
    ``403`` when the call is refused (for example a missing role), ``400``
    for a refused edit or a call error, ``409`` while the switch is off.
    """
    try:
        result = await request_classification_edit(operator, edit)
    except ClassificationOffError as exc:
        raise _off_409() from exc
    except ClassificationEditError as exc:
        raise _refused_400(exc) from exc
    status = str(result.get("status"))
    extras = result.get("extras") or {}
    body = ClassificationRequestResponse(
        status=status,
        approval_request_id=extras.get("approval_request_id"),
        message=(
            "The change waits until another tenant admin approves it."
            if status == "awaiting_approval"
            else str(result.get("error") or "The change is stored.")
        ),
        matched_operations=int(result["matched_operations"]),
        changed_operations=int(result["changed_operations"]),
    )
    return JSONResponse(
        status_code=_STATUS_CODES.get(status, 400), content=body.model_dump(mode="json")
    )


def _entry_out(entry: Any) -> ClassificationEntryOut:
    match = {
        key: value
        for key, value in (("connector", entry.connector), ("group", entry.group), ("op", entry.op))
        if value is not None
    }
    return ClassificationEntryOut(
        ref=entry.ref,
        scope=entry.scope,
        match=match,
        category=entry.category,
        labels=dict(entry.labels),
        risk=entry.risk,
        always_wait=entry.always_wait,
    )


#: The extra answer of the operation and group PATCH routes for a classification edit.
CLASSIFICATION_PATCH_RESPONSES: dict[int | str, dict[str, Any]] = {
    202: {
        "model": ClassificationRequestResponse,
        "description": (
            "category / labels were sent: the change waits until another tenant admin approves it."
        ),
    },
}


def classification_patch(
    match: dict[str, str], body: BaseModel, *, other_fields: tuple[str, ...]
) -> ClassificationEdit | None:
    """The classification edit in a PATCH *body*, or ``None`` when it sets none.

    ``category`` and ``labels`` are a change of rights that waits for a
    second admin, so they cannot ride along with fields that are stored at
    once: a body with both is refused with ``400``.
    """
    category = getattr(body, "category", None)
    labels = getattr(body, "labels", None)
    if category is None and labels is None:
        return None
    mixed = [name for name in other_fields if getattr(body, name, None) is not None]
    if mixed:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=(
                "classification_mixed: category and labels wait for another tenant admin, "
                f"so send them in a request of their own, without {', '.join(mixed)}."
            ),
        )
    return ClassificationEdit(match=match, category=category, labels=labels, always_wait=None)


@router.get("", response_model=ClassificationListResponse)
async def list_classification(operator: Operator = _require_operator) -> ClassificationListResponse:
    """Return the taxonomy and the stored entries that apply to the caller's tenant."""
    if not classification_enabled():
        return ClassificationListResponse(
            enabled=False,
            taxonomy=ClassificationTaxonomyOut(
                categories=list(DEFAULT_TAXONOMY.categories), default=DEFAULT_TAXONOMY.default
            ),
            entries=[],
        )
    async with get_sessionmaker()() as session:
        taxonomy, entries = await load_classification(session, operator.tenant_id)
    return ClassificationListResponse(
        enabled=True,
        taxonomy=ClassificationTaxonomyOut(
            categories=list(taxonomy.categories), default=taxonomy.default
        ),
        entries=[_entry_out(entry) for entry in entries],
    )


@router.post("/preview", response_model=ClassificationPreviewResponse)
async def preview_classification(
    body: ClassificationEditBody, operator: Operator = _require_admin
) -> ClassificationPreviewResponse:
    """Dry run of one edit: every matched operation, before and after. Writes nothing."""
    try:
        preview = await preview_classification_edit(operator, body.to_edit())
    except ClassificationOffError as exc:
        raise _off_409() from exc
    except ClassificationEditError as exc:
        raise _refused_400(exc) from exc
    return ClassificationPreviewResponse.model_validate(preview.to_dict())


_REQUEST_RESPONSES: dict[int | str, dict[str, Any]] = {
    202: {
        "model": ClassificationRequestResponse,
        "description": "The change waits until another tenant admin approves it.",
    },
}


@router.post(
    "",
    status_code=http_status.HTTP_202_ACCEPTED,
    response_model=ClassificationRequestResponse,
    responses=_REQUEST_RESPONSES,
)
async def request_classification(
    body: ClassificationEditBody, operator: Operator = _require_admin
) -> JSONResponse:
    """Ask for one edit. It waits until another tenant admin approves it.

    Refused with ``400`` (nothing asked) when the edit is not valid, matches
    no operation, or would give an operation a conflict: run the preview
    first to see why.
    """
    return await classification_request_response(operator, body.to_edit())


@connector_router.patch(
    "/{connector_id}/classification",
    status_code=http_status.HTTP_202_ACCEPTED,
    response_model=ClassificationRequestResponse,
    responses=_REQUEST_RESPONSES,
)
async def classify_connector(
    connector_id: str,
    body: ConnectorClassificationBody,
    operator: Operator = _require_admin,
) -> JSONResponse:
    """Ask to set the category, labels or "always wait" of one whole connector.

    Its groups and operations take these values unless they have their own.
    Waits until another tenant admin approves it.
    """
    edit = ClassificationEdit(
        match={"connector": connector_id},
        category=body.category,
        labels=body.labels,
        always_wait=body.always_wait,
    )
    return await classification_request_response(operator, edit)
