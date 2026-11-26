# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The texts that tell a person or an agent which switch turns a policy part on.

* The four messages that show while classification is off, and the console
  text of the classify page that repeats one of them, have an exact text.
  Each names ``POLICY_CLASSIFICATION`` first, which turns on classification
  only, and then ``CUSTOM_POLICY``, which turns on all three policy parts.
  None names ``POLICY_ENGINE_SHADOW`` or ``POLICY_APPROVER_ROUTING``.
* Each other text names ``CUSTOM_POLICY`` before an old switch
  (``POLICY_CLASSIFICATION``, ``POLICY_ENGINE_SHADOW``,
  ``POLICY_APPROVER_ROUTING``). No such text names only an old switch.
* The CLI help texts and the profile comment also say once that each older
  switch still turns on its own part alone.

The other texts: the CLI help of ``meho policy explain``,
``meho connector classify`` and ``meho connector overrides``, the CLI line
for an evaluator that is off, the comment of the shipped policy profile,
and the switch table of ``docs/codebase/mcp.md``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from meho_backplane.connectors.policy.apply import SWITCH_OFF_IMPORT
from meho_backplane.features import CATEGORY_WITHOUT_CLASSIFICATION
from meho_backplane.policy.explain import FILE_NEEDS_CLASSIFICATION
from meho_backplane.policy.store import SWITCH_OFF_REFUSAL

_REPO_ROOT = Path(__file__).resolve().parents[2]

OLD_SWITCHES = ("POLICY_CLASSIFICATION", "POLICY_ENGINE_SHADOW", "POLICY_APPROVER_ROUTING")

#: The sentence each help text, answer text, console text and the profile
#: comment says once, without backticks.
OLD_SWITCHES_ALONE = (
    "Each older switch (POLICY_CLASSIFICATION, POLICY_ENGINE_SHADOW, "
    "POLICY_APPROVER_ROUTING) still turns on its own part alone."
)

#: The two sentences each message that shows while classification is off
#: says once.
OFF_MESSAGE_SWITCHES = (
    "The POLICY_CLASSIFICATION setting turns on classification only. The CUSTOM_POLICY "
    "setting turns on all three policy parts: classification, the policy evaluator and "
    "approver routing."
)

#: The exact text of each message that shows while classification is off.
OFF_MESSAGE_TEXTS: dict[str, str] = {
    "answer: SWITCH_OFF_REFUSAL": (
        "Classification is off on this MEHO. An admin can turn it on in one of two ways. "
        + OFF_MESSAGE_SWITCHES
    ),
    "console: classify page": (
        "Classification is off on this MEHO. An admin can turn it on in one of two ways. "
        + OFF_MESSAGE_SWITCHES
    ),
    "answer: SWITCH_OFF_IMPORT": (
        "The policy file import is off on this MEHO, because classification is off. An "
        "admin can turn it on in one of two ways. " + OFF_MESSAGE_SWITCHES
    ),
    "answer: FILE_NEEDS_CLASSIFICATION": (
        "A policy file counts only while classification is on. Classification is off, so "
        "the file would change no answer. An admin can turn it on in one of two ways. "
        + OFF_MESSAGE_SWITCHES
    ),
    "answer: CATEGORY_WITHOUT_CLASSIFICATION": (
        "Approver routing is on and classification is off, so no change has a category "
        "and a routing entry that matches on `category` fits nothing. Turn on "
        "classification, or match on connector or risk. " + OFF_MESSAGE_SWITCHES
    ),
}

_GO_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _read(relative: str) -> str:
    return (_REPO_ROOT / relative).read_text(encoding="utf-8")


def _go_string(relative: str, anchor: str) -> str:
    """The Go string expression that starts on the line with ``anchor``.

    The expression is a chain of ``"..." +`` lines; it ends on the first
    line that does not end with ``+``.
    """
    lines = _read(relative).splitlines()
    start = next(i for i, line in enumerate(lines) if anchor in line)
    parts: list[str] = []
    first = lines[start]
    for line in [first[first.index(anchor) :], *lines[start + 1 :]]:
        parts.extend(_GO_STRING.findall(line))
        if not line.rstrip().endswith("+"):
            break
    text = "".join(parts)
    return text.replace("\\n", " ").replace('\\"', '"')


def _flat(text: str) -> str:
    return " ".join(text.replace("`", "").split())


def _console_text() -> str:
    html = _read("backend/src/meho_backplane/ui/templates/policy/classify.html")
    match = re.search(r"<p[^>]*data-classify-off[^>]*>(.*?)</p>", html, re.DOTALL)
    assert match is not None
    return _flat(match.group(1))


