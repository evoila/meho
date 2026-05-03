#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Generate Ed25519 key pair for MEHO license signing.

Run ONCE per environment. The public half is embedded in
``meho_app/core/licensing.py``; the private half MUST live in a vault.

Safe-by-default: the private key is never printed unless ``--unsafe-stdout``
is explicitly given. Choose exactly one output path:

* ``--vault-write projects/<PROJECT>/secrets/<NAME>`` — writes the private
  key to GCP Secret Manager (preferred). Bare ``<NAME>`` is also accepted
  if ``GOOGLE_CLOUD_PROJECT`` is set.
* ``--output-private FILE`` — writes the private key to ``FILE`` with mode
  ``0600``. Refuses to overwrite an existing file.
* ``--unsafe-stdout`` — prints the private key to stdout (legacy path,
  unsafe — clear your scrollback after).

The public key is always printed to stdout (it is not a secret).
"""

from __future__ import annotations

import base64
import os
from pathlib import Path  # noqa: TC003 -- typer resolves type hints at runtime
from typing import Annotated, NamedTuple

import typer
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

app = typer.Typer(
    name="generate-license-keypair",
    help=__doc__,
    no_args_is_help=False,
    add_completion=False,
)


class Keypair(NamedTuple):
    """Base64url-encoded Ed25519 keypair. Named fields prevent positional swaps."""

    private_b64: str
    public_b64: str


def generate_keypair() -> Keypair:
    """Generate an Ed25519 key pair and return base64url-encoded halves."""
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    private_bytes = private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    public_bytes = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)

    private_b64 = base64.urlsafe_b64encode(private_bytes).rstrip(b"=").decode()
    public_b64 = base64.urlsafe_b64encode(public_bytes).rstrip(b"=").decode()

    return Keypair(private_b64=private_b64, public_b64=public_b64)


def _resolve_secret_parent(secret_arg: str) -> str:
    """Accept either a full ``projects/PROJ/secrets/NAME`` path or a bare name.

    The full form must be exactly four ``/``-separated segments
    (``projects/<P>/secrets/<N>``) — versioned paths and trailing-segment
    garbage are rejected. A bare name is combined with
    ``GOOGLE_CLOUD_PROJECT``. Anything else errors out.
    """
    parts = secret_arg.split("/") if secret_arg else []
    is_full_path = (
        len(parts) == 4
        and parts[0] == "projects"
        and parts[1]
        and parts[2] == "secrets"
        and parts[3]
    )
    if is_full_path:
        return secret_arg
    if "/" in secret_arg or not secret_arg:
        typer.echo(
            f"ERROR: --vault-write expects either a bare secret name or a full "
            f"'projects/<PROJECT>/secrets/<NAME>' path; got: {secret_arg!r}",
            err=True,
        )
        raise typer.Exit(code=1)
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        typer.echo(
            "ERROR: --vault-write was given a bare secret name but "
            "GOOGLE_CLOUD_PROJECT is unset. Either set it, or pass the full "
            "path 'projects/<PROJECT>/secrets/<NAME>'.",
            err=True,
        )
        raise typer.Exit(code=1)
    return f"projects/{project}/secrets/{secret_arg}"


def _ensure_secretmanager_available() -> None:
    """Exit with a clear install hint if google-cloud-secret-manager is absent.

    Lazy import: the SDK is not a project dependency. Maintainers running this
    script install it locally; CI and other contributors are not affected.
    """
    try:
        from google.cloud import secretmanager  # noqa: F401 -- import-only check
    except ImportError:
        typer.echo(
            "ERROR: --vault-write requires google-cloud-secret-manager. Install with:\n"
            "  uv pip install 'google-cloud-secret-manager>=2.0.0'",
            err=True,
        )
        raise typer.Exit(code=1) from None


def _verify_vault_target(parent: str) -> None:
    """Pre-flight: confirm the secret resource exists and is reachable.

    Called BEFORE :func:`generate_keypair` so a missing secret resource, IAM
    gap, malformed parent, or transient network error never silently
    discards a freshly-minted private key (the key only lives in process
    memory until ``add_secret_version`` succeeds).
    """
    _ensure_secretmanager_available()
    from google.cloud import secretmanager

    try:
        secretmanager.SecretManagerServiceClient().get_secret(name=parent)
    except Exception as e:  # noqa: BLE001 -- SDK raises various google.api_core exceptions; pre-flight must fail-closed on any
        type_name = type(e).__name__
        if type_name == "NotFound":
            hint = (
                "\nCreate the secret first with:\n"
                "  gcloud secrets create <NAME> --replication-policy=automatic "
                "--project=<PROJECT>"
            )
        else:
            hint = ""
        typer.echo(
            f"ERROR: cannot access vault target {parent!r}: {type_name}: {e}{hint}",
            err=True,
        )
        raise typer.Exit(code=1) from None


def _write_to_vault(parent: str, private_b64: str) -> None:
    """Add a new version of an existing GCP Secret Manager secret.

    Caller MUST invoke :func:`_verify_vault_target` before
    :func:`generate_keypair` so SDK errors here don't discard the key.
    """
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    client.add_secret_version(
        request={"parent": parent, "payload": {"data": private_b64.encode("utf-8")}},
    )


def _write_to_file(target: Path, private_b64: str) -> None:
    """Atomically create ``target`` with mode ``0600`` and write the private key.

    ``os.open`` with ``O_CREAT | O_EXCL`` refuses to overwrite an existing
    file *and* sets the mode at creation time, so there is no window in
    which the file exists with the process umask before ``chmod`` lands.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(target, flags, 0o600)
    except FileExistsError:
        typer.echo(
            f"ERROR: refusing to overwrite existing file: {target}",
            err=True,
        )
        raise typer.Exit(code=1) from None
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(private_b64)
        f.write("\n")


