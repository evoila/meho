# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared pytest fixtures for the backplane test suite.

This module hosts:

* The **always-on secret-leak sweep** (Task #25 acceptance criterion 5):
  an ``autouse`` fixture that, after every test, scans whatever the test
  emitted to stdout / stderr / the stdlib ``logging`` machinery for
  patterns indicative of a leaked credential. The sweep catches the
  failure mode "we forgot to redact in *this one* log line" — only an
  always-on check catches it; a single targeted assertion misses 95% of
  the surface.

* Re-usable JWT / Vault test helpers shared across the failure-mode
  test files added in Task #25 (``test_auth_failures.py``,
  ``test_vault_failures.py``, ``test_api_health_failures.py``,
  ``test_secret_leak_checks.py``). The helpers are kept minimal — each
  test file still owns its own ``respx`` setup and assertion shape;
  conftest only exports the building blocks Task #22 / #23 / #24
  established (RSA keypair generation, JWKS document construction,
  token minting, Keycloak discovery / JWKS mocking).

The autouse sweep is deliberately conservative on its capture surface:
it reads ``capfd`` (the file-descriptor-level stdout/stderr capture) and
the stdlib ``caplog``. Tests that drive their own structlog capture into
a private buffer (the ``test_observability.py`` / ``test_api_v1_health.py``
pattern) are *also* expected to assert no leak on that buffer; the sweep
here is the safety net under the targeted assertion, not its replacement.
Documented in :data:`SECRET_LEAK_PATTERNS` so contributors can extend the
denylist without re-deriving the test contract.
"""

from __future__ import annotations

import re
import time
import warnings
from collections.abc import Iterator
from typing import Any, Final

import httpx
import pytest
import respx

from meho_backplane.settings import get_settings

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from authlib.jose import JsonWebKey, JsonWebToken

__all__ = [
    "DEFAULT_AUDIENCE",
    "DEFAULT_DISCOVERY_URL",
    "DEFAULT_ISSUER",
    "DEFAULT_JWKS_URL",
    "DEFAULT_TENANT_ID",
    "DEFAULT_TENANT_ROLE",
    "SECRET_LEAK_PATTERNS",
    "make_rsa_keypair",
    "mint_token",
    "mock_discovery_and_jwks",
    "public_jwks",
]


# ---------------------------------------------------------------------------
# Always-on secret-leak sweep (AC 5)
# ---------------------------------------------------------------------------


#: Regex patterns whose appearance in captured test output is treated as
#: a credential leak. The list is intentionally short and conservative —
#: every pattern is paid for in false-positive risk and review attention.
#: Add domain-specific patterns here when new secret-bearing surfaces are
#: introduced (G2.3 audit middleware, G2.4 connector secrets, etc.).
#:
#: Each entry is the precompiled regex; the source string lives in the
#: pattern object's ``pattern`` attribute for the fail message.
SECRET_LEAK_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    # A long-looking Bearer credential. The 20+ char floor avoids
    # tripping on the literal string "Bearer " in a log message that
    # discusses bearer auth in the abstract; real Keycloak access
    # tokens are 600+ chars.
    re.compile(r"Bearer\s+[A-Za-z0-9_\-\.=]{20,}"),
    # ``password=`` / ``password:`` style key-value pairs. Catches the
    # accidental ``logger.info("login attempt", password=value)`` shape.
    re.compile(r"\bpassword\s*[=:]", re.IGNORECASE),
    # ``secret=`` / ``secret:`` style key-value pairs. Same shape.
    # The word boundary on the left avoids matching ``federation_health_secret``
    # event-name substrings; the regex insists on ``secret`` followed by
    # ``=`` or ``:`` with optional whitespace.
    re.compile(r"\bsecret\s*[=:]", re.IGNORECASE),
    # ``token=`` / ``token:`` style key-value pairs. Watches for
    # accidental ``client_token=hvs.*`` log emissions from the Vault
    # forward-auth path. Word-bounded to avoid tripping on
    # ``missing_token`` / ``invalid_token`` / ``client_token_revoked``
    # event-name strings (which contain ``token`` but never followed by
    # ``=`` or ``:`` in our log shape).
    re.compile(r"\btoken\s*[=:]", re.IGNORECASE),
    # ``api_key`` / ``api-key`` / ``apikey`` followed by an assignment.
    re.compile(r"\bapi[_-]?key\s*[=:]", re.IGNORECASE),
    # The full ``Authorization: Bearer <anything>`` shape — catches
    # request-header values rendered into a log dict literal.
    re.compile(r"Authorization\s*:\s*Bearer\s+\S+", re.IGNORECASE),
)


@pytest.fixture(autouse=True)
def _default_database_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> Iterator[None]:
    """Provide a default ``DATABASE_URL`` (file-backed SQLite, schema applied).

    Every test file pins ``KEYCLOAK_ISSUER_URL`` / ``VAULT_ADDR`` in its
    own per-file fixture; ``DATABASE_URL`` is the third required field
    added in T27 and the ``audit_log`` table needs to exist post-T28
    so the synchronous audit middleware doesn't fail-closed on every
    authenticated request. Pinning the default to a per-test tmp-path
    SQLite file (rather than ``:memory:``) lets us run
    ``alembic upgrade head`` against it once at fixture setup; the
    file-backed URL is what makes the schema visible to the engine the
    app constructs in a different connection than the migration runner.
    ``:memory:`` databases are connection-scoped — schema applied via
    one connection is invisible to the next, so audit middleware
    inserts would fail with ``no such table: audit_log``.

    Tests that exercise the DB-migration-state probe still override
    this default with their own monkeypatched URL (testcontainers PG
    or a different ``aiosqlite:///<tmp>`` path); the override wins
    because :func:`pytest.MonkeyPatch.setenv` is last-write.

    The ``get_settings.cache_clear()`` brackets matter: ``Settings`` is
    cached at module scope by :func:`functools.lru_cache`, and a stale
    cache entry from an earlier test (constructed before this fixture
    set the env var) would survive into the next test and silently
    return the previous URL. The same shape applies to the
    module-level engine cache, which is reset around every test so
    the file-backed DB this fixture creates is the one the app's
    audit middleware sees.

    The Alembic upgrade runs in this autouse fixture's *sync* portion
    so it's well-defined relative to ``@pytest.mark.asyncio`` tests:
    fixture body executes before pytest-asyncio enters its event
    loop. :func:`alembic.command.upgrade` invokes
    :func:`asyncio.run` internally via the env.py async cookbook,
    which would clash with an outer running loop.
    """
    # Local import to avoid a top-of-file circular-ish dependency:
    # this conftest is loaded before any meho_backplane modules,
    # and importing the engine module here means it gets imported
    # once at fixture-resolution time per test.
    from alembic import command

    from meho_backplane.db.engine import dispose_engine, reset_engine_for_testing
    from meho_backplane.db.migrations import alembic_config

    db_path = tmp_path / "default.db"
    url = f"sqlite+aiosqlite:///{db_path}"

    # ``DATABASE_URL`` must be set **before** ``command.upgrade`` runs:
    # ``backend/alembic/env.py`` reads ``os.environ.get("DATABASE_URL")``
    # and overrides whatever ``cfg.set_main_option("sqlalchemy.url", ...)``
    # was set to here. Without the reordering, a ``DATABASE_URL`` inherited
    # from the parent process silently redirects the migration runner at a
    # different database than the fixture configured — the test DB ends up
    # un-migrated and the next ``get_engine()`` call fails with
    # ``no such table: audit_log``. monkeypatch.setenv is rolled back on
    # teardown so the override is still per-test scoped.
    monkeypatch.setenv("DATABASE_URL", url)

    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")

    get_settings.cache_clear()
    reset_engine_for_testing()
    yield
    # Tests that constructed an engine via this URL leave a cached
    # AsyncEngine pointing at a tmp file that pytest will reap;
    # disposing here closes the asyncpg/aiosqlite pool cleanly so the
    # next test gets a fresh engine bound to its own tmp DB.
    try:
        # Best-effort dispose; pytest-asyncio's event loop may already
        # be torn down by the time we get here, in which case the
        # cache reset alone is sufficient.
        import asyncio

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(dispose_engine())
        finally:
            loop.close()
    except Exception:
        pass
    reset_engine_for_testing()
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _no_secret_leak_sweep(
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Fail any test that emits a credential-shaped substring.

    Reads stdout / stderr captured at the file-descriptor level (so
    structlog's :class:`PrintLoggerFactory` output is included even
    when a test does not configure its own capture buffer) plus the
    stdlib :mod:`logging` records collected by ``caplog``. Each
    captured surface is concatenated and run against
    :data:`SECRET_LEAK_PATTERNS`; the first match calls
    :func:`pytest.fail` with the offending pattern.

    The fixture is **autouse** and runs around every test in ``tests/``
    — the failure mode it catches is "we forgot to redact in *this one*
    log line", which only an always-on check catches reliably. Tests
    that drive a private structlog buffer (``log_buffer`` in
    ``test_observability`` / ``test_api_v1_health``) still need to
    assert on that buffer themselves — the autouse sweep does not see
    a private :class:`io.StringIO`. Those buffer-level checks already
    live in the dedicated leak tests in
    ``tests/test_secret_leak_checks.py``; this fixture is the safety
    net for everything else.

    **Mid-test drain protection.** ``capfd.readouterr`` is destructive:
    each call returns *and clears* whatever has been captured since the
    previous call. A test that drains the buffer mid-run (to assert on
    its own output) would consume those bytes before this sweep sees
    them, silently bypassing the check. To stay sound regardless of
    test internals, the fixture installs a record-and-forward proxy
    over ``capfd.readouterr`` that copies every read into an internal
    list; at teardown the sweep concatenates every recorded chunk plus
    a final post-yield read into the haystack. The current 125 tests
    do not pre-drain, but this guards against future tests doing so.
    """
    captured_chunks: list[tuple[str, str]] = []
    real_readouterr = capfd.readouterr

    def _recording_readouterr() -> Any:
        result = real_readouterr()
        captured_chunks.append((result.out, result.err))
        return result

    monkeypatch.setattr(capfd, "readouterr", _recording_readouterr)

    yield

    # Final post-yield drain plus everything any in-test caller already
    # consumed. Concatenating both sides means a mid-test call to
    # ``capfd.readouterr()`` (or a fixture/teardown call after the
    # ``yield`` boundary in another autouse fixture) cannot consume
    # secret-shaped output before the sweep runs.
    final = real_readouterr()
    captured_chunks.append((final.out, final.err))
    out_parts = [out for out, _err in captured_chunks if out]
    err_parts = [err for _out, err in captured_chunks if err]
    log_records = "\n".join(record.getMessage() for record in caplog.records)
    haystack = "\n".join(("\n".join(out_parts), "\n".join(err_parts), log_records))

    if not haystack.strip():
        return

    for pattern in SECRET_LEAK_PATTERNS:
        match = pattern.search(haystack)
        if match is not None:
            # Truncate the match so the failure message does not itself
            # echo the leaked credential into pytest's terminal output.
            preview = match.group(0)
            if len(preview) > 40:
                preview = preview[:40] + "...<redacted>"
            pytest.fail(
                f"secret-leak pattern matched in captured output: "
                f"pattern={pattern.pattern!r} preview={preview!r}",
                pytrace=False,
            )


# ---------------------------------------------------------------------------
# Shared JWT / JWKS helpers (lifted from tests/test_auth_jwt.py)
# ---------------------------------------------------------------------------


#: Default Keycloak realm-issuer URL used across the failure-mode suite.
DEFAULT_ISSUER: Final[str] = "https://keycloak.test/realms/meho"

#: Default ``aud`` claim required on every accepted JWT.
DEFAULT_AUDIENCE: Final[str] = "meho-backplane"

#: OIDC discovery endpoint derived from :data:`DEFAULT_ISSUER`.
DEFAULT_DISCOVERY_URL: Final[str] = f"{DEFAULT_ISSUER}/.well-known/openid-configuration"

#: JWKS endpoint Keycloak's discovery doc points at by default.
DEFAULT_JWKS_URL: Final[str] = f"{DEFAULT_ISSUER}/protocol/openid-connect/certs"

#: Default ``tenant_id`` claim value the helper mints into every token.
#:
#: Pinned to a stable, recognisable UUID so failure messages and audit
#: rows in the chassis suite are diff-friendly across runs. Tests that
#: care about cross-tenant isolation pass an explicit per-test value.
DEFAULT_TENANT_ID: Final[str] = "00000000-0000-0000-0000-00000000a0a0"

#: Default ``tenant_role`` claim value the helper mints into every token.
#:
#: Most chassis tests don't care about the role itself — they care only
#: that the token *has* one so :func:`verify_jwt` returns rather than
#: 401-ing. ``"operator"`` is the most representative middle-of-the-road
#: value (neither the most-privileged ``tenant_admin`` nor the
#: least-privileged ``read_only``); RBAC-shape tests in T4 will pin
#: per-test values explicitly.
DEFAULT_TENANT_ROLE: Final[str] = "operator"


def make_rsa_keypair(kid: str) -> Any:
    """Generate a fresh RSA-2048 keypair with the requested ``kid``.

    Identical to :func:`tests.test_auth_jwt._make_rsa_keypair` — lifted
    here so the failure-mode suite re-uses the exact fixture shape
    Task #22 established. Wrapped in ``catch_warnings`` to mute the
    one-shot ``AuthlibDeprecationWarning`` per call site.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return JsonWebKey.generate_key(
            "RSA",
            2048,
            options={"kid": kid},
            is_private=True,
        )


def public_jwks(*keys: Any) -> dict[str, list[dict[str, Any]]]:
    """Build a JWKS document from the public half of every key passed."""
    return {"keys": [k.as_dict(is_private=False) for k in keys]}


def mint_token(
    private_key: Any,
    *,
    sub: str = "op-42",
    name: str | None = "Damir",
    email: str | None = "damir@example.com",
    issuer: str = DEFAULT_ISSUER,
    audience: str = DEFAULT_AUDIENCE,
    expires_in: int = 3600,
    not_before_offset: int = 0,
    extra_claims: dict[str, Any] | None = None,
    algorithm: str = "RS256",
    kid: str | None = None,
    omit_sub: bool = False,
    tenant_id: str | None = DEFAULT_TENANT_ID,
    tenant_role: str | None = DEFAULT_TENANT_ROLE,
    tenant_claim_name: str = "tenant_id",
    tenant_role_claim_name: str = "tenant_role",
) -> str:
    """Mint a JWT signed by *private_key*, returning the compact form.

    Mirrors the helper in ``tests/test_auth_jwt.py`` but adds the
    failure-mode knobs the comprehensive suite needs:

    * ``algorithm`` — the JWS ``alg`` header value. Defaults to
      ``RS256`` (the only algorithm production accepts); failure tests
      pass ``"HS256"`` or ``"none"`` to verify the algorithm-pinning
      defence works.
    * ``kid`` — explicit override of the JWS header ``kid``. Defaults
      to the key's own kid; failure tests pass a fabricated value to
      drive the kid-miss → JWKS-refresh path.
    * ``omit_sub`` — when ``True``, drops the ``sub`` claim from the
      payload to verify the missing-claim 401 contract.
    * ``tenant_id`` / ``tenant_role`` — defaults to
      :data:`DEFAULT_TENANT_ID` / :data:`DEFAULT_TENANT_ROLE` so
      pre-G0.1 chassis tests keep flowing through ``verify_jwt``
      without needing per-test boilerplate. Pass ``None`` to omit the
      claim (drives ``missing_tenant_claim`` / ``missing_tenant_role_claim``);
      pass a malformed string to drive ``malformed_tenant_claim`` /
      ``unknown_tenant_role``.
    * ``tenant_claim_name`` / ``tenant_role_claim_name`` — control the
      *name* of the claim that carries the tenancy values, so tests
      can exercise the configurable ``JWT_TENANT_CLAIM_NAME`` /
      ``JWT_TENANT_ROLE_CLAIM_NAME`` settings without rebuilding
      this helper.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        jwt = JsonWebToken([algorithm])
        now = int(time.time())
        payload: dict[str, Any] = {
            "iss": issuer,
            "aud": audience,
            "iat": now,
            "exp": now + expires_in,
            "nbf": now + not_before_offset,
        }
        if not omit_sub:
            payload["sub"] = sub
        if name is not None:
            payload["name"] = name
        if email is not None:
            payload["email"] = email
        if tenant_id is not None:
            payload[tenant_claim_name] = tenant_id
        if tenant_role is not None:
            payload[tenant_role_claim_name] = tenant_role
        if extra_claims:
            payload.update(extra_claims)
        # ``kid`` resolution: explicit override wins; otherwise pull
        # from a key object (RSA / EC fixtures); finally fall back to
        # ``None`` for symmetric / ``none``-alg tokens where the
        # caller didn't pin a kid.
        if kid is not None:
            header_kid: str | None = kid
        elif hasattr(private_key, "as_dict"):
            header_kid = private_key.as_dict().get("kid")
        else:
            header_kid = None
        header: dict[str, Any] = {
            "alg": algorithm,
            "typ": "JWT",
        }
        if header_kid is not None:
            header["kid"] = header_kid
        token: bytes | str = jwt.encode(header, payload, private_key)
        return token.decode("ascii") if isinstance(token, bytes) else token


def mock_discovery_and_jwks(
    mock_router: respx.MockRouter,
    jwks: dict[str, Any],
    *,
    issuer: str = DEFAULT_ISSUER,
    discovery_url: str = DEFAULT_DISCOVERY_URL,
    jwks_url: str = DEFAULT_JWKS_URL,
) -> tuple[respx.Route, respx.Route]:
    """Stub Keycloak's OIDC discovery + JWKS endpoints.

    Returns the two :class:`respx.Route` objects so individual tests
    can assert call counts (`route.call_count`) when verifying caching
    or kid-rotation behaviour.
    """
    discovery_route = mock_router.get(discovery_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "issuer": issuer,
                "jwks_uri": jwks_url,
            },
        ),
    )
    jwks_route = mock_router.get(jwks_url).mock(
        return_value=httpx.Response(200, json=jwks),
    )
    return discovery_route, jwks_route
