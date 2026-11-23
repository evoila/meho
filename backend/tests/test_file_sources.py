# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Named file sources: the address check and the connect-time screen."""

from __future__ import annotations

import ipaddress

import meho_connector_kit as kit
import pytest

from meho_backplane.connectors._shared import dial_guard
from meho_backplane.connectors._shared.dial_guard import screen_dial_addresses
from meho_backplane.connectors._shared.file_sources import (
    FileSource,
    FileSourceError,
    file_source_resolver,
    file_sources_from_hosts,
    file_sources_from_map,
    resolve_file_source,
)
from meho_backplane.targets import ssrf_guard
from meho_backplane.targets.ssrf_guard import (
    TargetDestinationBlockedError,
    assert_public_destination,
    screen_and_resolve,
)

BASE = "https://files.example.com/isos"
SOURCES = file_sources_from_map({"isos": BASE})


def _rule(url: str, sources: list[FileSource] = SOURCES) -> str:
    with pytest.raises(FileSourceError) as caught:
        resolve_file_source(url, sources)
    return caught.value.rule


# --- accepted -------------------------------------------------------------


def test_address_under_map_source_is_accepted() -> None:
    source = resolve_file_source(f"{BASE}/a/b.iso?token=abc", SOURCES)
    assert source.name == "isos"
    assert source.base.port == 443
    assert source.private_network_allowed is False


def test_address_under_host_list_source_is_accepted() -> None:
    sources = file_sources_from_hosts(["files.example.com", "mirror.example.com:8443"])
    assert resolve_file_source("https://files.example.com/x/y.bin", sources).name == (
        "files.example.com"
    )
    assert resolve_file_source("https://MIRROR.example.com:8443/y.bin", sources).base.port == 8443


def test_longest_path_prefix_wins() -> None:
    sources = file_sources_from_map({"all": "https://files.example.com", "isos": BASE + "/"})
    assert resolve_file_source(f"{BASE}/x.iso", sources).name == "isos"
    assert resolve_file_source("https://files.example.com/other", sources).name == "all"


# --- refused --------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "rule"),
    [
        (f"{BASE}/../secret", "path_dots"),
        (f"{BASE}/./x", "path_dots"),
        (f"{BASE}/%2e%2e/secret", "path_dots"),
        (f"{BASE}/%252e%252e/secret", "path_dots"),
        (f"{BASE}/%25252e%25252e/secret", "path_dots"),
        (f"{BASE}/..;x/secret", "path_dots"),
        (f"{BASE}/a/%2e%2e%2fsecret", "path_dots"),
        (f"{BASE}/a/..%5csecret", "path_dots"),
        (f"{BASE}/a%zz", "path_undecodable"),
        (f"{BASE}/a%", "path_undecodable"),
        (f"{BASE}/%ff", "path_undecodable"),
        (
            f"{BASE}/" + "%25" * 8 + "41",
            "path_undecodable",
        ),
        ("https://user@files.example.com/isos/a", "userinfo"),
        ("https://user:pw@files.example.com/isos/a", "userinfo"),
        (f"{BASE}/a#part", "fragment"),
        (f"{BASE}/a#", "fragment"),
        ("https://files.example.com:8443/isos/a", "no_source"),
        ("http://files.example.com/isos/a", "no_source"),
        ("https://other.example.com/isos/a", "no_source"),
        ("https://files.example.com/isos-evil/a", "path_outside_source"),
        ("https://files.example.com/isosx", "path_outside_source"),
        ("https://files.example.com/", "path_outside_source"),
        ("ftp://files.example.com/isos/a", "scheme"),
        ("file:///etc/passwd", "scheme"),
        ("//files.example.com/isos/a", "scheme"),
        ("https:///isos/a", "unparseable"),
        ("https://files.example.com:99999/isos/a", "unparseable"),
        (f"{BASE}/a b", "unparseable"),
        ("", "unparseable"),
    ],
)
def test_refused_before_any_connection(url: str, rule: str) -> None:
    assert _rule(url) == rule


def test_no_sources_refuses_everything() -> None:
    assert _rule(f"{BASE}/a", []) == "no_source"


@pytest.mark.parametrize(
    "entries",
    [["https://files.example.com"], ["files.example.com/x"], ["u@files.example.com"], [""]],
)
def test_host_list_reader_refuses_non_hosts(entries: list[str]) -> None:
    with pytest.raises(FileSourceError) as caught:
        file_sources_from_hosts(entries)
    assert caught.value.rule == "bad_source"