def _profile_comment() -> str:
    lines = _read("deploy/policy-profiles/separated-duties.yaml").splitlines()
    comment = [line.lstrip("#").strip() for line in lines if line.startswith("#")]
    return _flat(" ".join(comment))


def _mcp_switch_table() -> list[str]:
    lines = _read("docs/codebase/mcp.md").splitlines()
    start = lines.index("Three switches take text away while they are off:")
    rows: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("|"):
            rows.append(line)
        elif rows:
            break
    return rows


#: The messages that show while classification is off, as MEHO has them.
OFF_MESSAGES: dict[str, str] = {
    "answer: SWITCH_OFF_REFUSAL": SWITCH_OFF_REFUSAL,
    "console: classify page": _console_text(),
    "answer: SWITCH_OFF_IMPORT": SWITCH_OFF_IMPORT,
    "answer: FILE_NEEDS_CLASSIFICATION": FILE_NEEDS_CLASSIFICATION,
    "answer: CATEGORY_WITHOUT_CLASSIFICATION": CATEGORY_WITHOUT_CLASSIFICATION,
}

#: Texts that must also carry :data:`OLD_SWITCHES_ALONE`.
TEXTS_WITH_SENTENCE: dict[str, str] = {
    "cli help: meho policy explain": _flat(
        _go_string("cli/internal/cmd/policy/explain.go", 'Long: "explain calls')
    ),
    "cli help: meho connector classify": _flat(
        _go_string("cli/internal/cmd/connector/classify.go", "const classifyNote =")
    ),
    "cli help: meho connector overrides": _flat(
        _go_string("cli/internal/cmd/connector/overrides.go", 'Long: "overrides calls')
    ),
    "profile: separated-duties.yaml": _profile_comment(),
}

#: Texts that only must name CUSTOM_POLICY first.
TEXTS_WITHOUT_SENTENCE: dict[str, str] = {
    "cli output: evaluator off": _go_string(
        "cli/internal/cmd/policy/explain.go", 'Fprintln(w, "Policy evaluator: off'
    ),
    **{
        f"docs/codebase/mcp.md switch table row {i}": _flat(row)
        for i, row in enumerate(_mcp_switch_table())
    },
}


def _parts_naming_old_switch_alone(text: str) -> list[str]:
    """The sentences that name an old switch with no ``CUSTOM_POLICY`` before it."""
    rest = text.replace(OLD_SWITCHES_ALONE, "")
    bad: list[str] = []
    for sentence in re.split(r"(?<=[.:|])\s+", rest):
        for switch in OLD_SWITCHES:
            at = sentence.find(switch)
            if at == -1:
                continue
            custom = sentence.find("CUSTOM_POLICY")
            if custom == -1 or custom > at:
                bad.append(sentence)
                break
    return bad


@pytest.mark.parametrize("name", sorted({**TEXTS_WITH_SENTENCE, **TEXTS_WITHOUT_SENTENCE}))
def test_custom_policy_comes_first(name: str) -> None:
    text = {**TEXTS_WITH_SENTENCE, **TEXTS_WITHOUT_SENTENCE}[name]
    assert _parts_naming_old_switch_alone(text) == [], name


@pytest.mark.parametrize("name", sorted(TEXTS_WITH_SENTENCE))
def test_says_each_old_switch_still_works_alone(name: str) -> None:
    assert TEXTS_WITH_SENTENCE[name].count(OLD_SWITCHES_ALONE) == 1, name


@pytest.mark.parametrize("name", sorted(OFF_MESSAGES))
def test_off_message_names_the_part_switch_first(name: str) -> None:
    text = _flat(OFF_MESSAGES[name])
    assert text.count(OFF_MESSAGE_SWITCHES) == 1, name
    assert "still turns on its own part alone" not in text, name
    assert "POLICY_CLASSIFICATION" in text, name
    assert text.index("POLICY_CLASSIFICATION") < text.index("CUSTOM_POLICY"), name
    assert "POLICY_ENGINE_SHADOW" not in text, name
    assert "POLICY_APPROVER_ROUTING" not in text, name


@pytest.mark.parametrize("name", sorted(OFF_MESSAGES))
def test_off_messages_have_the_exact_text(name: str) -> None:
    assert OFF_MESSAGES[name] == OFF_MESSAGE_TEXTS[name], name


def test_the_texts_were_found() -> None:
    """Each collected text is there, so a moved text cannot pass by being empty."""
    for name, text in {**OFF_MESSAGES, **TEXTS_WITH_SENTENCE, **TEXTS_WITHOUT_SENTENCE}.items():
        assert text.strip(), name
    assert len(_mcp_switch_table()) == 5
    assert "Policy evaluator: off" in TEXTS_WITHOUT_SENTENCE["cli output: evaluator off"]
