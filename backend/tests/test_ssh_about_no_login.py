# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An SSH ``about`` read with no login says "no login", not "unreachable".

A system-started call (a Sensor on an install with no check-runner login)
carries no token. Reading the target's secret then fails with
``OperatorTokenRequiredError``. Each SSH connector's ``fingerprint`` turns
that into ``reachable=False``; the shared ``_assert_reachable`` guard must
still name the missing login. A real network failure stays "unreachable".
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest

from meho_backplane.connectors._shared.vault_creds import (
    OperatorTokenRequiredError,
    VaultCredentialsReadError,
)
from meho_backplane.connectors.adapters.ssh import (
    ConnectorLoginRequiredError,
    ConnectorUnreachableError,
    SshConnector,
)
from meho_backplane.connectors.bind9 import Bind9Connector
from meho_backplane.connectors.hyperv import HypervConnector
from meho_backplane.connectors.linux import LinuxSshConnector
from meho_backplane.connectors.msad import MsadConnector
from meho_backplane.connectors.pfsense import PfSenseConnector
from meho_backplane.connectors.rke2 import Rke2SshConnector
from meho_backplane.connectors.windows_dns import WindowsDnsConnector
from meho_backplane.connectors.winsrv import WinsrvConnector
from meho_backplane.connectors.wsfc import WsfcConnector

_CONNECTORS: list[type[SshConnector]] = [
    Bind9Connector,
    HypervConnector,
    LinuxSshConnector,
    MsadConnector,
    PfSenseConnector,
    Rke2SshConnector,
    WindowsDnsConnector,
    WinsrvConnector,
    WsfcConnector,
]

_NO_TOKEN_TEXT = (
    "operator-context credential read requires an authenticated operator; "
    "target='t' has no operator JWT"
)


@dataclass
class _Target:
    name: str = "t"
    host: str = "host.test.invalid"
    port: int | None = 22
    secret_ref: str = "meho/testing/t"


@pytest.mark.parametrize("cls", _CONNECTORS, ids=lambda c: c.__name__)
async def test_about_with_no_token_says_no_login(cls: type[SshConnector]) -> None:
    connector = cls()
    boom = AsyncMock(side_effect=OperatorTokenRequiredError(_NO_TOKEN_TEXT))
    with (
        patch.object(SshConnector, "_run_command", boom),
        pytest.raises(ConnectorLoginRequiredError, match="no login") as info,
    ):
        await connector.about(_Target(), {})
    assert not isinstance(info.value, ConnectorUnreachableError)
    assert "unreachable" not in str(info.value)


@pytest.mark.parametrize("cls", _CONNECTORS, ids=lambda c: c.__name__)
async def test_about_network_failure_still_unreachable(cls: type[SshConnector]) -> None:
    connector = cls()
    boom = AsyncMock(side_effect=OSError("Connection refused"))
    with (
        patch.object(SshConnector, "_run_command", boom),
        pytest.raises(ConnectorUnreachableError, match="unreachable"),
    ):
        await connector.about(_Target(), {})


async def test_other_credential_read_failure_still_unreachable() -> None:
    """A failed secret read that is not a missing login keeps the old error."""
    boom = AsyncMock(side_effect=VaultCredentialsReadError("vault said no"))
    with (
        patch.object(SshConnector, "_run_command", boom),
        pytest.raises(ConnectorUnreachableError),
    ):
        await LinuxSshConnector().about(_Target(), {})


async def test_probe_answer_for_no_login_is_unchanged() -> None:
    """``fingerprint`` still returns ``reachable=False``; only ``about`` raises."""
    boom = AsyncMock(side_effect=OperatorTokenRequiredError(_NO_TOKEN_TEXT))
    with patch.object(SshConnector, "_run_command", boom):
        result = await LinuxSshConnector().fingerprint(_Target())
    assert result.reachable is False
    assert "no operator JWT" in result.extras["error"]