def _print_public_summary(public_b64: str) -> None:
    """Print public-key summary. Always safe to call."""
    typer.echo("=" * 60)
    typer.echo("MEHO License Key Pair (Ed25519)")
    typer.echo("=" * 60)
    typer.echo("")
    typer.echo("Public key (embed in licensing.py _PUBLIC_KEY_B64):")
    typer.echo(f"  {public_b64}")
    typer.echo("")


@app.command()
def main(
    vault_write: Annotated[
        str | None,
        typer.Option(
            "--vault-write",
            help=(
                "Write private key to GCP Secret Manager. Accepts "
                "'projects/<PROJECT>/secrets/<NAME>' or bare '<NAME>' "
                "(requires GOOGLE_CLOUD_PROJECT)."
            ),
        ),
    ] = None,
    output_private: Annotated[
        Path | None,
        typer.Option(
            "--output-private",
            help="Write private key to FILE (mode 0600, refuses to overwrite).",
        ),
    ] = None,
    unsafe_stdout: Annotated[
        bool,
        typer.Option(
            "--unsafe-stdout",
            help="Print private key to stdout. Unsafe — leaves traces in scrollback.",
        ),
    ] = False,
) -> None:
    """Generate an Ed25519 keypair and route the private half to one destination."""
    chosen = sum(
        [vault_write is not None, output_private is not None, unsafe_stdout],
    )
    if chosen == 0:
        typer.echo(
            "ERROR: choose exactly one of --vault-write, --output-private, --unsafe-stdout.",
            err=True,
        )
        raise typer.Exit(code=1)
    if chosen > 1:
        typer.echo(
            "ERROR: choose only one of --vault-write, --output-private, --unsafe-stdout.",
            err=True,
        )
        raise typer.Exit(code=1)

    if vault_write is not None:
        parent = _resolve_secret_parent(vault_write)
        _verify_vault_target(parent)
        keypair = generate_keypair()
        _write_to_vault(parent, keypair.private_b64)
        _print_public_summary(keypair.public_b64)
        typer.echo(f"Private key written to vault: {parent}")
        typer.echo("")
        typer.echo("To embed the public key in licensing.py:")
        typer.echo('  Replace _PUBLIC_KEY_B64 = "..." with:')
        typer.echo(f'  _PUBLIC_KEY_B64 = "{keypair.public_b64}"')
        return

    if output_private is not None:
        keypair = generate_keypair()
        _write_to_file(output_private, keypair.private_b64)
        _print_public_summary(keypair.public_b64)
        typer.echo(f"Private key written to: {output_private} (mode 0600)")
        typer.echo("")
        typer.echo("To embed the public key in licensing.py:")
        typer.echo('  Replace _PUBLIC_KEY_B64 = "..." with:')
        typer.echo(f'  _PUBLIC_KEY_B64 = "{keypair.public_b64}"')
        return

    typer.echo(
        "WARNING: --unsafe-stdout was used; clear your terminal scrollback.",
        err=True,
    )
    keypair = generate_keypair()
    _print_public_summary(keypair.public_b64)
    typer.echo("Private key (KEEP SECRET — store in vault/secret manager):")
    typer.echo(f"  {keypair.private_b64}")
    typer.echo("")
    typer.echo("To embed the public key in licensing.py:")
    typer.echo('  Replace _PUBLIC_KEY_B64 = "..." with:')
    typer.echo(f'  _PUBLIC_KEY_B64 = "{keypair.public_b64}"')


if __name__ == "__main__":
    app()
