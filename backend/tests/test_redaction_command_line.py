# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The shared command-line hider reads ``-p`` per caller.

* Without ``p_programs`` (the ``k8s.job.create`` command line), ``-p`` is a
  secret flag after any program.
* ``hide_shell_script`` (the ``linux.script.run`` script) treats ``-p`` as a
  secret flag only for ``sshpass``, ``mysql*``, ``mariadb*``, ``mongo*``
  and a command with the word ``login``, also with the value glued to it,
  and a new line ends the command. It also hides a password glued to
  ``-u`` (``curl -uuser:password``).
"""

from __future__ import annotations

import time

import pytest

from meho_backplane.redaction.command_line import (
    HIDDEN,
    hide_command_text,
    hide_in_text,
    hide_shell_script,
)

_FAKE = "made-up-value-not-real-4471"
_USER = "admin"


def test_command_line_reads_dash_p_as_a_secret_flag_after_any_program() -> None:
    text, pending = hide_command_text(f"mytool -p {_FAKE}; id", None, shell=True)
    assert text == f"mytool -p {HIDDEN}; id"
    assert pending is None


def test_command_line_keeps_a_flag_waiting_across_a_new_line() -> None:
    text, _ = hide_command_text(f"mytool --password\n{_FAKE}", None, shell=True)
    assert _FAKE not in text


def test_shell_script_reads_dash_p_only_for_password_programs() -> None:
    script = f"mkdir -p /tmp/x\nsudo sshpass -p {_FAKE} ssh deploy@host id\nmariadb -p{_FAKE}\n"
    assert hide_shell_script(script) == (
        f"mkdir -p /tmp/x\nsudo sshpass -p {HIDDEN} ssh deploy@host id\nmariadb -p{HIDDEN}\n"
    )


def test_shell_script_ends_a_waiting_flag_at_a_new_line() -> None:
    script = "mytool --password\nid\n"
    assert hide_shell_script(script) == script


def test_shell_script_reads_dash_p_inside_a_bash_c_string() -> None:
    script = f"bash -c 'mkdir -p /tmp/x; mysql -p{_FAKE} -e \"select 1\"'\n"
    shown = hide_shell_script(script)
    assert _FAKE not in shown
    assert "mkdir -p /tmp/x" in shown


def test_shell_script_reads_dash_p_for_mongo_tools_and_login_commands() -> None:
    script = (
        f"mongosh -u admin -p {_FAKE}\n"
        f"docker login -u bot -p{_FAKE} registry.example.invalid\n"
        f"helm registry login registry.example.invalid -u bot -p {_FAKE}\n"
        "docker run -d -p 8080:80 nginx\n"
    )
    assert hide_shell_script(script) == (
        f"mongosh -u admin -p {HIDDEN}\n"
        f"docker login -u bot -p{HIDDEN} registry.example.invalid\n"
        f"helm registry login registry.example.invalid -u bot -p {HIDDEN}\n"
        "docker run -d -p 8080:80 nginx\n"
    )


def test_shell_script_hides_a_password_glued_to_dash_u() -> None:
    script = f"curl -u{_USER}:{_FAKE} https://example.invalid/api\ncurl -uadmin https://example.invalid\n"
    assert hide_shell_script(script) == (
        f"curl -u{_USER}:{HIDDEN} https://example.invalid/api\ncurl -uadmin https://example.invalid\n"
    )


def test_a_flag_waits_for_its_value_across_a_line_continuation() -> None:
    text, pending = hide_command_text(f"curl -u \\\n  {_USER}:{_FAKE} -k", None, shell=True)
    assert text == f"curl -u \\\n  {_USER}:{HIDDEN} -k"
    assert pending is None
    script = f"mysql \\\n  -p \\\n  {_FAKE} appdb\nmkdir -p \\\n  /tmp/x\n"
    assert hide_shell_script(script) == (
        f"mysql \\\n  -p \\\n  {HIDDEN} appdb\nmkdir -p \\\n  /tmp/x\n"
    )


_ONE_MIB = 1 << 20
_TIME_LIMIT_SECONDS = 2.0


@pytest.mark.parametrize("unit", ["x", "-x", "a://", "1+"])
def test_shell_script_with_one_very_long_line_is_hidden_within_the_time_bound(unit: str) -> None:
    script = unit * (_ONE_MIB // len(unit))
    started = time.perf_counter()
    shown = hide_shell_script(script)
    assert time.perf_counter() - started < _TIME_LIMIT_SECONDS
    assert shown == script


_HALF_MIB = 1 << 19


def test_quoted_part_with_many_words_is_hidden_within_the_time_bound() -> None:
    words = "w " * (_HALF_MIB // 2)
    script = f'echo "{words}" --password {_FAKE}\n'
    started = time.perf_counter()
    shown = hide_shell_script(script)
    assert time.perf_counter() - started < _TIME_LIMIT_SECONDS
    assert shown == f'echo "{words}" --password {HIDDEN}\n'


def test_url_password_is_still_hidden_inside_a_long_line() -> None:
    script = "x" * _ONE_MIB + f" curl https://{_USER}:{_FAKE}@host.example.invalid/\n"
    started = time.perf_counter()
    shown = hide_shell_script(script)
    assert time.perf_counter() - started < _TIME_LIMIT_SECONDS
    assert _FAKE not in shown
    assert f"https://{_USER}:{HIDDEN}@host.example.invalid/" in shown


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"https://{_USER}:{_FAKE}@h/", f"https://{_USER}:{HIDDEN}@h/"),
        (f"1abc://{_USER}:{_FAKE}@h", f"1abc://{_USER}:{HIDDEN}@h"),
        (f"+a.b-c://{_USER}:{_FAKE}@h", f"+a.b-c://{_USER}:{HIDDEN}@h"),
        (f"x=postgres://{_USER}:{_FAKE}@h:5432/db", f"x=postgres://{_USER}:{HIDDEN}@h:5432/db"),
        (f"1://{_USER}:{_FAKE}@h", f"1://{_USER}:{_FAKE}@h"),
        (f"://{_USER}:{_FAKE}@h", f"://{_USER}:{_FAKE}@h"),
        (f"https://{_USER}@h/:{_FAKE}@", f"https://{_USER}@h/:{_FAKE}@"),
    ],
)
def test_url_password_cases_keep_their_hidden_text(text: str, expected: str) -> None:
    assert hide_in_text(text) == expected
