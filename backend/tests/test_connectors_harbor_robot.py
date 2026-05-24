# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for Harbor robot lifecycle typed ops (G3.5-T9 #621).

Covers:
* ``harbor.robot.create`` handler — respx mock against Harbor API.
* ``harbor.robot.delete`` handler — respx mock against Harbor API.
* The dispatched operator threads into both handlers and reaches the
  operator-context Vault credential read (G3.10-T-followup #984).
* ``classify_op`` maps both op-ids to the right sensitivity classes.
* ``redact_payload("credential_mint", ...)`` — aggregate-only, no secret.
* No regression to ``credential_read`` classification for existing vault ops.

Auth: HTTP Basic (shared service account). The dispatched :class:`Operator`
is threaded into ``robot_create`` / ``robot_delete`` (the dispatcher passes
``operator`` by parameter name — see
:func:`~meho_backplane.operations._branches.dispatch_typed`) and forwarded to
:meth:`HarborConnector.auth_headers` / :meth:`HarborConnector._post_json`,
which resolves the per-target service-account credential via the
operator-context Vault read. These tests exercise that real path against the
in-process Vault fake (``install_fake_client``) — a non-empty ``raw_jwt``
resolves credentials, an empty ``raw_jwt`` (the synthesised system operator)
fails closed with :class:`VaultCredentialsReadError`. No credentials_loader
stub masks the read.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import httpx
import pytest
import respx
from structlog.testing import capture_logs

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast.events import classify_op, redact_payload
from meho_backplane.connectors._shared.system_operator import synthesise_system_operator
from meho_backplane.connectors._shared.vault_creds import (
    VaultCredentialsReadError,
    load_basic_credentials,
)
from meho_backplane.connectors.harbor import HarborConnector
from meho_backplane.connectors.harbor.session import load_credentials_from_vault
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.operations._branches import dispatch_typed
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

# ---------------------------------------------------------------------------
# Canary credential values — asserted to NEVER appear in any captured log.
# ---------------------------------------------------------------------------

_CANARY_USERNAME = "svc-harbor-robot-canary"
_CANARY_PASSWORD = "p4ss-canary-must-not-leak-robot-harbor"

# ---------------------------------------------------------------------------
# Registry isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_harbor_registry() -> None:
    """Re-register HarborConnector before each test, clear after.

    Mirrors the pattern in ``test_connectors_harbor_auth.py``.
    """
    clear_registry()
    register_connector_v2(
        product=HarborConnector.product,
        version=HarborConnector.version,
        impl_id=HarborConnector.impl_id,
        cls=HarborConnector,
    )
    yield
    clear_registry()


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin chassis env vars Settings reads (Vault client construction).

    The robot handlers now run the live operator-context Vault read
    through the in-process fake; the read still constructs a real Vault
    client from Settings, so the same env the credread E2E pins is
    required here.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value


_TARGET = _StubTarget(
    name="harbor-test",
    host="harbor.test.invalid",
    port=443,
    secret_ref="targets/harbor/harbor-test",
)


def _make_operator() -> Operator:
    """Operator carrying a non-empty raw_jwt (the fail-closed gate passes)."""
    return Operator(
        sub="op-robot-harbor",
        name="Harbor Robot Operator",
        email=None,
        raw_jwt="op.robot.harbor.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a3"),
        tenant_role=TenantRole.OPERATOR,
    )


def _make_connector() -> HarborConnector:
    """Connector with the DEFAULT (live) loader — no injected stub.

    The operator-context Vault read runs against the in-process fake the
    test installs, exercising ``load_credentials_from_vault`` ->
    ``load_basic_credentials`` -> ``vault_client_for_operator`` verbatim.
    """
    return HarborConnector()


