# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The approver reads a long ``linux.script.run`` script, page by page.

* A script is cut into 64 KiB pages after values that look like secrets are
  hidden over the whole script. A page border never splits a character.
* The SHA-256 is of the whole script and the same on every page; with hidden
  values no page shows it.
* ``linux.script.run`` refuses a script over 1 MiB before it waits.
* ``GET /api/v1/approvals/{id}/script`` reads a page of a waiting request,
  does not delete the stored script, and says so in plain words after the
  decision.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient

import meho_backplane.connectors.linux  # noqa: F401 -- import for registry side-effects
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.linux.script_preview import (
    SCRIPT_MAX_BYTES,
    SCRIPT_SHOWN_MAX_BYTES,
    ScriptPageOutOfRangeError,
    script_page_fields,
    script_preview_fields,
)
from meho_backplane.main import app
from meho_backplane.settings import get_settings
from tests.test_connectors_linux_write import (
    _CONNECTOR_ID,
    _OPERATOR,
    _TARGET_NAME,
    _required_settings_env,  # noqa: F401 -- autouse fixture
    linux_write_registered,  # noqa: F401 -- fixture
)

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks

_FAKE_VALUE = "made-up-value-not-real-4471"
_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _text(size: int) -> str:
    """Plain script text of exactly *size* ASCII bytes, in short lines."""
    line = "echo " + "a" * 58 + "\n"
    whole, rest = divmod(size, len(line))
    return line * whole + "b" * rest


def _all_pages(script: str) -> list[dict[str, Any]]:
    first = script_page_fields(script, 1)
    return [first] + [script_page_fields(script, n) for n in range(2, first["script_pages"] + 1)]


# ---------------------------------------------------------------------------
# The pages
# ---------------------------------------------------------------------------


def test_a_script_of_exactly_one_page_has_one_page_and_is_not_cut() -> None:
    script = _text(SCRIPT_SHOWN_MAX_BYTES)
    fields = script_preview_fields({"script": script})
    assert fields["script_pages"] == 1
    assert fields["script"] == script
    assert "script_truncated" not in fields


def test_one_byte_more_makes_a_second_page_that_holds_the_last_byte() -> None:
    script = _text(SCRIPT_SHOWN_MAX_BYTES - 1) + "bc"
    preview = script_preview_fields({"script": script})
    assert preview["script_pages"] == 2
    assert preview["script_truncated"] is True
    page1 = script_page_fields(script, 1)
    page2 = script_page_fields(script, 2)
    assert page1["script"] == preview["script"]
    assert page2["script"] == "c"
    assert page1["script"] + page2["script"] == script


def test_a_page_border_never_splits_a_character() -> None:
    script = ("é" * 30 + "\n") * 1300  # two bytes each; a bit over one page
    pages = _all_pages(script)
    assert len(pages) == 2
    assert "".join(p["script"] for p in pages) == script
    for p in pages:
        assert len(p["script"].encode("utf-8")) <= SCRIPT_SHOWN_MAX_BYTES
    # A three-byte character that starts one byte before the border moves whole.
    script = _text(SCRIPT_SHOWN_MAX_BYTES - 1) + "€€"
    pages = _all_pages(script)
    assert "".join(p["script"] for p in pages) == script
    assert pages[1]["script"] == "€€"


