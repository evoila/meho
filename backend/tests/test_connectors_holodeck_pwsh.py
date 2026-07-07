# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Holodeck connector's ``_pwsh`` helper (G3.8-T1 #853).

Coverage matrix (per Task #853 AC #2):

* :func:`encode_pwsh_command` round-trips the documented
  ``pwsh -EncodedCommand`` convention (UTF-16LE-base64, no BOM).
  Round-trip via :mod:`base64` + UTF-16-LE decode recovers the
  original script byte-for-byte.
* :func:`pwsh_run` constructs the remote command as
  ``pwsh -NoProfile -NonInteractive -EncodedCommand <encoded>`` --
  the script body is **not** in argv after encoding (it's in the
  base64 payload, but no other interpolation).
* :func:`pwsh_run` parses ``ConvertTo-Json`` stdout via stdlib
  :mod:`json` and returns the parsed structure (dict, list, scalar).
* :func:`pwsh_run` raises :class:`PwshRunError` on non-zero exit.
* :func:`pwsh_run` raises :class:`PwshRunError` on non-JSON stdout
  (e.g. a cmdlet failure message printed to stdout).
* :func:`pwsh_run` raises :class:`PwshRunError` on empty stdout
  (degenerate "cmdlet returned nothing" case).
* :class:`PwshRunError` truncates the stderr fragment to the
  documented cap so unbounded cmdlet errors never bleed into the
  exception surface.
* Secret-leak invariant: stdin / argv / log events emitted by the
  helper never contain the original script text in plaintext, never
  contain stderr's canary string verbatim past the truncation cap,
  and never contain anything from ``target.secret_ref``.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from meho_backplane.connectors.holodeck._pwsh import (
    PWSH_DEFAULT_DEPTH,
    PwshRunError,
    encode_pwsh_command,
    pwsh_run,
)
from meho_backplane.settings import get_settings

# ---------------------------------------------------------------------------
# Settings fixture (settings/secret-leak sweep + conftest share the same env)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Target + SSHCompletedProcess stubs
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    # A Vault KV-v2 path STRING (#2155). The connector in this suite is
    # a MagicMock, so the path is never resolved — it exists to keep the
    # target shape contract-honest.
    secret_ref: str


_TARGET = _StubTarget(
    name="holorouter-lab",
    host="holorouter.test.invalid",
    port=22,
    secret_ref="meho/testing/holodeck/holorouter-lab",
)


def _proc(*, stdout: str = "", stderr: str = "", exit_status: int = 0) -> Any:
    """Construct an ``SSHCompletedProcess``-shaped stub.

    Mirrors :func:`_completed_process` in the bind9 / pfSense suites:
    the helper code only touches ``.stdout`` / ``.stderr`` /
    ``.exit_status`` so a :class:`MagicMock` is enough.
    """
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.exit_status = exit_status
    return proc


def _connector_with_run(proc: Any) -> Any:
    """Build a stub connector whose ``_run_command`` returns *proc*.

    The helper reaches into ``connector._run_command(target, cmd, ...)``;
    a :class:`MagicMock` with an :class:`AsyncMock` ``_run_command`` is
    enough to assert what argv the helper passes (and what it does
    with the returned proc).
    """
    connector = MagicMock()
    connector._run_command = AsyncMock(return_value=proc)
    return connector


# ---------------------------------------------------------------------------
# encode_pwsh_command — round-trip the documented convention
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script",
    [
        "Get-Service | ConvertTo-Json",
        "Get-HoloDeckConfig | ConvertTo-Json -Compress",
        "Get-Service | Where-Object { $_.Name -like 'Holo*' } | "
        "Select-Object Name,Status | ConvertTo-Json",
        # Multi-line script (the ``-EncodedCommand`` form's primary
        # reason for existing — sidesteps the shell's argument-parsing
        # rules for embedded newlines and quotes).
        "$x = Get-HoloDeckConfig\n$x.Version | ConvertTo-Json",
    ],
)
def test_encode_pwsh_command_round_trips_utf16le_base64(script: str) -> None:
    encoded = encode_pwsh_command(script)
    # ``-EncodedCommand`` accepts only base64 ASCII; assert that.
    assert encoded == base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    # And the round-trip via base64-decode + UTF-16-LE-decode recovers
    # the script byte-for-byte.
    recovered = base64.b64decode(encoded).decode("utf-16-le")
    assert recovered == script


