#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Generate Ed25519 key pair for MEHO license signing.

Run ONCE. Store the private key securely (vault/secret manager).
The public key is embedded in meho_app/core/licensing.py.

Usage:
    python scripts/generate-license-keypair.py
"""

from __future__ import annotations

import base64
import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)


def generate_keypair() -> tuple[str, str]:
    """Generate an Ed25519 key pair and return base64url-encoded (private, public)."""
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    private_bytes = private_key.private_bytes(
        Encoding.Raw, PrivateFormat.Raw, NoEncryption()
    )
    public_bytes = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)

    private_b64 = base64.urlsafe_b64encode(private_bytes).rstrip(b"=").decode()
    public_b64 = base64.urlsafe_b64encode(public_bytes).rstrip(b"=").decode()

    return private_b64, public_b64


def main() -> None:
    """Generate and print an Ed25519 key pair for license signing."""
    private_b64, public_b64 = generate_keypair()

    print("=" * 60)
    print("MEHO License Key Pair (Ed25519)")
    print("=" * 60)
    print()
    print(f"Public key (embed in licensing.py _PUBLIC_KEY_B64):")
    print(f"  {public_b64}")
    print()
    print(f"Private key (KEEP SECRET -- store in vault/secret manager):")
    print(f"  {private_b64}")
    print()
    print("=" * 60)
    print("WARNING: The private key MUST be stored securely.")
    print("  - Do NOT commit it to any repository")
    print("  - Do NOT store it in plain text on disk")
    print("  - Use a vault or secret manager (e.g., GCP Secret Manager)")
    print()
    print("To embed the public key in licensing.py:")
    print(f'  Replace _PUBLIC_KEY_B64 = "..." with:')
    print(f'  _PUBLIC_KEY_B64 = "{public_b64}"')
    print("=" * 60)


if __name__ == "__main__":
    main()