def test_a_secret_value_that_crosses_the_page_border_is_hidden_on_every_page() -> None:
    prefix = "export DB_PASSWORD="
    line = f"{prefix}{_FAKE_VALUE}\n"
    padding = "# " + "x" * 60 + "\n"
    # The value starts 13 bytes before the first border and ends after it.
    before = 13
    head_size = SCRIPT_SHOWN_MAX_BYTES - before - len(prefix)
    head = padding * (head_size // len(padding))
    head += "#" * (head_size - len(head) - 1) + "\n"
    tail = padding * 1500
    script = head + line + tail
    assert script.index(_FAKE_VALUE) == SCRIPT_SHOWN_MAX_BYTES - before
    first_part, second_part = _FAKE_VALUE[:before], _FAKE_VALUE[before:]
    assert first_part and second_part
    pages = _all_pages(script)
    assert len(pages) >= 2
    for p in pages:
        assert first_part not in p["script"]
        assert second_part not in p["script"]
        assert p["script_values_hidden"] is True
        assert "script_sha256" not in p
    hidden_line = script_page_fields(line, 1)["script"]
    assert _FAKE_VALUE not in hidden_line
    assert "".join(p["script"] for p in pages) == head + hidden_line + tail


def test_the_checksum_is_of_the_whole_script_and_the_same_on_every_page() -> None:
    script = "echo hello\n" * 20_000
    pages = _all_pages(script)
    assert len(pages) == 4
    assert {p["script_sha256"] for p in pages} == {_sha(script)}
    assert {p["script_bytes"] for p in pages} == {len(script.encode("utf-8"))}
    assert all("script_values_hidden" not in p for p in pages)


def test_a_page_that_does_not_exist_is_refused() -> None:
    script = _text(SCRIPT_SHOWN_MAX_BYTES + 1)
    for page in (0, 3, 99):
        with pytest.raises(ScriptPageOutOfRangeError) as excinfo:
            script_page_fields(script, page)
        assert "has 2 page(s)" in str(excinfo.value)


def test_a_failed_redaction_pass_shows_no_text_on_any_page() -> None:
    from unittest.mock import patch

    with patch(
        "meho_backplane.connectors.linux.script_preview._hide_secret_values",
        side_effect=RuntimeError("boom"),
    ):
        fields = script_page_fields("echo hi\n", 1)
        assert "script" not in fields
        assert "script_sha256" not in fields
        assert fields["script_unavailable"]
        with pytest.raises(ScriptPageOutOfRangeError):
            script_page_fields("echo hi\n", 2)


# ---------------------------------------------------------------------------
# The size limit
# ---------------------------------------------------------------------------


async def _dispatch_script(script: str) -> dict[str, Any]:
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.operations import dispatch
    from meho_backplane.targets.resolver import resolve_target

    async with get_sessionmaker()() as session:
        target = await resolve_target(session, _OPERATOR.tenant_id, _TARGET_NAME)
    result = await dispatch(
        operator=_OPERATOR,
        connector_id=_CONNECTOR_ID,
        op_id="linux.script.run",
        target=target,
        params={"script": script},
        _approved=False,
    )
    return result.model_dump(mode="json")


async def _count_requests() -> int:
    from sqlalchemy import func, select

    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.db.models import ApprovalRequest

    async with get_sessionmaker()() as session:
        return int((await session.execute(select(func.count(ApprovalRequest.id)))).scalar_one())


@pytest.mark.asyncio
async def test_a_script_of_the_limit_waits_for_approval(
    linux_write_registered: None,  # noqa: F811 -- fixture
) -> None:
    script = _text(SCRIPT_MAX_BYTES)
    dumped = await _dispatch_script(script)
    assert dumped["status"] == "awaiting_approval", dumped
    assert await _count_requests() == 1


@pytest.mark.asyncio
async def test_a_script_over_the_limit_is_refused_with_plain_words_before_it_waits(
    linux_write_registered: None,  # noqa: F811 -- fixture
) -> None:
    # Two bytes per character: over the byte limit, under the character limit.
    script = "é" * 1000 + "\n" + _text(SCRIPT_MAX_BYTES - 2000)
    assert len(script.encode("utf-8")) == SCRIPT_MAX_BYTES + 1
    dumped = await _dispatch_script(script)
    assert dumped["status"] == "error", dumped
    assert "invalid_params" in dumped["error"]
    text = repr(dumped["extras"])
    assert "The script is 1,048,577 bytes." in text
    assert "MEHO accepts at most 1,048,576 bytes (1 MiB) for linux.script.run." in text
    assert "copy a large file to the host first" in text
    assert await _count_requests() == 0


@pytest.mark.asyncio
async def test_an_ascii_script_over_the_limit_is_refused_with_plain_words_before_it_waits(
    linux_write_registered: None,  # noqa: F811 -- fixture
) -> None:
    # Over the schema ``maxLength`` too: the plain sentence replaces its error.
    dumped = await _dispatch_script(_text(SCRIPT_MAX_BYTES + 1))
    assert dumped["status"] == "error", dumped
    assert "invalid_params" in dumped["error"]
    text = repr(dumped["extras"])
    assert "The script is 1,048,577 bytes." in text
    assert "MEHO accepts at most 1,048,576 bytes (1 MiB) for linux.script.run." in text
    assert "at most this many characters" not in text
    assert await _count_requests() == 0


def test_the_script_schema_says_the_limit() -> None:
    from meho_backplane.connectors.linux.ops_write import _SCRIPT_RUN_OP

    script_schema = _SCRIPT_RUN_OP.parameter_schema["properties"]["script"]
    assert script_schema["maxLength"] == SCRIPT_MAX_BYTES


def test_a_check_problem_replaces_only_the_length_error_on_its_own_field() -> None:
    from meho_backplane.operations._validate import params_errors

    script_error = {
        "path": "$.script",
        "validator": "maxLength",
        "expected": "string with at most this many characters: 1048576",
        "remediation": "use a shorter value",
    }
    ref_error = {**script_error, "path": "$.script_ref"}
    params = {"script": _text(SCRIPT_MAX_BYTES + 1)}
    errors = params_errors("linux.script.run", params, [script_error, ref_error])
    assert [e["path"] for e in errors] == ["script", "$.script_ref"]
    assert errors[0]["validator"] == "params_check"
    # Another kind of schema error: the schema errors stay as they are.
    type_error = {**script_error, "path": "$.timeout_seconds", "validator": "type"}
    assert params_errors("linux.script.run", params, [script_error, type_error]) == [
        script_error,
        type_error,
    ]
    # No check problem: the length error stays.
    small = {"script": "echo hi\n"}
    assert params_errors("linux.script.run", small, [ref_error]) == [ref_error]


# ---------------------------------------------------------------------------
# The REST read
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(app)


def _seed(script: str, *, status: str = "pending", op_id: str = "linux.script.run") -> uuid.UUID:
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.db.models import ApprovalRequest
    from meho_backplane.operations.approval_handoff import store

    request_id = uuid.uuid4()

    async def _go() -> None:
        async with get_sessionmaker()() as session, session.begin():
            row = ApprovalRequest(
                id=request_id,
                tenant_id=_TENANT_A,
                run_id=None,
                principal_sub="svc-requester",
                op_id=op_id,
                connector_id="linux-ssh",
                target_id=None,
                params_hash="0" * 64,
                params=None,
                proposed_effect={
                    "op_id": op_id,
                    "preview": script_preview_fields({"script": script}),
                },
                status=status,
            )
            session.add(row)
            await session.flush()
            row.execution_handle = store(
                session,
                request_id,
                _TENANT_A,
                {"params": {"script": script}, "resume_parent": None},
            )

    asyncio.run(_go())
    return request_id


_KEY = make_rsa_keypair("kid-script")


def _get(client: TestClient, path: str, *, role: TenantRole, tenant: uuid.UUID = _TENANT_A) -> Any:
    key = _KEY
    clear_jwks_cache()
    token = mint_token(
        key, sub="approver", tenant_role=role.value, tenant_id=str(tenant), fresh_sign_in=True
    )
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        return client.get(path, headers={"Authorization": f"Bearer {token}"})


def test_rest_reads_each_page_with_the_whole_script_checksum(client: TestClient) -> None:
    script = "echo hello\n" * 10_000
    request_id = _seed(script)
    first = _get(client, f"/api/v1/approvals/{request_id}/script", role=TenantRole.OPERATOR)
    assert first.status_code == 200, first.text
    body = first.json()
    assert (body["page"], body["pages"]) == (1, 2)
    assert body["script_bytes"] == len(script)
    assert body["script_sha256"] == _sha(script)
    second = _get(client, f"/api/v1/approvals/{request_id}/script?page=2", role=TenantRole.OPERATOR)
    assert second.status_code == 200, second.text
    body2 = second.json()
    assert (body2["page"], body2["pages"]) == (2, 2)
    assert body["script"] + body2["script"] == script
    assert body2["script_sha256"] == _sha(script)


def test_rest_page_read_hides_values_and_shows_no_checksum(client: TestClient) -> None:
    script = "echo a\n" * 20_000 + f"export DB_PASSWORD={_FAKE_VALUE}\n"
    request_id = _seed(script)
    response = _get(
        client, f"/api/v1/approvals/{request_id}/script?page=3", role=TenantRole.OPERATOR
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["script_values_hidden"] is True
    assert body["script_sha256"] is None
    assert _FAKE_VALUE not in response.text


def test_rest_page_read_does_not_delete_the_stored_script(client: TestClient) -> None:
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.db.models import ApprovalRequest
    from meho_backplane.operations.approval_handoff import consume

    script = "echo hello\n" * 10_000
    request_id = _seed(script)
    for _ in range(2):
        response = _get(client, f"/api/v1/approvals/{request_id}/script", role=TenantRole.OPERATOR)
        assert response.status_code == 200, response.text

    async def _consume() -> dict[str, Any]:
        async with get_sessionmaker()() as session:
            row = await session.get(ApprovalRequest, request_id)
            assert row is not None and row.execution_handle is not None
            return await consume(session, row.execution_handle, request_id, _TENANT_A)

    payload = asyncio.run(_consume())
    assert payload["params"]["script"] == script


@pytest.mark.parametrize("status", ["approved", "rejected", "expired"])
def test_rest_page_read_after_the_decision_says_the_script_is_not_kept(
    client: TestClient, status: str
) -> None:
    request_id = _seed("echo hi\n", status=status)
    response = _get(client, f"/api/v1/approvals/{request_id}/script", role=TenantRole.OPERATOR)
    assert response.status_code == 410, response.text
    assert "MEHO no longer keeps this script" in response.json()["detail"]


def test_rest_page_read_is_not_open_to_a_person_who_may_not_read_the_request(
    client: TestClient,
) -> None:
    request_id = _seed("echo hi\n")
    denied = _get(client, f"/api/v1/approvals/{request_id}/script", role=TenantRole.READ_ONLY)
    assert denied.status_code == 403, denied.text
    other_tenant = _get(
        client, f"/api/v1/approvals/{request_id}/script", role=TenantRole.OPERATOR, tenant=_TENANT_B
    )
    assert other_tenant.status_code == 404, other_tenant.text
    assert "echo hi" not in denied.text + other_tenant.text


def test_rest_page_that_does_not_exist_and_a_request_with_no_script_are_404(
    client: TestClient,
) -> None:
    request_id = _seed("echo hi\n")
    too_far = _get(
        client, f"/api/v1/approvals/{request_id}/script?page=2", role=TenantRole.OPERATOR
    )
    assert too_far.status_code == 404, too_far.text
    assert "has 1 page(s)" in too_far.json()["detail"]
    zero = _get(client, f"/api/v1/approvals/{request_id}/script?page=0", role=TenantRole.OPERATOR)
    assert zero.status_code == 422
    other_op = _seed("echo hi\n", op_id="linux.file.write")
    none = _get(client, f"/api/v1/approvals/{other_op}/script", role=TenantRole.OPERATOR)
    assert none.status_code == 404, none.text


def test_rest_page_read_of_a_script_that_cannot_be_decrypted_is_a_plain_refusal(
    client: TestClient,
) -> None:
    from sqlalchemy import update

    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.operations.approval_handoff import ApprovalExecutionPayload

    request_id = _seed("echo hi\n")

    async def _spoil() -> None:
        async with get_sessionmaker()() as session, session.begin():
            await session.execute(
                update(ApprovalExecutionPayload)
                .where(ApprovalExecutionPayload.request_id == request_id)
                .values(ciphertext=b"not-a-fernet-token")
            )

    asyncio.run(_spoil())
    response = _get(client, f"/api/v1/approvals/{request_id}/script", role=TenantRole.OPERATOR)
    assert response.status_code == 409, response.text
    assert "MEHO cannot read the stored script of this request" in response.json()["detail"]


# ---------------------------------------------------------------------------
# The redaction pass: once per request, in a fixed time
# ---------------------------------------------------------------------------


def test_reading_ten_pages_runs_the_redaction_pass_once(client: TestClient) -> None:
    from unittest.mock import patch

    from meho_backplane.connectors.linux.script_preview import _hide_secret_values

    script = _text(SCRIPT_SHOWN_MAX_BYTES * 10)
    request_id = _seed(script)
    with patch(
        "meho_backplane.connectors.linux.script_preview._hide_secret_values",
        side_effect=_hide_secret_values,
    ) as spy:
        pages = []
        for page in range(1, 11):
            response = _get(
                client,
                f"/api/v1/approvals/{request_id}/script?page={page}",
                role=TenantRole.OPERATOR,
            )
            assert response.status_code == 200, response.text
            pages.append(response.json()["script"])
    assert spy.call_count == 1
    assert "".join(pages) == script


def test_a_page_read_of_a_script_with_one_long_line_answers_in_a_fixed_time(
    client: TestClient,
) -> None:
    import time

    script = "a" * SCRIPT_MAX_BYTES
    request_id = _seed(script)
    started = time.monotonic()
    response = _get(
        client, f"/api/v1/approvals/{request_id}/script?page=3", role=TenantRole.OPERATOR
    )
    elapsed = time.monotonic() - started
    assert response.status_code == 200, response.text
    assert response.json()["script"] == "a" * SCRIPT_SHOWN_MAX_BYTES
    assert elapsed < 30, elapsed


def test_a_url_password_is_still_hidden_where_the_scheme_follows_digits() -> None:
    from meho_backplane.redaction.command_line import hide_in_text

    for text in (
        f"curl https://u:{_FAKE_VALUE}@host/x",
        f"x=1https://u:{_FAKE_VALUE}@host/x",
        f"git+ssh://u:{_FAKE_VALUE}@host/x",
    ):
        shown = hide_in_text(text)
        assert _FAKE_VALUE not in shown, shown
        assert "://u:" in shown


# ---------------------------------------------------------------------------
# A script that is not valid UTF-8
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_script_that_is_not_valid_utf8_is_refused_with_plain_words(
    linux_write_registered: None,  # noqa: F811 -- fixture
) -> None:
    dumped = await _dispatch_script("echo hi\n\ud800\n")
    assert dumped["status"] == "error", dumped
    assert "invalid_params" in dumped["error"]
    assert "The script is not valid UTF-8 text." in repr(dumped["extras"])
    assert await _count_requests() == 0


# ---------------------------------------------------------------------------
# The whole way: read a page, approve, run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_a_page_then_approve_and_the_run_gets_the_whole_stored_script(
    linux_write_registered: None,  # noqa: F811 -- fixture
) -> None:
    from unittest.mock import AsyncMock, patch

    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.db.models import ApprovalRequest
    from meho_backplane.operations._validate import params_hash_matches
    from meho_backplane.operations.approval_queue import (
        approve_request,
        resume_dispatch_after_approval,
    )
    from meho_backplane.operations.approval_script import read_script_page

    script = "é" * 100 + "\n" + _text(SCRIPT_SHOWN_MAX_BYTES * 2)
    dumped = await _dispatch_script(script)
    assert dumped["status"] == "awaiting_approval", dumped
    request_id = uuid.UUID(dumped["extras"]["approval_request_id"])

    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        page = await read_script_page(session, row, 2)
    assert page["script_pages"] == 3
    assert page["script_sha256"] == _sha(script)

    approver = _OPERATOR.model_copy(update={"sub": "script-approver", "self_approver": True})
    async with get_sessionmaker()() as session:
        approved = await approve_request(session, request_id, operator=approver)
        await session.commit()

    handler = AsyncMock(return_value={"stdout": "", "stderr": "", "exit_status": 0})
    with patch("meho_backplane.connectors.linux.ops_write.linux_script_run", handler):
        await resume_dispatch_after_approval(operator=approver, request=approved)

    handler.assert_awaited_once()
    call = handler.await_args
    ran = call.kwargs.get("params", call.args[2] if len(call.args) > 2 else None)
    assert isinstance(ran, dict), handler.await_args
    assert ran["script"] == script
    assert _sha(ran["script"]) == _sha(script)
    assert params_hash_matches(ran, approved.params_hash)
