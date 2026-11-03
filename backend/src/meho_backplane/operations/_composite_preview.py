# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Synthetic preview for governed-tier non-ingested ops (#3198 / #3312).

The counterpart to :mod:`._request_preview`, which previews the literal
would-be HTTP request of an ``source_kind='ingested'`` op (#1683). A
``typed`` / ``composite`` op runs a Python handler and has no single literal
HTTP request, so by default it previews as ``status="unavailable"``. The two
governed approval tiers are the exception and MUST be previewable:

* **``destructive``** (#3198): the governed-delete gate refuses to park the
  op unless a ``preview_operation`` of the identical tuple bound a matching
  ``preview_hash`` (decision ``governed-delete-operations.md`` requirement 2).
* **``requires_approval``** (#3312): the ``dangerous``-tier typed ops
  (canonical case ``vault.kv.delete``) park for a human. The approver already
  sees a rich park-time ``proposed_effect``; previewing removes the
  pre-dispatch asymmetry so the calling agent reads the *same* effect block
  instead of ``preview_unavailable``. A **credential-class** op (e.g.
  ``vault.kv.put``) is excluded upstream by
  :func:`~meho_backplane.operations._request_preview._is_previewable` — its
  secret rides in the params, which the ``redacted_body`` slot below cannot be
  trusted to scrub — so it never reaches here.

The synthetic preview binds the *logical request tuple* (a safe view of the
params on the ``redacted_body`` slot, a keyed digest of the full params on
the ``params_digest`` slot, a ``COMPOSITE`` sentinel ``method``, the
``op_id`` as ``resolved_path``) so :func:`compute_preview_hash` stays
param-sensitive, and layers on the reused park-time ``proposed_effect``. No
handler runs, no connector is dialled, and nothing is sent — it is a pure,
egress-free projection.

``redacted_body`` is the same safe view the generic plan shows
(:func:`safe_params_view`): a secret-named value is masked, a value whose
shape looks like a secret is masked, and every value of a free-form map (a
parameter the schema types as an object without fixed ``properties``, for
example the OVF properties of an appliance deploy) is masked while its keys
stay. The hash still changes with every value, through ``params_digest``.

Every synthetic preview says whether it shows the plan (#4028):
``plan_shown=True`` when ``proposed_effect`` is present, otherwise
``plan_shown=False`` plus a plain ``plan_note``. A builder that must read the
target declines here, and its plan is built only at park time.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations._preview import (
    _PARAM_REDACTED,
    PreviewContext,
    _redact_echoed_params,
    build_proposed_effect,
    describe_preview_provenance,
)
from meho_backplane.operations._request_preview import (
    PARAMS_DIGEST_KEY,
    compute_preview_hash,
)
from meho_backplane.operations._validate import compute_params_hash

__all__ = ["PLAN_NOT_SHOWN_NOTE", "build_composite_preview", "safe_params_view"]

_log = structlog.get_logger(__name__)

#: Sentinel ``method`` for a synthetic composite/typed preview (#3198) —
#: there is no HTTP verb, but the slot is one of the hashed keys, so a
#: constant keeps the hash stable while the params on ``redacted_body`` make
#: it param-sensitive.
_COMPOSITE_PREVIEW_METHOD: Final[str] = "COMPOSITE"

#: Plain note set when the synthetic preview carries no ``proposed_effect``
#: (#4028). The preview never contacts the target, so a plan builder that
#: must read the target first (DRS rule create/delete, Keycloak deletes,
#: ``windns.record.remove``) declines here. Without this note the caller saw
#: ``status="ok"`` + a hash and could not tell that the plan was missing.
PLAN_NOT_SHOWN_NOTE: Final[str] = (
    "This preview does not show what the operation will change. "
    "The preview never contacts the target, so it cannot build the plan "
    "here. MEHO builds the plan when you send the "
    "call. If the call waits for approval, the plan is shown on the "
    "approval request before anyone approves it."
)


async def _synthetic_proposed_effect(
    *,
    operator: Operator,
    connector_id: str,
    descriptor: EndpointDescriptor,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any] | None:
    """Reuse the park-time ``proposed_effect`` builder for a pre-dispatch preview (#3312).

    Runs :func:`~meho_backplane.operations._preview.build_proposed_effect` — the
    *same* park-path builder, so no effect logic is duplicated and redaction is
    identical — but with ``connector_instance=None`` to keep the preview
    **egress-free**: a pure builder (``vault.kv.delete``'s ``_kv_delete_preview``
    reads only its params) populates, while a live-read blast-radius builder
    declines for lack of a connector. Returns the effect only when genuinely
    populated (bespoke ``preview`` or generic ``params_echo``, per
    :func:`describe_preview_provenance`); a decline / credential suppression /
    fail-soft ``preview_unavailable`` marker yields ``None`` so the synthetic
    preview stays clean.
    """
    proposed_effect = await build_proposed_effect(
        PreviewContext(
            descriptor=descriptor,
            connector_instance=None,
            operator=operator,
            target=target,
            params=params,
            connector_id=connector_id,
        )
    )
    populated, _reason = describe_preview_provenance(proposed_effect, op_id=descriptor.op_id)
    return proposed_effect if populated else None


def _hide_free_form_map_values(value: Any, schema: Any) -> Any:
    """Return *value* with every value of a free-form map replaced by the marker.

    Walks *value* next to its JSON *schema*. A mapping whose schema fixes
    its keys (``properties``) is walked key by key, and a key the schema
    does not name is masked. A mapping whose schema fixes no keys is a
    free-form map: every value is masked and every key stays. A mapping
    with no schema at all counts as free-form too, so an unknown shape
    fails closed. A list is walked with its ``items`` schema. Scalars pass
    through. The input is never mutated.
    """
    if isinstance(value, Mapping):
        properties = schema.get("properties") if isinstance(schema, Mapping) else None
        if not isinstance(properties, Mapping):
            return {str(key): _PARAM_REDACTED for key in value}
        return {
            str(key): (
                _hide_free_form_map_values(item, properties[key])
                if key in properties
                else _PARAM_REDACTED
            )
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = schema.get("items") if isinstance(schema, Mapping) else None
        return [_hide_free_form_map_values(item, items) for item in value]
    return value


def safe_params_view(
    params: dict[str, Any],
    *,
    parameter_schema: Any,
    connector_id: str,
    operator: Operator,
    op_id: str,
) -> dict[str, Any]:
    """Return the view of *params* a synthetic preview may show.

    Three passes. First every value of a free-form map is replaced by the
    fixed marker (:func:`_hide_free_form_map_values`): the top-level keys
    are the operation's parameter names and always stay, and each value is
    walked with the matching ``properties`` entry of *parameter_schema*.
    Then the two passes of the generic plan
    (:func:`~meho_backplane.operations._preview._redact_echoed_params`)
    run: a secret-named value is masked, and the connector-boundary engine
    masks a value whose shape looks like a secret. So ``redacted_body``
    shows no more than the plan the approver sees.
    """
    properties = (
        parameter_schema.get("properties") if isinstance(parameter_schema, Mapping) else None
    )
    known = properties if isinstance(properties, Mapping) else {}
    hidden = {
        str(key): _hide_free_form_map_values(item, known.get(key)) for key, item in params.items()
    }
    tenant = str(operator.tenant_id) if operator.tenant_id is not None else None
    return _redact_echoed_params(hidden, connector_id=connector_id, tenant=tenant, op_id=op_id)


async def build_composite_preview(
    *,
    operator: Operator,
    connector_id: str,
    op_id: str,
    descriptor: EndpointDescriptor,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the synthetic preview for a governed-tier non-ingested op (#3198/#3312).

    A composite / typed op has no single literal HTTP request, so the
    preview-hash binding (decision ``governed-delete-operations.md``
    requirement 2) binds the *logical request tuple*: the safe view of the
    params (:func:`safe_params_view`) rides the ``redacted_body`` slot and the
    keyed digest of the full params
    (:func:`~meho_backplane.operations._validate.compute_params_hash`) rides
    the ``params_digest`` slot, so :func:`compute_preview_hash` — unaffected by
    the ``proposed_effect`` key it does not hash — is param-sensitive (two
    previews that differ in one masked value hash differently) while ``method``
    (a ``COMPOSITE`` sentinel) and ``resolved_path`` (the ``op_id``) name the
    op. The digest is keyed with the server key, so a masked value cannot be
    guessed from it. On
    top of that projection it reuses the park-time ``proposed_effect`` (#3312,
    via :func:`_synthetic_proposed_effect`) so the calling agent reads the same
    effect block the approver sees. Only reached for the ``destructive`` /
    ``requires_approval`` tiers (the gate in
    :func:`~meho_backplane.operations._request_preview._gate_and_validate`);
    every other typed/composite op keeps the ``"unavailable"`` contract. No
    handler runs and nothing is sent.
    """
    redacted_body = safe_params_view(
        params,
        parameter_schema=descriptor.parameter_schema,
        connector_id=connector_id,
        operator=operator,
        op_id=op_id,
    )
    _log.info(
        "preview_dispatch_composite",
        connector_id=connector_id,
        op_id=op_id,
        source_kind=descriptor.source_kind,
        safety_level=descriptor.safety_level,
        tenant_id=str(operator.tenant_id),
    )
    envelope: dict[str, Any] = {
        "status": "ok",
        "op_id": op_id,
        "connector_id": connector_id,
        "source_kind": descriptor.source_kind,
        "method": _COMPOSITE_PREVIEW_METHOD,
        "resolved_path": op_id,
        "query": None,
        "redacted_body": redacted_body,
        PARAMS_DIGEST_KEY: compute_params_hash(params),
    }
    envelope["preview_hash"] = compute_preview_hash(envelope)
    proposed_effect = await _synthetic_proposed_effect(
        operator=operator,
        connector_id=connector_id,
        descriptor=descriptor,
        target=target,
        params=params,
    )
    if proposed_effect is not None:
        envelope["proposed_effect"] = proposed_effect
        envelope["plan_shown"] = True
    else:
        envelope["plan_shown"] = False
        envelope["plan_note"] = PLAN_NOT_SHOWN_NOTE
    return envelope
