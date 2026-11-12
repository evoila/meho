# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``connector_auth_failed`` text names HTTP only for a real HTTP status (#6154).

A connector that does not use HTTP (a database login, for example) still
raises :class:`ConnectorAuthError` with a status code. Its error text must
say that the system refused the login and name the connector's own cause,
not "HTTP 401". An HTTP connector keeps the HTTP status in the text.
"""

from __future__ import annotations

from typing import Literal

import httpx
import pytest

from meho_backplane.connectors._shared.vcf_auth import (
    ConnectorAuthError,
    session_establish_auth_error,
)
from meho_backplane.operations._errors import result_connector_auth_failed


class _Target:
    name = "db-a"
    host = "db-a.example.invalid"
    secret_ref = "tenants/t1/db-a"


def _http_401() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api-a.example.invalid/api/session")
    response = httpx.Response(401, request=request, json={"message": "bad login"})
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return exc
    raise AssertionError("401 did not raise")  # pragma: no cover


_Stage = Literal["establish", "call", "after_relogin"]


def _not_http_error(stage: _Stage = "establish") -> ConnectorAuthError:
    return ConnectorAuthError(
        "The database refused the login of target 'db-a' (invalid username/password).",
        status_code=401,
        cause="db_login_refused",
        target_name="db-a",
        host="db-a.example.invalid",
        secret_ref="tenants/t1/db-a",
        remediation="Check the user name and password in the target's secret.",
        stage=stage,
        over_http=False,
    )


def test_http_connector_login_names_http_status() -> None:
    http_exc = _http_401()
    auth_err = session_establish_auth_error(http_exc, message="login failed", target=_Target())
    assert auth_err is not None
    assert auth_err.over_http is True

    out = result_connector_auth_failed("x.op", auth_err, _Target(), 1.0)

    assert out.error is not None
    assert "the login itself was rejected (HTTP 401)" in out.error
    assert out.extras["cause"] == "session_establish_401"
    assert out.extras["http_status"] == 401


def test_not_http_connector_login_says_the_system_refused_it() -> None:
    out = result_connector_auth_failed("db.op", _not_http_error(), _Target(), 1.0)

    assert out.error is not None
    assert "HTTP" not in out.error
    assert "login itself was rejected" not in out.error
    assert (
        "the system at db-a.example.invalid refused the login (cause db_login_refused)" in out.error
    )
    assert "Check the user name and password" in out.error
    # The connector's own cause and message stay as they are.
    assert out.extras["cause"] == "db_login_refused"
    assert out.extras["error_code"] == "connector_auth_failed"
    assert "invalid username/password" in out.extras["raw_message"]
    assert out.extras["upstream_message"] is None


@pytest.mark.parametrize("stage", ["call", "after_relogin"])
def test_not_http_connector_other_stages_name_no_http_status(stage: _Stage) -> None:
    out = result_connector_auth_failed("db.op", _not_http_error(stage), _Target(), 1.0)

    assert out.error is not None
    assert "HTTP" not in out.error
    assert "(cause db_login_refused)" in out.error
    assert out.extras["cause"] == "db_login_refused"


def test_connector_auth_error_is_http_by_default() -> None:
    err = ConnectorAuthError("m", status_code=403, cause="session_establish_403")
    assert err.over_http is True

    out = result_connector_auth_failed("x.op", err, _Target(), 1.0)

    assert out.error is not None
    assert "(HTTP 403)" in out.error