# ---------------------------------------------------------------------------
# harbor.robot.create — handler (real operator-context credential read)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_robot_create_posts_to_harbor_and_returns_id_name_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """robot_create() calls POST /api/v2.0/robots and returns
    {id, name, secret} from the Harbor response."""
    install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )
    connector = _make_connector()
    harbor_response = {
        "id": 42,
        "name": "robot$myproject+ci-push",
        "secret": "minted-secret-value",
        "creation_time": "2026-05-20T12:00:00.000Z",
        "expiration_time": -1,
    }
    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://harbor.test.invalid/api/v2.0/robots").mock(
            return_value=respx.MockResponse(201, json=harbor_response)
        )
        result = await connector.robot_create(
            _make_operator(),
            _TARGET,
            {"name": "ci-push", "project": "myproject", "duration": -1},
        )

    assert result == {"id": 42, "name": "robot$myproject+ci-push", "secret": "minted-secret-value"}
    await connector.aclose()


@pytest.mark.asyncio
async def test_robot_create_reads_credential_under_operator_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """robot_create() forwards the dispatched operator's JWT to the Vault read."""
    fake = install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )
    connector = _make_connector()
    operator = _make_operator()

    with respx.mock() as mock:
        mock.post("https://harbor.test.invalid/api/v2.0/robots").mock(
            return_value=respx.MockResponse(
                201, json={"id": 1, "name": "robot$p+n", "secret": "s", "expiration_time": -1}
            )
        )
        await connector.robot_create(
            operator,
            _TARGET,
            {"name": "bot", "project": "proj", "duration": 90},
        )

    # The default loader read Vault under the operator's identity — not the
    # empty-JWT system operator.
    assert fake.auth.jwt.login_calls[-1]["jwt"] == operator.raw_jwt
    assert fake.secrets.kv.v2.read_calls[-1]["path"] == _TARGET.secret_ref
    await connector.aclose()


@pytest.mark.asyncio
async def test_robot_create_sends_basic_auth_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """robot_create() includes the Authorization: Basic header on the POST request."""
    install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )
    connector = _make_connector()
    captured_headers: dict[str, str] = {}

    def _capture(request: respx.models.Request) -> respx.MockResponse:
        captured_headers.update(dict(request.headers))
        return respx.MockResponse(
            201,
            json={"id": 1, "name": "robot$proj+bot", "secret": "s", "expiration_time": -1},
        )

    with respx.mock() as mock:
        mock.post("https://harbor.test.invalid/api/v2.0/robots").mock(side_effect=_capture)
        await connector.robot_create(
            _make_operator(),
            _TARGET,
            {"name": "bot", "project": "proj", "duration": 90},
        )

    assert "authorization" in captured_headers
    assert captured_headers["authorization"].startswith("Basic ")
    await connector.aclose()


@pytest.mark.asyncio
async def test_robot_create_sends_correct_permission_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """robot_create() sends the Harbor permission structure with push+pull access."""
    install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )
    connector = _make_connector()
    captured_body: dict = {}

    def _capture(request: respx.models.Request) -> respx.MockResponse:
        import json as _json

        captured_body.update(_json.loads(request.content))
        return respx.MockResponse(
            201,
            json={"id": 10, "name": "robot$alpha+deployer", "secret": "x", "expiration_time": 90},
        )

    with respx.mock() as mock:
        mock.post("https://harbor.test.invalid/api/v2.0/robots").mock(side_effect=_capture)
        await connector.robot_create(
            _make_operator(),
            _TARGET,
            {"name": "deployer", "project": "alpha", "duration": 90},
        )

    assert captured_body["name"] == "deployer"
    assert captured_body["duration"] == 90
    assert captured_body["level"] == "project"
    perms = captured_body["permissions"]
    assert len(perms) == 1
    assert perms[0]["namespace"] == "alpha"
    accesses = {a["action"] for a in perms[0]["access"]}
    assert "push" in accesses
    assert "pull" in accesses
    await connector.aclose()


