# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.settings`.

The validator under test is the async-DSN guard on
:attr:`Settings.database_url` (T27 review iteration 1, M2): ADR 0004
mandates that every database I/O path off the request hot loop is
``await``-able, so a sync DSN like ``postgresql://`` or ``sqlite:///``
must fail at :class:`Settings` construction time rather than silently
landing as the engine URL and blocking the FastAPI event loop on the
first DB checkout.

The tests below cover both halves of the contract:

* Accepted async schemes (``postgresql+asyncpg://`` and
  ``sqlite+aiosqlite://``) construct successfully.
* Sync schemes raise :class:`ValueError` with the supported-schemes
  message (so the operator can fix ``DATABASE_URL`` from the error
  text alone).

A second batch (#964 M2) covers env-var hygiene at ingest for the
three Vault-rendered UI knobs: trailing-newline values must be
stripped by :func:`get_settings` so downstream Fernet /
``client_secret_basic`` consumers see the operator's intended value.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from meho_backplane.settings import Settings, get_settings


def _settings_kwargs(database_url: str) -> dict[str, object]:
    """Return the minimum kwargs needed to construct :class:`Settings`.

    Only ``database_url`` varies across tests; the rest are fixed at
    valid sentinels so the construction failure (when it happens) is
    unambiguously the validator under test.
    """
    return {
        "keycloak_issuer_url": "https://keycloak.test/realms/meho",
        "keycloak_audience": "meho-backplane",
        "vault_addr": "https://vault.test",
        "database_url": database_url,
    }


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://meho:secret@db.test:5432/meho",
        "sqlite+aiosqlite:///:memory:",
        "sqlite+aiosqlite:///tmp/test.db",
    ],
)
def test_database_url_accepts_async_drivers(url: str) -> None:
    """Both supported async schemes construct without raising."""
    settings = Settings(**_settings_kwargs(url))  # type: ignore[arg-type]
    assert settings.database_url == url


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://meho:secret@db.test:5432/meho",
        "postgresql+psycopg2://meho:secret@db.test:5432/meho",
        "postgres://meho:secret@db.test:5432/meho",
        "sqlite:///:memory:",
        "mysql://meho:secret@db.test/meho",
    ],
)
def test_database_url_rejects_sync_or_unsupported_drivers(url: str) -> None:
    """Sync DSNs and unsupported schemes raise with the supported-schemes message.

    The error message must name the supported schemes so the operator
    can fix ``DATABASE_URL`` from the error text alone — without
    grepping the codebase or hunting for ADR 0004. The assertion below
    pins both supported scheme prefixes literally.
    """
    with pytest.raises(ValidationError) as exc_info:
        Settings(**_settings_kwargs(url))  # type: ignore[arg-type]
    message = str(exc_info.value)
    assert "postgresql+asyncpg://" in message
    assert "sqlite+aiosqlite://" in message


# ---------------------------------------------------------------------------
# #964 M2 — Vault-rendered env vars stripped at ingest
# ---------------------------------------------------------------------------


