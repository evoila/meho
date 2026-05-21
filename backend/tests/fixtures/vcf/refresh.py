# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Recorded-fixture refresh tool for the four VCF management-plane connectors.

Hits a live VCF appliance, records HTTP responses, redacts well-known
secret fields, and writes JSON-serialised ``RecordedResponse`` files into
``backend/tests/fixtures/vcf/<connector>/`` for the E2E tests in #837 /
#838 / #839 / #840 to replay.

Operator-run only (the four products have no public CI simulator — same
constraint NSX and SDDC Manager hit). Not invoked from automated tests.

Usage::

    uv run python backend/tests/fixtures/vcf/refresh.py \\
        --connector vcf-operations \\
        --target vrops-a \\
        --host vrops-a.lab.example.com \\
        --username admin \\
        --password "$VROPS_PASSWORD" \\
        --output-dir backend/tests/fixtures/vcf/vcf-operations

See ``docs/cross-repo/vcf-fixture-refresh.md`` for the full recipe and
the credential / safety caveats.

Safety
------

* Refuses to overwrite an existing fixture file without ``--force``. Stale
  fixtures from a prior appliance version are silently masked otherwise
  (one of NSX-T's bigger fixture-debt incidents in 2025-Q2).
* Redacts ``Set-Cookie``, ``Authorization``, ``sessionId``, and
  ``X-XSRF-TOKEN`` from response headers; redacts ``password`` /
  ``session_token`` / ``sessionId`` keys from JSON response bodies. The
  redaction list is configurable via ``--redact-header`` /
  ``--redact-json-key`` flags.
* ``--dry-run`` records nothing — prints the would-be operations for the
  operator to eyeball.
* Refuses to run with no ``--insecure`` flag against an appliance with a
  self-signed cert; operators run against lab appliances with proper certs
  or pass ``--insecure`` explicitly.

The fixture format is intentionally human-readable JSON (not the pickled
``httpx.Response`` form) so a maintainer can hand-edit a redacted field or
inspect what's checked in. The replay side reads the same JSON and
constructs a respx response from it.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from meho_backplane.connectors._shared.vcf_auth import (
    SessionLoginError,
    basic_auth_header,
    vcf_session_login,
)

_log = logging.getLogger("vcf.fixture.refresh")


# ---------------------------------------------------------------------------
# Recorded response shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordedResponse:
    """JSON-serialisable snapshot of an HTTP response.

    Stored as ``backend/tests/fixtures/vcf/<connector>/<fixture-name>.json``;
    the E2E replay layer reads this back and builds a
    :class:`httpx.Response` for ``respx`` to mock with.

    ``request_path`` carries the path that was hit so a maintainer can
    diff fixtures across appliance versions without re-deriving the path
    from the filename.
    """

    fixture_name: str
    request_method: str
    request_path: str
    response_status: int
    response_headers: dict[str, str]
    response_body: Any  # JSON-deserialised dict / list / scalar
    recorded_at: str  # ISO-8601 UTC

    def to_json_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class FixtureCall:
    """A single endpoint to record.

    ``auth`` is one of:

    * ``"basic"`` — Basic auth from the operator-provided credentials.
    * ``"session"`` — POST credentials to ``session_login_path``, extract
      token via ``session_token_header``, send subsequent calls with
      ``Authorization: Bearer <token>``.
    * ``"none"`` — unauthenticated (probe / version endpoints).

    ``params`` is forwarded to ``httpx.AsyncClient.get`` as the query
    string. ``fixture_name`` becomes the on-disk filename stem.
    """

    fixture_name: str
    method: str
    path: str
    params: dict[str, str] = field(default_factory=dict)
    auth: str = "basic"  # "basic" | "session" | "none"


@dataclass(frozen=True)
class ConnectorRecipe:
    """All endpoints the refresh tool captures for one product."""

    connector_id: str  # "vcf-operations" | "vcf-logs" | "vcf-fleet" | "vcf-automation"
    calls: tuple[FixtureCall, ...]
    session_login_path: str | None = None  # only when any call uses auth="session"
    session_token_header: str | None = None
    session_payload_provider: str | None = None  # vRLI provider field; None ⇒ omit


# ---------------------------------------------------------------------------
# Per-connector recipes
# ---------------------------------------------------------------------------
#
# The recipes are intentionally minimal at first ship — enough to cover the
# version + about + a single list call per connector so the E2E tests have
# something to replay. The downstream skeletons (#829/#830/#831) extend the
# recipes as they add ops.

_VCF_OPERATIONS_RECIPE = ConnectorRecipe(
    connector_id="vcf-operations",
    calls=(
        FixtureCall(
            fixture_name="versions-current",
            method="GET",
            path="/suite-api/api/versions/current",
            auth="none",
        ),
        FixtureCall(
            fixture_name="adapters",
            method="GET",
            path="/suite-api/api/adapters",
            auth="basic",
        ),
    ),
)

_VCF_LOGS_RECIPE = ConnectorRecipe(
    connector_id="vcf-logs",
    calls=(
        FixtureCall(
            fixture_name="version",
            method="GET",
            path="/api/v2/version",
            auth="none",
        ),
        FixtureCall(
            fixture_name="cluster-info",
            method="GET",
            path="/api/v2/cluster",
            auth="session",
        ),
    ),
    session_login_path="/api/v2/sessions",
    session_token_header="sessionId",
    session_payload_provider="Local",
)

_VCF_FLEET_RECIPE = ConnectorRecipe(
    connector_id="vcf-fleet",
    calls=(
        FixtureCall(
            fixture_name="about",
            method="GET",
            path="/lcm/lcops/api/v2/about",
            auth="none",
        ),
        FixtureCall(
            fixture_name="datacenters",
            method="GET",
            path="/lcm/api/v2/datacenters",
            auth="basic",
        ),
    ),
)

_RECIPES: dict[str, ConnectorRecipe] = {
    "vcf-operations": _VCF_OPERATIONS_RECIPE,
    "vcf-logs": _VCF_LOGS_RECIPE,
    "vcf-fleet": _VCF_FLEET_RECIPE,
    # vcf-automation has dual-plane bespoke auth and skips this shared
    # tooling — its E2E fixtures live alongside the Automation connector
    # itself in #840. Listed here in the comment so future maintainers
    # don't add it back by reflex.
}


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


_DEFAULT_REDACT_HEADERS: frozenset[str] = frozenset(
    {"authorization", "set-cookie", "sessionid", "x-xsrf-token", "cookie"}
)

_DEFAULT_REDACT_JSON_KEYS: frozenset[str] = frozenset(
    {"password", "session_token", "sessionid", "token", "access_token", "refresh_token"}
)

_REDACTED_VALUE = "<redacted>"


def _redact_headers(headers: dict[str, str], denylist: Iterable[str]) -> dict[str, str]:
    """Return a copy of *headers* with denylisted names (case-insensitive) redacted."""
    deny_lower = {h.lower() for h in denylist}
    return {k: (_REDACTED_VALUE if k.lower() in deny_lower else v) for k, v in headers.items()}


def _redact_json(value: Any, denylist: Iterable[str]) -> Any:
    """Walk a JSON-deserialised structure and redact denylisted keys.

    Case-insensitive on key names. Nested dicts and lists are walked
    recursively. Non-dict/non-list values are returned as-is.
    """
    deny_lower = {k.lower() for k in denylist}
    if isinstance(value, dict):
        return {
            k: (_REDACTED_VALUE if k.lower() in deny_lower else _redact_json(v, denylist))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_json(item, denylist) for item in value]
    return value


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


@dataclass
class RecordingOptions:
    """Per-run knobs."""

    connector: str
    target_name: str
    host: str
    port: int
    username: str
    password: str
    output_dir: Path
    insecure: bool
    force: bool
    dry_run: bool
    redact_headers: frozenset[str]
    redact_json_keys: frozenset[str]


async def _record_one(
    client: httpx.AsyncClient,
    call: FixtureCall,
    *,
    options: RecordingOptions,
    session_token: str | None,
) -> RecordedResponse:
    """Execute *call* against *client* and return a redacted snapshot."""
    headers: dict[str, str] = {}
    if call.auth == "basic":
        headers["Authorization"] = basic_auth_header(options.username, options.password)
    elif call.auth == "session":
        if session_token is None:
            raise RuntimeError(
                f"call {call.fixture_name!r} requires session auth but no "
                "session_token was established; check the recipe's "
                "session_login_path"
            )
        headers["Authorization"] = f"Bearer {session_token}"
    elif call.auth == "none":
        pass
    else:
        raise ValueError(f"unknown auth mode {call.auth!r} for {call.fixture_name!r}")

    _log.info("recording %s %s %s", call.method, call.path, call.params or "")
    resp = await client.request(call.method, call.path, params=call.params or None, headers=headers)
    if resp.status_code >= 400:
        # Refuse to write a vendor error payload as a fixture — the recorded-
        # fixture E2E suites in #837/#838/#839/#840 replay these as the canonical
        # "appliance returned X" shape, and a 4xx/5xx body would poison the set.
        # main()'s outer ``except Exception`` returns exit code 1, so the
        # operator sees the failure immediately and can rerun once the
        # appliance call is fixed.
        raise RuntimeError(
            f"{call.fixture_name}: {call.method} {call.path} returned HTTP "
            f"{resp.status_code}; refusing to record a non-2xx response as a "
            f"fixture (body[:200]={resp.text[:200]!r})"
        )
    try:
        body: Any = resp.json()
    except ValueError:
        body = resp.text

    from datetime import UTC, datetime

    return RecordedResponse(
        fixture_name=call.fixture_name,
        request_method=call.method,
        request_path=call.path,
        response_status=resp.status_code,
        response_headers=_redact_headers(dict(resp.headers), options.redact_headers),
        response_body=_redact_json(body, options.redact_json_keys),
        recorded_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )


def _write_fixture(snapshot: RecordedResponse, options: RecordingOptions) -> Path:
    """Write a single fixture to disk; respect ``--dry-run`` and ``--force``."""
    target_path = options.output_dir / f"{snapshot.fixture_name}.json"
    if options.dry_run:
        _log.info("[dry-run] would write %s", target_path)
        return target_path
    if target_path.exists() and not options.force:
        raise FileExistsError(
            f"fixture {target_path} already exists; pass --force to overwrite "
            "(stale fixtures from a prior appliance version mask drift)"
        )
    options.output_dir.mkdir(parents=True, exist_ok=True)
    target_path.write_text(
        json.dumps(snapshot.to_json_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _log.info("wrote %s", target_path)
    return target_path


async def _refresh(options: RecordingOptions) -> list[Path]:
    """Run the full refresh for one connector. Returns the list of written paths."""
    recipe = _RECIPES.get(options.connector)
    if recipe is None:
        raise SystemExit(f"unknown connector {options.connector!r}; supported: {sorted(_RECIPES)}")

    base_url = f"https://{options.host}:{options.port}"
    verify_arg: bool = not options.insecure

    written: list[Path] = []
    async with httpx.AsyncClient(
        base_url=base_url,
        verify=verify_arg,
        timeout=httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0),
    ) as client:
        session_token: str | None = None
        if any(call.auth == "session" for call in recipe.calls):
            if recipe.session_login_path is None or recipe.session_token_header is None:
                raise SystemExit(
                    f"recipe for {options.connector!r} lists session-auth calls "
                    "but is missing session_login_path / session_token_header"
                )

            def _extractor(
                resp: httpx.Response,
                header: str = recipe.session_token_header,
            ) -> str | None:
                value = resp.headers.get(header)
                return value if value is None else str(value)

            payload_builder: Callable[[str, str], dict[str, Any]] | None = None
            if recipe.session_payload_provider is not None:
                provider = recipe.session_payload_provider

                def payload_builder(u: str, p: str, _provider: str = provider) -> dict[str, Any]:
                    return {"username": u, "password": p, "provider": _provider}

            try:
                session_token = await vcf_session_login(
                    client,
                    recipe.session_login_path,
                    username=options.username,
                    password=options.password,
                    target_name=options.target_name,
                    payload_builder=payload_builder,
                    token_extractor=_extractor,
                )
            except SessionLoginError as exc:
                raise SystemExit(f"session-login failed: {exc}") from exc

        for call in recipe.calls:
            snapshot = await _record_one(client, call, options=options, session_token=session_token)
            written.append(_write_fixture(snapshot, options))

    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vcf-fixture-refresh",
        description=(
            "Record HTTP responses from a live VCF management-plane appliance "
            "into backend/tests/fixtures/vcf/<connector>/ for E2E replay."
        ),
    )
    parser.add_argument(
        "--connector",
        required=True,
        choices=sorted(_RECIPES),
        help="Connector identifier (matches the recipe registry).",
    )
    parser.add_argument(
        "--target",
        required=True,
        help="Logical target name (used in log messages; not a hostname).",
    )
    parser.add_argument("--host", required=True, help="Appliance hostname.")
    parser.add_argument("--port", type=int, default=443, help="Appliance port (default 443).")
    parser.add_argument("--username", required=True, help="Service-account username.")
    parser.add_argument(
        "--password",
        required=True,
        help=(
            "Service-account password. Prefer passing via $VCF_PASSWORD env var "
            'and `--password "$VCF_PASSWORD"` so the value doesn\'t land in shell '
            "history."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help=(
            "Destination directory (default: backend/tests/fixtures/vcf/<connector>/ "
            "relative to the repo root). Created if missing."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing fixtures (refuses by default to mask drift).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Hit the appliance + print would-be writes; do not write anything.",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Skip TLS verification (lab appliances with self-signed certs).",
    )
    parser.add_argument(
        "--redact-header",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "Add a header name (case-insensitive) to the redaction denylist. "
            "Defaults: Authorization, Set-Cookie, sessionId, X-XSRF-TOKEN, Cookie."
        ),
    )
    parser.add_argument(
        "--redact-json-key",
        action="append",
        default=[],
        metavar="KEY",
        help=(
            "Add a JSON key (case-insensitive) to the response-body redaction "
            "denylist. Defaults: password, session_token, sessionId, token, "
            "access_token, refresh_token."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="DEBUG-level logging.",
    )
    return parser


def _resolve_output_dir(connector: str, override: Path | None) -> Path:
    if override is not None:
        return override
    # Place fixtures alongside this script so the layout matches what the
    # replay tests expect, regardless of where the operator invoked the
    # script from.
    return Path(__file__).resolve().parent / connector


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    options = RecordingOptions(
        connector=args.connector,
        target_name=args.target,
        host=args.host,
        port=args.port,
        username=args.username,
        password=args.password,
        output_dir=_resolve_output_dir(args.connector, args.output_dir),
        insecure=args.insecure,
        force=args.force,
        dry_run=args.dry_run,
        redact_headers=_DEFAULT_REDACT_HEADERS | frozenset(args.redact_header),
        redact_json_keys=_DEFAULT_REDACT_JSON_KEYS | frozenset(args.redact_json_key),
    )

    try:
        written = asyncio.run(_refresh(options))
    except SystemExit:
        raise
    except Exception as exc:
        _log.error("fixture refresh failed: %s", exc)
        return 1
    _log.info("recorded %d fixture(s) under %s", len(written), options.output_dir)
    return 0


if __name__ == "__main__":  # pragma: no cover — CLI entrypoint
    sys.exit(main())