@pytest.mark.asyncio
async def test_robot_create_raises_on_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """robot_create() propagates httpx.HTTPStatusError on 4xx/5xx responses."""
    install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )
    connector = _make_connector()
    with respx.mock() as mock:
        mock.post("https://harbor.test.invalid/api/v2.0/robots").mock(
            return_value=respx.MockResponse(409, json={"errors": [{"code": "CONFLICT"}]})
        )
        with pytest.raises(httpx.HTTPStatusError):
            await connector.robot_create(
                _make_operator(),
                _TARGET,
                {"name": "ci-push", "project": "myproject", "duration": -1},
            )
    await connector.aclose()


@pytest.mark.asyncio
async def test_robot_create_system_operator_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A system operator (empty raw_jwt) cannot read the credential — fail closed.

    The credential read raises before any Harbor request is issued, so the
    fail-closed boundary the synthesised system operator enforces holds for
    the mint op.

    The assertion is **isolation-robust** (#984 PR #998 CI fix): the read
    must raise AND the in-process Vault fake must never be touched
    (``login_calls``/``read_calls`` stay empty). Under CI's
    ``pytest -n 6 --dist loadscope`` a ``pytest.raises``-only check could
    pass vacuously if cross-test state ever satisfied the call without the
    guard firing; asserting the fake was never reached makes a leaked
    successful read fail loudly here instead of silently passing. Mirrors
    the loader-boundary precedent in
    ``test_connectors_vcf_shared_auth.test_default_credentials_loader_rejects_empty_operator_jwt``.
    """
    fake = install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )
    connector = _make_connector()
    system_operator = synthesise_system_operator()
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post("https://harbor.test.invalid/api/v2.0/robots").mock(
            return_value=respx.MockResponse(201, json={"id": 1, "name": "x", "secret": "s"})
        )
        with pytest.raises(VaultCredentialsReadError):
            await connector.robot_create(
                system_operator,
                _TARGET,
                {"name": "ci-push", "project": "myproject", "duration": -1},
            )
    # Fail-closed: the guard raises BEFORE Vault is touched and before the
    # Harbor request is issued. A leaked successful read would have logged
    # in to the fake and read the secret — assert neither happened so the
    # boundary cannot be satisfied by cross-test state.
    assert fake.auth.jwt.login_calls == []
    assert fake.secrets.kv.v2.read_calls == []
    assert not route.called
    await connector.aclose()


@pytest.mark.asyncio
async def test_system_operator_credential_read_fails_closed_at_loader_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fail-closed guard holds at the credential-read boundary itself (AC3).

    This is the **isolation-robust** anchor for AC3 (#984 PR #998 CI fix).
    It exercises the guard at its true unit — the shared
    :func:`load_basic_credentials` and the harbor default loader
    :func:`load_credentials_from_vault` — with no connector instance, no
    ``_creds_cache``, and no respx route in scope. Because nothing
    cross-test (a leaked connector singleton, a populated credential
    cache, a stale HTTP route) participates in this path, a leak that
    short-circuits the handler-level test cannot satisfy this one: the
    empty-``raw_jwt`` operator must raise ``VaultCredentialsReadError``
    before Vault is touched, every time, on every worker. Mirrors the
    loader-boundary precedent in
    ``test_connectors_vcf_shared_auth.test_default_credentials_loader_rejects_empty_operator_jwt``.
    """
    fake = install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )
    system_operator = synthesise_system_operator()

    # The shared helper raises directly — the guard precedes any Vault call.
    with pytest.raises(VaultCredentialsReadError):
        await load_basic_credentials(_TARGET, system_operator)

    # The harbor default loader (what the live connector uses) delegates to
    # the helper, so it fails closed identically.
    with pytest.raises(VaultCredentialsReadError):
        await load_credentials_from_vault(_TARGET, system_operator)

    # Neither path logged in to Vault or read a secret — the guard ran
    # before the in-process fake was ever reached.
    assert fake.auth.jwt.login_calls == []
    assert fake.secrets.kv.v2.read_calls == []


