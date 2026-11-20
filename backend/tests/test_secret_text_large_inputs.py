# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The text pass of the default view stays fast and keeps YAML readable.

* A text with very many keys goes through the default view in time in line
  with its size, and its secret field is hidden.
* A large text value is read in a worker thread: the event loop keeps
  serving other work while the pass runs.
* An alias in a merge list (``<<: [*name]``) whose anchor stands in a hidden
  value becomes an empty map, so the text still reads as YAML.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from typing import Any

import pytest
import yaml

from meho_backplane.operations import dispatcher as dispatcher_module
from meho_backplane.operations._secret_values import hide_secret_values
from meho_backplane.redaction.middleware import RedactionMiddlewareResult
from meho_backplane.redaction.secret_text import hide_secret_text
from meho_backplane.settings import get_settings

# Made-up values, built from parts so secret scanners do not flag the source.
_FAKE = "FAKE" + "-made-up-large-9183"
_FAKE_MERGE = "FAKE" + "-made-up-merge-2716"
_MARKER = "[REDACTED:param_name]"

#: Generous for a slow CI runner; a walk that reads all keys again for each
#: key takes far longer on these inputs.
_TIME_LIMIT_S = 20.0


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # The audit copy gets a keyed fingerprint, whose key comes from the settings.
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _shown_text(text: str) -> str:
    shown = hide_secret_values("test.text.read", {"text": text})
    assert isinstance(shown, dict)
    return str(shown["text"])


def test_json_text_with_many_keys_is_hidden_in_time() -> None:
    document: dict[str, Any] = {f"key_{i}": i for i in range(100_000)}
    document["password"] = _FAKE
    started = time.monotonic()
    shown = _shown_text(json.dumps(document))
    assert time.monotonic() - started < _TIME_LIMIT_S
    assert _FAKE not in shown
    assert json.loads(shown)["password"] == _MARKER


def test_json_text_with_many_keys_and_an_snmp_community_is_hidden_in_time() -> None:
    document: dict[str, Any] = {"community": _FAKE}
    document.update({f"key_{i}": i for i in range(100_000)})
    document["snmp_port"] = 161
    started = time.monotonic()
    shown = _shown_text(json.dumps(document))
    assert time.monotonic() - started < _TIME_LIMIT_S
    assert _FAKE not in shown


def test_yaml_text_with_one_anchor_and_many_aliases_is_hidden_in_time() -> None:
    aliases = "".join(f"key_{i}: *b\n" for i in range(100_000))
    text = f"credentials: {{base: &b {{user: app, pass: {_FAKE}}}}}\n{aliases}"
    started = time.monotonic()
    shown = _shown_text(text)
    assert time.monotonic() - started < _TIME_LIMIT_S
    assert _FAKE not in shown
    loaded = yaml.safe_load(shown)
    assert loaded["credentials"] == _MARKER
    assert loaded["key_99999"] == _MARKER


async def test_large_text_value_lets_other_work_go_on() -> None:
    row = {"name": "app", "password": _FAKE, "note": "n" * 4000}
    text = json.dumps([row] * (10 * 1024 * 1024 // len(json.dumps(row)) + 1))
    assert len(text) >= 10 * 1024 * 1024
    redaction = RedactionMiddlewareResult(
        raw={"text": text}, redacted={"text": text}, manifest=(), policy_id="test-policy"
    )
    ticks = 0
    done = asyncio.Event()

    async def other_work() -> None:
        nonlocal ticks
        while not done.is_set():
            ticks += 1
            await asyncio.sleep(0.001)

    worker = asyncio.create_task(other_work())
    await asyncio.sleep(0)
    ticks_before = ticks
    result = await dispatcher_module._protect_secret_values(
        redaction, op_id="test.text.read", secret_view="hidden"
    )
    done.set()
    await worker
    assert ticks - ticks_before >= 20
    assert _FAKE not in result.redacted["text"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            f"credentials: {{base: &b {{user: app, pass: {_FAKE_MERGE}}}}}\n"
            "other: {<<: [*b], port: 1}\n",
            {"credentials": _MARKER, "other": {"port": 1}},
        ),
        (
            f"c: &c {{x: 1}}\ncredentials:\n  base: &b\n    user: app\n    pass: {_FAKE_MERGE}\n"
            "other:\n  <<:\n    - *b\n    - *c\n  port: 1\n",
            {"c": {"x": 1}, "credentials": _MARKER, "other": {"x": 1, "port": 1}},
        ),
        (
            f"credentials: &b {{user: app, pass: {_FAKE_MERGE}}}\nother: {{<<: [*b], port: 1}}\n",
            {"credentials": _MARKER, "other": {"port": 1}},
        ),
        (
            f"credentials: &b {{user: app, pass: {_FAKE_MERGE}}}\nother:\n  <<: *b\n  port: 1\n",
            {"credentials": _MARKER, "other": {"port": 1}},
        ),
    ],
    ids=["inline-list", "block-list", "inline-list-hidden-anchor", "direct-hidden-anchor"],
)
def test_yaml_merge_list_alias_of_a_hidden_value_still_reads(
    text: str, expected: dict[str, Any]
) -> None:
    shown = hide_secret_text(text)
    assert _FAKE_MERGE not in shown
    assert yaml.safe_load(shown) == expected


def test_yaml_quoted_merge_name_is_a_plain_key() -> None:
    text = f'credentials: {{base: &b {{pass: {_FAKE_MERGE}}}}}\nother: {{"<<": *b}}\n'
    shown = hide_secret_text(text)
    assert _FAKE_MERGE not in shown
    assert yaml.safe_load(shown) == {"credentials": _MARKER, "other": {"<<": _MARKER}}


_FAKE_MARKUP = "FAKE" + "-made-up-markup-4062"
_MANY = 100_000


@pytest.mark.parametrize(
    "text",
    [
        "".join(f"# key_{i}: value {i}\nkey_{i}: {i}\n" for i in range(_MANY))
        + f"# dbPassword: {_FAKE_MARKUP}\n",
        "bad: [\n"
        + "".join(f"key_{i}: value\n" for i in range(_MANY))
        + f"dbPassword: {_FAKE_MARKUP}\n",
        "bad: [\n"
        + "".join(f"{'  ' * (i % 50)}dbSecret:\n" for i in range(_MANY))
        + f"  {_FAKE_MARKUP}\n",
        "<config>"
        + "".join(f'<key_{i} name="n" value="v">{i}</key_{i}>' for i in range(_MANY))
        + f"<dbPassword>{_FAKE_MARKUP}</dbPassword></config>",
        "".join(f"<apiKey{i}><token>" for i in range(_MANY)) + f'<db password="{_FAKE_MARKUP}"/>',
        '<a x="' + "<b " * _MANY + f'"> <c token="{_FAKE_MARKUP}"/>',
    ],
    ids=[
        "yaml-comments",
        "broken-yaml",
        "broken-yaml-nested-blocks",
        "xml-elements",
        "xml-unclosed-secret-elements",
        "xml-open-quote",
    ],
)
def test_large_comment_broken_yaml_and_xml_texts_are_hidden_in_time(text: str) -> None:
    started = time.monotonic()
    shown = _shown_text(text)
    assert time.monotonic() - started < _TIME_LIMIT_S
    assert _FAKE_MARKUP not in shown
    assert _MARKER in shown
