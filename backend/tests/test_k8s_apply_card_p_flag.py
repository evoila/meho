# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``k8s.apply`` card hides the argument after ``-p`` after any program.

Each call goes through ``dispatch`` to the stored approval request, over the
stand-in Kubernetes API server of ``test_k8s_apply_card_through_dispatch``,
then a second person approves it and MEHO runs it. Each value is made up,
with no word a hide rule knows.

* The card's list of what each container runs shows ``-p`` and then the
  hidden mark, as the Job previews do for the same command line, in the
  console card, the person REST view and the stored card, also after the run.
* A custom kind with a map under a secret name (``credentials``) shows the
  whole map hidden, on every card and in every view.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest
import yaml

from meho_backplane.connectors.kubernetes.op_classes import hide_command_secrets
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from tests.test_k8s_apply_card_secrets import _approve_and_run, _made_up
from tests.test_k8s_apply_card_through_dispatch import (
    _bff_env,
    _console_card,
    _kubernetes_registry,
    _login,
    _park,
    _setup,
    _views,
)
from tests.test_k8s_apply_one_secret_rule import _RecordingServer

__all__ = ["_bff_env", "_kubernetes_registry"]

_HIDDEN = "***REDACTED***"

#: A command line with ``V`` after ``-p``, after programs that are not in the
#: list of the storage rule (``prog-a``, ``prog-b``) and after ``mysql``.
_P_FORMS: dict[str, str] = {
    "program and -p as items": "command: [prog-a, -u, user, -p, '{v}']",
    "program, -p and more words": "command: [prog-b, -u, guest, -p, '{v}', list, queues]",
    "inside sh -c": "command: [sh, -c, \"prog-a -u user -p {v} -e 'select 1'\"]",
    "one argument item": "args: ['prog-b -p {v} list queues']",
    "mysql with -p in its argument": "command: [mysql]\n    args: ['-h db -p {v}']",
}


def _pod_block(container: str) -> str:
    """A Pod alone in the manifest, with one container in YAML block style."""
    return (
        "apiVersion: v1\nkind: Pod\nmetadata: {name: report, namespace: prod}\n"
        "spec:\n  containers:\n  - name: app\n    image: report:1\n"
        f"    {container}\n"
    )


def _run_commands(effect: dict[str, Any]) -> list[dict[str, Any]]:
    return effect["preview"]["run_commands"] if "preview" in effect else effect["run_commands"]


def _command_line(effect: dict[str, Any]) -> list[Any]:
    (entry,) = _run_commands(effect)
    return [*entry.get("command", []), *entry.get("args", [])]


def _stored(row_id: uuid.UUID) -> ApprovalRequest:
    async def _read() -> ApprovalRequest:
        async with get_sessionmaker()() as session:
            row = await session.get(ApprovalRequest, row_id)
            assert row is not None
            session.expunge(row)
            return row

    return asyncio.run(_read())


@pytest.mark.parametrize("form", sorted(_P_FORMS))
def test_the_card_hides_the_argument_of_p_after_any_program(
    monkeypatch: pytest.MonkeyPatch, form: str
) -> None:
    server = _RecordingServer()
    _setup(monkeypatch, server)
    value = _made_up()
    manifest = _pod_block(_P_FORMS[form].format(v=value))

    row = _park(_login("person-token"), manifest)
    line = _command_line(row.proposed_effect)

    assert value not in json.dumps(line)
    assert any(_HIDDEN in str(part) for part in line)
    assert value not in json.dumps(_run_commands(_views(row)["rest-person"]))
    assert value not in _console_card(row)

    run = _approve_and_run(monkeypatch, row)
    assert run.status == "ok", run
    after = _stored(row.id)
    assert _command_line(after.proposed_effect) == line
    assert value not in json.dumps(_run_commands(_views(after)["rest-person"]))


@pytest.mark.parametrize("form", sorted(_P_FORMS))
def test_the_card_hides_the_argument_of_p_as_the_job_previews_do(
    monkeypatch: pytest.MonkeyPatch, form: str
) -> None:
    server = _RecordingServer()
    _setup(monkeypatch, server)
    value = _made_up()
    container = _P_FORMS[form].format(v=value)
    manifest = _pod_block(container)

    row = _park(_login("person-token"), manifest)

    (entry,) = _run_commands(row.proposed_effect)
    assert _command_line(row.proposed_effect) == hide_command_secrets(_raw_line(manifest))
    assert value not in json.dumps(entry)


def _raw_line(manifest: str) -> list[Any]:
    (container,) = yaml.safe_load(manifest)["spec"]["containers"]
    return [*container.get("command", []), *container.get("args", [])]


def test_a_map_under_credentials_is_hidden_on_every_card(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _RecordingServer()
    _setup(monkeypatch, server)
    value = _made_up()
    manifest = (
        "apiVersion: example.test/v1\nkind: Widget\nmetadata: {name: w1, namespace: prod}\n"
        f"spec: {{credentials: {{user: admin, phrase: '{value}'}}}}\n"
    )

    row = _park(_login("person-token"), manifest)

    effect = row.proposed_effect
    echo = effect["preview"]["manifest"] if "preview" in effect else effect["manifest"]
    assert "admin" not in json.dumps(echo)
    assert "credentials" in json.dumps(echo)
    assert value not in json.dumps(effect)
    for view_name, view in _views(row).items():
        assert value not in json.dumps(view), view_name
    assert value not in _console_card(row)
