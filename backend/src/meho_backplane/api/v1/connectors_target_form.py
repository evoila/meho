# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /api/v1/connectors/target-form`` -- what a target of a product needs.

Returns what the connector classes of one product declare: the extra settings
of a target, the ways to log in with the fields of each, and the test read.
The console uses it to build the target form and the credential form.

Role: ``operator``. The answer holds what the classes declare: names, labels,
help texts, kinds, choices and the declared defaults of target settings. A
credential field has no default and no example value. The route never reads
the secret store and never returns a value of any secret.

Errors: 404 with a plain message when no connector serves the product.

``test_operation`` names the test read the class declares (``null`` when it
declares none). For a class with a test read, ``test_operation_run`` also
says whether the caller can run that read now, with no wait. MEHO decides it
with the steps a real call of the read takes for the same caller
(:func:`~meho_backplane.operations.run_now_check.run_now`): the call's own row
lookup and the call's own policy. When the caller cannot, the answer holds a
fixed ``code`` and one fixed sentence, so the console button can show them:

* ``not_enabled``: the read is turned off for the caller's tenant, or the
  tenant's own rows of the connector do not have it. A call is refused.
* ``waits_for_approval``: a call of the read waits for a person to approve it.
* ``not_allowed``: the caller may not run the read, for example an agent login
  with no grant for it while the agent grant setting is on.

The answer holds no stored value: no risk level text from a row, no override
reason and no tenant id.

The caller sees the connector classes installed with MEHO, and a class made
by an import only when the caller may see that connector: the import is
built in, or it belongs to the caller's tenant. A connector that another
tenant imported answers 404, as if it did not exist.

This route must be registered before the ``/{connector_id}`` routes of
:mod:`meho_backplane.api.v1.connectors_ingest`, so ``target-form`` is not read
as a connector id. See ``docs/codebase/connector-target-form.md``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from pydantic import BaseModel

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.connectors.base import Connector, shim_kind
from meho_backplane.connectors.registry import connectors_for_tenant
from meho_backplane.connectors.target_form import CredentialWay, TargetField
from meho_backplane.operations._lookup import connector_triples_visible_to
from meho_backplane.operations.run_now_check import RunNow, run_now

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


class RunNowModel(BaseModel):
    """Whether the caller can run the test read now, with no wait.

    ``code`` and ``reason`` are ``None`` when ``can_run`` is true. Otherwise
    ``code`` is one of ``not_enabled``, ``waits_for_approval`` and
    ``not_allowed``, and ``reason`` is the one fixed sentence of that code.
    """

    can_run: bool
    code: str | None
    reason: str | None


class TargetFormClass(BaseModel):
    """What one connector class declares. ``None`` means "not declared".

    ``test_operation`` names the test read the class declares.
    ``test_operation_run`` is ``None`` when the class declares none. Otherwise
    it says whether the caller can run that read now, with the code and the
    fixed sentence of the reason when not.
    """

    impl_id: str
    version: str
    supported_version_range: str | None
    default_port: int | None
    target_fields: list[TargetFieldModel] | None
    credential_ways: list[CredentialWayModel] | None
    test_operation: str | None
    test_operation_run: RunNowModel | None


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


def _describe(cls: type[Connector], *, test_run: RunNow | None) -> TargetFormClass:
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
        test_operation_run=(
            None
            if test_run is None
            else RunNowModel(can_run=test_run.can_run, code=test_run.code, reason=test_run.sentence)
        ),
    )


async def _classes_for(operator: Operator, wanted: str) -> list[type[Connector]]:
    """The classes of product *wanted* that *operator* may see, each once, in a stable order.

    A hand-written class is installed with MEHO, so every tenant sees it.
    Any other class (one made by an import) counts only when its connector
    is in :func:`connector_triples_visible_to` for the caller's tenant.
    """
    matching = [
        (key, cls)
        for key, cls in sorted(connectors_for_tenant(operator.tenant_id).items())
        if cls.product.lower() == wanted
    ]
    if not matching:
        return []
    visible: set[tuple[str, str, str]] | None = None
    kept: set[type[Connector]] = set()
    for key, cls in matching:
        if shim_kind(cls) != "none":
            if visible is None:
                visible = await connector_triples_visible_to(operator.tenant_id)
            if key not in visible:
                continue
        kept.add(cls)
    return sorted(kept, key=lambda cls: (cls.product, cls.version, cls.impl_id, cls.__name__))


async def _test_run(operator: Operator, cls: type[Connector]) -> RunNow | None:
    """Can *operator* run the test read of *cls* now? ``None`` when it declares none."""
    if cls.test_operation is None:
        return None
    return await run_now(
        operator,
        product=cls.product,
        version=cls.version,
        impl_id=cls.impl_id,
        op_id=cls.test_operation,
    )


@router.get(
    "/target-form",
    response_model=TargetFormResponse,
    summary="What a target of a product needs",
)
async def get_target_form(
    product: str = _PRODUCT_QUERY,
    operator: Operator = _require_operator,
) -> TargetFormResponse:
    """Return the target form declarations of every connector class of *product*."""
    classes = await _classes_for(operator, product.strip().lower())
    if not classes:
        raise HTTPException(
            http_status.HTTP_404_NOT_FOUND,
            f"No connector serves the product {product!r}.",
        )
    return TargetFormResponse(
        product=classes[0].product,
        classes=[_describe(c, test_run=await _test_run(operator, c)) for c in classes],
    )
