# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the global request-body cap middleware.

A minimal FastAPI app carries only :class:`RequestBodyLimitMiddleware`
and one route that reports how many bytes it read. The cap is
monkeypatched to a few dozen bytes so the tests stay small.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Form, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel

from meho_backplane.request_body_limit import RequestBodyLimitMiddleware

_CAP = 64


class _Item(BaseModel):
    name: str


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(
        "meho_backplane.request_body_limit.get_settings",
        lambda: SimpleNamespace(http_max_request_body_bytes=_CAP),
    )
    app = FastAPI()
    app.add_middleware(RequestBodyLimitMiddleware)
    calls: list[int] = []
    app.state.calls = calls

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        body = await request.body()
        calls.append(len(body))
        return {"bytes": len(body)}

    @app.post("/item")
    async def item(payload: _Item) -> dict[str, int]:
        calls.append(len(payload.name))
        return {"bytes": len(payload.name)}

    @app.post("/form")
    async def form(name: str = Form()) -> dict[str, int]:
        calls.append(len(name))
        return {"bytes": len(name)}

    with TestClient(app) as test_client:
        yield test_client


def test_body_under_cap_passes(client: TestClient) -> None:
    response = client.post("/echo", content=b"x" * _CAP)
    assert response.status_code == 200
    assert response.json() == {"bytes": _CAP}


def test_declared_length_over_cap_is_refused_before_the_handler(client: TestClient) -> None:
    response = client.post("/echo", content=b"x" * (_CAP + 1))
    assert response.status_code == 413
    assert response.json() == {"detail": f"request body exceeds {_CAP} bytes"}
    assert client.app.state.calls == []


def test_chunked_body_over_cap_is_refused_while_reading(client: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        yield b"x" * 40
        yield b"x" * 40

    response = client.post("/echo", content=chunks())
    assert response.status_code == 413
    assert client.app.state.calls == []


def test_chunked_body_under_cap_passes(client: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        yield b"x" * 20
        yield b"x" * 20

    response = client.post("/echo", content=chunks())
    assert response.status_code == 200
    assert response.json() == {"bytes": 40}


def test_body_less_request_passes(client: TestClient) -> None:
    response = client.post("/echo")
    assert response.status_code == 200
    assert response.json() == {"bytes": 0}


def test_declared_json_body_over_cap_is_refused_with_413(client: TestClient) -> None:
    """FastAPI's parsing of a declared JSON body keeps the 413, not its generic 400."""

    def chunks() -> Iterator[bytes]:
        yield b'{"name": "'
        yield b"x" * _CAP
        yield b'"}'

    response = client.post("/item", content=chunks(), headers={"content-type": "application/json"})
    assert response.status_code == 413
    assert response.json() == {"detail": f"request body exceeds {_CAP} bytes"}
    assert client.app.state.calls == []


def test_declared_json_body_under_cap_passes(client: TestClient) -> None:
    response = client.post("/item", json={"name": "ok"})
    assert response.status_code == 200
    assert response.json() == {"bytes": 2}


def test_declared_form_body_over_cap_is_refused_with_413(client: TestClient) -> None:
    """FastAPI's parsing of a declared form body keeps the 413 too."""

    def chunks() -> Iterator[bytes]:
        yield b"name="
        yield b"x" * _CAP

    response = client.post(
        "/form",
        content=chunks(),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 413
    assert response.json() == {"detail": f"request body exceeds {_CAP} bytes"}
    assert client.app.state.calls == []


def test_body_less_request_never_reads_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """A request without body bytes needs no settings, so the app starts without them."""

    def no_settings() -> SimpleNamespace:
        raise KeyError("KEYCLOAK_ISSUER_URL")

    monkeypatch.setattr("meho_backplane.request_body_limit.get_settings", no_settings)
    app = FastAPI()
    app.add_middleware(RequestBodyLimitMiddleware)

    @app.get("/")
    async def root() -> dict[str, str]:
        return {"name": "meho-backplane"}

    with TestClient(app) as test_client:
        assert test_client.get("/").status_code == 200
        assert test_client.post("/", content=b"").status_code == 405
