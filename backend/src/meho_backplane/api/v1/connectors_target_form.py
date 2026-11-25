# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /api/v1/connectors/target-form`` -- what a target of a product needs.

Returns what the connector classes of one product declare: the extra settings
of a target, the ways to log in with the fields of each, and the test read.
The console uses it to build the target form and the credential form.

Role: ``operator``. The answer holds names and labels only. The route never
reads the secret store and never returns a value of any secret.

Errors: 404 with a plain message when no connector serves the product.

This route must be registered before the ``/{connector_id}`` routes of
:mod:`meho_backplane.api.v1.connectors_ingest`, so ``target-form`` is not read
as a connector id. See ``docs/codebase/connector-target-form.md``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from pydantic import BaseModel

from meho_backplane.auth.operator import TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.target_form import CredentialWay, TargetField
from meho_backplane.operations.connector_declaration_check import registered_connector_classes

__all__ = ["TargetFormClass", "TargetFormResponse", "router"]

router = APIRouter(prefix="/api/v1/connectors", tags=["connectors"])

_require_operator = Depends(require_role(TenantRole.OPERATOR))
_PRODUCT_QUERY = Query(description="The product slug, for example `harbor` or `linux`.")


class TargetFieldModel(BaseModel):
    """One extra setting of a target."""

    key: str
    label: str
    help: str
    required: bool
    kind: str
    choices: list[str]
    default: str | int | bool | None


class CredentialFieldModel(BaseModel):
    """One field of the secret."""

    name: str
    label: str
    help: str
    required: bool
    hidden: bool
    multiline: bool


class CredentialWayModel(BaseModel):
    """One way to log in."""

    key: str
    label: str
    fields: list[CredentialFieldModel]


class TargetFormClass(BaseModel):
    """What one connector class declares. ``None`` means "not declared"."""

    impl_id: str
    version: str
    supported_version_range: str | None
    default_port: int | None
    target_fields: list[TargetFieldModel] | None
    credential_ways: list[CredentialWayModel] | None
    test_operation: str | None


class TargetFormResponse(BaseModel):
    """The declarations of every connector class of one product."""

    product: str
    classes: list[TargetFormClass]


def _target_field(field: TargetField) -> TargetFieldModel:
    return TargetFieldModel(
        key=field.key,
        label=field.label,
        help=field.help,
        required=field.required,
        kind=field.kind,
        choices=list(field.choices),
        default=field.default,
    )


def _way(way: CredentialWay) -> CredentialWayModel:
    return CredentialWayModel(
        key=way.key,
        label=way.label,
        fields=[
            CredentialFieldModel(
                name=f.name,
                label=f.label,
                help=f.help,
                required=f.required,
                hidden=f.hidden,
                multiline=f.multiline,
            )
            for f in way.fields
        ],
    )


def _describe(cls: type[Connector]) -> TargetFormClass:
    return TargetFormClass(
        impl_id=cls.impl_id,
        version=cls.version,
        supported_version_range=cls.supported_version_range,
        default_port=cls.default_port,
        target_fields=(
            None if cls.target_fields is None else [_target_field(f) for f in cls.target_fields]
        ),
        credential_ways=(
            None if cls.credential_ways is None else [_way(w) for w in cls.credential_ways]
        ),
        test_operation=cls.test_operation,
    )


@router.get(
    "/target-form",
    response_model=TargetFormResponse,
    summary="What a target of a product needs",
    dependencies=[_require_operator],
)
async def get_target_form(product: str = _PRODUCT_QUERY) -> TargetFormResponse:
    """Return the target form declarations of every connector class of *product*."""
    wanted = product.strip().lower()
    classes = [cls for cls in registered_connector_classes() if cls.product.lower() == wanted]
    if not classes:
        raise HTTPException(
            http_status.HTTP_404_NOT_FOUND,
            f"No connector serves the product {product!r}.",
        )
    return TargetFormResponse(product=classes[0].product, classes=[_describe(c) for c in classes])
