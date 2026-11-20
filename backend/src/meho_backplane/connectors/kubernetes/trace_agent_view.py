# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What an agent reads in the stored trace of a Kubernetes call.

The flight recorder stores the Kubernetes API answer of a call, with the
whole object: for a pod or a Deployment, the full pod definition. A person
reads the stored trace as it is. An agent gets a copy made by
:func:`agent_view`, which hides, at any depth of the span:

* the ``env`` values that the direct answer of an operation hides: each
  ``env`` list goes through the same secret rules as the default view of
  an answer
  (:func:`~meho_backplane.operations._secret_values.hide_secret_values`):
  :func:`~meho_backplane.broadcast.events.scrub_secret_named_values` with
  numbers hidden, then
  :func:`~meho_backplane.redaction.secret_text.hide_secret_text_leaves`.
  So the value of an entry with a secret-shaped name (``DB_PASSWORD``) is
  hidden, and so is a secret field inside a text value. A plain value
  (``LOG_LEVEL=debug``), the names, ``valueFrom`` references and
  ``envFrom`` stay. Being at any depth, this covers pods, Deployments,
  StatefulSets, DaemonSets, ReplicaSets, Jobs, CronJobs, pod templates in
  custom resources, and the items of a list;
* the secret values on every command line: a ``command`` and ``args`` list
  pair is read as one command line and passed through the same pass the
  ``k8s.job.create`` approval preview uses
  (:func:`~meho_backplane.connectors.kubernetes.ops_write_preview._hide_command_line`).
  The words stay. The value of ``PGPASSWORD=...`` or ``--password=...``,
  the argument after ``--password`` or ``--token``, the password in a URL
  and tokens with a known vendor prefix are hidden. The script after
  ``sh -c`` is read as a shell script;
* annotation values: a value that is a JSON object or list (the
  ``kubectl.kubernetes.io/last-applied-configuration`` annotation holds a
  whole earlier object) is cleaned the same way, each text inside it is
  passed through :func:`~meho_backplane.redaction.command_line.hide_in_text`,
  and it is written back as JSON. The last-applied annotation becomes
  :data:`LAST_APPLIED_MARKER` when it is not such a value. Any other text value is passed through
  :func:`~meho_backplane.redaction.command_line.hide_in_text`, which hides
  the password in a URL and tokens with a known vendor prefix.

Nothing else in the span changes: ``managedFields`` and plain values stay
as they are in the stored span. The capture-time redaction (Secret
values, secret-named fields, credential shapes) has already run over the
stored span.

The walk takes time in line with the size of the span. The caller runs it
in a worker thread, so a large trace does not hold up other requests.
"""

from __future__ import annotations

import json
from typing import Any, Final

from meho_backplane.broadcast.events import scrub_secret_named_values
from meho_backplane.connectors.kubernetes.ops_write_preview import _hide_command_line
from meho_backplane.redaction.command_line import hide_in_text
from meho_backplane.redaction.secret_text import hide_secret_text_leaves

__all__ = ["LAST_APPLIED_MARKER", "agent_view"]

#: Put in place of a last-applied annotation that is not a JSON object or list.
LAST_APPLIED_MARKER: Final[str] = "[MEHO-REDACTED:last-applied-configuration]"

_LAST_APPLIED: Final[str] = "kubectl.kubernetes.io/last-applied-configuration"


def agent_view(node: Any) -> Any:
    """Copy *node* with the values listed in the module docstring hidden.

    Never changes *node*. Raises ``RecursionError`` on a structure nested
    deeper than Python's limit; the caller then withholds the trace.
    """
    if isinstance(node, dict):
        command_line = _hide_command_line(node)
        out: dict[str, Any] = {}
        for key, item in node.items():
            if key in command_line:
                out[key] = command_line[key]
            elif key == "env" and isinstance(item, list):
                out[key] = _env_list(item)
            elif key == "annotations" and isinstance(item, dict):
                out[key] = {
                    name: _annotation_value(str(name), value) for name, value in item.items()
                }
            else:
                out[key] = agent_view(item)
        return out
    if isinstance(node, list):
        return [agent_view(item) for item in node]
    return node


def _env_list(env: list[Any]) -> Any:
    """One ``env`` list with the secret rules of the direct answer applied."""
    shown, _ = scrub_secret_named_values(agent_view(env), hide_numbers=True)
    return hide_secret_text_leaves(shown)


def _annotation_value(name: str, value: Any) -> Any:
    """One annotation value, cleaned as described in the module docstring."""
    if not isinstance(value, str):
        return _hide_texts(agent_view(value))
    if value.lstrip()[:1] in ("{", "["):
        try:
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            parsed = None
        if isinstance(parsed, (dict, list)):
            cleaned = _hide_texts(agent_view(parsed))
            return json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"))
    if name == _LAST_APPLIED:
        return LAST_APPLIED_MARKER
    return hide_in_text(value)


def _hide_texts(node: Any) -> Any:
    """Copy *node* with :func:`hide_in_text` applied to every text value in it."""
    if isinstance(node, dict):
        return {key: _hide_texts(item) for key, item in node.items()}
    if isinstance(node, list):
        return [_hide_texts(item) for item in node]
    if isinstance(node, str):
        return hide_in_text(node)
    return node