# ---------------------------------------------------------------------------
# harbor.robot.delete — handler (real operator-context credential read)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_robot_delete_sends_delete_to_harbor_and_returns_synthetic_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """robot_delete() sends DELETE /api/v2.0/robots/{id} and
    returns {id, deleted: True} since Harbor returns HTTP 200 with empty body."""
    install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )
    connector = _make_connector()
    with respx.mock(assert_all_called=True) as mock:
        mock.delete("https://harbor.test.invalid/api/v2.0/robots/42").mock(
            return_value=respx.MockResponse(200)
        )
        result = await connector.robot_delete(
            _make_operator(),
            _TARGET,
            {"project": "myproject", "id": 42},
        )

    assert result == {"id": 42, "deleted": True}
    await connector.aclose()


@pytest.mark.asyncio
async def test_robot_delete_reads_credential_under_operator_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """robot_delete() forwards the dispatched operator's JWT to the Vault read."""
    fake = install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )
    connector = _make_connector()
    operator = _make_operator()

    with respx.mock() as mock:
        mock.delete("https://harbor.test.invalid/api/v2.0/robots/7").mock(
            return_value=respx.MockResponse(200)
        )
        await connector.robot_delete(operator, _TARGET, {"project": "proj", "id": 7})

    assert fake.auth.jwt.login_calls[-1]["jwt"] == operator.raw_jwt
    assert fake.secrets.kv.v2.read_calls[-1]["path"] == _TARGET.secret_ref
    await connector.aclose()


@pytest.mark.asyncio
async def test_robot_delete_sends_basic_auth_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """robot_delete() includes the Authorization: Basic header on the DELETE request."""
    install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )
    connector = _make_connector()
    captured_headers: dict[str, str] = {}

    def _capture(request: respx.models.Request) -> respx.MockResponse:
        captured_headers.update(dict(request.headers))
        return respx.MockResponse(200)

    with respx.mock() as mock:
        mock.delete("https://harbor.test.invalid/api/v2.0/robots/7").mock(side_effect=_capture)
        await connector.robot_delete(_make_operator(), _TARGET, {"project": "proj", "id": 7})

    assert "authorization" in captured_headers
    assert captured_headers["authorization"].startswith("Basic ")
    await connector.aclose()


@pytest.mark.asyncio
async def test_robot_delete_raises_on_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """robot_delete() propagates httpx.HTTPStatusError on 4xx/5xx responses."""
    install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )
    connector = _make_connector()
    with respx.mock() as mock:
        mock.delete("https://harbor.test.invalid/api/v2.0/robots/99").mock(
            return_value=respx.MockResponse(404)
        )
        with pytest.raises(httpx.HTTPStatusError):
            await connector.robot_delete(
                _make_operator(), _TARGET, {"project": "myproject", "id": 99}
            )
    await connector.aclose()


