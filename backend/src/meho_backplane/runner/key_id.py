# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The id of a runner's public key.

Central and the runner compute the same id from the same key. So the runner
can check that its private key belongs to its saved login, and central can
tell which key it enrolled.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping

__all__ = ["ec_key_id"]


def ec_key_id(jwk: Mapping[str, str]) -> str:
    """RFC 7638 thumbprint of an EC public key (base64url SHA-256)."""
    canonical = json.dumps(
        {name: jwk[name] for name in ("crv", "kty", "x", "y")},
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = hashlib.sha256(canonical.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
