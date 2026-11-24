# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Named file sources: the address check and the connect-time screen."""

from __future__ import annotations

import ipaddress
import traceback

import httpx
import meho_connector_kit as kit
import pytest

from meho_backplane.connectors._shared import dial_guard
from meho_backplane.connectors._shared.dial_guard import screen_dial_addresses
from meho_backplane.connectors._shared.file_sources import (
    FileSource,
    FileSourceError,
    file_source_resolver,
    file_source_transport,
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
        ("https://files.example.com/isos%2Fa.iso", "path_outside_source"),
        ("https://files.example.com/isos%252Fa.iso", "path_outside_source"),
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
        with pytest.raises(FileSourceError, match="private_network_allowed"):
            file_source_resolver(closed)("files.example.com", 443)
        assert file_source_resolver(open_)("files.example.com", 443) == [private]


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "::1",
        "169.254.169.254",
        "169.254.1.1",
        "fe80::1",
        "100.64.0.1",
        "0.0.0.0",
        "fd00:ec2::254",
        "fd00:ec2::23",
    ],
)
def test_loopback_link_local_and_metadata_are_refused_even_when_private_is_allowed(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    open_ = file_sources_from_map({"a": BASE}, private_network_allowed=True)[0]
    _resolving_to(monkeypatch, address)
    with pytest.raises(FileSourceError) as caught:
        file_source_resolver(open_)("files.example.com", 443)
    assert caught.value.rule == "address_blocked"


def test_one_private_answer_among_public_ones_needs_the_source_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _resolving_to(monkeypatch, "93.184.216.34", "192.168.1.5")
    closed = file_sources_from_map({"a": BASE})[0]
    open_ = file_sources_from_map({"a": BASE}, private_network_allowed=True)[0]
    with pytest.raises(FileSourceError):
        file_source_resolver(closed)("files.example.com", 443)
    assert file_source_resolver(open_)("files.example.com", 443) == [
        "93.184.216.34",
        "192.168.1.5",
    ]


@pytest.mark.parametrize(
    "listed",
    ["169.254.0.0/16", "169.254.169.254", "127.0.0.0/8", "fd00:ec2::/32", "10.0.0.0/8"],
)
@pytest.mark.parametrize("private_allowed", [False, True])
def test_a_listed_network_never_opens_a_blocked_address_for_a_file_source(
    monkeypatch: pytest.MonkeyPatch, listed: str, private_allowed: bool
) -> None:
    monkeypatch.setenv(ssrf_guard.TARGET_SSRF_ALLOWLIST_ENV, listed)
    source = file_sources_from_map({"a": BASE}, private_network_allowed=private_allowed)[0]
    network = ipaddress.ip_network(listed, strict=False)
    blocked = str(network.network_address + 1) if network.num_addresses > 1 else str(network[0])
    if private_allowed and blocked.startswith("10."):
        blocked = "169.254.169.254"
    _resolving_to(monkeypatch, blocked)
    with pytest.raises(FileSourceError) as caught:
        file_source_resolver(source)("files.example.com", 443)
    assert caught.value.rule == "address_blocked"


def test_a_listed_host_name_is_still_resolved_and_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ssrf_guard.TARGET_SSRF_ALLOWLIST_ENV, "files.example.com")
    source = file_sources_from_map({"a": BASE}, private_network_allowed=True)[0]
    _resolving_to(monkeypatch, "169.254.169.254")
    with pytest.raises(FileSourceError) as caught:
        file_source_resolver(source)("files.example.com", 443)
    assert caught.value.rule == "address_blocked"
    # A public answer is dialed by its checked address, never by the name.
    _resolving_to(monkeypatch, "93.184.216.34")
    assert file_source_resolver(source)("files.example.com", 443) == ["93.184.216.34"]


def test_a_host_name_that_does_not_resolve_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _resolving_to(monkeypatch)
    source = file_sources_from_map({"a": BASE})[0]
    with pytest.raises(FileSourceError) as caught:
        file_source_resolver(source)("files.example.com", 443)
    assert caught.value.rule == "address_blocked"


