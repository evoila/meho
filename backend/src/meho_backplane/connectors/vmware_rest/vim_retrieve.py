# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""One property read of many vCenter objects, with every page (#4862).

``PropertyCollector.RetrievePropertiesEx`` may answer with only some of the
objects and a ``token`` for the rest. This can happen even when the caller
sets no page size: the pinned ``vi-json.yaml`` says that without
``maxObjects`` "PropertyCollector policy may still limit the number of
objects". A caller that ignores the token gets a short list and does not
know it.

:func:`retrieve_all_pages` is the one place that follows the token. It sends
the first ``RetrievePropertiesEx``, then ``ContinueRetrievePropertiesEx`` with
each token, on the vCenter session of the first call (vCenter accepts a token
only there, and only once), until a page comes back without a token. The
caller gets one answer in the usual ``{"objects": [...]}`` shape, so its
parser does not change.

A failed page fails the whole read: the objects of the earlier pages are not
returned, so a partial answer is never taken for a whole one. An answer that
has not ended after :data:`_MAX_PAGES` pages raises :class:`TooManyPagesError`.

On a standalone ESXi host the connector speaks SOAP, and its SOAP layer has
no ``ContinueRetrievePropertiesEx``. A split answer there makes the read fail
with the connector's "no SOAP builder" error. It never gives a short list.

Reads of one object (one VM, one host, the licence manager, a task poll) do
not need this helper: vCenter splits an answer only between objects, so the
answer for one object fits on one page.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vmware_rest.session import SessionTokenUsed
from meho_backplane.connectors.vmware_rest.vim_body import unwrap_vim_value

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike

__all__ = [
    "CONTINUE_RETRIEVE_PROPERTIES_PATH",
    "RETRIEVE_PROPERTIES_PATH",
    "TooManyPagesError",
    "retrieve_all_pages",
]

# Spec-relative VI-JSON paths; ``_post_vmomi_json`` puts them on the
# ``/sdk/vim25/{release}`` base. The PropertyCollector is a singleton whose
# moId is the literal ``propertyCollector``.
RETRIEVE_PROPERTIES_PATH: Final = "/PropertyCollector/propertyCollector/RetrievePropertiesEx"
CONTINUE_RETRIEVE_PROPERTIES_PATH: Final = (
    "/PropertyCollector/propertyCollector/ContinueRetrievePropertiesEx"
)
#: Most pages one read follows: a guard against an answer that never ends.
_MAX_PAGES: Final = 100


class TooManyPagesError(RuntimeError):
    """vCenter's answer had not ended after :data:`_MAX_PAGES` pages."""


def _payload(retrieve_result: Any) -> Any:
    """The ``RetrieveResult`` of one page, with the VI-JSON boxes removed.

    The legacy ``/rest`` mount wraps the answer in a ``{"value": ...}``
    envelope; the callers' parsers tolerated that box on the raw answer, so
    the helper does too.
    """
    payload = unwrap_vim_value(retrieve_result)
    if isinstance(payload, dict) and set(payload) == {"value"}:
        payload = payload["value"]
    return payload


def _objects(retrieve_result: Any) -> list[dict[str, Any]]:
    payload = _payload(retrieve_result)
    if isinstance(payload, dict):
        payload = payload.get("objects", [])
    if not isinstance(payload, list):
        return []
    return [obj for obj in payload if isinstance(obj, dict)]


def _token(retrieve_result: Any) -> str | None:
    """The continuation ``token`` of a ``RetrieveResult``, or ``None`` on the last page."""
    payload = _payload(retrieve_result)
    token = payload.get("token") if isinstance(payload, dict) else None
    return token if isinstance(token, str) and token else None


async def retrieve_all_pages(
    connector: VmwareRestConnector,
    target: VsphereTargetLike,
    operator: Operator,
    body: dict[str, Any],
    *,
    promote_missing: bool = False,
) -> dict[str, Any]:
    """Run one ``RetrievePropertiesEx`` and follow every continuation page.

    *body* is the request body (see
    :func:`~meho_backplane.connectors.vmware_rest.vim_body.retrieve_properties_body`).
    ``promote_missing`` goes to the first call as
    ``promote_managed_object_not_found``, for the callers that turn a missing
    object into MEHO's normal ``not_found`` answer.

    Returns ``{"objects": [...]}`` with the objects of all pages. A failed
    page raises the connector's error; an answer that has not ended after
    :data:`_MAX_PAGES` pages raises :class:`TooManyPagesError`.
    """
    session = SessionTokenUsed()
    result = await connector._post_vmomi_json(
        target,
        RETRIEVE_PROPERTIES_PATH,
        operator=operator,
        json=body,
        promote_managed_object_not_found=promote_missing,
        session_used=session,
    )
    objects = _objects(result)
    token = _token(result)
    pages = 1
    while token is not None:
        if pages >= _MAX_PAGES:
            raise TooManyPagesError(
                f"vCenter's answer had not ended after {_MAX_PAGES} pages; the read is incomplete"
            )
        result = await connector._post_vmomi_json(
            target,
            CONTINUE_RETRIEVE_PROPERTIES_PATH,
            operator=operator,
            json={"token": token},
            session_token=session.token,
        )
        objects.extend(_objects(result))
        token = _token(result)
        pages += 1
    return {"objects": objects}