@pytest.mark.parametrize(
    "address",
    [
        "ftp://files.example.com/x",
        "https://u:p@files.example.com/",
        "https://files.example.com/a/../b",
        "https://files.example.com/?q=1",
        "not a url",
    ],
)
def test_map_reader_refuses_bad_base(address: str) -> None:
    with pytest.raises(FileSourceError) as caught:
        file_sources_from_map({"x": address})
    assert caught.value.rule == "bad_source"


def test_error_text_never_holds_the_address_or_query() -> None:
    needle = "needle-value"
    urls = [
        f"https://user:{needle}@files.example.com/isos/a",
        f"https://files.example.com:8443/isos/a?token={needle}",
        f"https://other.example.com/isos/%252e%252e/a?token={needle}",
        f"https://files.example.com/isos/a?token={needle}#frag",
        f"ftp://files.example.com/isos/a?token={needle}",
        f"https://files.example.com/other/a?token={needle}",
        f"https://files.example.com/isos/a%zz?token={needle}",
    ]
    for url in urls:
        with pytest.raises(FileSourceError) as caught:
            resolve_file_source(url, SOURCES)
        text = f"{caught.value} {caught.value!r} {caught.value.args}"
        assert needle not in text
        assert "files.example.com" not in text
        assert "token" not in text


# --- address guard --------------------------------------------------------


def _resolving_to(monkeypatch: pytest.MonkeyPatch, *addresses: str) -> None:
    monkeypatch.setattr(
        ssrf_guard, "_resolve_addrs", lambda host: [ipaddress.ip_address(a) for a in addresses]
    )


@pytest.fixture(autouse=True)
def _no_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ssrf_guard.TARGET_SSRF_ALLOWLIST_ENV, raising=False)


def test_private_address_is_refused_unless_the_source_allows_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = file_sources_from_map({"a": BASE})[0]
    open_ = file_sources_from_map({"a": BASE}, private_network_allowed=True)[0]
    for private in ("192.168.1.5", "10.0.0.7", "172.16.4.4", "fd00::5"):
        _resolving_to(monkeypatch, private)
        with pytest.raises(TargetDestinationBlockedError):
            file_source_resolver(closed)("files.example.com", 443)
        assert file_source_resolver(open_)("files.example.com", 443) == [private]


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "::1", "169.254.169.254", "169.254.1.1", "fe80::1", "100.64.0.1", "0.0.0.0"],
)
def test_loopback_link_local_and_metadata_are_refused_even_when_private_is_allowed(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    open_ = file_sources_from_map({"a": BASE}, private_network_allowed=True)[0]
    _resolving_to(monkeypatch, address)
    with pytest.raises(TargetDestinationBlockedError):
        file_source_resolver(open_)("files.example.com", 443)


def test_one_private_answer_among_public_ones_needs_the_source_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _resolving_to(monkeypatch, "93.184.216.34", "192.168.1.5")
    with pytest.raises(TargetDestinationBlockedError):
        screen_and_resolve("files.example.com", 443)
    assert screen_and_resolve("files.example.com", 443, allow_private=True) == [
        "93.184.216.34",
        "192.168.1.5",
    ]


def test_public_address_passes_for_every_source(monkeypatch: pytest.MonkeyPatch) -> None:
    _resolving_to(monkeypatch, "93.184.216.34")
    closed = file_sources_from_map({"a": BASE})[0]
    assert file_source_resolver(closed)("files.example.com", 443) == ["93.184.216.34"]


# --- other callers of the guard are unchanged -----------------------------


@pytest.mark.parametrize("address", ["10.0.0.7", "192.168.1.5", "172.16.4.4", "fd00::5"])
def test_existing_callers_still_refuse_private_addresses(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    _resolving_to(monkeypatch, address)
    with pytest.raises(TargetDestinationBlockedError):
        assert_public_destination("files.example.com")
    with pytest.raises(TargetDestinationBlockedError):
        screen_and_resolve("files.example.com", 443)
    with pytest.raises(TargetDestinationBlockedError):
        screen_dial_addresses("files.example.com", 443)
    assert dial_guard.screen_dial_addresses is screen_dial_addresses


def test_source_flag_does_not_change_the_install_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _resolving_to(monkeypatch, "192.168.1.5")
    open_ = file_sources_from_map({"a": BASE}, private_network_allowed=True)[0]
    assert file_source_resolver(open_)("files.example.com", 443) == ["192.168.1.5"]
    with pytest.raises(TargetDestinationBlockedError):
        assert_public_destination("files.example.com")


# --- the kit --------------------------------------------------------------


def test_kit_offers_the_file_source_names() -> None:
    assert kit.resolve_file_source is resolve_file_source
    assert kit.FileSource is FileSource
    assert kit.FileSourceError is FileSourceError
    assert kit.file_sources_from_map is file_sources_from_map
    assert kit.file_sources_from_hosts is file_sources_from_hosts
