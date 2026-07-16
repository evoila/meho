# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the backend-agnostic credential-resolution seam.

These pin the pure routing layer in
:mod:`meho_backplane.connectors._shared.credential_backend` — the scheme
split, the kind-keyed registry, and the unknown-kind error — independent
of any store read. The Vault-backed dispatch (schemeless / ``vault:`` →
today's KV-v2 read) and the unknown-scheme rejection *through the loader*
are covered in ``test_connectors_vault_creds.py``.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.credential_backend import (
    CREDENTIAL_BACKEND_REGISTRY,
    CredentialBackend,
    UnknownCredentialBackendError,
    register_credential_backend,
    resolve_credential_backend,
    split_credential_ref,
)


class _FakeBackend:
    """A minimal :class:`CredentialBackend` for registry tests."""

    async def load_secret_data(
        self,
        secret_ref: str,
        operator: Operator,
        *,
        target_name: str,
        mount: str,
    ) -> dict[str, object]:
        return {"seen_ref": secret_ref}


@pytest.fixture
def _clean_kind() -> Iterator[str]:
    """Yield a throwaway registry kind and remove it after the test.

    The registry is process-global (populated at import time by every
    backend module), so a test that registers a kind must clean up or it
    poisons sibling tests with a duplicate-registration ``ValueError``.
    """
    kind = "test-fake-backend"
    try:
        yield kind
    finally:
        CREDENTIAL_BACKEND_REGISTRY.pop(kind, None)


# ---------------------------------------------------------------------------
# Scheme split
# ---------------------------------------------------------------------------


def test_schemeless_ref_resolves_through_default_backend() -> None:
    """A bare KV-v2 path has no scheme → the deployment default backend."""
    assert split_credential_ref("targets/vc-01", default_backend="vault") == (
        "vault",
        "targets/vc-01",
    )


def test_explicit_vault_scheme_is_split_off() -> None:
    """``vault:targets/vc-01`` → kind ``vault`` + the schemeless remainder."""
    assert split_credential_ref("vault:targets/vc-01", default_backend="vault") == (
        "vault",
        "targets/vc-01",
    )


def test_gsm_scheme_is_split_off_with_field_fragment() -> None:
    """An unregistered scheme still splits; the remainder keeps its ``#field``."""
    assert split_credential_ref("gsm:proj/secret#pw", default_backend="vault") == (
        "gsm",
        "proj/secret#pw",
    )


def test_default_backend_selects_schemeless_target() -> None:
    """``config.credentialBackend`` (the *default_backend* arg) routes schemeless refs.

    Proves AC #4 at the split layer: with the default set to ``gsm``, a
    schemeless ref resolves through ``gsm`` — a Vault install (default
    ``vault``) keeps routing schemeless refs to Vault, so the setting is a
    no-op there.
    """
    assert split_credential_ref("targets/vc-01", default_backend="gsm") == (
        "gsm",
        "targets/vc-01",
    )


@pytest.mark.parametrize(
    "ref",
    [
        "vsphere/vcenter-a:8443",  # colon deeper in a slashed segment
        "targets/data-center-01",  # no colon at all
        "vault:",  # empty remainder — not a scheme split
        ":targets/x",  # empty kind — not a scheme split
    ],
)
def test_non_scheme_colons_stay_schemeless(ref: str) -> None:
    """A colon that isn't a ``<bare-token>:<non-empty>`` prefix is left in place.

    The split treats a colon as a scheme separator only when the segment
    before it is a bare scheme token (leading letter, no slash) *and* the
    remainder is non-empty. Anything else resolves through the default
    backend with the ref passed through verbatim, so a logical KV-v2 path
    that happens to contain a colon is never mis-split.
    """
    assert split_credential_ref(ref, default_backend="vault") == ("vault", ref)


# ---------------------------------------------------------------------------
# Registry: register / resolve / unknown
# ---------------------------------------------------------------------------


def test_vault_kind_is_registered_at_import() -> None:
    """Importing the credential path registers the built-in ``vault`` backend."""
    # ``vault_creds`` registers ``"vault"`` at import; importing the loader
    # module here guarantees the side effect has run.
    import meho_backplane.connectors._shared.vault_creds  # noqa: F401

    backend = resolve_credential_backend("vault")
    assert isinstance(backend, CredentialBackend)


def test_register_and_resolve_roundtrip(_clean_kind: str) -> None:
    """A registered backend resolves back by its kind."""
    backend = _FakeBackend()
    register_credential_backend(_clean_kind, backend)
    assert resolve_credential_backend(_clean_kind) is backend


def test_duplicate_registration_raises(_clean_kind: str) -> None:
    """Re-registering a kind is a wiring bug → ``ValueError``, not silent shadow."""
    register_credential_backend(_clean_kind, _FakeBackend())
    with pytest.raises(ValueError, match="already registered"):
        register_credential_backend(_clean_kind, _FakeBackend())


def test_resolve_unknown_kind_raises_with_actionable_message() -> None:
    """An unregistered kind raises naming the kind and the registered kinds."""
    with pytest.raises(UnknownCredentialBackendError) as exc:
        resolve_credential_backend("definitely-not-registered")

    msg = str(exc.value)
    assert "no credential backend registered for kind 'definitely-not-registered'" in msg
    # The registered kinds are listed so the misconfig is fixable at a glance.
    assert "vault" in msg


def test_fake_backend_satisfies_protocol_structurally() -> None:
    """The runtime-checkable Protocol accepts any class with ``load_secret_data``."""
    assert isinstance(_FakeBackend(), CredentialBackend)
