# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``k8s.apply`` check and preview read each secret value whole.

* A file text: a value that starts on the next line, goes on below its
  line, or ends at the end of its shell word.
* A ``name``/``value`` pair anywhere, and a number under a secret name in a
  ``data`` map of any kind.
* A JSON text as an argument of a command line, also inside a JSON
  annotation.
* A map that names one key twice, in the manifest or in a JSON text.

A manifest the check flags keeps no copy on the stored request, and the
stored preview shows no secret value. The preview changes only the secret
values of a JSON text, and each shared ``data`` map is read once.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator, Mapping
from typing import Any

import pytest

from meho_backplane.connectors.kubernetes.op_classes import _SecretValueCheck
from meho_backplane.operations.approval_queue import params_with_secret_fields_hidden
from meho_backplane.redaction.file_text import hide_file_text
from meho_backplane.redaction.json_text import (
    UnreadableTextError,
    load_json_text,
    replace_changed_values,
)
from tests.test_apply_value_check_every_place import (
    _VALUE,
    _apply_preview,
    _config_map,
    _flagged,
    _manifest,
    _settings_env,  # noqa: F401  (autouse fixture)
    _store_with_preview,
)

_NUMBER = 918273645


def _pod(container: dict[str, Any]) -> str:
    return _manifest(
        {
            "metadata": {"name": "p"},
            "spec": {"containers": [{"name": "c", "image": "i", **container}]},
        },
        "Pod",
    )


def _annotation(text: str) -> str:
    return _manifest({"metadata": {"name": "w", "annotations": {"example.invalid/a": text}}})


_WHOLE_VALUE_FORMS = {
    "yaml-value-on-next-line": _config_map("app.yaml", "password:\n  " + _VALUE + "\nport: 80\n"),
    "yaml-plain-text-goes-on": _config_map(
        "app.yaml", "password: abc\n  " + _VALUE + "\nport: 80\n"
    ),
    "yaml-list-value": _config_map("app.yaml", "password:\n- " + _VALUE + "\nport: 80\n"),
    "properties-continued-line": _config_map(
        "app.properties", "password=abc\\\n  " + _VALUE + "\nport=80\n"
    ),
    "env-file-open-quote": _config_map("app.env", 'DB_PASS="abc\n' + _VALUE + '"\nPORT=80\n'),
    "shell-word": _config_map("run.sh", "#!/bin/sh\nDB_PASS=" + _VALUE + " ./run --mode fast\n"),
    "pair-outside-env": _manifest(
        {"metadata": {"name": "w"}, "spec": {"settings": [{"name": "DB_PASS", "value": _VALUE}]}}
    ),
    "number-in-spec-data": _manifest(
        {"metadata": {"name": "w"}, "spec": {"data": {"password": _NUMBER}}}
    ),
    "json-argument": _pod({"args": ["--config", json.dumps({"password": _VALUE})]}),
    "json-argument-in-json-annotation": _annotation(
        json.dumps({"command": ["app", json.dumps({"password": _VALUE})]})
    ),
    "json-file-repeated-key": _config_map(
        "a.json", '{"password": "' + _VALUE + '", "password": ""}'
    ),
    "json-annotation-repeated-key": _annotation('{"password": "' + _VALUE + '", "password": ""}'),
    "yaml-repeated-key": (
        "apiVersion: example.invalid/v1\nkind: Widget\nmetadata: {name: w}\n"
        "spec:\n  password: " + _VALUE + "\n  password: ''\n"
    ),
}


def _shown(stored: str) -> bool:
    return _VALUE in stored or str(_NUMBER) in stored


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["waiting", "approved", "rejected"])
@pytest.mark.parametrize(
    "manifest", list(_WHOLE_VALUE_FORMS.values()), ids=list(_WHOLE_VALUE_FORMS)
)
async def test_the_stored_request_and_preview_keep_no_secret_value(
    monkeypatch: pytest.MonkeyPatch, manifest: str, stage: str
) -> None:
    assert _flagged(manifest) is True
    preview = await _apply_preview(monkeypatch, manifest)
    assert not _shown(json.dumps(preview))

    effect, stored = await _store_with_preview(manifest, preview, stage)

    assert effect["preview"]["manifest"]
    assert not _shown(stored)


@pytest.mark.parametrize(
    "manifest", list(_WHOLE_VALUE_FORMS.values()), ids=list(_WHOLE_VALUE_FORMS)
)
def test_the_clean_up_of_a_row_removes_the_manifest(manifest: str) -> None:
    hidden = params_with_secret_fields_hidden({"manifest": manifest}, op_id="k8s.apply")
    assert hidden is not None
    assert not _shown(json.dumps(hidden))


def _preview_of(monkeypatch: pytest.MonkeyPatch, manifest: str) -> dict[str, Any]:
    return asyncio.run(_apply_preview(monkeypatch, manifest))