@pytest.mark.asyncio
async def test_robot_delete_system_operator_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A system operator (empty raw_jwt) cannot read the credential — fail closed.

    Isolation-robust (#984 PR #998 CI fix): same shape as the create twin —
    the read must raise AND the in-process Vault fake must never be touched,
    so a leaked successful read fails loudly here instead of passing
    vacuously under CI's ``pytest -n 6 --dist loadscope``.
    """
    fake = install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )
    connector = _make_connector()
    system_operator = synthesise_system_operator()
    with respx.mock(assert_all_called=False) as mock:
        route = mock.delete("https://harbor.test.invalid/api/v2.0/robots/7").mock(
            return_value=respx.MockResponse(200)
        )
        with pytest.raises(VaultCredentialsReadError):
            await connector.robot_delete(system_operator, _TARGET, {"project": "proj", "id": 7})
    assert fake.auth.jwt.login_calls == []
    assert fake.secrets.kv.v2.read_calls == []
    assert not route.called
    await connector.aclose()


# ---------------------------------------------------------------------------
# Dispatcher routing — the dispatched operator reaches the handlers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_typed_threads_operator_into_robot_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real dispatcher (``dispatch_typed``) threads the dispatched operator
    into ``robot_create`` (not the system operator), exercising the name-keyed
    operator routing the production dispatcher uses."""
    fake = install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )
    connector = _make_connector()
    operator = _make_operator()

    with respx.mock() as mock:
        mock.post("https://harbor.test.invalid/api/v2.0/robots").mock(
            return_value=respx.MockResponse(
                201, json={"id": 5, "name": "robot$p+n", "secret": "s", "expiration_time": -1}
            )
        )
        # The bound method carries ``operator`` in its signature, so the
        # real dispatcher routes the dispatched operator into it.
        result = await dispatch_typed(
            handler=connector.robot_create,
            operator=operator,
            target=_TARGET,
            params={"name": "n", "project": "p", "duration": -1},
        )

    assert result == {"id": 5, "name": "robot$p+n", "secret": "s"}
    # Proof the *dispatched* operator (not the empty-JWT system operator)
    # reached the credential read.
    assert fake.auth.jwt.login_calls[-1]["jwt"] == operator.raw_jwt
    await connector.aclose()


@pytest.mark.asyncio
async def test_dispatch_typed_threads_operator_into_robot_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real dispatcher (``dispatch_typed``) threads the dispatched operator
    into ``robot_delete`` (not the system operator)."""
    fake = install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )
    connector = _make_connector()
    operator = _make_operator()

    with respx.mock() as mock:
        mock.delete("https://harbor.test.invalid/api/v2.0/robots/9").mock(
            return_value=respx.MockResponse(200)
        )
        result = await dispatch_typed(
            handler=connector.robot_delete,
            operator=operator,
            target=_TARGET,
            params={"project": "p", "id": 9},
        )

    assert result == {"id": 9, "deleted": True}
    assert fake.auth.jwt.login_calls[-1]["jwt"] == operator.raw_jwt
    await connector.aclose()


# ---------------------------------------------------------------------------
# No credential / JWT leak in logs (matching the #941/#942 convention)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_robot_ops_never_leak_credential_or_jwt_in_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither the Vault-read credential nor the operator JWT appears in any
    captured log event produced by the robot ops."""
    install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )
    connector = _make_connector()
    operator = _make_operator()

    with capture_logs() as captured, respx.mock() as mock:
        mock.post("https://harbor.test.invalid/api/v2.0/robots").mock(
            return_value=respx.MockResponse(
                201,
                json={
                    "id": 1,
                    "name": "robot$p+n",
                    "secret": "minted-secret-value",
                    "expiration_time": -1,
                },
            )
        )
        mock.delete("https://harbor.test.invalid/api/v2.0/robots/1").mock(
            return_value=respx.MockResponse(200)
        )
        create_result = await connector.robot_create(
            operator, _TARGET, {"name": "n", "project": "p", "duration": -1}
        )
        delete_result = await connector.robot_delete(operator, _TARGET, {"project": "p", "id": 1})

    log_blob = repr(captured)
    assert _CANARY_USERNAME not in log_blob
    assert _CANARY_PASSWORD not in log_blob
    assert operator.raw_jwt not in log_blob
    # The minted secret rides the handler's return value (the caller needs
    # it) but must never enter a log event.
    assert create_result["secret"] == "minted-secret-value"
    assert "minted-secret-value" not in log_blob
    assert delete_result == {"id": 1, "deleted": True}
    await connector.aclose()


