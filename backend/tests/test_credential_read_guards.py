# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Reads that return credentials get the hand-written guards (#4617).

The op id is on ``broadcast.events._CREDENTIAL_READ_OPS`` (or declared by
its connector package), so the dispatcher replaces every secret-named
field in the answer with ``[REDACTED:param_name]`` before the caller sees
it. The SDDC Manager reads that first used this guard left the public tree
with evoila's own connectors (#3345).

``bind9.config.show`` is on the list. Its answer is one text field,
so the ``bind_key_secret`` text rule replaces the value of every
``secret "..."`` line (TSIG and rndc keys) and the rest of the file stays
readable. Since #5132 it reads only config files (no key files), so it
runs without approval.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

import meho_backplane.connectors.bind9  # noqa: F401 -- registers the connector at import
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.bind9 import BIND9_OPS
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.operations import (
    dispatch,
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.settings import get_settings

_REDACTED = "[REDACTED:param_name]"
# Assembled from fragments so gitleaks' built-in rules don't false-positive
# on the test source.
_FAKE_TSIG_VALUE = "exampleTsig" + "Value=="
_FAKE_BARE_TSIG_VALUE = "exampleBare" + "Value=="
_FAKE_OLD_TSIG_VALUE = "exampleOld" + "Value=="
_FAKE_PREVIOUS_TSIG_VALUE = "examplePrevious" + "Value=="
_FAKE_COOKIE_VALUE = "0123abcd" + "ef" * 12


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


def _make_operator() -> Operator:
    return Operator(
        sub="op-4617-test",
        name="Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-000000004617"),
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


# ---------------------------------------------------------------------------
# bind9.config.show -- both guards plus the BIND key text rule
# ---------------------------------------------------------------------------


def test_bind9_config_show_is_safe_without_approval() -> None:
    """#5132: the read refuses key files and hides key secrets, so it runs at once."""
    op = next(o for o in BIND9_OPS if o.op_id == "bind9.config.show")
    assert op.safety_level == "safe"
    assert op.requires_approval is False


def test_bind9_config_show_classifies_credential_read() -> None:
    assert classify_op("bind9.config.show") == "credential_read"


# A config BIND accepts, with the shapes a review found risky: a comment
# that holds the word ``secret`` and an unclosed ``/*`` before the first
# key block, a ``#`` inside a quoted string, a commented-out old key, and
# a ``*/`` with a quoted string after it at the end of the file.
_NAMED_CONF = (
    "# the secret /* of every key stays on the server\n"
    'key "rndc-key" {\n'
    "    algorithm hmac-sha256;\n"
    f'    secret /* secret "{_FAKE_PREVIOUS_TSIG_VALUE}"; */ "{_FAKE_TSIG_VALUE}";\n'
    "};\n"
    'key "transfer-key" {\n'
    "    algorithm hmac-sha256;\n"
    f"    secret {_FAKE_BARE_TSIG_VALUE};\n"
    "};\n"
    f'// key "old-key" {{ algorithm hmac-md5; secret "{_FAKE_OLD_TSIG_VALUE}"; }};\n'
    "options {\n"
    '    directory "/var/cache/bind";\n'
    '    version "none # hidden";\n'
    f"    cookie-secret {_FAKE_COOKIE_VALUE};\n"
    "    allow-transfer { key rndc-key; };\n"
    "};\n"
    '# */ "end of file"\n'
)

_NAMED_CONF_REDACTED = (
    _NAMED_CONF.replace(f'"{_FAKE_PREVIOUS_TSIG_VALUE}";', "[REDACTED:bind_key_secret];")
    .replace(f'"{_FAKE_TSIG_VALUE}";', "[REDACTED:bind_key_secret];")
    .replace(f"secret {_FAKE_BARE_TSIG_VALUE};", "secret [REDACTED:bind_key_secret];")
    .replace(f'secret "{_FAKE_OLD_TSIG_VALUE}";', "secret [REDACTED:bind_key_secret];")
    .replace(f"cookie-secret {_FAKE_COOKIE_VALUE};", "cookie-secret [REDACTED:bind_key_secret];")
)


def _assert_no_bind_secret(dumped: str) -> None:
    for value in (
        _FAKE_TSIG_VALUE,
        _FAKE_PREVIOUS_TSIG_VALUE,
        _FAKE_BARE_TSIG_VALUE,
        _FAKE_OLD_TSIG_VALUE,
        _FAKE_COOKIE_VALUE,
    ):
        assert value not in dumped


class _NoOpBind9Connector(Connector):
    """Connector class satisfying the resolver lookup for the typed stand-in."""

    product = "bind9"
    version = "9.x"
    impl_id = "bind9-ssh"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self,
        target: Any,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        raise NotImplementedError


async def _bind9_config_show_handler(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Stand-in for the real handler: the file text with a key secret in it."""
    return {"file": "/etc/bind/named.conf", "content": _NAMED_CONF}


async def _register_bind9_config_show(embedding_service: AsyncMock) -> None:
    register_connector_v2(product="bind9", version="", impl_id="", cls=_NoOpBind9Connector)
    await register_typed_operation(
        product="bind9",
        version="9.x",
        impl_id="bind9-ssh",
        op_id="bind9.config.show",
        handler=_bind9_config_show_handler,
        summary="Stand-in for the config read.",
        description="Returns a config file that holds a key secret.",
        parameter_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        when_to_use=None,
        embedding_service=embedding_service,
    )


async def test_bind9_config_show_key_secret_is_redacted(
    stub_embedding_service: AsyncMock,
) -> None:
    """Every key value becomes the placeholder; the rest of the file is unchanged.

    The file holds a quoted secret, a secret without quotes, a
    ``cookie-secret``, and an old secret in a comment between the
    ``secret`` keyword and its value. BIND accepts all of them.
    """
    await _register_bind9_config_show(stub_embedding_service)

    result = await dispatch(
        operator=_make_operator(),
        connector_id="bind9-ssh-9.x",
        op_id="bind9.config.show",
        target=None,
        params={"path": "named.conf"},
    )
    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result["file"] == "/etc/bind/named.conf"
    assert result.result["content"] == _NAMED_CONF_REDACTED
    _assert_no_bind_secret(result.model_dump_json())


async def test_bind9_config_show_key_secret_stays_redacted_with_reveal_secret(
    stub_embedding_service: AsyncMock,
) -> None:
    """``reveal_secret`` does not bring any key secret back: the text rule still runs."""
    await _register_bind9_config_show(stub_embedding_service)

    result = await dispatch(
        operator=_make_operator(),
        connector_id="bind9-ssh-9.x",
        op_id="bind9.config.show",
        target=None,
        params={"path": "named.conf", "reveal_secret": True},
    )
    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result["content"] == _NAMED_CONF_REDACTED
    _assert_no_bind_secret(result.model_dump_json())
