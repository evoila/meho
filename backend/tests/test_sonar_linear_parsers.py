# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Compare parser output with the former grammar and bound large malformed inputs."""

from __future__ import annotations

import itertools
import os
import random
import re
import subprocess
import sys

from meho_backplane.connectors.linux.ops_storage import _mount8_fields
from meho_backplane.connectors.pfsense.ops_read import _lease_statement_value, _lease_timestamp
from meho_backplane.kb.passages import _heading
from meho_backplane.operations.ingest._llm_grouping_internals import strip_code_fences


def strings(alphabet: str, length: int, count: int) -> list[str]:
    randomizer = random.Random(4431)
    return ["".join(randomizer.choices(alphabet, k=length)) for _ in range(count)]


def test_heading_keeps_existing_title_rules() -> None:
    old_pattern = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
    lines = ["".join(chars) for n in range(8) for chars in itertools.product("# \t", repeat=n)]
    lines += [
        " " * indent + "#" * level + "\t " + body
        for indent in range(5)
        for level in range(1, 9)
        for body in ["", "#", "a #", "a###", "a \t### \t", "a\u00a0#", "a\r#"]
    ]
    lines += ["## " + text for text in strings("abc #\t\r\u00a0", 60, 2000)]
    for line in lines:
        old = old_pattern.match(line)
        expected = (len(old.group(1)), old.group(2).strip()) if old else None
        actual = _heading(line)
        if actual:
            actual = (actual[0], actual[1].strip())
        assert actual == expected, repr(line)


def test_fence_stripping_matches_previous_parser() -> None:
    old = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)
    values = ["".join(chars) for n in range(9) for chars in itertools.product("` \n", repeat=n)]
    values += [
        start + body + end
        for start in ["```", " ```json", "```JSON", "```json\n", "```jsonx"]
        for body in ["", "x", " [] ", "\n{}\n", "```", "\r\n abc ``` def\n"]
        for end in ["```", "``` \n", "", "````"]
    ]
    values += strings("`jsonx \t\n", 80, 2000)
    for value in values:
        match = old.match(value)
        expected = match.group(1).strip() if match else value.strip()
        assert strip_code_fences(value) == expected, repr(value)


def test_dhcp_timestamp_values_match_previous_parser() -> None:
    for keyword in ["starts", "ends"]:
        old = re.compile(rf"^{keyword}\s+(?P<val>.+?)\s*;\s*$")
        tails = ["".join(chars) for n in range(7) for chars in itertools.product("x ;\t", repeat=n)]
        tails += [" 1 2026/10/25 10:00:00;", " never;", " bad;tail", "\t\u00a0never; "]
        tails += strings("abc ;\t\r", 50, 2000)
        for tail in tails:
            line = keyword + tail
            match = old.match(line)
            actual = _lease_statement_value(line, keyword)
            assert (actual is not None) == (match is not None), repr(line)
            if match:
                assert _lease_timestamp(actual) == _lease_timestamp(match.group("val")), repr(line)


def test_mount_fields_preserve_ambiguous_separator_names() -> None:
    old = re.compile(
        r"^(?P<source>.+?) on (?P<target>.+?) type (?P<fstype>\S+) \((?P<options>.*)\)\s*$"
    )
    values = [
        source + " on " + target + " type " + fstype + " (" + options + ")" + suffix
        for source in ["dev", "a on b", " ", "a type b"]
        for target in ["/mnt", "a type b", "a on b", " ", ""]
        for fstype in ["ext4", "nfs", "bad type good", "", "x\ty"]
        for options in ["", "rw", "a)b", "a (x)"]
        for suffix in ["", " \t\n", "x"]
    ]
    values += strings("on type ()ab\t\r", 90, 3000)
    for line in values:
        match = old.match(line)
        expected = (
            {key: value.strip() for key, value in match.groupdict().items()} if match else None
        )
        assert _mount8_fields(line) == expected, repr(line)


def test_large_malformed_inputs_finish_in_bounded_subprocess() -> None:
    """Old patterns take minutes here; allow ample interpreter startup time."""
    script = """
from meho_backplane.kb.passages import _heading
from meho_backplane.connectors.pfsense.ops_read import _lease_statement_value
from meho_backplane.connectors.linux.ops_storage import _mount8_fields
from meho_backplane.operations.ingest._llm_grouping_internals import strip_code_fences
n = 200_000
assert _heading('# x' + ' ' * n + 'y')[1].endswith('y')
assert _lease_statement_value('starts ' + ' ' * n + 'x', 'starts') is None
assert _lease_statement_value('ends ' + ' ' * n + 'x', 'ends') is None
assert strip_code_fences('```' + ' ' * n + 'x').endswith('x')
assert _mount8_fields('x on ' * 40_000 + 'x type bad') is None
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
