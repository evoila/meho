# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Console: the approval modal shows the script of a ``linux.script.run``.

* The script text renders as code in its own block, with its SHA-256, the
  reviewed copy the caller named and the result of the hash check.
* The script is not repeated in the field table; the other preview fields
  (argument size, env names) stay there.
* A script with hidden values or a cut text says so; a preview without a
  text says why; a preview without script fields shows no script block.
* Script text is escaped as HTML.
"""

from __future__ import annotations

from meho_backplane.connectors.linux.script_preview import SCRIPT_CHECK_MISMATCH
from tests.test_ui_approvals import (
    _bff_env,  # noqa: F401 -- autouse fixture: pins the BFF env for these tests
    _render_modal,
)

_SCRIPT = "#!/bin/bash\nset -eu\nuname -a\n  df -h /\n"
_SHA = "a" * 64


def _effect(preview: dict[str, object]) -> dict[str, object]:
    return {
        "op_id": "linux.script.run",
        "connector_id": "linux-ssh",
        "op_class": "credential_write",
        "preview": {"target": "linux-1", "interpreter": "/bin/bash", **preview},
        "preview_populated": True,
        "safety_level": "dangerous",
    }


def _block(body: str) -> str:
    after = body.split('data-testid="approval-script"')[1]
    return after.split('aria-label="Proposed effect fields"')[0]


def test_modal_shows_the_script_body_and_its_sha256() -> None:
    body = _render_modal(
        _effect(
            {
                "script": _SCRIPT,
                "script_bytes": len(_SCRIPT),
                "script_sha256": _SHA,
                "script_ref": "app-repo@abc123:scripts/check.sh",
                "script_check": "matches",
                "argument_bytes": 12,
                "env_var_names": ["APP_MODE"],
            }
        )
    )
    block = _block(body)
    assert "Script to run" in block
    # Line breaks and the indent stay as they are, inside a <pre>.
    assert "<pre" in block
    assert "set -eu\nuname -a\n  df -h /" in block
    assert _SHA in block
    assert "app-repo@abc123:scripts/check.sh" in block
    assert ">matches<" in block
    table = body.split('aria-label="Proposed effect fields"')[1].split("</table>")[0]
    # The script is shown once, in its block; the table keeps the other fields.
    assert ">script<" not in table
    assert "uname -a" not in table
    assert "argument_bytes" in table
    assert "APP_MODE" in table


def test_modal_shows_a_hash_mismatch() -> None:
    body = _render_modal(
        _effect(
            {
                "script": _SCRIPT,
                "script_bytes": len(_SCRIPT),
                "script_sha256": _SHA,
                "script_check": SCRIPT_CHECK_MISMATCH,
            }
        )
    )
    block = _block(body)
    assert f">{SCRIPT_CHECK_MISMATCH}<" in block
    assert "badge-error" in block


def test_modal_says_when_values_are_hidden_or_the_text_is_cut() -> None:
    body = _render_modal(
        _effect(
            {
                "script": "export DB_[REDACTED:api_key]\n",
                "script_bytes": 40,
                "script_values_hidden": True,
                "script_truncated": True,
            }
        )
    )
    block = _block(body)
    assert 'data-testid="approval-script-hidden"' in block
    assert 'data-testid="approval-script-truncated"' in block
    assert 'data-testid="approval-script-sha256"' not in block


def test_modal_says_why_no_script_text_is_shown() -> None:
    body = _render_modal(
        _effect({"script_bytes": 10, "script_unavailable": "MEHO could not check the script"})
    )
    block = _block(body)
    assert 'data-testid="approval-script-unavailable"' in block
    assert "MEHO could not check the script" in block
    assert 'data-testid="approval-script-body"' not in block


def test_modal_escapes_script_text() -> None:
    script = "echo '<script>alert(1)</script>'\n"
    body = _render_modal(_effect({"script": script, "script_bytes": len(script)}))
    block = _block(body)
    assert "<script>alert(1)</script>" not in block
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in block


def test_modal_without_script_fields_shows_no_script_block() -> None:
    body = _render_modal(
        {
            "op_id": "linux.file.write",
            "connector_id": "linux-ssh",
            "op_class": "credential_write",
            "preview": {"target": "linux-1", "path": "/etc/app.conf", "content_bytes": 10},
            "preview_populated": True,
            "safety_level": "dangerous",
        }
    )
    assert 'data-testid="approval-script"' not in body
    assert 'aria-label="Proposed effect fields"' in body
