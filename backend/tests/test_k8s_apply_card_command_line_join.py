# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``k8s.apply`` card hides what the Job previews hide and what the storage rule counts.

For each command line of the table, the card's list of what a container runs
(:func:`apply_run_commands`) is the join, item by item, of two answers for
the same line: the Job previews' hiding (``hide_command_secrets(line)``) and
the storage rule's hiding (``hide_command_secrets(line,
p_after_programs=True)``). When both change one item in different ways, the
card shows the whole item as one hidden mark.

``prog-a`` and ``prog-b`` are made-up programs outside the storage rule's
list. Each value is made up.
"""

from __future__ import annotations

import json
from typing import Any

import yaml

from meho_backplane.connectors.kubernetes.op_classes import hide_command_secrets
from meho_backplane.connectors.kubernetes.ops_write_preview import apply_run_commands

_HIDDEN = "***REDACTED***"
_V = "Qz7mWk2"
_W = "Hx4tRn9"

#: ``(command, args)`` of one container. ``_V`` and ``_W`` are the values.
_LINES: list[tuple[list[Any], list[Any]]] = [
    # A flag after -p.
    (["prog-a", "-p", "--user", f"reader:{_V}"], []),
    (["prog-a"], ["-p", "--verbose", _V]),
    # A secret flag and its value inside one item: a space, a new line and a
    # line continuation, as a direct argument and inside sh -c.
    (["prog-a"], [f"--password {_V}"]),
    (["prog-a"], [f"--password\n{_V}"]),
    (["prog-a"], [f"--password \\\n{_V}"]),
    (["sh", "-c"], [f"prog-a --password {_V}"]),
    (["sh", "-c"], [f"prog-a --password\n{_V}"]),
    (["sh", "-c"], [f"prog-a --password \\\n{_V}"]),
    # The forms the storage rule counts, also inside sh -c.
    (["mysql"], [f"-p{_V}"]),
    (["mariadb", f"-p{_V}"], []),
    (["mongo"], ["-p", _V]),
    (["sshpass", "-p", _V, "prog-b"], []),
    (["login", "-p", _V], []),
    (["prog-a", "--password"], [f"-{_V}"]),
    (["prog-a"], [f"-ureader:{_V}"]),
    (["sh", "-c"], [f"mysql -p{_V}"]),
    (["sh", "-c"], [f"prog-a -ureader:{_V}"]),
    # A line for which both rules give the same answer.
    (["sh", "-c"], [f"prog-a --password -{_V}"]),
    # Lines where nothing is hidden.
    (["prog-a", "--verbose", "list"], []),
    (["sh", "-c"], ["prog-b --count 3 && prog-a list"]),
    ([], ["prog-b", "-n", 5]),
    # One item read as a shell script, where the two answers hide different words.
    (["sh", "-c"], [f"prog-b -p {_W} && mysql -p{_V}"]),
]


def _no_redaction(entries: Any) -> Any:
    return entries


def _join(raw: Any, preview: Any, storage: Any) -> Any:
    if preview == storage:
        return preview
    if preview == raw:
        return storage
    if storage == raw:
        return preview
    return _HIDDEN


def test_the_card_command_line_is_the_join_of_the_job_preview_and_the_storage_rule() -> None:
    whole_item_hidden = 0
    for command, args in _LINES:
        line = [*command, *args]
        container: dict[str, Any] = {"name": "app", "image": "app:1"}
        if command:
            container["command"] = command
        if args:
            container["args"] = args
        manifest = yaml.safe_dump(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {"name": "report", "namespace": "prod"},
                "spec": {"containers": [container]},
            }
        )

        (entry,) = apply_run_commands(manifest, redact=_no_redaction)
        card = [*entry.get("command", []), *entry.get("args", [])]

        preview = hide_command_secrets(line)
        storage = hide_command_secrets(line, p_after_programs=True)
        assert card == [_join(*item) for item in zip(line, preview, storage, strict=True)], line
        for value in (_V, _W):
            if value in json.dumps(line) and value not in json.dumps(preview):
                assert value not in json.dumps(card), line
        whole_item_hidden += sum(
            shown == _HIDDEN and before != hidden_a != hidden_b != before
            for shown, before, hidden_a, hidden_b in zip(card, line, preview, storage, strict=True)
        )

    assert whole_item_hidden == 1
