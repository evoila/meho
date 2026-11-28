# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The dry run that an agent's own preview of ``k8s.apply`` shows.

With the switch ``K8S_APPLY_PREVIEW_DRY_RUN`` on, ``preview_operation`` for
``k8s.apply`` asks the cluster what the apply would do. MEHO sends a
server-side apply with the API's ``dryRun=All`` for every document of the
manifest, with the target's own cluster account, the same account as the
real apply. Every request that can change something carries ``dryRun=All``.
The only other requests are ``GET`` reads of the cluster's API list
(``/version``, ``/api``, ``/apis`` and the group paths). Nothing is stored
on the cluster. With the switch off, this module is never called.

For each document the preview shows:

* ``document``: its place in the manifest, counted from 1. Empty documents
  count too, as in the refusal of the manifest check;
* ``kind``, ``namespace`` and ``name``, hidden as on the approval request;
* ``change``: ``create`` (the API server answers 201 Created), ``change``
  (it answers 200 OK), ``refused`` (the cluster decided against it: 400,
  403, 404, 409, 422 or another answer of the 4xx range) or ``not_checked``
  (the cluster did not decide: 401, 408, 429 or a 5xx answer, no answer, or
  a kind the cluster does not know);
* ``warnings``: the API server's warning texts;
* ``error`` for a refused document and ``reason`` for one that was not
  checked;
* ``note`` when the namespace of a refused object is made by a document of
  the same manifest: a dry run cannot see that namespace yet.

Each text of the cluster is shown with secret-shaped text hidden. A
document holds hidden values when the preview hides a value of it in its
echo of the manifest (``ops_write_preview.apply_manifest_echo``, then the
shared params redaction). For such a document, ``warnings``, ``error`` and
``reason`` hold one fixed sentence (:data:`HIDDEN_VALUES_TEXT`) in place of
the cluster's text; ``warnings`` holds it only when the cluster sent a
warning. A document the preview cannot name safely is not sent and is
``not_checked``.

``not_checked`` lists the documents that were not checked. The preview then
says that it does not show the whole plan. A list of documents longer than
the limit for a list in an answer (more than 50 entries or 4 KB) is stored
behind a result handle; ``result_query`` reads every entry.

