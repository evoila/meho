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

import os
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


@pytest.fixture(scope="session")
def _schema_template_db(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Build the migrated SQLite schema ONCE per xdist worker; tests copy it.

    :func:`_default_database_url` previously ran ``alembic upgrade head``
    against a fresh per-test SQLite file. That replays the full migration
    chain on every test — ~1.02s/test in CI x ~3.2k tests ~= ~9 min of
    cumulative DB-setup wall, the dominant remaining unit-job cost after the
    #771 / #786 / #799 work (the #793 "next target"). The schema is identical
    across every test, so we replay the chain *once per worker* into a
    template file and :func:`shutil.copyfile` it per test: a byte copy is
    ~1000x cheaper than a full Alembic replay, preserves the exact per-test
    fresh-file isolation, and copies the ``alembic_version`` stamp so tests
    still observe ``head``.

    Session scope + ``tmp_path_factory`` mirrors :func:`_shared_model_cache_dir`:
    one build per worker (N total, not per-test), each worker's template
    distinct so there is no cross-worker write race.

    Migration-state tests (``test_alembic_probe``, ``test_migration_compat``,
    ``test_migration_0011_*``) override ``DATABASE_URL`` with their own DB and
    run their own Alembic, so the template never reaches them.
    """
    from alembic import command

    from meho_backplane.db.migrations import alembic_config

    template = tmp_path_factory.mktemp("schema-template") / "template.db"
    url = f"sqlite+aiosqlite:///{template}"
    # ``backend/alembic/env.py`` reads ``DATABASE_URL``; set it for the
    # one-time build then restore the prior value so this session-scoped
    # fixture does not leak a URL into the per-test fixture below.
    prev = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        cfg = alembic_config()
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "head")
    finally:
        if prev is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev
    return str(template)


@pytest.fixture(autouse=True)
def _default_database_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    _schema_template_db: str,
) -> Iterator[None]:
    """Provide a default ``DATABASE_URL`` (file-backed SQLite, schema applied).

    Every test file pins ``KEYCLOAK_ISSUER_URL`` / ``VAULT_ADDR`` in its
    own per-file fixture; ``DATABASE_URL`` is the third required field
    added in T27 and the ``audit_log`` table needs to exist post-T28
    so the synchronous audit middleware doesn't fail-closed on every
    authenticated request. Pinning the default to a per-test tmp-path
    SQLite file (rather than ``:memory:``) lets us copy the per-worker
    migrated template (see :func:`_schema_template_db`) into it at fixture
    setup; the file-backed URL is what makes the schema visible to the engine
    the app constructs in a different connection than the migration runner.
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

    The Alembic replay now happens once per session in
    :func:`_schema_template_db` (outside any test's event loop); this
    fixture's per-test work is a plain :func:`shutil.copyfile`, so there is
    no ``asyncio.run``-vs-running-loop clash to sequence around.
    """
    # Local import to avoid a top-of-file circular-ish dependency:
    # this conftest is loaded before any meho_backplane modules,
    # and importing the engine module here means it gets imported
    # once at fixture-resolution time per test.
    import shutil

    from meho_backplane.db.engine import dispose_engine, reset_engine_for_testing

    db_path = tmp_path / "default.db"
    url = f"sqlite+aiosqlite:///{db_path}"

    # Copy the per-worker migrated template (#793 amortization — see
    # :func:`_schema_template_db`) instead of replaying the full Alembic chain
    # per test. The byte copy preserves the schema *and* the ``alembic_version``
    # stamp; per-test isolation is unchanged (still a fresh file per test).
    shutil.copyfile(_schema_template_db, db_path)

    # ``DATABASE_URL`` must be set so the engine the app constructs binds to
    # this per-test DB. ``monkeypatch.setenv`` is rolled back on teardown so the
    # override stays per-test scoped; migration-state tests that set their own
    # ``DATABASE_URL`` win via last-write.
    monkeypatch.setenv("DATABASE_URL", url)

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


@pytest.fixture(scope="session")
def _shared_model_cache_dir(tmp_path_factory: pytest.TempPathFactory) -> str:
    """A single fastembed cache dir reused by every test in the worker.

    The ONNX model is identical across all tests, so a read-only cache
    needs no per-test isolation. The previous per-test ``tmp_path`` dir
    forced fastembed to re-materialise the model into a fresh empty
    directory on *every* app-booting test — cheap solo (~3s) but
    catastrophic under ``pytest-xdist``: N workers each re-fetch
    per-test and saturate the HF Hub unauthenticated rate limit through
    the cluster's shared NAT egress, inflating per-test setup to ~22s
    (the #761 / #771 unit-job-wall finding). Session scope means the
    model materialises once per worker (N fetches total, not per-test),
    and ``tmp_path_factory`` keeps each worker's dir distinct so there
    is no cross-worker write race on first fetch (the partial/
    symlink-broken-cache failure mode of evoila/meho#574).
    """
    return str(tmp_path_factory.mktemp("fastembed-cache-shared"))


@pytest.fixture(autouse=True)
def _default_retrieval_model_cache_dir(
    monkeypatch: pytest.MonkeyPatch,
    _shared_model_cache_dir: str,
) -> Iterator[None]:
    """Redirect fastembed's ONNX model cache to a writable shared dir.

    The dir is session-scoped (see :func:`_shared_model_cache_dir`) so
    the model materialises once per worker rather than once per test.

    The production default at :attr:`Settings.retrieval_model_cache_dir`
    is ``/opt/meho/model-cache`` — the image layer the default model is
    baked into at build time (evoila/meho#574). That path does not
    exist (and its parent is not writable) on macOS dev sandboxes or on
    the gha-runner-scale-set CI sandbox, so a test that constructs
    :class:`Settings` without this override would hit the same
    ``PermissionError`` the old ``/var/cache/fastembed`` default caused
    (every recent merged PR on main once showed 3 FAILURE statuses in
    ``statusCheckRollup`` for exactly this reason; see Task #472).

    Failure surface: the FastAPI lifespan in
    :mod:`meho_backplane.main` calls
    :func:`run_typed_op_registrars` post-G0.6-T-Refactor-Vault/K8s
    (#461 / #463); each shipped registrar goes through
    :func:`register_typed_operation` which computes
    :attr:`EndpointDescriptor.embedding` for every brand-new descriptor
    row. The :class:`~meho_backplane.retrieval.embedding.EmbeddingService`
    resolves its ``cache_dir`` from ``settings.retrieval_model_cache_dir``;
    its lazy ``_ensure_loaded`` hits ``os.makedirs(cache_dir)`` on
    first embed and raises ``PermissionError: [Errno 13] Permission
    denied: '/var/cache'`` against a read-only parent.

    Tests touched by this failure surface: the entire
    ``test_mcp_*`` family that boots the FastAPI app via
    :class:`TestClient`, plus a handful of middleware /
    auth_config tests that exercise the lifespan transitively. Setting
    the env var here, before any test imports
    :func:`get_settings`, ensures every settings construction in the
    test process resolves to the writable session-shared dir.

    Why a sibling fixture instead of merging into
    :func:`_default_database_url`: the responsibilities are genuinely
    distinct (one pins the DB + pre-migrates it, one pins the model
    cache); a future refactor that fully stubs out
    :class:`EmbeddingService` for tests via the existing
    :func:`run_typed_op_registrars` test seam can delete this fixture
    cleanly without disturbing the DB-bootstrap flow.

    The ``get_settings.cache_clear()`` brackets mirror the existing
    autouse pattern — without them a stale cached ``Settings`` from
    an earlier test would survive into the next test and silently
    return the previous cache_dir value.
    """
    monkeypatch.setenv("RETRIEVAL_MODEL_CACHE_DIR", _shared_model_cache_dir)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _stub_descriptor_embedding(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> Iterator[None]:
    """Stub default-path descriptor embedding in the per-test lifespan boot (#771).

    Every test that constructs ``TestClient(meho_backplane.main.app)``
    (the whole ``test_mcp_*`` family, plus middleware / auth_config
    tests that exercise the lifespan) runs ``run_typed_op_registrars``
    against the fresh per-test DB, which re-embeds every typed-op
    descriptor through the real fastembed model. That encode is the
    dominant unit-job cost — ~5.5 s locally and 35-47 s on CI per
    app-booting test (#771; confirmed via CI-side ``--durations`` +
    cold-boot profiling). Setting ``MEHO_TEST_STUB_DESCRIPTOR_EMBEDDING``
    makes :func:`encode_endpoint_text` return a zero vector on the
    default (no explicit service) path, so registration is near-instant.
    Descriptor rows still INSERT with a valid-shaped embedding column;
    tool-registration / RBAC / schema tests never read the vector.

    Scope is the *default path only* — callers that pass an explicit
    ``embedding_service`` (the deterministic-embedding test seam, e.g.
    :mod:`tests.test_operations_meta_tools`) are unaffected, so they keep
    asserting ranking against their own seeded vectors. A test that
    relies on *lifespan-registered* descriptor embeddings for ranking
    requests :func:`real_descriptor_embeddings` to opt out.
    """
    if "real_descriptor_embeddings" in request.fixturenames:
        # The opt-out fixture owns the env var for this test.
        yield
        return
    monkeypatch.setenv("MEHO_TEST_STUB_DESCRIPTOR_EMBEDDING", "1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def real_descriptor_embeddings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Opt out of the #771 default-path descriptor-embedding stub.

    For the rare test that asserts operation semantic-search ranking
    over *lifespan-registered* descriptors (rather than its own
    explicit-service-seeded corpus). Clears
    ``MEHO_TEST_STUB_DESCRIPTOR_EMBEDDING`` so registration computes the
    real fastembed embedding for this test.
    """
    monkeypatch.delenv("MEHO_TEST_STUB_DESCRIPTOR_EMBEDDING", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _amortize_typed_op_registrars(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> Iterator[None]:
    """Amortize the per-test app-boot ``run_typed_op_registrars`` cost (#901).

    Every app-booting test (the whole ``test_mcp_*`` family, plus
    middleware / auth_config tests that exercise the lifespan via
    ``TestClient``) re-runs every connector's typed-op registrar against
    the fresh per-test DB: ~90 ops x (group resolve + natural-key lookup
    + INSERT + flush). The work is identical on every boot (deterministic
    op set; embeddings already stubbed to a zero vector by
    :func:`_stub_descriptor_embedding`), so it amortizes cleanly. Setting
    ``MEHO_TEST_AMORTIZE_TYPED_OP_REGISTRARS`` makes
    :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
    snapshot the built-in descriptor + group rows on the **first** boot in
    the worker, then replay that snapshot as two bulk inserts on every
    subsequent boot — ~105 ms -> ~1 ms per boot (the #901 measurement),
    the same replay-a-once-computed-artifact shape as the schema-template
    amortization above (#793/#898).

    Gated off when ``real_descriptor_embeddings`` is requested: that
    opt-out wants the registrar to compute genuine vectors, so it must
    take the real (un-amortized, un-stubbed) path.

    The snapshot deliberately **persists across tests** in the worker —
    that cross-boot reuse is the whole amortization. It stays correct
    because the built-in op corpus is fixed in source (no test mutates a
    built-in descriptor's body), and the runner fingerprints the
    registrar set so a boot whose registrar list differs re-captures
    rather than replaying a stale corpus.
    """
    if "real_descriptor_embeddings" in request.fixturenames:
        yield
        return
    monkeypatch.setenv("MEHO_TEST_AMORTIZE_TYPED_OP_REGISTRARS", "1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _default_backplane_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin a non-empty ``BACKPLANE_URL`` so the lifespan boots in tests.

    G0.8-T4 (#633) added :func:`meho_backplane.main._assert_mcp_resource_uri_configured`
    to the FastAPI lifespan: the ``/mcp`` router is mounted
    unconditionally, so a deploy with neither ``MCP_RESOURCE_URI`` nor
    ``BACKPLANE_URL`` set now fails loudly at startup instead of
    serving a dark, silent ``/mcp``. The whole ``test_mcp_*`` /
    ``test_auth_config`` / ``test_middleware`` family boots the app via
    ``with TestClient(app)`` (which runs the lifespan); without a
    process-default for ``BACKPLANE_URL`` every one of them would now
    crash on the new guard for a reason unrelated to what they test.

    Setting it here, before any test imports :func:`get_settings`,
    makes the default deploy-shape "MCP audience resolvable" so the
    guard passes silently. Tests that *exercise* the unresolvable path
    (``test_empty_audience_setting_returns_401``,
    ``test_audience_not_configured_401_detail_is_actionable``,
    ``test_mcp_startup_guard``) override with
    ``monkeypatch.setenv("BACKPLANE_URL", "")`` — ``MonkeyPatch.setenv``
    is last-write, so the per-test override wins over this autouse
    default (same precedence contract :func:`_default_database_url`
    documents).

    The ``get_settings.cache_clear()`` brackets mirror the sibling
    autouse fixtures so a stale cached ``Settings`` cannot leak the
    value across tests.
    """
    monkeypatch.setenv("BACKPLANE_URL", "https://meho.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _default_target_ssrf_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exempt the suite's fixture address space from the target SSRF guard.

    evoila-bosnia/meho-internal#153 rejects targets whose ``host``/
    ``fqdn`` is — or resolves to — a private / loopback / link-local
    address at create/update *and* connect time, overridable only via
    ``MEHO_TARGET_SSRF_ALLOWLIST``. The suite predates that guard and
    registers hundreds of targets on ``10.x`` / ``192.168.x`` /
    ``127.0.0.1`` literals (plus testcontainers bridge addresses in
    ``172.16.0.0/12`` for the integration lane), which are exactly the
    on-prem shapes the allowlist exists for. Pin the documented
    override here so every pre-existing fixture keeps validating,
    instead of scattering per-test env plumbing.

    Two knobs, same precedence contract as ``_default_backplane_url``
    (last-write wins, so per-test overrides beat this default):

    1. The env allowlist covers the RFC 1918 / loopback / ULA /
       link-local **IP-literal** fixtures. ``169.254.0.0/16`` is
       deliberately absent — no fixture uses metadata space, and
       keeping it blocked preserves the sharpest edge of the guard
       even suite-wide.
    2. ``_resolve_addrs`` (the guard's single DNS seam) is stubbed to
       "unresolvable" so the fixtures' synthetic hostnames
       (``vcenter.invalid``, ``vrli.lab.internal``, …) never leave the
       process as real DNS queries — the guard fail-opens on
       unresolvable names by design and the mocked transports never
       dial anyway. Guard-focused tests (``test_targets_ssrf_guard``)
       re-patch the seam with their own resolver and
       ``monkeypatch.delenv`` the allowlist to exercise the real
       behaviour.
    """
    monkeypatch.setenv(
        "MEHO_TARGET_SSRF_ALLOWLIST",
        "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.0/8,::1/128,fc00::/7,fe80::/10",
    )
    monkeypatch.setattr("meho_backplane.targets.ssrf_guard._resolve_addrs", lambda host: [])


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

    **Capture surface depends on the runner (#585/#604).** The
    ``caplog`` (stdlib :mod:`logging`) scan is **always** active — it is
    reliably per-test isolated even under ``pytest-xdist``. The
    ``capfd`` (OS-fd-level) scan is active **only single-process**: fd
    capture is not cleanly per-test under xdist, so a real ``Bearer``
    emitted by some test's late/async/500-path on a worker would be
    mis-attributed to whichever test's teardown sweep runs next — a
    non-deterministic false positive (not a real leak: production
    redacts ``SENSITIVE_HEADERS`` and the full serial suite is clean).
    Under xdist the fd scan is skipped; coverage is preserved by the
    always-on ``caplog`` scan, the explicit
    ``tests/test_secret_leak_checks.py`` assertions, and production-side
    redaction (defense in depth). The body details the gate.

    **Mid-test drain protection (single-process path).**
    ``capfd.readouterr`` is destructive: each call returns *and clears*
    what was captured since the previous call. A test that drains the
    buffer mid-run would consume those bytes before this sweep sees
    them. The fixture installs a record-and-forward proxy over
    ``capfd.readouterr`` that copies every read into an internal list;
    at teardown the sweep concatenates every recorded chunk plus a
    final post-yield read. (Only installed when not under xdist.)
    """
    # xdist gate (#585/#604). ``capfd`` is OS-fd-level capture; under
    # pytest-xdist it is NOT cleanly per-test isolated — a real
    # ``Bearer`` emitted by *some* test's late / async / 500-path on a
    # worker lands in whichever test's teardown sweep happens to run
    # next, a non-deterministic FALSE POSITIVE. Proven not a production
    # leak: ``RequestContextMiddleware`` redacts ``SENSITIVE_HEADERS``,
    # this sweep is autouse serially too, and the full serial suite is
    # clean (2007/0/0) while parallel runs flag different innocent
    # tests run-to-run. So the fd-level scan runs ONLY single-process
    # (local dev + any serial security context); under xdist we rely on
    # the ``caplog`` scan below (stdlib ``logging`` IS reliably
    # per-test under xdist) PLUS the explicit
    # ``tests/test_secret_leak_checks.py`` assertions PLUS the
    # production-side header redaction — defense in depth, no real
    # coverage lost. A capfd-under-xdist redesign is tracked separately
    # if an fd-only leak surface ever emerges.
    under_xdist = os.environ.get("PYTEST_XDIST_WORKER") is not None

    captured_chunks: list[tuple[str, str]] = []
    real_readouterr = capfd.readouterr

    if not under_xdist:
        # Discard pre-test fd residue so the sweep inspects only what
        # THIS test emits, then record every drain so a mid-test
        # ``capfd.readouterr()`` cannot consume secret-shaped output
        # before the post-yield sweep sees it.
        real_readouterr()

        def _recording_readouterr() -> Any:
            result = real_readouterr()
            captured_chunks.append((result.out, result.err))
            return result

        monkeypatch.setattr(capfd, "readouterr", _recording_readouterr)

    yield

    if not under_xdist:
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
# Global-registry test isolation (#585 — unblocks pytest-xdist)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def _force_import_mcp_modules() -> None:
    """Import every MCP tool/resource module ONCE per worker, up front.

    ``register_mcp_tool`` / ``register_mcp_resource`` fire as *module-
    import side effects*; :func:`eager_import_mcp_modules` only triggers
    them on a module's first import (Python caches imports).
    ``clear_registries()`` (the per-test reset in
    ``tests/test_mcp_registry.py``) empties ``_TOOLS`` but cannot
    un-import modules, so whether a later lifespan-driven
    ``eager_import_mcp_modules()`` re-populates real tools depends purely
    on import history — deterministic only in full-suite *serial*
    collection order. That coupling is exactly what made
    ``test_mcp_registry`` / ``test_audit_middleware`` pass serially but
    fail under ``pytest-xdist`` (#585; the #540 class generalised).

    Forcing the imports once at session start makes the registration
    side effects fire before any test, so every subsequent
    ``eager_import_mcp_modules()`` (lifespan startup, any worker, any
    order) is a cached no-op. Combined with the per-test
    snapshot/restore below, registry state becomes a pure function of
    what each test explicitly registers — order- and worker-independent.
    """
    from meho_backplane.mcp.registry import eager_import_mcp_modules

    eager_import_mcp_modules()


@pytest.fixture(scope="session", autouse=True)
def _force_import_connector_modules() -> None:
    """Import every ``connectors/<product>/`` subpackage ONCE per worker.

    Same hazard as :func:`_force_import_mcp_modules` above, but for the
    connector registry: ``register_connector`` / ``register_connector_v2``
    fire as module-import side effects, so the first
    :func:`_eager_import_connectors` call populates the v1+v2 tables;
    every subsequent call is a cached no-op (Python caches imports).
    The per-test :func:`_isolate_global_registries` snapshot/restore then
    pins the registry to whatever was registered *before* the snapshot
    was first taken — empty if no prior test triggered a lifespan,
    populated otherwise. That coupling bit G3.11-T10 #1253 when the
    chassis lifespan started calling
    :func:`validate_catalog_registry_coverage` post-:func:`load_catalog`:
    a test exercising the lifespan after a registry-clearing test (or
    in isolation, before any other test had triggered an import) saw an
    empty registry and the validator raised
    ``catalog_registry_triple_mismatch``.

    Forcing the imports once at session start makes the registrations
    land before any test runs; the snapshot then captures the populated
    baseline; every subsequent lifespan ``_eager_import_connectors``
    call (any worker, any order) is a cached no-op against an
    already-populated registry. The triple-check validator finds every
    catalog row's triple in the v2 table and lifespan startup tests
    boot cleanly.
    """
    from meho_backplane.connectors.registry import _eager_import_connectors

    _eager_import_connectors()


@pytest.fixture(autouse=True)
def _isolate_global_registries() -> Iterator[None]:
    """Snapshot/restore every process-global registry around each test.

    The backplane keeps mutable module-level registries that the app
    lifespan and ``@register_*`` decorators populate:
    ``mcp.registry._TOOLS`` / ``_RESOURCES``,
    ``connectors.registry._REGISTRY`` / ``_REGISTRY_V2``,
    ``operations.typed_register._TYPED_OP_REGISTRARS``,
    ``operations._preview._PREVIEW_BUILDERS`` / ``_PERMISSION_PREFLIGHTS``,
    and the dispatcher's per-process caches
    ``operations._handler_resolve._HANDLER_CACHE`` /
    ``_CONNECTOR_INSTANCE_CACHE``. None had a process-wide per-test
    reset, so a test that triggered the lifespan (or dispatched an op)
    leaked real registrations / a cached connector singleton into
    whatever test the (xdist) scheduler ran next on the same worker.
    #540 fixed exactly one of these with a local fixture; this
    generalises the snapshot/restore to all of them, process-wide.

    The connector-instance cache is the load-bearing addition for #984
    (PR #998): a dispatch-based test that patches the cached connector
    singleton's ``_credentials_loader`` to an operator-ignoring stub
    (``test_broadcast_credential_mint_dispatch``) relied on its own
    ``reset_dispatcher_caches()`` teardown; the autouse net now restores
    the cache regardless, so a leaked patched singleton can never bleed
    into another connector test on the same worker. Mirrors the
    ``reset_handler_cache`` / ``reset_connector_instance_cache`` pair in
    :mod:`meho_backplane.operations._handler_resolve`.

    It also snapshots/restores the dispatcher's default reducer binding,
    ``operations.dispatcher._DEFAULT_REDUCER``. The app lifespan swaps the
    shipped ``PassThroughReducer`` for the production ``JsonFluxReducer``
    via ``set_default_reducer(...)`` (``main.py``) and never restores it,
    so the same lifespan-leak failure mode applied to the reducer too —
    it bit the #753 vcsim e2e (green single-file, red in the full CI
    sweep). This is the process-wide safety net beneath the per-test
    reducer fixtures (e.g. those swapping in a row-thresholded
    ``JsonFluxReducer``), which keep working unchanged.

    Snapshot the registries' *contents* (not the binding — other modules
    hold the same dict/list objects, so rebinding would not propagate)
    and restore them verbatim. The reducer is the opposite shape: a
    single module-level binding that ``dispatch()`` reads fresh on each
    call, so its identity is captured and restored through the only
    supported rebind path, ``set_default_reducer(saved)``. Restore-not-
    clear: registrations (and reducer swaps) a test legitimately makes
    survive *within* that test; only the cross-test bleed is removed.
    """
    from meho_backplane.connectors import registry as conn_reg
    from meho_backplane.mcp import registry as mcp_reg
    from meho_backplane.operations import _handler_resolve as handler_resolve
    from meho_backplane.operations import _preview as preview_reg
    from meho_backplane.operations import dispatcher, set_default_reducer
    from meho_backplane.operations import typed_register as typed_reg

    tools = dict(mcp_reg._TOOLS)
    resources = dict(mcp_reg._RESOURCES)
    connectors_v1 = dict(conn_reg._REGISTRY)
    connectors_v2 = dict(conn_reg._REGISTRY_V2)
    registrars = list(typed_reg._TYPED_OP_REGISTRARS)
    handler_cache = dict(handler_resolve._HANDLER_CACHE)
    instance_cache = dict(handler_resolve._CONNECTOR_INSTANCE_CACHE)
    # The per-op approval-preview registries (#1437 / #1504 / #1608 / #1857).
    # ``test_connectors_registry_v2`` evicts every ``connectors.*`` subpackage
    # from ``sys.modules`` and re-runs ``_eager_import_connectors``, so each
    # connector's import-time ``register_preview_builder`` /
    # ``register_permission_preflight`` re-fires under a FRESH module object
    # (a new ``_vm_create_preview`` etc.). That test restores ``sys.modules``
    # but not these registries, leaving ``_PREVIEW_BUILDERS`` pointing at the
    # re-imported function objects while the original ``_write_preview`` module
    # still holds the originals — so a later same-worker
    # ``test_all_eight_write_composites_register_a_preview_builder`` saw its
    # ``is``-identity check fail under ``--dist loadscope`` bucketing. Snapshot
    # + restore them with the other process-global registries.
    preview_builders = dict(preview_reg._PREVIEW_BUILDERS)
    permission_preflights = dict(preview_reg._PERMISSION_PREFLIGHTS)
    default_reducer = dispatcher._DEFAULT_REDUCER
    try:
        yield
    finally:
        mcp_reg._TOOLS.clear()
        mcp_reg._TOOLS.update(tools)
        mcp_reg._RESOURCES.clear()
        mcp_reg._RESOURCES.update(resources)
        conn_reg._REGISTRY.clear()
        conn_reg._REGISTRY.update(connectors_v1)
        conn_reg._REGISTRY_V2.clear()
        conn_reg._REGISTRY_V2.update(connectors_v2)
        typed_reg._TYPED_OP_REGISTRARS[:] = registrars
        handler_resolve._HANDLER_CACHE.clear()
        handler_resolve._HANDLER_CACHE.update(handler_cache)
        handler_resolve._CONNECTOR_INSTANCE_CACHE.clear()
        handler_resolve._CONNECTOR_INSTANCE_CACHE.update(instance_cache)
        preview_reg._PREVIEW_BUILDERS.clear()
        preview_reg._PREVIEW_BUILDERS.update(preview_builders)
        preview_reg._PERMISSION_PREFLIGHTS.clear()
        preview_reg._PERMISSION_PREFLIGHTS.update(permission_preflights)
        set_default_reducer(default_reducer)


@pytest.fixture(autouse=True)
def _isolate_readiness_state() -> Iterator[None]:
    """Reset the readiness-probe registry + verdict cache around each test.

    ``meho_backplane.health`` keeps two pieces of process-global mutable
    state: the probe registry ``_probes`` (populated by
    ``register_probe`` — the app lifespan in ``main.py`` registers the
    real ``keycloak`` / ``vault`` / ``db`` / ``broadcast`` /
    ``docs_backends`` probes there) and the ``_readiness_cache`` verdict
    (plus its in-flight ``_readiness_refresh_task`` background handle).
    Neither had a process-wide per-test reset, so any test that booted
    the full app — directly or by importing ``meho_backplane.main`` —
    left the real probes registered for whatever test the (xdist
    ``--dist loadscope``) scheduler ran next on the same worker.

    That cross-test leak is benign while readiness is only swept on an
    explicit ``/ready`` hit, but #1776 made each ``/ui/*`` render schedule
    a *background* readiness refresh (``ui_readiness_verdict`` ->
    ``_schedule_readiness_refresh`` -> ``run_probes_async``). A leaked DB-
    touching ``db`` probe then runs that sweep *concurrently* with a
    later test's own SQLite writes, and aiosqlite reports
    ``database is locked`` (it surfaced as a flake in
    ``test_ui_runbooks_acceptance``). Pre-fix the sweep ran inline on the
    request path, so a leaked probe merely slowed it rather than
    contending.

    Clearing (not snapshot/restoring like ``_isolate_global_registries``)
    is correct here: probes are re-registered from scratch on every app
    boot, so no test should depend on a probe registered by a *previous*
    test — that would itself be a latent order-dependency bug.
    ``clear_readiness_cache`` additionally cancels and detaches any
    in-flight background refresh, so a sweep one test scheduled cannot
    write into a sibling's cache or leak a pending task onto a
    soon-to-close event loop. Reset both before *and* after so a test
    that aborts mid-run cannot leak either. Redundant-but-harmless for
    the handful of suites (``test_health``, ``test_alembic_probe``,
    ``test_ui_readiness_pill``, ...) that already clear the registry
    themselves.
    """
    from meho_backplane.health import clear_probes, clear_readiness_cache

    clear_probes()
    clear_readiness_cache()
    try:
        yield
    finally:
        clear_probes()
        clear_readiness_cache()


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
    omit_exp: bool = False,
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
    * ``omit_exp`` — when ``True``, drops the ``exp`` claim so the
      decoder's essential-``exp`` enforcement surfaces a
      ``missing_exp`` 401 rather than accepting a non-expiring token.
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
            "nbf": now + not_before_offset,
        }
        if not omit_exp:
            payload["exp"] = now + expires_in
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


# ---------------------------------------------------------------------------
# CI-PERF TIMING INSTRUMENTATION (permanent, dormant unless enabled)
#
# Reusable per-step / per-fixture setup-cost attribution for the suite. Both
# the hook and the autouse fixture are pure no-ops unless their env var is
# set, so a normal run is unaffected. Harvested from the #771 investigation
# (PR #793), where it attributed the unit-job wall to per-test descriptor
# re-embedding — `--durations` alone could only blame the *fixture*, not the
# step inside the lifespan.
#
# To use (one-off diagnostic run, locally or on a throwaway branch):
#
#   MEHO_LIFESPAN_TIMING=/tmp/lifespan \
#   MEHO_FIXTURE_TIMING_FILE=/tmp/fixt \
#     uv run pytest -n 6 --dist loadscope --ignore=tests/integration tests/
#   uv run python tests/_perf_timing_report.py   # prints both tables
#
#   MEHO_LIFESPAN_TIMING=<path>      per-test timing of each FastAPI lifespan
#       step, by wrapping meho_backplane.main's module-level callables. One
#       JSON line per call, written to "<path>.<worker>".
#   MEHO_FIXTURE_TIMING_FILE=<path>  per-fixture setup duration for every
#       fixture (outer attribution: which fixture's setup dominates).
#       Accumulated in process, flushed once per worker at session finish.
# ---------------------------------------------------------------------------

_PERF_FIXTURE_TIMINGS: list[tuple[float, str, str]] = []


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef, request):
    """Record per-fixture setup duration when MEHO_FIXTURE_TIMING_FILE is set."""
    if not os.environ.get("MEHO_FIXTURE_TIMING_FILE"):
        yield
        return
    start = time.perf_counter()
    yield
    _PERF_FIXTURE_TIMINGS.append(
        (round(time.perf_counter() - start, 3), fixturedef.argname, str(fixturedef.scope)),
    )


def pytest_sessionfinish(session, exitstatus):
    """Flush accumulated fixture-setup timings once per worker."""
    import json

    path = os.environ.get("MEHO_FIXTURE_TIMING_FILE")
    if not path or not _PERF_FIXTURE_TIMINGS:
        return
    worker = os.environ.get("PYTEST_XDIST_WORKER", "master")
    with open(f"{path}.{worker}", "w") as fh:
        for dur, name, scope in _PERF_FIXTURE_TIMINGS:
            fh.write(json.dumps({"dur": dur, "fix": name, "scope": scope}) + "\n")


@pytest.fixture(autouse=True)
def _perf_lifespan_step_timing(request: pytest.FixtureRequest) -> Iterator[None]:
    """Time each FastAPI lifespan step per test when MEHO_LIFESPAN_TIMING is set.

    Wraps the module-level callables the lifespan invokes (resolved from
    meho_backplane.main's globals at call time) with timing wrappers that
    append one record per call. Restored on teardown. Test-only; touches no
    production code.
    """
    import asyncio
    import json

    path = os.environ.get("MEHO_LIFESPAN_TIMING")
    if not path:
        yield
        return

    import meho_backplane.main as main_mod

    worker = os.environ.get("PYTEST_XDIST_WORKER", "master")
    node = request.node.nodeid
    out = f"{path}.{worker}"

    def _record(step: str, dur: float) -> None:
        with open(out, "a") as fh:
            fh.write(json.dumps({"step": step, "dur": round(dur, 3), "node": node}) + "\n")

    def _wrap_sync(orig: Any, name: str) -> Any:
        def w(*a: Any, **k: Any) -> Any:
            t = time.perf_counter()
            try:
                return orig(*a, **k)
            finally:
                _record(name, time.perf_counter() - t)

        return w

    def _wrap_async(orig: Any, name: str) -> Any:
        async def w(*a: Any, **k: Any) -> Any:
            t = time.perf_counter()
            try:
                return await orig(*a, **k)
            finally:
                _record(name, time.perf_counter() - t)

        return w

    names = [
        "get_engine",
        "get_broadcast_client",
        "_eager_import_connectors",
        "run_typed_op_registrars",
        "eager_import_mcp_modules",
        "_preload_embedding_model",
        "start_topology_refresh_scheduler",
    ]
    originals: dict[str, Any] = {}
    for name in names:
        orig = getattr(main_mod, name)
        originals[name] = orig
        is_async = asyncio.iscoroutinefunction(orig)
        wrapper = _wrap_async(orig, name) if is_async else _wrap_sync(orig, name)
        setattr(main_mod, name, wrapper)
    try:
        yield
    finally:
        for name, orig in originals.items():
            setattr(main_mod, name, orig)