def test_get_settings_strips_trailing_newline_from_ui_env_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trailing-newline env-var values are stripped by :func:`get_settings`.

    Regression for #964 M2. Vault-rendering chains (Vault Agent
    templates, ``vault kv get -field`` default mode) commonly emit
    secret values with a trailing ``\\n``. The non-empty check at the
    consumer site passes, but downstream surfaces reject the padded
    value:

    * ``client_secret_basic`` token-endpoint auth -- Keycloak returns
      ``invalid_client`` on a credential with a trailing newline.
    * ``Fernet(key.encode("ascii"))`` -- raises ``binascii.Error`` on
      the padded base64 string.

    Stripping at ingest in ``get_settings`` is the single seam every
    consumer flows through, so the downstream errors above become
    unreachable from Vault-shaped misconfig.
    """
    fernet_key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    # Trailing newline on each of the three UI knobs the task scopes.
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", "meho-web\n")
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_SECRET", "s3cr3t\n")
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", f"{fernet_key}\n")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.ui_keycloak_client_id == "meho-web"
        assert settings.ui_keycloak_client_secret == "s3cr3t"
        assert settings.ui_session_encryption_key == fernet_key
        # Fernet construction round-trip proves the stripped key is the
        # 32-byte URL-safe base64 shape ``Fernet`` accepts -- the failure
        # mode this guard prevents in production.
        Fernet(settings.ui_session_encryption_key.encode("ascii"))
    finally:
        get_settings.cache_clear()


def test_get_settings_preserves_empty_string_default_for_unset_ui_env_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Strip is a no-op for unset env vars; the empty-string default flows through.

    Sanity check: the strip change must not regress the unset-default
    path. An empty default ``""`` stays ``""`` after ``.strip()``;
    the downstream ``_ensure_client_configured`` guard in
    :mod:`meho_backplane.ui.auth.flow` (and the BFF route 503 mapping)
    continues to fire on the genuine "operator never rendered the
    credentials" case.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.delenv("UI_KEYCLOAK_CLIENT_ID", raising=False)
    monkeypatch.delenv("UI_KEYCLOAK_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("UI_SESSION_ENCRYPTION_KEY", raising=False)
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.ui_keycloak_client_id == ""
        assert settings.ui_keycloak_client_secret == ""
        assert settings.ui_session_encryption_key == ""
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# G8.2-T2 (#1010) — MCP_REQUIRE_SESSION_ID env knob
# ---------------------------------------------------------------------------


def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the chassis-required env vars so ``get_settings()`` constructs."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")


def test_mcp_require_session_id_defaults_false_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset ``MCP_REQUIRE_SESSION_ID`` → ``False`` (sessions optional)."""
    _base_env(monkeypatch)
    monkeypatch.delenv("MCP_REQUIRE_SESSION_ID", raising=False)
    get_settings.cache_clear()
    try:
        assert get_settings().mcp_require_session_id is False
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "TRUE", "On"])
def test_mcp_require_session_id_truthy_spellings(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    """Canonical truthy spellings flip the knob to ``True`` via ``parse_bool_env``."""
    _base_env(monkeypatch)
    monkeypatch.setenv("MCP_REQUIRE_SESSION_ID", raw)
    get_settings.cache_clear()
    try:
        assert get_settings().mcp_require_session_id is True
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "disabled", ""])
def test_mcp_require_session_id_non_truthy_stays_false(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    """Anything outside the accept-list (incl. typos / empty) stays ``False``."""
    _base_env(monkeypatch)
    monkeypatch.setenv("MCP_REQUIRE_SESSION_ID", raw)
    get_settings.cache_clear()
    try:
        assert get_settings().mcp_require_session_id is False
    finally:
        get_settings.cache_clear()


def test_result_handle_max_spill_rows_defaults_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset ``RESULT_HANDLE_MAX_SPILL_ROWS`` → the 10000 field default."""
    _base_env(monkeypatch)
    monkeypatch.delenv("RESULT_HANDLE_MAX_SPILL_ROWS", raising=False)
    get_settings.cache_clear()
    try:
        assert get_settings().result_handle_max_spill_rows == 10000
    finally:
        get_settings.cache_clear()


def test_result_handle_max_spill_rows_env_override_takes_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``RESULT_HANDLE_MAX_SPILL_ROWS`` flows through ``get_settings()``.

    Regression guard for the wiring gap where the field was declared on
    :class:`Settings` but never read from the environment, so the
    documented operator override was a silent no-op and the 10000 default
    always won.
    """
    _base_env(monkeypatch)
    monkeypatch.setenv("RESULT_HANDLE_MAX_SPILL_ROWS", "250000")
    get_settings.cache_clear()
    try:
        assert get_settings().result_handle_max_spill_rows == 250000
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# G11.5-T6 #1080 — AGENT_RUNS_DISABLED_TENANTS UUID validation
# ---------------------------------------------------------------------------


# Whitespace tolerated; trailing comma -> empty chunk skipped; doubled
# commas tolerated (the gate parser already skips empties so the
# validator's accept-set matches it exactly).
_UUID_A = "11111111-1111-1111-1111-111111111111"
_UUID_B = "22222222-2222-2222-2222-222222222222"


@pytest.mark.parametrize(
    "raw",
    [
        "",  # default / "no tenants disabled"
        _UUID_A,
        f"{_UUID_A},{_UUID_B}",
        f"  {_UUID_A}  ,  {_UUID_B}  ",
        f"{_UUID_A},",
        f",,{_UUID_A},,",
    ],
)
def test_agent_runs_disabled_tenants_accepts_uuid_csv(raw: str) -> None:
    """Valid CSVs of UUIDs (including the empty default) construct without raising.

    CR iter-3 B1 (G11.5-T6 #1080) — fail-fast at startup when the
    kill-switch list is malformed, but tolerate the empty value (the
    documented default) and benign whitespace / separator artefacts so
    the validator's accept-set is exactly the gate parser's accept-set
    in :func:`~meho_backplane.operations.budget_enforcement.evaluate_pre_run_budget`.
    """
    settings = Settings(
        **_settings_kwargs("sqlite+aiosqlite:///:memory:"),  # type: ignore[arg-type]
        agent_runs_disabled_tenants=raw,
    )
    assert settings.agent_runs_disabled_tenants == raw


@pytest.mark.parametrize(
    "raw",
    [
        "not-a-uuid",
        "11111111-1111-1111-1111-111111111111,nope",
        "abc123",
        "11111111-1111-1111-1111-11111111111",  # one digit short
    ],
)
def test_agent_runs_disabled_tenants_rejects_non_uuid(raw: str) -> None:
    """Any non-UUID entry fails :class:`Settings` construction with a clear message.

    CR iter-3 B1 — a malformed UUID in ``AGENT_RUNS_DISABLED_TENANTS``
    would otherwise survive startup and silently match nothing at
    gate-evaluation time, leaving the deploy thinking it's
    kill-switched when it isn't. Eager validation at construction time
    surfaces the typo with the offending entry quoted in the error
    message so the operator can fix the env var from the failure text
    alone.
    """
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            **_settings_kwargs("sqlite+aiosqlite:///:memory:"),  # type: ignore[arg-type]
            agent_runs_disabled_tenants=raw,
        )
    assert "AGENT_RUNS_DISABLED_TENANTS" in str(exc_info.value)


