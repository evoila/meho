# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The agent hint of ``k8s.apply`` behind the switch ``K8S_APPLY_ONE_APPROVAL_HINT``.

The switch is
:attr:`~meho_backplane.settings.Settings.k8s_apply_one_approval_hint`, off by
default. With it on, ``register_operations()`` stores ``k8s.apply`` with these
texts:

* the summary line "Server-side apply one or more manifests in order; needs
  one approval per call." It shows in every ``search_operations`` answer;
* a ``when_to_use`` text, a ``dry_run`` parameter hint and an
  ``output_shape`` text. The agent reads them in the ``guidance`` block of an
  exact-op ``search_operations`` call. That block is there only while
  ``OPERATION_CONTRACT_GUIDANCE`` is on too (off by default).

The ``when_to_use`` text says that ``preview_operation`` needs no approval and
does not contact the cluster: it shows the manifest text that was sent, after
the check of the parameter shape. It says to send all manifests of one deploy
in one ``k8s.apply`` call, as one manifest text with the documents split by
``---``. No text tells the agent to call ``k8s.apply`` with a dry run, or to
read a dry-run answer, before the real call.

With the switch off, ``k8s.apply`` keeps the texts written in
:mod:`~meho_backplane.connectors.kubernetes.ops_write_meta`. The switch changes
no rule: the risk level, the approval rule and the parameters stay.
"""

from __future__ import annotations

import os
from dataclasses import replace
from typing import TYPE_CHECKING

from meho_backplane.settings import parse_bool_env

if TYPE_CHECKING:
    from meho_backplane.connectors.kubernetes.ops import KubernetesOp

__all__ = ["APPLY_OP_ID", "apply_hint_on", "with_apply_hint", "with_switched_apply_hint"]

APPLY_OP_ID = "k8s.apply"

_WHEN_TO_USE = (
    "Apply one or more manifests (replaces 'kubectl apply -f'). preview_operation needs no "
    "approval and does not contact the cluster: it shows the manifest text that was sent, "
    "after the check of the parameter shape. Send all manifests of one deploy in one "
    "k8s.apply call, as one manifest text with the documents split by '---', in the order "
    "they must exist (for example namespace, deployment, service). The call applies them in "
    "that order and needs one approval. A call with dry_run='server' waits for approval too. "
    "The approval request shows the result of a server dry run. When the same call creates "
    "the namespace, that check of the objects in it can fail, because the namespace does not "
    "exist yet. Every call waits for a person, also with approval off; a standing grant does "
    "not cover it."
)
_DRY_RUN_HINT = (
    "'server' checks the manifests on the cluster and changes nothing, but the call still "
    "waits for approval. 'none' applies."
)
_OUTPUT_SHAPE = (
    "{dry_run, force_conflicts, field_manager, applied: [{api_version, kind, name, "
    "namespace, resource_version, uid, warnings, takes_over?}], total}. takes_over "
    "(dry run with force_conflicts) lists {owner, field} pairs the apply takes over. "
    "A conflict without force_conflicts is an error naming the owner and fields. "
    "warnings lists the API server's warnings for that document, such as a "
    "deprecated apiVersion."
)
_SUMMARY = "Server-side apply one or more manifests in order; needs one approval per call."


def apply_hint_on() -> bool:
    """Return the state of the ``K8S_APPLY_ONE_APPROVAL_HINT`` switch.

    Read from the env name, with the same parser as
    :attr:`~meho_backplane.settings.Settings.k8s_apply_one_approval_hint`, so
    operation registration does not need the rest of the settings.
    """
    return parse_bool_env(os.environ.get("K8S_APPLY_ONE_APPROVAL_HINT"))


def with_apply_hint(op: KubernetesOp) -> KubernetesOp:
    """Return ``k8s.apply`` with the switch-on summary and ``llm_instructions`` texts."""
    instructions = dict(op.llm_instructions or {})
    instructions["when_to_use"] = _WHEN_TO_USE
    instructions["output_shape"] = _OUTPUT_SHAPE
    hints = dict(instructions.get("parameter_hints") or {})
    hints["dry_run"] = _DRY_RUN_HINT
    instructions["parameter_hints"] = hints
    return replace(op, summary=_SUMMARY, llm_instructions=instructions)


def with_switched_apply_hint(op: KubernetesOp) -> KubernetesOp:
    """Return *op* as registered: ``k8s.apply`` gets the switch-on texts while the switch is on."""
    if op.op_id == APPLY_OP_ID and apply_hint_on():
        return with_apply_hint(op)
    return op