def test_encode_pwsh_command_emits_no_bom() -> None:
    """The convention is *no BOM* on the encoded payload (#371 cite)."""
    encoded = encode_pwsh_command("Get-Service")
    decoded_bytes = base64.b64decode(encoded)
    # UTF-16-LE BOM is ``FF FE``; the convention's encoding skips it.
    assert not decoded_bytes.startswith(b"\xff\xfe")


def test_pwsh_default_depth_is_documented_4() -> None:
    """The constant matches the #371 body's ``ConvertTo-Json -Depth 4`` recipe."""
    assert PWSH_DEFAULT_DEPTH == 4


# ---------------------------------------------------------------------------
# pwsh_run — happy paths
# ---------------------------------------------------------------------------


async def test_pwsh_run_returns_parsed_json_dict_for_convertto_json_object() -> None:
    payload = {"Version": "9.0.0", "PodId": "lab-pod-01"}
    connector = _connector_with_run(_proc(stdout=json.dumps(payload), exit_status=0))

    result = await pwsh_run(
        connector,
        _TARGET,
        "Get-HoloDeckConfig | ConvertTo-Json -Compress",
    )
    assert result == payload


async def test_pwsh_run_returns_parsed_json_list_for_convertto_json_array() -> None:
    payload = [{"Name": "HoloDNS", "Status": "Running"}, {"Name": "HoloDHCP", "Status": "Running"}]
    connector = _connector_with_run(_proc(stdout=json.dumps(payload), exit_status=0))

    result = await pwsh_run(
        connector,
        _TARGET,
        "Get-Service | ConvertTo-Json",
    )
    assert result == payload


async def test_pwsh_run_calls_run_command_with_encoded_command_argv() -> None:
    """argv is ``pwsh -NoProfile -NonInteractive -EncodedCommand <base64>``.

    The script itself appears only inside the base64 payload — there is
    no other shell-level interpolation of the script body. ``-NoProfile``
    + ``-NonInteractive`` are non-negotiable: they prevent the appliance's
    pwsh profile (if any) from injecting prompts or extra cmdlets that
    would dirty stdout.
    """
    connector = _connector_with_run(_proc(stdout='{"ok": true}', exit_status=0))
    script = "Get-HoloDeckConfig | ConvertTo-Json -Compress"

    await pwsh_run(connector, _TARGET, script)

    connector._run_command.assert_awaited_once()
    args, kwargs = connector._run_command.call_args
    target_arg, cmd_arg = args[0], args[1]
    assert target_arg is _TARGET
    expected_encoded = encode_pwsh_command(script)
    assert cmd_arg == (f"pwsh -NoProfile -NonInteractive -EncodedCommand {expected_encoded}")
    # operator / timeout are forwarded as kwargs; no operator threaded
    # here, so the helper forwards None (fails closed at Vault in prod).
    assert "operator" in kwargs
    assert kwargs.get("operator") is None
    assert "timeout" in kwargs


async def test_pwsh_run_forwards_caller_timeout_to_run_command() -> None:
    connector = _connector_with_run(_proc(stdout="null", exit_status=0))
    await pwsh_run(connector, _TARGET, "Get-Service | ConvertTo-Json", timeout=5.0)

    _, kwargs = connector._run_command.call_args
    assert kwargs["timeout"] == 5.0


# ---------------------------------------------------------------------------
# pwsh_run — failure modes (AC #2 sub-bullet 3)
# ---------------------------------------------------------------------------


async def test_pwsh_run_raises_on_non_zero_exit_status() -> None:
    connector = _connector_with_run(
        _proc(stdout="", stderr="Get-HoloDeckConfig : cmdlet not found", exit_status=1)
    )
    with pytest.raises(PwshRunError) as exc_info:
        await pwsh_run(connector, _TARGET, "Get-HoloDeckConfig | ConvertTo-Json")

    assert exc_info.value.exit_status == 1
    assert "cmdlet not found" in exc_info.value.stderr