@pytest.mark.parametrize(
    "text,hidden,kept",
    [
        (
            "password:\n  " + _VALUE + "\nport: 80\n",
            "password:\n  ***REDACTED***\nport: 80\n",
            "\nport: 80\n",
        ),
        (
            "password:\n- " + _VALUE + "\nport: 80\n",
            "password:\n***REDACTED***\nport: 80\n",
            "\nport: 80\n",
        ),
        (
            "password=abc\\\n  " + _VALUE + "\nport=80\n",
            "password=***REDACTED***\n  ***REDACTED***\nport=80\n",
            "\nport=80\n",
        ),
        (
            "#!/bin/sh\nMODE=fast DB_PASS=" + _VALUE + " ./run --mode fast\n",
            "#!/bin/sh\nMODE=fast DB_PASS=***REDACTED*** ./run --mode fast\n",
            "#!/bin/sh\nMODE=fast DB_PASS=***REDACTED*** ./run --mode fast\n",
        ),
        (
            "#!/bin/sh\nDB_PASS='a " + _VALUE + "' ./run\n",
            "#!/bin/sh\nDB_PASS='***REDACTED***' ./run\n",
            "#!/bin/sh\nDB_PASS='***REDACTED***' ./run\n",
        ),
    ],
    ids=[
        "yaml-next-line",
        "yaml-list",
        "properties-continued",
        "shell-command-stays",
        "shell-quoted",
    ],
)
def test_the_whole_value_is_hidden_and_the_rest_stays(
    monkeypatch: pytest.MonkeyPatch, text: str, hidden: str, kept: str
) -> None:
    assert hide_file_text(text) == hidden
    preview = _preview_of(monkeypatch, _config_map("app.conf", text))
    shown = preview["manifest"][0]["data"]["app.conf"]
    assert _VALUE not in shown
    assert kept in shown


@pytest.mark.parametrize(
    "text",
    [
        "password:\nport: 80\n",
        "credentials:\n  file: /etc/pw\n  mode: read\n",
        "#!/bin/sh\nDB_PASS= ./run --mode fast\n",
    ],
    ids=["empty-value", "map-below", "shell-empty-word"],
)
def test_a_file_text_without_a_secret_value_stays(text: str) -> None:
    assert hide_file_text(text) == text
    assert _flagged(_config_map("app.conf", text)) is False


def test_a_yaml_merge_key_is_not_a_repeated_key() -> None:
    manifest = (
        "apiVersion: example.invalid/v1\nkind: Widget\nmetadata: {name: w}\n"
        "base: &b {port: 1, mode: a}\nspec:\n  <<: *b\n  port: 2\n"
    )
    assert _flagged(manifest) is False


def test_the_preview_keeps_a_pair_outside_env_without_its_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest(
        {
            "metadata": {"name": "w"},
            "spec": {
                "settings": [
                    {"name": "DB_PASS", "value": _VALUE},
                    {"name": "MODE", "value": "fast"},
                ]
            },
        }
    )
    preview = _preview_of(monkeypatch, manifest)
    assert preview["manifest"][0]["spec"]["settings"] == [
        {"name": "DB_PASS", "value": "***REDACTED***"},
        {"name": "MODE", "value": "fast"},
    ]


def test_the_run_commands_hide_only_the_secret_of_a_json_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    argument = '{"mode": "fast", "password": "' + _VALUE + '"}'
    preview = _preview_of(monkeypatch, _pod({"command": ["app"], "args": ["--config", argument]}))
    [entry] = preview["run_commands"]
    assert entry["command"] == ["app"]
    assert entry["args"] == ["--config", '{"mode": "fast", "password": "***REDACTED***"}']


def test_a_json_annotation_keeps_its_line_breaks(monkeypatch: pytest.MonkeyPatch) -> None:
    text = '{\n  "user": "app",\n  "password": "' + _VALUE + '",\n  "port": 80\n}'
    preview = _preview_of(monkeypatch, _annotation(text))
    shown = preview["manifest"][0]["metadata"]["annotations"]["example.invalid/a"]
    assert shown == '{\n  "user": "app",\n  "password": "***REDACTED***",\n  "port": 80\n}'


def test_a_json_annotation_with_only_a_reference_stays(monkeypatch: pytest.MonkeyPatch) -> None:
    text = '{\n  "password": {"secretKeyRef": {"name": "db", "key": "pw"}},\n  "port": 80\n}'
    manifest = _annotation(text)
    assert _flagged(manifest) is False
    preview = _preview_of(monkeypatch, manifest)
    assert preview["manifest"][0]["metadata"]["annotations"]["example.invalid/a"] == text


def test_replace_changed_values_keeps_all_other_text() -> None:
    text = '[ {"a" :1,"password":  "x"} ,\n\t{"b": [1, 2]} ]'
    parsed = load_json_text(text)
    changed = [{"a": 1, "password": "H"}, {"b": [1, 2]}]
    assert (
        replace_changed_values(text, parsed, changed)
        == '[ {"a" :1,"password":  "H"} ,\n\t{"b": [1, 2]} ]'
    )


def test_a_repeated_json_key_is_unreadable() -> None:
    with pytest.raises(UnreadableTextError):
        load_json_text('{"a": {"b": 1, "b": 2}}')


class _CountingMap(Mapping[str, Any]):
    """A map that counts how many of its values were read."""

    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values
        self.values_read = 0

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def values(self) -> Iterator[Any]:  # type: ignore[override]
        for value in self._values.values():
            self.values_read += 1
            yield value


def test_a_shared_data_map_has_its_texts_read_once() -> None:
    shared = _CountingMap({f"file{index}.conf": f"mode=m{index}\n" for index in range(10)})
    doc = {
        "apiVersion": "example.invalid/v1",
        "kind": "Widget",
        "metadata": {"name": "w"},
        "spec": {"copies": [{"data": shared} for _ in range(8)]},
    }
    assert _SecretValueCheck()(doc) is False
    assert shared.values_read == 10