Unlike the dry run of the approval request, this one does not stop at the
first refused document: the agent sees the answer for each one. The
approval request itself is not changed.
"""

from __future__ import annotations

from typing import Any, Final

import structlog
from kubernetes_asyncio import client
from kubernetes_asyncio.client.exceptions import ApiException
from kubernetes_asyncio.dynamic import DynamicClient

from meho_backplane.connectors.kubernetes._apply_manifest import parse_numbered_manifest
from meho_backplane.connectors.kubernetes.ops_write_dangerous import (
    _reject,
    _response_warnings,
    _send_apply,
    _status_message,
    error_subject,
)
from meho_backplane.connectors.kubernetes.ops_write_preview import _hide_values, card_documents
from meho_backplane.operations._preview import (
    _PREVIEW_ERROR_MAX_LEN,
    PreviewContext,
    _identity,
    _k8s_apply_manifest_echo,
    _k8s_apply_redactor,
    _name_objects_as_on_card,
    _redact_preview_message,
)
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer

__all__ = ["HIDDEN_VALUES_TEXT", "NOT_CHECKED_KEY", "DryRunSetupError", "k8s_apply_agent_dry_run"]

_log = structlog.get_logger(__name__)

#: The key of the preview that lists the documents the dry run could not check.
NOT_CHECKED_KEY: Final[str] = "not_checked"

#: The text shown in place of the cluster's text for a document that holds hidden values.
HIDDEN_VALUES_TEXT: Final[str] = (
    "MEHO does not pass on the cluster's text for a document that holds hidden values."
)

#: The answers that are not a decision of the cluster about the document.
_NOT_DECIDED_STATUSES: Final[frozenset[int]] = frozenset({401, 408, 429})

#: The same limits as every list in an answer (more than 50 entries or 4 KB).
_DOCUMENTS_REDUCER: Final[JsonFluxReducer] = JsonFluxReducer()

#: The note on a refused object whose namespace the same manifest makes.
_NAMESPACE_IN_MANIFEST_NOTE: Final[str] = (
    "The namespace of this object is made by a document of this same "
    "manifest. A dry run cannot see that namespace yet, so the real apply "
    "may not be refused."
)

#: The reason for a document the preview cannot name safely.
_NOT_NAMED_REASON: Final[str] = (
    "MEHO cannot show the name of this document safely, so it did not send its dry run."
)


class DryRunSetupError(Exception):
    """The dry run could not start; the message is a plain sentence for the agent."""


class _NotDecidedError(Exception):
    """The cluster answered the apply of one document without a decision."""


def _safe_text(ctx: PreviewContext, exc: Exception) -> str:
    """The text of *exc* for the agent: names as on the card, secret-shaped text hidden."""
    _name_objects_as_on_card(ctx, exc)
    message = str(exc) or "the request to the cluster failed"
    text = _redact_preview_message(message).replace("\x00", "\\0")
    if len(text) > _PREVIEW_ERROR_MAX_LEN:
        text = text[:_PREVIEW_ERROR_MAX_LEN] + " [truncated]"
    return text


def _made_namespaces(docs: list[dict[str, Any]]) -> set[str]:
    """The names of the namespaces the manifest itself makes."""
    return {
        str((doc.get("metadata") or {}).get("name"))
        for doc in docs
        if doc.get("apiVersion") == "v1" and doc.get("kind") == "Namespace"
    }


def _decided(status: int) -> bool:
    """True when an answer with *status* is a decision of the cluster about the document."""
    return status not in _NOT_DECIDED_STATUSES and status < 500


async def _dry_run_one(
    ctx: PreviewContext,
    dyn: DynamicClient,
    doc: dict[str, Any],
    *,
    force_conflicts: bool,
    made_namespaces: set[str],
    holds_hidden: bool,
) -> dict[str, Any]:
    """Ask the cluster what the apply of *doc* would do; never raises for a cluster fault.

    With *holds_hidden*, each text of the cluster is :data:`HIDDEN_VALUES_TEXT`.
    """

    def text_of(exc: Exception) -> str:
        return HIDDEN_VALUES_TEXT if holds_hidden else _safe_text(ctx, exc)

    try:
        resource = await dyn.resources.get(api_version=doc["apiVersion"], kind=doc["kind"])
        response, body = await _send_apply(
            dyn, resource, doc, dry_run=True, force_conflicts=force_conflicts
        )
    except Exception as exc:
        return {"change": "not_checked", "reason": text_of(exc)}
    warnings = _response_warnings(response)
    if holds_hidden and warnings:
        warnings = [HIDDEN_VALUES_TEXT]
    if 200 <= response.status <= 299:
        return {"change": "create" if response.status == 201 else "change", "warnings": warnings}
    if not _decided(response.status):
        status_line = " ".join(str(part) for part in (response.status, response.reason) if part)
        message = _status_message(body)
        detail = f": {message}" if message else ""
        not_decided = _NotDecidedError(
            f"the cluster did not check {error_subject(doc)} (HTTP {status_line}){detail}"
        )
        return {
            "change": "not_checked",
            "warnings": warnings,
            "reason": text_of(not_decided),
        }
    error = _reject(response, body, error_subject(doc), force_conflicts=force_conflicts)
    entry: dict[str, Any] = {
        "change": "refused",
        "warnings": warnings,
        "error": text_of(error),
    }
    namespace = (doc.get("metadata") or {}).get("namespace")
    if response.status == 404 and namespace in made_namespaces:
        entry["note"] = _NAMESPACE_IN_MANIFEST_NOTE
    return entry


def _identities(
    ctx: PreviewContext, plain_docs: list[dict[str, Any]]
) -> list[dict[str, Any] | None]:
    """``kind``, ``namespace`` and ``name`` of each document as the card shows them.

    ``None`` for a document whose hidden form the redaction did not give
    back: the preview cannot name it safely.
    """
    redact = _k8s_apply_redactor(ctx)
    shown = redact(plain_docs)
    if len(shown) == len(plain_docs):
        return [_identity(doc) for doc in shown]
    identities: list[dict[str, Any] | None] = []
    for doc in plain_docs:
        one = redact([doc])
        identities.append(_identity(one[0]) if len(one) == 1 else None)
    return identities


def _holds_hidden(ctx: PreviewContext, plain_docs: list[dict[str, Any]]) -> list[bool]:
    """For each document, whether the manifest echo hides a value of it.

    The same hiding as the echo: the connector's own (secret values, ``env``
    and ``httpHeaders`` values, command lines, the last-applied annotation),
    then the shared params redaction over the whole list, so a rule for one
    place in the list counts. When the redaction does not give back one
    document for each, every document counts as one with hidden values.
    """
    shown = _k8s_apply_redactor(ctx)([_hide_values(doc) for doc in plain_docs])
    if len(shown) != len(plain_docs):
        return [True] * len(plain_docs)
    return [hidden != doc for hidden, doc in zip(shown, plain_docs, strict=True)]


async def _cluster_client(ctx: PreviewContext) -> DynamicClient:
    """The dynamic client of the target; raise :class:`DryRunSetupError` with a plain reason."""
    connector: Any = ctx.connector_instance
    try:
        api_client = await connector._get_api_client(ctx.target, ctx.operator)
    except Exception as exc:
        reason = _safe_text(ctx, exc)
        raise DryRunSetupError(f"MEHO could not sign in to the cluster: {reason}") from None
    try:
        dyn: DynamicClient = await DynamicClient(api_client)
    except Exception as exc:
        raise await _discovery_error(ctx, api_client, exc) from None
    return dyn


async def _discovery_error(
    ctx: PreviewContext, api_client: Any, exc: Exception
) -> DryRunSetupError:
    """Why the read of the cluster's API list failed, in a plain sentence.

    The client library hides the answer of a refused read, so MEHO asks for
    ``/version`` once more to learn the HTTP status.
    """
    try:
        await client.VersionApi(api_client).get_code()
    except ApiException as answer:
        if answer.status == 401:
            return DryRunSetupError(
                "the cluster login failed: the cluster refused the login of the "
                "target's cluster account (HTTP 401)"
            )
        if answer.status == 403:
            return DryRunSetupError(
                "the cluster refused the target's cluster account the read of its "
                "API list (HTTP 403 Forbidden)"
            )
        return DryRunSetupError(
            f"the cluster did not give its API list (HTTP {answer.status} {answer.reason})"
        )
    except Exception as failure:
        exc = failure
    return DryRunSetupError(
        f"MEHO could not read the API list of the cluster: {_safe_text(ctx, exc)}"
    )


async def _reduced_documents(ctx: PreviewContext, documents: list[dict[str, Any]]) -> Any:
    """*documents*, or its summary with a result handle when the list is too long."""
    context = {
        "op_id": ctx.descriptor.op_id,
        "tenant_id": str(ctx.operator.tenant_id),
        "operator_sub": ctx.operator.sub,
    }
    summary, handle = await _DOCUMENTS_REDUCER.reduce({"documents": documents}, None, context)
    if handle is None:
        return documents
    return {**summary, "handle": handle.model_dump(mode="json")}


async def k8s_apply_agent_dry_run(ctx: PreviewContext) -> dict[str, Any]:
    """The preview of ``k8s.apply`` for the agent: the dry run of every document.

    Needs ``ctx.connector_instance`` (a :class:`KubernetesConnector`) and
    ``ctx.target``. Every request that can change something carries
    ``dryRun=All``; the other requests read the cluster's API list. A fault
    before the first document (the manifest cannot be read, the cluster
    login fails) raises, and the caller shows that the dry run did not work.
    Every document of the manifest has one entry, made for that document.
    """
    manifest: str = ctx.params["manifest"]
    numbered = parse_numbered_manifest(manifest)
    plain_docs = card_documents(manifest)
    identities = _identities(ctx, plain_docs)
    holds_hidden = _holds_hidden(ctx, plain_docs)
    force_conflicts = ctx.params.get("force_conflicts") is True
    dyn = await _cluster_client(ctx)
    made_namespaces = _made_namespaces([doc for _, doc in numbered])
    documents: list[dict[str, Any]] = []
    for (number, doc), identity, hidden in zip(numbered, identities, holds_hidden, strict=True):
        if identity is None:
            documents.append(
                {"document": number, "change": "not_checked", "reason": _NOT_NAMED_REASON}
            )
            continue
        answer = await _dry_run_one(
            ctx,
            dyn,
            doc,
            force_conflicts=force_conflicts,
            made_namespaces=made_namespaces,
            holds_hidden=hidden,
        )
        documents.append({"document": number, **identity, **answer})
    not_checked = [entry["document"] for entry in documents if entry["change"] == "not_checked"]
    _log.info(
        "k8s_apply_preview_dry_run",
        documents=len(documents),
        not_checked=len(not_checked),
        tenant_id=str(ctx.operator.tenant_id),
    )
    preview: dict[str, Any] = {
        "dry_run": True,
        "documents": await _reduced_documents(ctx, documents),
    }
    if not_checked:
        preview[NOT_CHECKED_KEY] = not_checked
    shown = [identity for identity in identities if identity is not None]
    preview.update(_k8s_apply_manifest_echo(ctx, {"applied": shown}))
    return preview