# ---------------------------------------------------------------------------
# G6 classifier — classify_op
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op_id, expected",
    [
        ("harbor.robot.create", "credential_mint"),
        ("harbor.robot.delete", "write"),
        # No regression to credential_read
        ("vault.kv.read", "credential_read"),
        ("vault.kv.list", "credential_read"),
        # Suffix-based write ops still work
        ("vsphere.vm.create", "write"),
        ("vsphere.vm.delete", "write"),
    ],
)
def test_classify_op_maps_harbor_robot_ops_correctly(op_id: str, expected: str) -> None:
    """classify_op returns credential_mint for harbor.robot.create, write for delete."""
    assert classify_op(op_id) == expected


def test_classify_op_credential_mint_precedes_write_suffix() -> None:
    """harbor.robot.create ends with .create but must classify as credential_mint,
    not write. Verifies the allowlist check precedes the suffix check."""
    assert classify_op("harbor.robot.create") == "credential_mint"
    # A hypothetical non-minting create still falls through to write
    assert classify_op("vsphere.vm.create") == "write"


# ---------------------------------------------------------------------------
# G6 redaction — redact_payload for credential_mint
# ---------------------------------------------------------------------------


def test_redact_payload_credential_mint_removes_secret_field() -> None:
    """redact_payload('credential_mint', ...) returns aggregate shape — no secret."""
    raw = {"id": 42, "name": "robot$proj+bot", "secret": "minted-secret", "other": "keep"}
    result = redact_payload("credential_mint", raw, "ok")

    assert "secret" not in str(result)
    assert "minted-secret" not in str(result)
    assert result["op_class"] == "credential_mint"
    assert result["result_status"] == "ok"


def test_redact_payload_credential_mint_preserves_only_aggregate_fields() -> None:
    """credential_mint aggregate payload is exactly {op_class, result_status}."""
    raw = {"id": 1, "name": "robot$p+n", "secret": "S3cr3t!"}
    result = redact_payload("credential_mint", raw, "ok")

    assert set(result.keys()) == {"op_class", "result_status"}


def test_redact_payload_credential_mint_no_regression_on_credential_read() -> None:
    """credential_read still produces aggregate payload after the credential_mint
    extension — no regression to full-detail broadcast."""
    raw = {"data": {"db_password": "super-secret"}, "version": 3}
    result = redact_payload("credential_read", raw, "ok")

    assert "db_password" not in str(result)
    assert result["op_class"] == "credential_read"
    assert set(result.keys()) == {"op_class", "result_status"}


def test_redact_payload_credential_mint_aggregate_on_error_result() -> None:
    """credential_mint aggregate applies regardless of result_status value."""
    raw = {"secret": "leaked-if-full", "error": "timeout"}
    result = redact_payload("credential_mint", raw, "error")

    assert "leaked-if-full" not in str(result)
    assert result["result_status"] == "error"


def test_redact_payload_write_class_broadcasts_full_detail() -> None:
    """harbor.robot.delete is write-classified — full detail (no secret in payload
    anyway, but the redaction path is full, not aggregate)."""
    raw = {"id": 42, "deleted": True}
    result = redact_payload("write", raw, "ok")

    assert result == {"op_class": "write", "params": raw, "result_status": "ok"}


def test_redact_payload_credential_mint_with_explicit_aggregate_detail() -> None:
    """When detail='aggregate' is passed explicitly, credential_mint stays aggregate."""
    raw = {"secret": "s", "id": 1}
    result = redact_payload("credential_mint", raw, "ok", detail="aggregate")

    assert "secret" not in str(result)
    assert set(result.keys()) == {"op_class", "result_status"}


def test_redact_payload_credential_mint_with_explicit_full_detail() -> None:
    """When detail='full' is passed (G6.3 operator opt-in), credential_mint broadcasts
    the full payload. The G6.3 resolver is responsible for deciding this — the
    redact_payload function just renders the decided detail level."""
    raw = {"secret": "s", "id": 1, "name": "robot$p+n"}
    result = redact_payload("credential_mint", raw, "ok", detail="full")

    assert result == {"op_class": "credential_mint", "params": raw, "result_status": "ok"}
