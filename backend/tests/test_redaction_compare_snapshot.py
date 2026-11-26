# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The secret-hiding steps still answer the way the snapshot records, and keep three properties.

``tests/snapshots/redaction_compare.jsonl`` holds, for each of about 1,500
made-up inputs, one checksum over the full answers of all secret-hiding
steps the input runs on. It holds no lists of hidden and shown parts. This
test fails on any change of any answer, also when a step only starts or
stops hiding a value, and names the inputs. The CI job "Secret hiding
compared with the base branch" builds the lists of hidden and shown parts
itself, for the base branch and for the pull request, each from its own
code, and judges them.

When the code changes an answer on purpose, write the snapshot again, from
``backend/``::

    uv run python ../scripts/ci/redaction_compare.py --write

and list the wanted difference with a reason in
``tests/snapshots/redaction_compare_approved.yaml``.

Three properties hold on the same inputs:

* a text with no secret comes back unchanged, on every step;
* hiding twice gives the same answer as hiding once;
* the time grows in a straight line with the input size.
"""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests._time_limits import check_time_limit
from tests.redaction_compare.answers import STEPS, answer_text, rest_text, snapshot_lines
from tests.redaction_compare.inputs import CompareInput, plain_inputs, secret_inputs

_SNAPSHOT = Path(__file__).parent / "snapshots" / "redaction_compare.jsonl"
_WRITE_COMMAND = "uv run python ../scripts/ci/redaction_compare.py --write"


def _sums(lines: list[str]) -> dict[str, str]:
    return {entry["id"]: entry["sum"] for entry in map(json.loads, lines) if entry}


def test_answers_match_the_snapshot() -> None:
    expected = _sums(_SNAPSHOT.read_text(encoding="utf-8").splitlines())
    actual = _sums(snapshot_lines())
    if actual == expected:
        return
    changed = sorted(i for i in expected.keys() & actual.keys() if expected[i] != actual[i])
    missing = sorted(expected.keys() - actual.keys())
    new = sorted(actual.keys() - expected.keys())
    report = [
        f"{len(changed)} input(s) answer differently: {changed[:20]}",
        f"{len(missing)} input(s) are in the snapshot but no longer built: {missing[:20]}",
        f"{len(new)} input(s) are new and not in the snapshot: {new[:20]}",
    ]
    pytest.fail(
        "\n".join(report) + f"\nIf the change is wanted, run from backend/: {_WRITE_COMMAND}\n"
        "then list each wanted difference with a reason in "
        "tests/snapshots/redaction_compare_approved.yaml."
    )


def test_the_snapshot_holds_one_checksum_per_input_and_nothing_else() -> None:
    entries = [json.loads(line) for line in _SNAPSHOT.read_text(encoding="utf-8").splitlines()]
    assert all(set(entry) == {"id", "sum"} for entry in entries)
    assert [entry["id"] for entry in entries] == sorted(i.id for i in secret_inputs())


def test_input_set_is_about_fifteen_hundred_and_stable() -> None:
    first, second = secret_inputs(), secret_inputs()
    assert [(i.id, i.payload) for i in first] == [(i.id, i.payload) for i in second]
    assert len({i.id for i in first}) == len(first)
    assert 1_400 <= len(first) <= 1_700


@pytest.mark.parametrize("build", [secret_inputs, plain_inputs], ids=["secret", "plain"])
def test_every_planted_part_is_in_its_input(build: Callable[[], list[CompareInput]]) -> None:
    missing = [
        f"{item.id} {part.pid}={part.value!r}"
        for item in build()
        for part in item.parts
        if part.value not in answer_text(item.payload)
    ]
    assert not missing, f"{len(missing)} part(s) not in their input, first: {missing[:20]}"


def test_the_rest_text_is_the_same_when_only_a_secret_is_hidden() -> None:
    before = rest_text('host=plain0 password="Ab3 Cd4" port=443', ["Ab3", "Cd4"])
    assert before == rest_text('host=plain0 password="[REDACTED:password]" port=443', [])
    assert before == rest_text('host=plain0 password="***REDACTED***" port=443', [])


def test_the_rest_text_is_the_same_when_only_a_plain_part_is_hidden() -> None:
    before = rest_text("host=plain0 password=[REDACTED:password]", ["plain0"])
    assert before == rest_text("host=[REDACTED:hostname] password=[REDACTED:password]", [])


def test_the_rest_text_differs_when_more_than_the_secret_changes() -> None:
    before = rest_text("host=plain0 password='Ab3' port=443", ["Ab3"])
    assert before != rest_text("host=plain0 password=[REDACTED:password] port=443", [])
    assert before != rest_text("host=plain0 password='[REDACTED:password]'", [])


def _every_step(items: list[CompareInput]) -> list[tuple[CompareInput, str]]:
    return [(item, path) for item in items for path in item.paths]


def _run(path: str, payload: Any) -> Any:
    return STEPS[path](payload if isinstance(payload, str) else json.loads(json.dumps(payload)))


def test_a_text_with_no_secret_comes_back_unchanged() -> None:
    changed = [
        f"{item.id} {path}"
        for item, path in _every_step(plain_inputs())
        if answer_text(_run(path, item.payload)) != answer_text(item.payload)
    ]
    assert not changed, f"{len(changed)} plain input(s) changed, first: {changed[:20]}"


def test_hiding_twice_gives_the_same_answer_as_hiding_once() -> None:
    failures = []
    for item, path in _every_step(secret_inputs()):
        once = _run(path, item.payload)
        twice = _run(path, once)
        if answer_text(once) != answer_text(twice):
            failures.append(f"{item.id} {path}")
    assert not failures, f"{len(failures)} input(s) change on a second pass, first: {failures[:20]}"


# --- time: a straight line, not a square -----------------------------------

_SMALL, _LARGE = 500, 4_000  # 8 times apart
_MAX_RATIO = 24  # a straight line gives about 8, a square-time step about 64


def _text_of(n: int) -> str:
    return "".join(f"host=plain{i} password=Zq{i}x name: plain{i}\n" for i in range(n))


def _command_of(n: int) -> str:
    return " ".join(f"tool --name plain{i} --password Zq{i}x" for i in range(n))


def _rows_of(n: int) -> dict[str, Any]:
    return {"rows": [{"name": f"plain{i}", "password": f"Zq{i}x", "size": i} for i in range(n)]}


_SHAPES: list[tuple[str, Callable[[int], Any], tuple[str, ...]]] = [
    (
        "text",
        lambda n: {"text": _text_of(n)},
        (
            "redact",
            "scrub_content",
            "scrub_broadcast_params",
            "redact_error_payload",
            "default_view",
        ),
    ),
    (
        "rows",
        _rows_of,
        (
            "redact",
            "scrub_content",
            "scrub_broadcast_params",
            "redact_error_payload",
            "default_view",
        ),
    ),
    ("command", _command_of, ("hide_command_text", "hide_shell_script")),
]


def _shortest_of_three(path: str, payload: Any) -> float:
    best = float("inf")
    for _ in range(3):
        fresh = copy.deepcopy(payload)
        started = time.perf_counter()
        STEPS[path](fresh)
        best = min(best, time.perf_counter() - started)
    return best


@pytest.mark.parametrize(
    ("shape", "build", "path"),
    [(name, build, path) for name, build, paths in _SHAPES for path in paths],
    ids=lambda value: value if isinstance(value, str) else "",
)
@pytest.mark.slow_runner_tolerant
def test_time_grows_in_a_straight_line(shape: str, build: Callable[[int], Any], path: str) -> None:
    small = _shortest_of_three(path, build(_SMALL))
    large = _shortest_of_three(path, build(_LARGE))
    check_time_limit(
        large / small,
        _MAX_RATIO,
        f"{shape} on {path}: {small:.4f}s at {_SMALL}, {large:.4f}s at {_LARGE}",
    )