# ---------------------------------------------------------------------------
# #1725 M2 — vault_kv_tenant_scope_prefix template validation at construction
# ---------------------------------------------------------------------------


def test_vault_tenant_scope_prefix_default_is_valid() -> None:
    """The shipped default-on prefix constructs without raising.

    The mount-pinned ``secret/tenants/{tenant_id}/`` default carries the
    required ``{tenant_id}`` placeholder and no other, so it passes the
    construction-time template check.
    """
    settings = Settings(**_settings_kwargs("sqlite+aiosqlite:///:memory:"))  # type: ignore[arg-type]
    assert settings.vault_kv_tenant_scope_prefix == "secret/tenants/{tenant_id}/"


@pytest.mark.parametrize(
    "raw",
    [
        "",  # explicit-disable sentinel
        "   ",  # whitespace-only → disabled
        "secret/tenants/{tenant_id}/",  # the default
        "kv-prod/{tenant_id}/",  # custom mount, single placeholder
        "{tenant_id}/",  # path-only single placeholder
    ],
)
def test_vault_tenant_scope_prefix_accepts_valid_templates(raw: str) -> None:
    """The empty sentinel and any clean single-``{tenant_id}`` template construct."""
    settings = Settings(
        **_settings_kwargs("sqlite+aiosqlite:///:memory:"),  # type: ignore[arg-type]
        vault_kv_tenant_scope_prefix=raw,
    )
    assert settings.vault_kv_tenant_scope_prefix == raw


@pytest.mark.parametrize(
    "raw",
    [
        "secret/tenants/",  # missing the {tenant_id} placeholder
        "secret/tenants/{tenant_id",  # unbalanced brace
        "secret/tenants/{0}/",  # positional placeholder, not named
        "secret/tenants/{tenant_id}/{region}/",  # extra placeholder
        "secret/{sub}/{tenant_id}/",  # extra named placeholder
    ],
)
def test_vault_tenant_scope_prefix_rejects_malformed_templates(raw: str) -> None:
    """A non-empty prefix that isn't a clean ``{tenant_id}`` template fails construction.

    #1725 M2 — a malformed override would otherwise fail at first
    ``vault.kv.*`` call (and a placeholder-less one silently collapses
    every operator to one shared namespace). Eager validation at
    construction surfaces the misconfig at pod startup with the offending
    value quoted.
    """
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            **_settings_kwargs("sqlite+aiosqlite:///:memory:"),  # type: ignore[arg-type]
            vault_kv_tenant_scope_prefix=raw,
        )
    assert "VAULT_KV_TENANT_SCOPE_PREFIX" in str(exc_info.value)


# ---------------------------------------------------------------------------
# #2229 (Initiative #2227) — CREDENTIAL_BACKEND env knob
# ---------------------------------------------------------------------------