@pytest.mark.parametrize(
    ("host", "port"),
    [
        ("other.example.com", 443),
        ("files.example.com", 8443),
        ("10.0.0.7", 443),
        ("files.example.com.evil.example", 443),
    ],
)
def test_a_connection_to_another_host_or_port_is_refused(
    monkeypatch: pytest.MonkeyPatch, host: str, port: int
) -> None:
    _resolving_to(monkeypatch, "93.184.216.34")
    source = file_sources_from_map({"a": BASE}, private_network_allowed=True)[0]
    with pytest.raises(FileSourceError) as caught:
        file_source_resolver(source)(host, port)
    assert caught.value.rule == "other_host"


def test_the_source_host_matches_in_any_case_and_with_a_trailing_dot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _resolving_to(monkeypatch, "93.184.216.34")
    source = file_sources_from_map({"a": "https://Files.Example.com:8443/x"})[0]
    assert file_source_resolver(source)("files.example.com.", 8443) == ["93.184.216.34"]
    v6 = file_sources_from_map({"a": "https://[2001:db8::1]/"})[0]
    with pytest.raises(FileSourceError) as caught:
        file_source_resolver(v6)("2001:db8::1", 443)
    assert caught.value.rule == "address_blocked"


@pytest.mark.parametrize(
    ("configured", "requested", "expected"),
    [
        ("files.example.com.", "files.example.com", "93.184.216.34"),
        ("files.example.com", "files.example.com.", "10.0.0.7"),
    ],
)
def test_the_lookup_uses_the_source_spelling_of_the_host(
    monkeypatch: pytest.MonkeyPatch, configured: str, requested: str, expected: str
) -> None:
    answers = {"files.example.com.": "93.184.216.34", "files.example.com": "10.0.0.7"}
    looked_up: list[str] = []

    def _resolve(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        looked_up.append(host)
        return [ipaddress.ip_address(answers[host])]

    monkeypatch.setattr(ssrf_guard, "_resolve_addrs", _resolve)
    source = file_sources_from_map({"a": f"https://{configured}/x"}, private_network_allowed=True)[
        0
    ]
    assert resolve_file_source(f"https://{requested}/x/a.iso", [source]) is source
    assert file_source_resolver(source)(requested, 443) == [expected]
    assert looked_up == [configured]


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


def test_formatted_traceback_never_holds_the_address() -> None:
    secret = "dummy-secret-77"
    urls = [
        f"https://files.example.com:{secret}/isos/a",
        f"https://[{secret}/isos/a",
        f"https://files.example.com/isos/%ff{secret}",
    ]
    for url in urls:
        with pytest.raises(FileSourceError) as caught:
            resolve_file_source(url, SOURCES)
        assert secret not in "".join(traceback.format_exception(caught.value))
    for bad_base in (f"https://files.example.com:{secret}/", f"https://[{secret}/"):
        with pytest.raises(FileSourceError) as caught:
            file_sources_from_map({"a": bad_base})
        assert secret not in "".join(traceback.format_exception(caught.value))


def test_host_list_reader_refuses_a_bare_string() -> None:
    with pytest.raises(FileSourceError) as caught:
        file_sources_from_hosts("files.example.com")
    assert caught.value.rule == "bad_source"


def test_map_reader_refuses_a_value_that_is_not_a_map() -> None:
    for value in ("https://files.example.com/", ["https://files.example.com/"]):
        with pytest.raises(FileSourceError) as caught:
            file_sources_from_map(value)  # type: ignore[arg-type]
        assert caught.value.rule == "bad_source"


async def test_kit_transport_connects_only_to_checked_addresses_of_the_source() -> None:
    """Uses only public kit names. No connection is opened: each case is refused first."""
    closed = kit.file_sources_from_map({"a": "http://10.0.0.7/files"})[0]
    open_ = kit.file_sources_from_map(
        {"a": "http://127.0.0.1/files"}, private_network_allowed=True
    )[0]
    public = kit.file_sources_from_map({"a": "http://192.0.2.10/files"})[0]
    cases = [
        (closed, "http://10.0.0.7/files/a.iso", "address_blocked"),
        (open_, "http://127.0.0.1/files/a.iso", "address_blocked"),
        (public, "http://192.0.2.10:8080/files/a.iso", "other_host"),
        (public, "http://10.0.0.8/files/a.iso", "other_host"),
    ]
    for source, url, rule in cases:
        async with httpx.AsyncClient(transport=kit.file_source_transport(source)) as client:
            with pytest.raises(kit.FileSourceError) as caught:
                await client.get(url)
        assert caught.value.rule == rule


def _fake_server_redirecting_to(monkeypatch: pytest.MonkeyPatch, location: str) -> list[str]:
    """Put a fake server under the public transport: it answers every request with a redirect.

    The fake replaces the step that sends a request on a new or a reused
    connection, so it sees only the requests that passed the check.
    """
    seen: list[str] = []

    async def _send(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": location})

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _send)
    return seen


@pytest.mark.parametrize(
    ("location", "rule"),
    [
        ("https://files.example.com/other/a.iso", "path_outside_source"),
        ("https://files.example.com/isosx/a.iso", "path_outside_source"),
        ("https://files.example.com/isos/%252e%252e/etc/a", "path_dots"),
        ("https://files.example.com/isos%2Fa.iso", "path_outside_source"),
        ("https://files.example.com/isos%2fsub/a.iso", "path_outside_source"),
        ("https://files.example.com/isos%252Fa.iso", "path_outside_source"),
        ("http://files.example.com:443/isos/a.iso", "other_host"),
        ("http://files.example.com/isos/a.iso", "other_host"),
        ("https://other.example.com/isos/a.iso", "other_host"),
    ],
)
async def test_a_redirect_is_checked_before_it_is_sent(
    monkeypatch: pytest.MonkeyPatch, location: str, rule: str
) -> None:
    source = kit.file_sources_from_map({"isos": BASE})[0]
    seen = _fake_server_redirecting_to(monkeypatch, location)
    transport = kit.file_source_transport(source)
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        with pytest.raises(FileSourceError) as caught:
            await client.get(f"{BASE}/a.iso")
    assert caught.value.rule == rule
    assert seen == [f"{BASE}/a.iso"]


def test_an_encoded_letter_under_the_source_is_accepted() -> None:
    assert resolve_file_source(f"{BASE}/%61.iso", SOURCES) is SOURCES[0]
    assert resolve_file_source("https://files.example.com/is%6Fs/a.iso", SOURCES) is SOURCES[0]


async def test_a_redirect_under_the_source_is_followed(monkeypatch: pytest.MonkeyPatch) -> None:
    source = kit.file_sources_from_map({"isos": BASE})[0]
    seen = _fake_server_redirecting_to(monkeypatch, f"{BASE}/b.iso")
    transport = kit.file_source_transport(source)
    async with httpx.AsyncClient(
        transport=transport, follow_redirects=True, max_redirects=1
    ) as client:
        with pytest.raises(httpx.TooManyRedirects):
            await client.get(f"{BASE}/a.iso")
    assert seen == [f"{BASE}/a.iso", f"{BASE}/b.iso"]


async def test_kit_transport_checks_each_request_before_it_connects() -> None:
    """The public transport refuses a request outside the source before any connection."""
    public = kit.file_sources_from_map({"a": "https://192.0.2.10/files"})[0]
    cases = [
        ("https://192.0.2.10/other/a.iso", "path_outside_source"),
        ("http://192.0.2.10:443/files/a.iso", "other_host"),
    ]
    for url, rule in cases:
        async with httpx.AsyncClient(transport=kit.file_source_transport(public)) as client:
            with pytest.raises(kit.FileSourceError) as caught:
                await client.get(url)
        assert caught.value.rule == rule


def test_kit_offers_the_file_source_names() -> None:
    assert kit.resolve_file_source is resolve_file_source
    assert kit.FileSource is FileSource
    assert kit.FileSourceError is FileSourceError
    assert kit.file_sources_from_map is file_sources_from_map
    assert kit.file_sources_from_hosts is file_sources_from_hosts
    assert kit.file_source_transport is file_source_transport
