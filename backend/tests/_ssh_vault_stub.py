# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Test stub for the SSH adapter's Vault secret resolution (#2155).

``SshConnector._resolve_secret`` resolves ``target.secret_ref`` — a Vault
KV-v2 **path string** — via an operator-context Vault read
(``_shared/vault_creds.load_vault_secret_data``). Unit and container
tests have no Vault; this stub reroutes the resolution to an in-memory
registry keyed by the path string, preserving the string-shaped
``secret_ref`` contract end to end. A target carrying the pre-#2155
anti-shape (an embedded credential dict) fails loudly here, exactly as
the real loader's precondition guard would.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
from meho_backplane.connectors.adapters.ssh import SshConnector

__all__ = ["stub_ssh_vault_secrets"]


@contextlib.contextmanager
def stub_ssh_vault_secrets(
    secrets: dict[str, dict[str, Any]],
) -> Iterator[dict[str, dict[str, Any]]]:
    """Patch ``SshConnector._resolve_secret`` to read from *secrets*.

    *secrets* maps a KV-v2 path string (the value a target carries in
    ``secret_ref``) to the secret's data dict. Mutating the yielded
    mapping mid-test is supported — resolution reads it live.

    Raises :class:`VaultCredentialsReadError` from the patched seam for
    a non-string / empty ``secret_ref`` (the unconfigured-target and
    anti-shape cases) and for a path with no registered secret, so
    callers exercise the same error surface the real loader produces.
    """

    async def _resolve(self: SshConnector, target: Any, operator: Any = None) -> dict[str, Any]:
        del self, operator  # registry lookup — identity plays no role in tests
        ref = getattr(target, "secret_ref", None)
        if not isinstance(ref, str) or not ref:
            raise VaultCredentialsReadError(
                f"target {getattr(target, 'name', target)!r} has no usable secret_ref: "
                f"expected a Vault KV-v2 path string, got {type(ref).__name__}"
            )
        data = secrets.get(ref)
        if data is None:
            raise VaultCredentialsReadError(f"no stubbed Vault secret registered at {ref!r}")
        return dict(data)

    with patch.object(SshConnector, "_resolve_secret", _resolve):
        yield secrets
