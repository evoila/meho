# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The approver of ``linux.script.run`` reads the script.

* The preview carries the script text, its size and its SHA-256.
* Values that look like secrets are hidden in the text; then the preview
  says so and leaves out the SHA-256.
* A long script is cut and marked; the SHA-256 is of the full script.
* The optional ``script_ref`` and ``expected_sha256`` give "matches" or
  "does not match"; the handler refuses a script that does not match.
* The argument values and the env values stay out of the preview.
* A failed redaction pass shows no text and no hash.
* The parked approval row carries the script in its preview.
"""

from __future__ import annotations

import hashlib
import types
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import meho_backplane.connectors.linux  # noqa: F401 -- import for registry side-effects
from meho_backplane.connectors.linux.ops_write import (
    LinuxWriteSafetyError,
    _linux_script_run_preview,
    linux_script_run,
)
from meho_backplane.connectors.linux.script_preview import (
    SCRIPT_CHECK_MATCHES,
    SCRIPT_CHECK_MISMATCH,
    SCRIPT_SHOWN_MAX_BYTES,
    script_matches_expected,
    script_preview_fields,
)
from tests.test_connectors_linux_write import (
    _ARG_CANARY,
    _CONNECTOR_ID,
    _ENV_VALUE_CANARY,
    _OPERATOR,
    _TARGET,
    _TARGET_NAME,
    _connector_with_secret,
    _proc,
    _required_settings_env,  # noqa: F401 -- autouse fixture
    linux_write_registered,  # noqa: F401 -- fixture
)

_SCRIPT = "#!/bin/bash\nset -eu\nuname -a\ndf -h /\n"
# Made-up value, shaped so the default redaction policy hides it.
_FAKE_VALUE = "made-up-value-not-real-4471"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ctx(params: dict[str, Any]) -> Any:
    return types.SimpleNamespace(params=params, target=types.SimpleNamespace(name="linux-1"))


@pytest.mark.asyncio
async def test_preview_shows_script_size_and_sha256() -> None:
    preview = await _linux_script_run_preview(_ctx({"script": _SCRIPT}))
    assert preview is not None
    assert preview["script"] == _SCRIPT
    assert preview["script_bytes"] == len(_SCRIPT.encode("utf-8"))
    assert preview["script_sha256"] == _sha(_SCRIPT)
    for key in ("script_values_hidden", "script_truncated", "script_ref", "script_check"):
        assert key not in preview


@pytest.mark.asyncio
async def test_preview_keeps_argument_and_env_values_out() -> None:
    preview = await _linux_script_run_preview(
        _ctx({"script": _SCRIPT, "arguments": _ARG_CANARY, "env": {"APP_MODE": _ENV_VALUE_CANARY}})
    )
    assert preview is not None
    assert preview["script"] == _SCRIPT
    assert preview["env_var_names"] == ["APP_MODE"]
    assert _ARG_CANARY not in repr(preview)
    assert _ENV_VALUE_CANARY not in repr(preview)


@pytest.mark.parametrize(
    "line",
    [
        f"export DB_PASSWORD={_FAKE_VALUE}",
        f'echo \'{{"password": "{_FAKE_VALUE}"}}\'',
        f"curl https://svc:{_FAKE_VALUE}@example.invalid/x",
        f'curl -H "Authorization: Bearer {_FAKE_VALUE}" https://example.invalid/',
    ],
)
def test_values_that_look_like_secrets_are_hidden_and_no_hash_is_shown(line: str) -> None:
    script = f"#!/bin/bash\n{line}\nuname -a\n"
    fields = script_preview_fields({"script": script})
    assert _FAKE_VALUE not in repr(fields)
    assert fields["script_values_hidden"] is True
    assert "script_sha256" not in fields
    # The rest of the script stays readable.
    assert "uname -a" in fields["script"]
    assert fields["script_bytes"] == len(script.encode("utf-8"))


def test_a_hidden_value_does_not_hide_the_hash_check() -> None:
    script = f"export DB_PASSWORD={_FAKE_VALUE}\n"
    fields = script_preview_fields({"script": script, "expected_sha256": _sha(script)})
    assert fields["script_check"] == SCRIPT_CHECK_MATCHES
    assert _sha(script) not in repr(fields)


def test_a_long_script_is_cut_and_marked() -> None:
    script = "echo é\n" * (SCRIPT_SHOWN_MAX_BYTES // 4)
    fields = script_preview_fields({"script": script})
    assert fields["script_truncated"] is True
    assert len(fields["script"].encode("utf-8")) <= SCRIPT_SHOWN_MAX_BYTES
    assert script.startswith(fields["script"])
    # The hash and the size are of the full script, not of the shown text.
    assert fields["script_sha256"] == _sha(script)
    assert fields["script_bytes"] == len(script.encode("utf-8"))


def test_a_script_of_exactly_the_limit_is_not_cut() -> None:
    script = "a" * SCRIPT_SHOWN_MAX_BYTES
    fields = script_preview_fields({"script": script})
    assert fields["script"] == script
    assert "script_truncated" not in fields


def test_an_empty_script_is_shown_as_empty() -> None:
    fields = script_preview_fields({"script": ""})
    assert fields["script"] == ""
    assert fields["script_bytes"] == 0
    assert fields["script_sha256"] == _sha("")


@pytest.mark.parametrize(
    ("expected", "check"),
    [
        (_sha(_SCRIPT), SCRIPT_CHECK_MATCHES),
        (_sha(_SCRIPT).upper(), SCRIPT_CHECK_MATCHES),
        ("0" * 64, SCRIPT_CHECK_MISMATCH),
        (12345, SCRIPT_CHECK_MISMATCH),
    ],
)
def test_expected_sha256_gives_matches_or_does_not_match(expected: Any, check: str) -> None:
    fields = script_preview_fields(
        {"script": _SCRIPT, "script_ref": "app-repo@abc123:check.sh", "expected_sha256": expected}
    )
    assert fields["script_ref"] == "app-repo@abc123:check.sh"
    assert fields["script_check"] == check


def test_no_expected_sha256_gives_no_check() -> None:
    assert script_matches_expected(_SCRIPT, None) is None
    fields = script_preview_fields({"script": _SCRIPT, "script_ref": ""})
    assert "script_check" not in fields
    assert "script_ref" not in fields


def test_a_failed_redaction_pass_shows_no_text_and_no_hash() -> None:
    with patch(
        "meho_backplane.connectors.linux.script_preview.redact",
        side_effect=RuntimeError("boom"),
    ):
        fields = script_preview_fields({"script": _SCRIPT, "expected_sha256": "0" * 64})
    assert "script" not in fields
    assert "script_sha256" not in fields
    assert fields["script_unavailable"]
    assert fields["script_check"] == SCRIPT_CHECK_MISMATCH


@pytest.mark.asyncio
async def test_handler_refuses_a_script_that_does_not_match_expected_sha256() -> None:
    connector = _connector_with_secret()
    sudo = AsyncMock(return_value=_proc(stdout="ok\n"))
    with (
        patch("meho_backplane.connectors.linux.ops_write.run_remote_bash_with_sudo", sudo),
        pytest.raises(LinuxWriteSafetyError, match="expected_sha256"),
    ):
        await linux_script_run(
            connector, _TARGET, {"script": _SCRIPT, "use_sudo": True, "expected_sha256": "0" * 64}
        )
    sudo.assert_not_awaited()


@pytest.mark.asyncio
async def test_handler_runs_a_script_that_matches_expected_sha256() -> None:
    connector = _connector_with_secret()
    sudo = AsyncMock(return_value=_proc(stdout="ok\n"))
    with patch("meho_backplane.connectors.linux.ops_write.run_remote_bash_with_sudo", sudo):
        result = await linux_script_run(
            connector,
            _TARGET,
            {"script": _SCRIPT, "use_sudo": True, "expected_sha256": _sha(_SCRIPT)},
        )
    assert result["exit_code"] == 0
    sudo.assert_awaited_once()


@pytest.mark.asyncio
async def test_parked_row_preview_carries_the_script(
    linux_write_registered: None,  # noqa: F811 -- fixture
) -> None:
    from sqlalchemy import select

    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.db.models import ApprovalRequest
    from meho_backplane.operations import dispatch
    from meho_backplane.targets.resolver import resolve_target

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        target = await resolve_target(session, _OPERATOR.tenant_id, _TARGET_NAME)
    result = await dispatch(
        operator=_OPERATOR,
        connector_id=_CONNECTOR_ID,
        op_id="linux.script.run",
        target=target,
        params={
            "script": _SCRIPT,
            "env": {"APP_MODE": _ENV_VALUE_CANARY},
            "script_ref": "app-repo@abc123:check.sh",
            "expected_sha256": _sha(_SCRIPT),
        },
        _approved=False,
    )
    dumped = result.model_dump(mode="json")
    assert dumped["status"] == "awaiting_approval", dumped
    request_id = dumped["extras"]["approval_request_id"]
    async with sessionmaker() as session:
        row = (
            await session.execute(
                select(ApprovalRequest).where(ApprovalRequest.id == uuid.UUID(request_id))
            )
        ).scalar_one()
    assert row.params is None
    assert row.proposed_effect is not None
    preview = row.proposed_effect["preview"]
    assert preview["script"] == _SCRIPT
    assert preview["script_sha256"] == _sha(_SCRIPT)
    assert preview["script_ref"] == "app-repo@abc123:check.sh"
    assert preview["script_check"] == SCRIPT_CHECK_MATCHES
    assert _ENV_VALUE_CANARY not in repr(row.proposed_effect)
