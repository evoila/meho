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
* A script of more than one page shows "page N of M", and while the request
  waits, "previous page" and "next page" links; the page route gives the page.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

import respx

from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.linux.script_preview import (
    SCRIPT_CHECK_MISMATCH,
    SCRIPT_SHOWN_MAX_BYTES,
    script_preview_fields,
)
from tests.test_ui_approvals import (
    _RESOLVE_OPERATOR,
    _REVIEWER_SUB,
    _TENANT_A,
    _authenticated_client,
    _bff_env,  # noqa: F401 -- autouse fixture: pins the BFF env for these tests
    _operator,
    _render_modal,
    _render_modal_of,
    _seed_request,
    _seed_session_sync,
    _seed_tenant,
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
    # The script fields are shown once, in their block; the table keeps the other fields.
    assert ">script" not in table
    assert "uname -a" not in table
    assert _SHA not in table
    assert "app-repo@abc123" not in table
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
    # No SHA-256 is shown, so the cut note does not ask to compare one.
    assert "Compare its SHA-256" not in block
    assert "Read the full script in a" in block


def test_modal_asks_to_compare_the_sha256_of_a_cut_script() -> None:
    body = _render_modal(
        _effect(
            {
                "script": _SCRIPT,
                "script_bytes": 70_000,
                "script_sha256": _SHA,
                "script_truncated": True,
            }
        )
    )
    block = _block(body)
    assert "Compare its SHA-256" in block


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


def _text(size: int) -> str:
    line = "echo " + "a" * 58 + "\n"
    whole, rest = divmod(size, len(line))
    return line * whole + "b" * rest


def _seed_script_request(script: str, *, status_value: str = "pending") -> uuid.UUID:
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.db.models import ApprovalRequest
    from meho_backplane.operations.approval_handoff import store

    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _seed_request(
        tenant_id=_TENANT_A,
        op_id="linux.script.run",
        connector_id="linux-ssh",
        status_value=status_value,
        proposed_effect=_effect(script_preview_fields({"script": script})),
    )

    async def _attach() -> None:
        async with get_sessionmaker()() as session, session.begin():
            row = await session.get(ApprovalRequest, rid)
            assert row is not None
            row.execution_handle = store(
                session, rid, _TENANT_A, {"params": {"script": script}, "resume_parent": None}
            )

    asyncio.run(_attach())
    return rid


def test_modal_of_a_long_script_shows_page_one_of_two_with_a_next_link() -> None:
    script = _text(SCRIPT_SHOWN_MAX_BYTES + 100)
    rid = _seed_script_request(script)
    block = _block(_render_modal_of(rid))
    assert "page 1 of 2" in block
    assert f"/ui/approvals/{rid}/script?page=2" in block
    assert 'data-testid="approval-script-prev"' not in block
    assert 'data-testid="approval-script-next"' in block


def test_modal_of_a_decided_long_script_shows_no_page_links() -> None:
    rid = _seed_script_request(_text(SCRIPT_SHOWN_MAX_BYTES + 100), status_value="approved")
    block = _block(_render_modal_of(rid))
    assert "page 1 of 2" in block
    assert "/script?page=" not in block
    assert 'data-testid="approval-script-truncated"' in block
    assert "Only page 1 is shown." in block
    assert "the other pages are no longer shown" in block


def test_modal_of_a_one_page_script_shows_no_page_line() -> None:
    block = _block(_render_modal_of(_seed_script_request(_SCRIPT)))
    assert "approval-script-pages" not in block


def _get_script_page(rid: uuid.UUID, page: int, **operator_args: object) -> object:
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(tenant_id=_TENANT_A, sub=_REVIEWER_SUB, **operator_args)  # type: ignore[arg-type]
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator):
            return client.get(f"/ui/approvals/{rid}/script?page={page}")


def test_script_page_route_gives_the_last_page_with_a_previous_link() -> None:
    script = _text(SCRIPT_SHOWN_MAX_BYTES) + "last-line\n"
    rid = _seed_script_request(script)
    response = _get_script_page(rid, 2)
    assert response.status_code == 200, response.text  # type: ignore[attr-defined]
    text = response.text  # type: ignore[attr-defined]
    assert "page 2 of 2" in text
    assert "last-line" in text
    assert "echo aaaa" not in text
    assert f"/ui/approvals/{rid}/script?page=1" in text
    assert 'data-testid="approval-script-next"' not in text
    assert 'id="approval-script-block"' in text


def test_script_page_route_after_the_decision_says_the_script_is_not_kept() -> None:
    # A page link of a modal opened before the decision: the block is replaced
    # by a plain sentence, with no page links, instead of showing nothing.
    rid = _seed_script_request(_text(SCRIPT_SHOWN_MAX_BYTES + 100), status_value="rejected")
    response = _get_script_page(rid, 2)
    assert response.status_code == 200  # type: ignore[attr-defined]
    text = response.text  # type: ignore[attr-defined]
    assert "MEHO no longer keeps this script" in text
    assert 'id="approval-script-block"' in text
    assert "/script?page=" not in text
    assert "echo aaaa" not in text


def test_script_page_route_of_a_script_that_cannot_be_decrypted_says_so() -> None:
    from sqlalchemy import update

    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.operations.approval_handoff import ApprovalExecutionPayload

    rid = _seed_script_request(_text(SCRIPT_SHOWN_MAX_BYTES + 100))

    async def _spoil() -> None:
        async with get_sessionmaker()() as session, session.begin():
            await session.execute(
                update(ApprovalExecutionPayload)
                .where(ApprovalExecutionPayload.request_id == rid)
                .values(ciphertext=b"not-a-fernet-token")
            )

    asyncio.run(_spoil())
    response = _get_script_page(rid, 2)
    assert response.status_code == 200  # type: ignore[attr-defined]
    assert "MEHO cannot read the stored script of this request" in response.text  # type: ignore[attr-defined]


def test_modal_shows_control_and_format_characters_as_visible_marks() -> None:
    # A right-to-left override, an escape code, a zero-width space and a NUL.
    script = "echo safe\u202e;tpircs-live\nprintf '\x1b[2J'\u200b\x00\n\tindented\n"
    body = _render_modal(_effect({"script": script, "script_bytes": len(script.encode("utf-8"))}))
    shown = _block(body).split('data-testid="approval-script-body">')[1].split("</pre>")[0]
    for raw in ("\u202e", "\x1b", "\u200b", "\x00"):
        assert raw not in shown
    assert "echo safe\\u202e;tpircs-live" in shown
    assert "printf &#39;\\x1b[2J&#39;\\u200b\\x00" in shown
    # A new line and a tab stay as they are.
    assert "\n\tindented\n" in shown


def test_script_page_route_refuses_a_page_that_does_not_exist() -> None:
    rid = _seed_script_request(_SCRIPT)
    assert _get_script_page(rid, 2).status_code == 404  # type: ignore[attr-defined]


def test_script_page_route_is_not_open_to_a_person_who_may_not_read_the_request() -> None:
    rid = _seed_script_request(_text(SCRIPT_SHOWN_MAX_BYTES + 100))
    denied = _get_script_page(rid, 2, role=TenantRole.READ_ONLY)
    assert denied.status_code == 403  # type: ignore[attr-defined]
    assert "echo" not in denied.text  # type: ignore[attr-defined]
    # A read-only person with the approver right may read it, like the request itself.
    allowed = _get_script_page(rid, 2, role=TenantRole.READ_ONLY, approver=True)
    assert allowed.status_code == 200  # type: ignore[attr-defined]