def test_credential_backend_defaults_to_vault_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset ``CREDENTIAL_BACKEND`` → ``vault`` (zero migration for existing installs)."""
    _base_env(monkeypatch)
    monkeypatch.delenv("CREDENTIAL_BACKEND", raising=False)
    get_settings.cache_clear()
    try:
        assert get_settings().credential_backend == "vault"
    finally:
        get_settings.cache_clear()


def test_credential_backend_reads_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A set ``CREDENTIAL_BACKEND`` names the schemeless default backend."""
    _base_env(monkeypatch)
    monkeypatch.setenv("CREDENTIAL_BACKEND", "gsm")
    get_settings.cache_clear()
    try:
        assert get_settings().credential_backend == "gsm"
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("raw", ["", "   ", "\n"])
def test_credential_backend_blank_env_falls_back_to_vault(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    """A blank / whitespace-only value falls back to ``vault``, never an empty kind.

    A blank chart value must not yield an unknown ("") backend kind — the
    ``min_length=1`` field would reject it and every credential read would
    fail. The env loader coerces blank to the ``vault`` default.
    """
    _base_env(monkeypatch)
    monkeypatch.setenv("CREDENTIAL_BACKEND", raw)
    get_settings.cache_clear()
    try:
        assert get_settings().credential_backend == "vault"
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# #2230 (Initiative #2227) — GSM_IMPERSONATE_SA env knob
# ---------------------------------------------------------------------------


def test_gsm_impersonate_sa_defaults_to_empty_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset ``GSM_IMPERSONATE_SA`` → empty (direct-ADC read, no impersonation)."""
    _base_env(monkeypatch)
    monkeypatch.delenv("GSM_IMPERSONATE_SA", raising=False)
    get_settings.cache_clear()
    try:
        assert get_settings().gsm_impersonate_sa == ""
    finally:
        get_settings.cache_clear()


def test_gsm_impersonate_sa_reads_and_strips_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A set ``GSM_IMPERSONATE_SA`` names the SA to impersonate; whitespace is stripped."""
    _base_env(monkeypatch)
    monkeypatch.setenv("GSM_IMPERSONATE_SA", "  reader@proj.iam.gserviceaccount.com \n")
    get_settings.cache_clear()
    try:
        assert get_settings().gsm_impersonate_sa == "reader@proj.iam.gserviceaccount.com"
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# #2232 (Initiative #2227) — GSM_WIF_* Workload Identity Federation knobs
# ---------------------------------------------------------------------------


def test_gsm_wif_defaults_are_empty_or_oidc_jwt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset GSM_WIF_* ⇒ empty audience/pool/provider/SA (SA-direct path) and
    the OIDC-JWT subject-token type default."""
    _base_env(monkeypatch)
    for key in (
        "GSM_WIF_AUDIENCE",
        "GSM_WIF_POOL_ID",
        "GSM_WIF_PROVIDER_ID",
        "GSM_WIF_SERVICE_ACCOUNT",
        "GSM_WIF_SUBJECT_TOKEN_TYPE",
    ):
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.gsm_wif_audience == ""
        assert settings.gsm_wif_pool_id == ""
        assert settings.gsm_wif_provider_id == ""
        assert settings.gsm_wif_service_account == ""
        assert settings.gsm_wif_subject_token_type == "urn:ietf:params:oauth:token-type:jwt"
    finally:
        get_settings.cache_clear()


def test_gsm_wif_reads_and_strips_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Set GSM_WIF_* values are read and surrounding whitespace stripped."""
    _base_env(monkeypatch)
    monkeypatch.setenv(
        "GSM_WIF_AUDIENCE",
        "  //iam.googleapis.com/projects/123/locations/global/"
        "workloadIdentityPools/meho-pool/providers/keycloak \n",
    )
    monkeypatch.setenv("GSM_WIF_POOL_ID", " meho-pool ")
    monkeypatch.setenv("GSM_WIF_PROVIDER_ID", " keycloak ")
    monkeypatch.setenv("GSM_WIF_SERVICE_ACCOUNT", " reader@proj.iam.gserviceaccount.com ")
    monkeypatch.setenv("GSM_WIF_SUBJECT_TOKEN_TYPE", " urn:ietf:params:oauth:token-type:id_token ")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.gsm_wif_audience == (
            "//iam.googleapis.com/projects/123/locations/global/"
            "workloadIdentityPools/meho-pool/providers/keycloak"
        )
        assert settings.gsm_wif_pool_id == "meho-pool"
        assert settings.gsm_wif_provider_id == "keycloak"
        assert settings.gsm_wif_service_account == "reader@proj.iam.gserviceaccount.com"
        assert settings.gsm_wif_subject_token_type == "urn:ietf:params:oauth:token-type:id_token"
    finally:
        get_settings.cache_clear()


def test_gsm_wif_subject_token_type_blank_falls_back_to_jwt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blank GSM_WIF_SUBJECT_TOKEN_TYPE falls back to the OIDC-JWT type
    rather than an empty (min_length=1 rejected) value."""
    _base_env(monkeypatch)
    monkeypatch.setenv("GSM_WIF_SUBJECT_TOKEN_TYPE", "   ")
    get_settings.cache_clear()
    try:
        assert get_settings().gsm_wif_subject_token_type == "urn:ietf:params:oauth:token-type:jwt"
    finally:
        get_settings.cache_clear()
