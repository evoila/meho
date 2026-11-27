# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The dry run that an agent's own preview of ``k8s.apply`` shows.

With the switch ``K8S_APPLY_PREVIEW_DRY_RUN`` on, ``preview_operation`` for
``k8s.apply`` asks the cluster what the apply would do. MEHO sends a
server-side apply with the API's ``dryRun=All`` for every document of the
manifest, with the target's own cluster account, the same account as the
real apply. Nothing is stored on the cluster.

For each document the preview shows:

* ``document``: its place in the manifest, counted from 1;
* ``kind``, ``namespace`` and ``name``, hidden as on the approval request;
* ``change``: ``create`` (the API server answers 201 Created), ``change``
  (it answers 200 OK), ``refused`` (it answers with an error) or
  ``not_checked`` (MEHO could not ask, for example the kind is not known on
  the cluster);
* ``warnings``: the API server's warning texts;
* ``error`` for a refused document and ``reason`` for one that was not
  checked, with secret values hidden;
* ``note`` when the namespace of a refused object is made by a document of
  the same manifest: a dry run cannot see that namespace yet.

``not_checked`` lists the documents that could not be checked. The preview
then says that it does not show the whole plan.

Unlike the dry run of the approval request, this one does not stop at the
first refused document: the agent sees the answer for each one. The
approval request itself is not changed.
"""

from __future__ import annotations

from typing import Any, Final

import structlog
from kubernetes_asyncio.dynamic import DynamicClient

from meho_backplane.connectors.kubernetes._apply_manifest import _parse_manifest
from meho_backplane.connectors.kubernetes.ops_write_dangerous import (
    _reject,
    _response_warnings,
    _send_apply,
    error_subject,
)
from meho_backplane.connectors.kubernetes.ops_write_preview import card_documents
from meho_backplane.operations._preview import (
    PreviewContext,
    _identity,
    _k8s_apply_manifest_echo,
    _k8s_apply_redactor,
    _name_objects_as_on_card,
    preview_failure_reason,
)

__all__ = ["NOT_CHECKED_KEY", "k8s_apply_agent_dry_run"]

_log = structlog.get_logger(__name__)

#: The key of the preview that lists the documents the dry run could not check.
NOT_CHECKED_KEY: Final[str] = "not_checked"

#: The note on a refused object whose namespace the same manifest makes.
_NAMESPACE_IN_MANIFEST_NOTE: Final[str] = (
    "The namespace of this object is made by a document of this same "
    "manifest. A dry run cannot see that namespace yet, so the real apply "
    "may not be refused."
)


def _reason(ctx: PreviewContext, exc: Exception) -> str:
    """The safe text of *exc*: object names as on the card, secret values hidden."""
    _name_objects_as_on_card(ctx, exc)
    return preview_failure_reason(exc)


def _made_namespaces(docs: list[dict[str, Any]]) -> set[str]:
    """The names of the namespaces the manifest itself makes."""
    return {
        str((doc.get("metadata") or {}).get("name"))
        for doc in docs
        if doc.get("apiVersion") == "v1" and doc.get("kind") == "Namespace"
    }


async def _dry_run_one(
    ctx: PreviewContext,
    dyn: DynamicClient,
    doc: dict[str, Any],
    *,
    force_conflicts: bool,
    made_namespaces: set[str],
) -> dict[str, Any]:
    """Ask the cluster what the apply of *doc* would do; never raises for a cluster fault."""
    try:
        resource = await dyn.resources.get(api_version=doc["apiVersion"], kind=doc["kind"])
        response, body = await _send_apply(
            dyn, resource, doc, dry_run=True, force_conflicts=force_conflicts
        )
    except Exception as exc:
        return {"change": "not_checked", "reason": _reason(ctx, exc)}
    warnings = _response_warnings(response)
    if 200 <= response.status <= 299:
        return {"change": "create" if response.status == 201 else "change", "warnings": warnings}
    error = _reject(response, body, error_subject(doc), force_conflicts=force_conflicts)
    entry: dict[str, Any] = {
        "change": "refused",
        "warnings": warnings,
        "error": _reason(ctx, error),
    }
    namespace = (doc.get("metadata") or {}).get("namespace")
    if response.status == 404 and namespace in made_namespaces:
        entry["note"] = _NAMESPACE_IN_MANIFEST_NOTE
    return entry


async def k8s_apply_agent_dry_run(ctx: PreviewContext) -> dict[str, Any]:
    """The preview of ``k8s.apply`` for the agent: the dry run of every document.

    Needs ``ctx.connector_instance`` (a :class:`KubernetesConnector`) and
    ``ctx.target``. Every request carries ``dryRun=All``. A fault before the
    first document (the manifest cannot be read, the cluster login fails)
    raises, and the caller shows that the dry run did not work.
    """
    manifest: str = ctx.params["manifest"]
    docs = _parse_manifest(manifest)
    shown = [_identity(doc) for doc in _k8s_apply_redactor(ctx)(card_documents(manifest))]
    force_conflicts = ctx.params.get("force_conflicts") is True
    connector: Any = ctx.connector_instance
    api_client = await connector._get_api_client(ctx.target, ctx.operator)
    dyn = await DynamicClient(api_client)
    made_namespaces = _made_namespaces(docs)
    documents = [
        {
            "document": number,
            **identity,
            **await _dry_run_one(
                ctx, dyn, doc, force_conflicts=force_conflicts, made_namespaces=made_namespaces
            ),
        }
        for number, (doc, identity) in enumerate(zip(docs, shown, strict=False), start=1)
    ]
    not_checked = [entry["document"] for entry in documents if entry["change"] == "not_checked"]
    _log.info(
        "k8s_apply_preview_dry_run",
        documents=len(documents),
        not_checked=len(not_checked),
        tenant_id=str(ctx.operator.tenant_id),
    )
    preview: dict[str, Any] = {"dry_run": True, "documents": documents}
    if not_checked:
        preview[NOT_CHECKED_KEY] = not_checked
    preview.update(_k8s_apply_manifest_echo(ctx, {"applied": shown}))
    return preview
