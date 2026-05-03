#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Mint signed enterprise license tokens against the production Ed25519 key.

Maintainer-operated, biometric-gated through the 1Password ``op`` CLI, and
fail-closed against the license-issuance audit log (#520). Three subcommands:

* ``issue`` — mint a signed token. Reads the private key from 1Password,
  records an audit-log row, and writes the token to stdout (default) or a
  ``0600`` file (with ``--output``). The audit-log write must succeed before
  the token is returned to the caller; a failure aborts the issuance with no
  token written or printed.
* ``verify`` — check a token against the embedded public key. Dev-sanity tool.
* ``decode`` — inspect a token's payload without verifying. For support cases.

Configuration: ``issue`` requires ``MEHO_LICENSE_SIGNING_KEY_REF`` to be set to
the 1Password secret reference for the signing key. The production value is
documented in the maintainer custody runbook
(``.claude/operations/license-key-custody.md``); the runbook is intentionally
excluded from the public OSS mirror so the URI does not appear here either.

Custody runbook: ``.claude/operations/license-key-custody.md``.
Safe ``op`` patterns: ``.claude/skills/op-cli/SKILL.md``.
Verifier (the inverse of ``issue``): :mod:`meho_app.core.licensing`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003 -- typer resolves type hints at runtime
from typing import Annotated, Any

import typer
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# 1Password secret reference for the signing key. Required env var; the
# production value lives only in the maintainer custody runbook
# (.claude/operations/license-key-custody.md), which is intentionally excluded
# from the public OSS mirror. Hardcoding it here would re-leak the vault layout
# (vault name + item title) the runbook is hidden to protect.
_SECRET_REF_ENV = "MEHO_LICENSE_SIGNING_KEY_REF"  # noqa: S105 -- env var name, not a secret value

_HEADER: dict[str, str] = {"alg": "EdDSA", "typ": "MEHO-LICENSE"}

app = typer.Typer(
    name="issue-license",
    help=__doc__,
    no_args_is_help=True,
    add_completion=False,
)


def _b64url(data: bytes) -> str:
    """Base64url-encode without padding — matches the verifier's reconstruction."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    """Base64url-decode with re-padded input — mirrors :mod:`meho_app.core.licensing`."""
    return base64.urlsafe_b64decode(value + "==")


def _resolve_secret_ref() -> str:
    """Read the secret reference from ``MEHO_LICENSE_SIGNING_KEY_REF``.

    Errors out if the env var is unset — the production value lives in the
    maintainer custody runbook, which is excluded from the public mirror.
    """
    ref = os.environ.get(_SECRET_REF_ENV)
    if not ref:
        typer.echo(
            f"ERROR: {_SECRET_REF_ENV} is not set. The production secret "
            "reference is documented in the maintainer custody runbook at "
            "`.claude/operations/license-key-custody.md`. Export the value, then re-run.",
            err=True,
        )
        raise typer.Exit(code=1)
    return ref


def _check_op_available() -> None:
    """Pre-flight: require ``op`` on PATH and an active session.

    Run before any keypair work so a missing CLI or expired session never
    leaves a half-issued token in process memory.
    """
    if shutil.which("op") is None:
        typer.echo(
            "ERROR: 1Password CLI (`op`) is not on PATH. Install it from "
            "https://developer.1password.com/docs/cli/.",
            err=True,
        )
        raise typer.Exit(code=1)

    # `op whoami` exits non-zero when the session is expired; suppress its
    # stdout (it carries the maintainer's account email).
    result = subprocess.run(  # noqa: S603 -- args are static
        ["op", "whoami"],  # noqa: S607 -- relies on op on PATH
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        typer.echo(
            "ERROR: `op whoami` failed — 1Password session is not active. "
            "Re-authenticate (Touch ID prompt or `op signin`) and try again.",
            err=True,
        )
        raise typer.Exit(code=1)


def _read_private_key_from_op(secret_ref: str) -> bytes:
    """Fetch the 32-byte Ed25519 private key from 1Password.

    Returns the raw private-key bytes. Never logs the value; the caller signs
    once and lets Python garbage-collect it.

    Raises:
        typer.Exit: ``op read`` exited non-zero (vault, network, IAM, etc.) or
            the secret value is not a valid base64url-encoded 32-byte key.
    """
    try:
        result = subprocess.run(  # noqa: S603 -- args are static
            ["op", "read", "--no-newline", secret_ref],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        # Don't echo exc.stderr — it can include the secret-reference URI
        # which is sensitive operational metadata.
        typer.echo(
            f"ERROR: `op read` failed (exit {exc.returncode}). Check vault "
            "membership, secret reference, and `op whoami`.",
            err=True,
        )
        raise typer.Exit(code=1) from None

    private_b64 = result.stdout.strip()
    try:
        raw = _b64url_decode(private_b64)
    except ValueError:
        typer.echo("ERROR: vault value is not valid base64url.", err=True)
        raise typer.Exit(code=1) from None
    if len(raw) != 32:
        typer.echo(
            f"ERROR: vault value is {len(raw)} bytes; Ed25519 expects 32.",
            err=True,
        )
        raise typer.Exit(code=1)
    return raw


def _sign_token(private_key_bytes: bytes, payload: dict[str, Any]) -> str:
    """Construct a JWT-shaped ``<header>.<payload>.<signature>`` token.

    Signing input is ``<header_b64>.<payload_b64>`` encoded to UTF-8 bytes —
    matches the reconstruction in
    :func:`meho_app.core.licensing._validate_license_key`.
    """
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    header_b64 = _b64url(json.dumps(_HEADER, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}".encode()
    sig_b64 = _b64url(private_key.sign(signing_input))
    return f"{header_b64}.{payload_b64}.{sig_b64}"


def _build_payload(
    *,
    org: str,
    tier: str,
    features: list[str],
    expires_at: datetime | None,
    max_tenants: int | None,
) -> dict[str, Any]:
    """Construct the license payload dict — schema mirrors ``LicensePayload``."""
    return {
        "license_id": str(uuid.uuid4()),
        "org": org,
        "tier": tier,
        "features": features,
        "issued_at": datetime.now(UTC).isoformat(),
        "expires_at": expires_at.isoformat() if expires_at else None,
        "max_tenants": max_tenants,
    }


def _atomic_write_0600(target: Path, content: str) -> None:
    """Atomic ``0600`` file write — refuses to overwrite an existing file.

    ``os.open`` with ``O_CREAT | O_EXCL`` plus an explicit mode argument creates
    the file at the right permissions atomically: there is no window in which
    the file exists with the process umask before ``chmod`` lands. Mirrors the
    pattern in :func:`scripts.generate-license-keypair._write_to_file`.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(target, flags, 0o600)
    except FileExistsError:
        typer.echo(f"ERROR: refusing to overwrite existing file: {target}", err=True)
        raise typer.Exit(code=1) from None
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
        f.write("\n")


async def _record_issuance(payload_dict: dict[str, Any], issuer: str) -> None:
    """Write the audit-log row before the token is returned. Fail-closed.

    Lazy imports keep ``--help``, ``verify``, and ``decode`` (which don't need
    the DB) from paying the FastAPI/SQLAlchemy import cost.
    """
    from meho_app.core.licensing import LicensePayload
    from meho_app.database import get_session_maker
    from meho_app.modules.licensing.audit import LicenseAuditRepository

    payload = LicensePayload(**payload_dict)
    session_maker = get_session_maker()
    async with session_maker() as session:
        repo = LicenseAuditRepository(session)
        await repo.record_issuance(payload, issuer=issuer, issuer_type="user")


def _parse_features(features: str) -> list[str]:
    """Comma-separated string -> list of stripped non-empty entries."""
    return [f.strip() for f in features.split(",") if f.strip()]


def _parse_expires_at(value: str | None) -> datetime | None:
    """Parse ISO 8601 ``--expires-at`` input; default naive datetimes to UTC."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        typer.echo(
            f"ERROR: --expires-at must be ISO 8601 (e.g. 2027-05-01); got {value!r}.",
            err=True,
        )
        raise typer.Exit(code=1) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


@app.command(name="issue")
def cmd_issue(
    org: Annotated[str, typer.Option("--org", help="Customer organization name.")],
    tier: Annotated[str, typer.Option("--tier", help="License tier (e.g. 'enterprise').")],
    features: Annotated[
        str,
        typer.Option(
            "--features",
            help="Comma-separated feature list (e.g. 'multi_tenant,sso').",
        ),
    ],
    issuer: Annotated[
        str,
        typer.Option(
            "--issuer",
            help=(
                "Identity of the principal minting the token (audit-log column). "
                'Recommended: --issuer "$(op whoami --format=json | jq -r .user.email)".'
            ),
        ),
    ],
    expires_at: Annotated[
        str | None,
        typer.Option(
            "--expires-at",
            help="License expiry as ISO 8601 (e.g. 2027-05-01). Omit for perpetual.",
        ),
    ] = None,
    max_tenants: Annotated[
        int | None,
        typer.Option("--max-tenants", help="Tenant cap. Omit for no cap."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            help="Write token to FILE (mode 0600, refuses overwrite). Default: stdout.",
        ),
    ] = None,
) -> None:
    """Mint a signed enterprise license token. Audit-logs before returning."""
    feature_list = _parse_features(features)
    if not feature_list:
        typer.echo(
            "ERROR: --features must include at least one non-empty entry.",
            err=True,
        )
        raise typer.Exit(code=1)
    expires_at_dt = _parse_expires_at(expires_at)

    _check_op_available()
    secret_ref = _resolve_secret_ref()
    private_bytes = _read_private_key_from_op(secret_ref)

    payload = _build_payload(
        org=org,
        tier=tier,
        features=feature_list,
        expires_at=expires_at_dt,
        max_tenants=max_tenants,
    )
    token = _sign_token(private_bytes, payload)

    # Reserve the output target before the audit-log write so a file-write
    # failure can't leave an orphan audit row claiming the token was issued.
    # The audit log is the authoritative compliance record; rows must be 1:1
    # with delivered tokens. ``_atomic_write_0600``'s ``O_EXCL`` is still the
    # race-free safety net at the actual write below.
    if output is not None and output.exists():
        typer.echo(f"ERROR: refusing to overwrite existing file: {output}", err=True)
        raise typer.Exit(code=1)

    # Audit log lands before any token output. record_issuance() raising
    # aborts before the token reaches stdout/disk.
    asyncio.run(_record_issuance(payload, issuer=issuer))

    license_id = payload["license_id"]
    if output is not None:
        _atomic_write_0600(output, token)
        typer.echo(f"Token written to {output} (mode 0600).", err=True)
        typer.echo(f"license_id={license_id}", err=True)
    else:
        typer.echo(f"license_id={license_id}", err=True)
        # stdout is reserved for the token itself so callers can pipe / capture.
        typer.echo(token)


@app.command(name="verify")
def cmd_verify(
    token: Annotated[str, typer.Option("--token", help="Token to verify.")],
) -> None:
    """Verify a token against the embedded public key. Dev-sanity tool."""
    from meho_app.core.licensing import _validate_license_key

    payload = _validate_license_key(token)
    if payload is None:
        typer.echo("INVALID", err=True)
        raise typer.Exit(code=1)
    typer.echo("VALID")
    typer.echo(json.dumps(payload.model_dump(), indent=2, default=str))


@app.command(name="decode")
def cmd_decode(
    token: Annotated[str, typer.Option("--token", help="Token to decode.")],
) -> None:
    """Inspect a token's payload without verifying. For support cases."""
    parts = token.strip().split(".")
    if len(parts) != 3:
        typer.echo("ERROR: token must be three dot-separated segments.", err=True)
        raise typer.Exit(code=1)

    try:
        header = json.loads(_b64url_decode(parts[0]))
        payload = json.loads(_b64url_decode(parts[1]))
    except (ValueError, json.JSONDecodeError) as exc:
        typer.echo(f"ERROR: malformed token: {exc}", err=True)
        raise typer.Exit(code=1) from None

    typer.echo(json.dumps({"header": header, "payload": payload}, indent=2))


if __name__ == "__main__":
    app()
