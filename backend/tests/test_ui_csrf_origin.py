# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The origin check of the console CSRF middleware, on the headers alone."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from meho_backplane.ui import csrf


def _scope(**headers: str) -> dict[str, object]:
    return {
        "headers": [(k.lower().encode("ascii"), v.encode("latin-1")) for k, v in headers.items()]
    }


@pytest.fixture
def own_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        csrf, "get_settings", lambda: SimpleNamespace(backplane_url="https://meho.example.test")
    )


@pytest.mark.parametrize(
    ("headers", "refused"),
    [
        ({"Origin": "https://meho.example.test"}, False),
        ({"Origin": "https://MEHO.example.test"}, False),
        ({"Origin": "https://sibling.example.test"}, True),
        ({"Origin": "https://meho.example.test.evil.test"}, True),
        ({"Origin": "https://sibling.example.test", "Sec-Fetch-Site": "same-origin"}, False),
        ({"Origin": "https://meho.example.test", "Sec-Fetch-Site": "cross-site"}, True),
        ({"Sec-Fetch-Site": "none"}, False),
        ({}, False),
    ],
)
def test_origin_is_compared_with_the_backplane_url(
    own_url: None, headers: dict[str, str], refused: bool
) -> None:
    assert csrf._from_another_origin(_scope(**headers)) is refused


def test_origin_of_the_request_host_is_own_origin(own_url: None) -> None:
    scope = _scope(Origin="https://internal.example.test:8443", Host="internal.example.test:8443")
    assert csrf._from_another_origin(scope) is False
