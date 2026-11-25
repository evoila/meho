# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The agent hint of ``k8s.apply``: one preview, then one call, one approval (#6921).

The old hint said: call ``k8s.apply`` with ``dry_run='server'`` first, then
with ``dry_run='none'``. Both calls wait for approval, so each manifest cost
two approvals.

The new hint says two things instead:

* look at the manifests first with ``preview_operation``. It needs no
  approval and does not contact the cluster. It shows the manifests the call
  would apply. The approval request of the real call shows the result of a
  server dry run to the person who approves;
* put all manifests of one deploy into one ``k8s.apply`` call. The call
  applies them in order, so one approval covers the whole deploy.

All of this is behind the switch ``K8S_APPLY_ONE_APPROVAL_HINT``
(:attr:`~meho_backplane.settings.Settings.k8s_apply_one_approval_hint`, off
by default). Off: ``k8s.apply`` is described as before. No rule changes: the
call waits for approval, the risk level and the parameters stay.
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
    "Apply a manifest (replaces 'kubectl apply -f'). Look at the change first with "
    "preview_operation: it needs no approval and shows the manifests the call would apply. "
    "Then send all manifests of one deploy in one k8s.apply call, in the order they must "
    "exist (for example namespace, deployment, service). The call applies them in that order "
    "and needs one approval. Do not call k8s.apply with dry_run='server' to check first: that "
    "call waits for approval too. The approval request shows the result of a server dry run. "
    "If the same call creates the namespace, the check of the objects in it can fail there "
    "because the namespace does not exist yet. Requires approval."
)
_DRY_RUN_HINT = (
    "'server' checks the manifests on the cluster and changes nothing, but the call still "
    "waits for approval. 'none' applies."
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
    """Return ``k8s.apply`` with the new hint in its summary and ``llm_instructions``."""
    instructions = dict(op.llm_instructions or {})
    instructions["when_to_use"] = _WHEN_TO_USE
    hints = dict(instructions.get("parameter_hints") or {})
    hints["dry_run"] = _DRY_RUN_HINT
    instructions["parameter_hints"] = hints
    return replace(op, summary=_SUMMARY, llm_instructions=instructions)


def with_switched_apply_hint(op: KubernetesOp) -> KubernetesOp:
    """Return *op* as it is registered: ``k8s.apply`` gets the new hint while the switch is on."""
    if op.op_id == APPLY_OP_ID and apply_hint_on():
        return with_apply_hint(op)
    return op
