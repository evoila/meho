# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Regression tests for the frontend nginx deploy-time contract.

The frontend container serves a stable-filename runtime config at /config.js
that carries KEYCLOAK_CLIENT_ID, API_URL, KEYCLOAK_URL, and KEYCLOAK_REALM.
Because the filename does not change across deploys, the response must never
be persisted in client caches; a 1-year immutable rule would silently pin
returning evaluators to a stale client id.

These tests guard the nginx contract for both the active template served by
Docker (``nginx.conf.template``) and the fallback served in local/non-Docker
runs (``nginx.conf``). They parse the files as text rather than booting nginx
so the suite stays fast, deterministic, and free of container dependencies.

See docs/codebase/first-run-experience.md ``Runtime-config propagation``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

CONF_FILES = (
    REPO_ROOT / "nginx.conf",
    REPO_ROOT / "nginx.conf.template",
)

CONFIG_JS_BLOCK_HEADER = "location = /config.js {"
WILDCARD_ASSET_BLOCK_HEADER_RE = re.compile(
    r"location\s+~\*\s+\\\.\(js\|css\|png\|jpg\|jpeg\|gif\|ico\|svg\|woff\|woff2\|ttf\|eot\)\$\s*\{"
)

REQUIRED_SECURITY_HEADERS = (
    "X-Frame-Options",
    "X-Content-Type-Options",
    "X-XSS-Protection",
    "Referrer-Policy",
    "Strict-Transport-Security",
    "Permissions-Policy",
    "Content-Security-Policy-Report-Only",
)


def _extract_block(text: str, opening_line: str) -> str:
    """Return the body of the first nginx block whose opening line matches.

    Brace-balanced scan starting at ``opening_line``. Raises if the block is
    missing or unbalanced so test failures surface a specific reason rather
    than a generic empty-string substring miss.
    """
    start = text.find(opening_line)
    if start == -1:
        raise AssertionError(f"missing block: {opening_line!r}")
    depth = 0
    i = text.find("{", start)
    assert i != -1, f"no opening brace after {opening_line!r}"
    for idx in range(i, len(text)):
        ch = text[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1 : idx]
    raise AssertionError(f"unterminated block: {opening_line!r}")


def _extract_wildcard_block(text: str) -> str:
    match = WILDCARD_ASSET_BLOCK_HEADER_RE.search(text)
    if match is None:
        raise AssertionError("missing wildcard static-asset block")
    opening_line = match.group(0)
    return _extract_block(text, opening_line)


@pytest.mark.parametrize("conf_path", CONF_FILES, ids=lambda p: p.name)
class TestConfigJsNoStore:
    """Structural contract for the /config.js location block."""

    def test_block_exists(self, conf_path: Path) -> None:
        text = conf_path.read_text()
        assert CONFIG_JS_BLOCK_HEADER in text, (
            f"{conf_path.name} is missing the `location = /config.js` block. "
            "Returning evaluators would cache runtime config for one year."
        )

    def test_block_declares_no_store(self, conf_path: Path) -> None:
        block = _extract_block(conf_path.read_text(), CONFIG_JS_BLOCK_HEADER)
        assert 'add_header Cache-Control "no-store"' in block, (
            f"{conf_path.name} /config.js block must set Cache-Control: no-store"
        )

    def test_block_does_not_declare_immutable(self, conf_path: Path) -> None:
        block = _extract_block(conf_path.read_text(), CONFIG_JS_BLOCK_HEADER)
        assert "immutable" not in block, (
            f"{conf_path.name} /config.js block must not contain `immutable` — "
            "that is the precise bug this regression guards against."
        )

    @pytest.mark.parametrize("header", REQUIRED_SECURITY_HEADERS)
    def test_block_repeats_security_header(self, conf_path: Path, header: str) -> None:
        block = _extract_block(conf_path.read_text(), CONFIG_JS_BLOCK_HEADER)
        pattern = re.compile(rf'add_header\s+{re.escape(header)}\s+"[^"]*"\s+always\s*;')
        assert pattern.search(block), (
            f"{conf_path.name} /config.js block must declare {header} with the "
            "`always` qualifier so it applies to error responses too. nginx "
            "silently removes ALL parent add_header directives when a child "
            "location defines any of its own; every security header must be "
            "repeated here, and `always` is what makes them fire on 4xx/5xx."
        )


@pytest.mark.parametrize("conf_path", CONF_FILES, ids=lambda p: p.name)
class TestWildcardAssetBlock:
    """The wildcard .js|.css|... block must keep its 1y immutable contract."""

    def test_wildcard_keeps_immutable(self, conf_path: Path) -> None:
        block = _extract_wildcard_block(conf_path.read_text())
        assert 'add_header Cache-Control "public, immutable"' in block, (
            f"{conf_path.name} wildcard block regressed off public, immutable. "
            "Hashed Vite bundle assets carry content-addressed filenames and "
            "should remain cached for one year."
        )

    def test_wildcard_cache_control_excludes_always(self, conf_path: Path) -> None:
        """Cache-Control must NOT carry `always` on the wildcard block.

        With `always`, the immutable directive attaches to 4xx/5xx responses
        too — a 404 for a stale hashed asset URL during a partial rollout
        would be cached as immutable by CDNs and clients that respect RFC
        8246. Security headers below this line keep `always` because they
        should fire on error responses; Cache-Control specifically must not.
        """
        block = _extract_wildcard_block(conf_path.read_text())
        bad = re.compile(r'add_header\s+Cache-Control\s+"[^"]*"\s+always\s*;')
        assert not bad.search(block), (
            f"{conf_path.name} wildcard block has `always` on its "
            "Cache-Control directive. That makes the `public, immutable` "
            "rule apply to 4xx/5xx responses, which CDNs will then cache. "
            "Drop `always` from Cache-Control only; keep it on every "
            "security header."
        )

    def test_wildcard_still_has_expires(self, conf_path: Path) -> None:
        block = _extract_wildcard_block(conf_path.read_text())
        assert "expires 1y" in block


@pytest.mark.parametrize("conf_path", CONF_FILES, ids=lambda p: p.name)
def test_config_js_block_precedes_wildcard(conf_path: Path) -> None:
    """More-specific rule before the wildcard is canonical nginx style.

    Correctness does not depend on this — exact-match short-circuits regex
    locations — but keeping the file readable top-down matches evaluation
    order and helps reviewers catch future edits that move the wildcard
    above the exact block.
    """
    text = conf_path.read_text()
    config_js_pos = text.find(CONFIG_JS_BLOCK_HEADER)
    wildcard_match = WILDCARD_ASSET_BLOCK_HEADER_RE.search(text)
    assert config_js_pos != -1
    assert wildcard_match is not None
    assert config_js_pos < wildcard_match.start(), (
        f"{conf_path.name}: place `location = /config.js` before the "
        "wildcard regex block so the file reads in evaluation order."
    )