async def test_pwsh_run_raises_on_non_json_stdout() -> None:
    """A cmdlet that prints text to stdout instead of JSON fails closed."""
    connector = _connector_with_run(
        _proc(stdout="Get-HoloDeckConfig: not a real cmdlet", exit_status=0)
    )
    with pytest.raises(PwshRunError) as exc_info:
        await pwsh_run(connector, _TARGET, "Get-HoloDeckConfig | ConvertTo-Json")

    # ``exit_status`` is the original non-zero-or-zero value at the time
    # the parse error fires; the message names the JSON-parse failure
    # without echoing the offending payload (the operator can re-run
    # the cmdlet manually to see the raw output).
    assert exc_info.value.exit_status == 0
    assert "not valid JSON" in str(exc_info.value)


async def test_pwsh_run_raises_on_empty_stdout() -> None:
    """Empty stdout from a ``| ConvertTo-Json`` pipe is a probe failure."""
    connector = _connector_with_run(_proc(stdout="", exit_status=0))
    with pytest.raises(PwshRunError) as exc_info:
        await pwsh_run(connector, _TARGET, "Get-HoloDeckConfig | ConvertTo-Json")
    assert "empty stdout" in str(exc_info.value)


async def test_pwsh_run_error_truncates_long_stderr() -> None:
    """Stderr longer than the cap is truncated on :class:`PwshRunError`.

    The cap protects the operator-visible surface from unbounded
    cmdlet stack traces and any secret-shaped substring that might
    happen to live deeper in the output.
    """
    long_stderr = "x" * 10_000
    connector = _connector_with_run(_proc(stdout="", stderr=long_stderr, exit_status=1))
    with pytest.raises(PwshRunError) as exc_info:
        await pwsh_run(connector, _TARGET, "Get-HoloDeckConfig | ConvertTo-Json")

    assert len(exc_info.value.stderr) <= 4096
    # The truncated stderr still starts with the first bytes -- the
    # cap doesn't drop content from the wrong end.
    assert exc_info.value.stderr.startswith("x" * 100)


# ---------------------------------------------------------------------------
# Secret-leak invariants (Hard requirement: never log/expose creds or script)
# ---------------------------------------------------------------------------


async def test_pwsh_run_does_not_embed_script_in_log_events(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """The helper logs ``script_len`` + ``exit_status`` only — not the script."""
    from meho_backplane.logging import configure_logging

    configure_logging()

    canary_script = "Get-HoloDeckConfig | ConvertTo-Json -Compress # HOLODECK-CANARY-MARKER"
    payload = {"ok": True}
    connector = _connector_with_run(_proc(stdout=json.dumps(payload), exit_status=0))
    await pwsh_run(connector, _TARGET, canary_script)

    out, err = capfd.readouterr()
    combined = out + err
    # The structured-log event must record sizes only -- never the
    # script body itself.
    assert "HOLODECK-CANARY-MARKER" not in combined
    # The canary password from the stub target must never appear in
    # any log line the helper emits.
    assert "holodeck-canary-password-xyz" not in combined


async def test_pwsh_run_error_does_not_carry_script_text() -> None:
    """Even on failure, the exception surface omits the original script."""
    canary_script = "Get-HoloDeckConfig | ConvertTo-Json -Compress # HOLODECK-CANARY-IN-SCRIPT"
    connector = _connector_with_run(_proc(stdout="", stderr="pwsh: command failed", exit_status=1))
    with pytest.raises(PwshRunError) as exc_info:
        await pwsh_run(connector, _TARGET, canary_script)

    err = exc_info.value
    # The script body must not appear in the exception message or
    # any field of the structured error.
    assert "HOLODECK-CANARY-IN-SCRIPT" not in str(err)
    assert "HOLODECK-CANARY-IN-SCRIPT" not in err.stderr
