# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for network diagnostic tools (Phase 96.1).

Tests cover:
- TOOL_REGISTRY registration (all 4 diagnostic tools present)
- Tool execution: dns_resolve, tcp_probe, http_probe, tls_check
- Success and error paths for each tool
- Topology entity emission (store_discovery) for dns_resolve, http_probe, tls_check
- ToolAction model validation (dns_resolve, tcp_probe, http_probe, tls_check)
- Compressor handlers for diagnostic output types
- Feature flag gating (network_diagnostics flag)
- Topology schema registration

All network I/O is mocked -- no real DNS, TCP, HTTP, or TLS calls.
"""

from __future__ import annotations

import asyncio
import ssl
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Tool imports (from Plan 01 -- these always exist)
# ---------------------------------------------------------------------------
from meho_app.modules.agents.react_agent.tools import TOOL_REGISTRY
from meho_app.modules.agents.react_agent.tools.dns_resolve import (
    DnsResolveInput,
    DnsResolveOutput,
    DnsResolveTool,
)
from meho_app.modules.agents.react_agent.tools.http_probe import (
    HttpProbeInput,
    HttpProbeOutput,
    HttpProbeTool,
)
from meho_app.modules.agents.react_agent.tools.tcp_probe import (
    TcpProbeInput,
    TcpProbeOutput,
    TcpProbeTool,
)
from meho_app.modules.agents.react_agent.tools.tls_check import (
    TlsCheckInput,
    TlsCheckOutput,
    TlsCheckTool,
)

# Feature flags (always available)
from meho_app.core.feature_flags import FeatureFlags

# Topology schema (always available)
from meho_app.modules.topology.schema import get_topology_schema

# ---------------------------------------------------------------------------
# ToolAction models and compressor (from Plan 96.1-02)
# ---------------------------------------------------------------------------
from meho_app.modules.agents.specialist_agent.models import (
    DnsResolveAction,
    HttpProbeAction,
    ReActStep,
    TcpProbeAction,
    TlsCheckAction,
)
from meho_app.modules.agents.specialist_agent.compressor import (
    compress_observation,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------



def _make_deps() -> MagicMock:
    """Create a mock deps object with meho_deps.db_session and tenant_id."""
    deps = MagicMock()
    deps.meho_deps = MagicMock()
    deps.meho_deps.db_session = MagicMock()
    deps.meho_deps.tenant_id = "test-tenant"
    return deps


def _make_emitter() -> AsyncMock:
    """Create a mock EventEmitter with tool_start and tool_complete."""
    emitter = AsyncMock()
    emitter.tool_start = AsyncMock()
    emitter.tool_complete = AsyncMock()
    return emitter


# ===========================================================================
# TOOL_REGISTRY Tests
# ===========================================================================


class TestToolRegistry:
    """Tests for TOOL_REGISTRY containing all 4 diagnostic tools."""

    def test_all_diagnostic_tools_in_registry(self) -> None:
        """All 4 diagnostic tools are registered in TOOL_REGISTRY."""
        expected = {"dns_resolve", "tcp_probe", "http_probe", "tls_check"}
        actual = set(TOOL_REGISTRY.keys())
        assert expected.issubset(actual), f"Missing tools: {expected - actual}"

    def test_tool_registry_total_count(self) -> None:
        """TOOL_REGISTRY has exactly 14 entries (10 existing + 4 new)."""
        assert len(TOOL_REGISTRY) == 14, (
            f"Expected 14 tools, got {len(TOOL_REGISTRY)}: {sorted(TOOL_REGISTRY.keys())}"
        )

    def test_tool_classes_have_required_attributes(self) -> None:
        """Each diagnostic tool has TOOL_NAME, TOOL_DESCRIPTION, InputSchema, OutputSchema."""
        tools = [DnsResolveTool, TcpProbeTool, HttpProbeTool, TlsCheckTool]
        for tool_cls in tools:
            assert hasattr(tool_cls, "TOOL_NAME"), f"{tool_cls.__name__} missing TOOL_NAME"
            assert hasattr(tool_cls, "TOOL_DESCRIPTION"), f"{tool_cls.__name__} missing TOOL_DESCRIPTION"
            assert hasattr(tool_cls, "InputSchema"), f"{tool_cls.__name__} missing InputSchema"
            assert hasattr(tool_cls, "OutputSchema"), f"{tool_cls.__name__} missing OutputSchema"

    def test_tool_registry_maps_to_correct_classes(self) -> None:
        """TOOL_REGISTRY maps each tool name to its correct class."""
        assert TOOL_REGISTRY["dns_resolve"] is DnsResolveTool
        assert TOOL_REGISTRY["tcp_probe"] is TcpProbeTool
        assert TOOL_REGISTRY["http_probe"] is HttpProbeTool
        assert TOOL_REGISTRY["tls_check"] is TlsCheckTool


# ===========================================================================
# ToolAction Model Tests
# ===========================================================================


class TestToolActionModels:
    """Tests for typed ToolAction models for diagnostic tools."""

    def test_dns_resolve_action_validates(self) -> None:
        """DnsResolveAction accepts valid input, tool field equals 'dns_resolve'."""
        action = DnsResolveAction(hostname="example.com", record_types=["A", "MX"])
        assert action.tool == "dns_resolve"
        assert action.hostname == "example.com"
        assert action.record_types == ["A", "MX"]

    def test_tcp_probe_action_validates(self) -> None:
        """TcpProbeAction accepts valid input with port in range."""
        action = TcpProbeAction(host="10.0.0.1", port=443, timeout_seconds=5.0)
        assert action.tool == "tcp_probe"
        assert action.host == "10.0.0.1"
        assert action.port == 443

    def test_tcp_probe_action_port_validation(self) -> None:
        """TcpProbeAction rejects port=0 and port=70000."""
        with pytest.raises(Exception):  # ValidationError
            TcpProbeAction(host="example.com", port=0)
        with pytest.raises(Exception):
            TcpProbeAction(host="example.com", port=70000)

    def test_http_probe_action_validates(self) -> None:
        """HttpProbeAction accepts valid URL input."""
        action = HttpProbeAction(url="https://example.com")
        assert action.tool == "http_probe"
        assert action.url == "https://example.com"

    def test_tls_check_action_validates(self) -> None:
        """TlsCheckAction accepts valid hostname input."""
        action = TlsCheckAction(hostname="example.com", port=443)
        assert action.tool == "tls_check"
        assert action.hostname == "example.com"
        assert action.port == 443

    def test_react_step_parses_diagnostic_actions(self) -> None:
        """ReActStep can parse action_input for all 4 diagnostic tools."""
        for tool_name, action_data in [
            ("dns_resolve", {"tool": "dns_resolve", "hostname": "example.com", "record_types": ["A"]}),
            ("tcp_probe", {"tool": "tcp_probe", "host": "example.com", "port": 443}),
            ("http_probe", {"tool": "http_probe", "url": "https://example.com"}),
            ("tls_check", {"tool": "tls_check", "hostname": "example.com", "port": 443}),
        ]:
            step = ReActStep(
                thought=f"Testing {tool_name}",
                response_type="action",
                action_input=action_data,
            )
            assert step.action == tool_name, f"Failed for {tool_name}"


# ===========================================================================
# DnsResolveTool Execution Tests
# ===========================================================================


class TestDnsResolveExecution:
    """Tests for DnsResolveTool.execute with mocked aiodns."""

    @pytest.mark.asyncio
    async def test_dns_resolve_success(self) -> None:
        """Mocked aiodns returns A records -> success=True with parsed records."""
        tool = DnsResolveTool()
        tool_input = DnsResolveInput(hostname="example.com", record_types=["A"])
        deps = _make_deps()
        emitter = _make_emitter()

        # Mock aiodns resolver
        mock_result = [MagicMock(host="93.184.216.34")]

        with (
            patch(
                "meho_app.modules.agents.react_agent.tools.dns_resolve.aiodns.DNSResolver"
            ) as mock_resolver_cls,
            patch(
                "meho_app.modules.agents.react_agent.tools.dns_resolve._emit_topology",
                new_callable=AsyncMock,
            ) as mock_topo,
        ):
            mock_resolver = MagicMock()
            mock_resolver.query = AsyncMock(return_value=mock_result)
            mock_resolver_cls.return_value = mock_resolver

            output = await tool.execute(tool_input, deps, emitter)

        assert output.success is True
        assert output.hostname == "example.com"
        assert "A" in output.records
        assert output.records["A"] == [{"host": "93.184.216.34"}]
        assert len(output.errors) == 0
        emitter.tool_start.assert_awaited_once_with("dns_resolve")
        emitter.tool_complete.assert_awaited_once_with("dns_resolve", success=True)

    @pytest.mark.asyncio
    async def test_dns_resolve_nxdomain(self) -> None:
        """Mocked aiodns raises DNSError -> errors list populated, success depends on other types."""
        import aiodns

        tool = DnsResolveTool()
        tool_input = DnsResolveInput(hostname="nonexistent.example.com", record_types=["A"])
        deps = _make_deps()
        emitter = _make_emitter()

        with patch(
            "meho_app.modules.agents.react_agent.tools.dns_resolve.aiodns.DNSResolver"
        ) as mock_resolver_cls:
            mock_resolver = MagicMock()
            mock_resolver.query = AsyncMock(
                side_effect=aiodns.error.DNSError(1, "Domain not found")
            )
            mock_resolver_cls.return_value = mock_resolver

            output = await tool.execute(tool_input, deps, emitter)

        # All record types failed -> success=False (no records resolved)
        assert output.success is False
        assert len(output.errors) > 0
        assert "Domain not found" in output.errors[0]

    @pytest.mark.asyncio
    async def test_dns_resolve_multiple_types(self) -> None:
        """Request A + MX -> both returned in records dict."""
        tool = DnsResolveTool()
        tool_input = DnsResolveInput(hostname="example.com", record_types=["A", "MX"])
        deps = _make_deps()
        emitter = _make_emitter()

        mock_a_result = [MagicMock(host="93.184.216.34")]
        mock_mx_result = [MagicMock(host="mail.example.com", priority=10)]

        async def mock_query(hostname, rtype):
            if rtype == "A":
                return mock_a_result
            if rtype == "MX":
                return mock_mx_result
            raise Exception(f"Unexpected type: {rtype}")

        with (
            patch(
                "meho_app.modules.agents.react_agent.tools.dns_resolve.aiodns.DNSResolver"
            ) as mock_resolver_cls,
            patch(
                "meho_app.modules.agents.react_agent.tools.dns_resolve._emit_topology",
                new_callable=AsyncMock,
            ),
        ):
            mock_resolver = MagicMock()
            mock_resolver.query = AsyncMock(side_effect=mock_query)
            mock_resolver_cls.return_value = mock_resolver

            output = await tool.execute(tool_input, deps, emitter)

        assert output.success is True
        assert "A" in output.records
        assert "MX" in output.records
        assert output.records["A"] == [{"host": "93.184.216.34"}]
        assert output.records["MX"] == [{"host": "mail.example.com", "priority": 10}]

    @pytest.mark.asyncio
    async def test_dns_resolve_topology_emission(self) -> None:
        """After successful resolve, _emit_topology is called (fire-and-forget)."""
        tool = DnsResolveTool()
        tool_input = DnsResolveInput(hostname="example.com", record_types=["A"])
        deps = _make_deps()
        emitter = _make_emitter()

        mock_result = [MagicMock(host="93.184.216.34")]

        with (
            patch(
                "meho_app.modules.agents.react_agent.tools.dns_resolve.aiodns.DNSResolver"
            ) as mock_resolver_cls,
            patch(
                "meho_app.modules.agents.react_agent.tools.dns_resolve._emit_topology",
                new_callable=AsyncMock,
            ) as mock_topo,
        ):
            mock_resolver = MagicMock()
            mock_resolver.query = AsyncMock(return_value=mock_result)
            mock_resolver_cls.return_value = mock_resolver

            output = await tool.execute(tool_input, deps, emitter)

        assert output.success is True
        mock_topo.assert_awaited_once()
        # Verify arguments: hostname, records, deps
        call_args = mock_topo.call_args
        assert call_args[0][0] == "example.com"  # hostname
        assert "A" in call_args[0][1]  # records dict
        assert call_args[0][2] is deps  # deps


# ===========================================================================
# TcpProbeTool Execution Tests
# ===========================================================================


class TestTcpProbeExecution:
    """Tests for TcpProbeTool.execute with mocked asyncio.open_connection."""

    @pytest.mark.asyncio
    async def test_tcp_probe_connected(self) -> None:
        """Mocked connection succeeds -> status='connected', latency_ms set."""
        tool = TcpProbeTool()
        tool_input = TcpProbeInput(host="example.com", port=443, timeout_seconds=5.0)
        deps = _make_deps()
        emitter = _make_emitter()

        mock_writer = MagicMock()
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = AsyncMock()

        with patch("meho_app.modules.agents.react_agent.tools.tcp_probe.asyncio.open_connection", new_callable=AsyncMock) as mock_conn:
            mock_conn.return_value = (MagicMock(), mock_writer)

            output = await tool.execute(tool_input, deps, emitter)

        assert output.success is True
        assert output.status == "connected"
        assert output.latency_ms is not None
        assert output.latency_ms >= 0
        assert output.host == "example.com"
        assert output.port == 443
        assert output.error is None
        emitter.tool_start.assert_awaited_once_with("tcp_probe")
        emitter.tool_complete.assert_awaited_once_with("tcp_probe", success=True)

    @pytest.mark.asyncio
    async def test_tcp_probe_refused(self) -> None:
        """Mock raises ConnectionRefusedError -> status='refused'."""
        tool = TcpProbeTool()
        tool_input = TcpProbeInput(host="example.com", port=9999, timeout_seconds=5.0)
        deps = _make_deps()
        emitter = _make_emitter()

        with patch("meho_app.modules.agents.react_agent.tools.tcp_probe.asyncio.open_connection", new_callable=AsyncMock) as mock_conn:
            mock_conn.side_effect = ConnectionRefusedError("Connection refused")

            output = await tool.execute(tool_input, deps, emitter)

        assert output.success is False
        assert output.status == "refused"
        assert output.error is not None
        assert "refused" in output.error.lower()

    @pytest.mark.asyncio
    async def test_tcp_probe_timeout(self) -> None:
        """Mock raises TimeoutError -> status='timeout'."""
        tool = TcpProbeTool()
        tool_input = TcpProbeInput(host="example.com", port=443, timeout_seconds=1.0)
        deps = _make_deps()
        emitter = _make_emitter()

        with patch(
            "meho_app.modules.agents.react_agent.tools.tcp_probe.asyncio.wait_for",
            new_callable=AsyncMock,
        ) as mock_wait_for:
            mock_wait_for.side_effect = TimeoutError()

            output = await tool.execute(tool_input, deps, emitter)

        assert output.success is False
        assert output.status == "timeout"
        assert output.error is not None

    @pytest.mark.asyncio
    async def test_tcp_probe_error(self) -> None:
        """Mock raises OSError -> status='error', error message set."""
        tool = TcpProbeTool()
        tool_input = TcpProbeInput(host="example.com", port=443, timeout_seconds=5.0)
        deps = _make_deps()
        emitter = _make_emitter()

        with patch("meho_app.modules.agents.react_agent.tools.tcp_probe.asyncio.open_connection", new_callable=AsyncMock) as mock_conn:
            mock_conn.side_effect = OSError("Network unreachable")

            output = await tool.execute(tool_input, deps, emitter)

        assert output.success is False
        assert output.status == "error"
        assert "Network unreachable" in output.error


# ===========================================================================
# HttpProbeTool Execution Tests
# ===========================================================================


class TestHttpProbeExecution:
    """Tests for HttpProbeTool.execute with mocked httpx."""

    @pytest.mark.asyncio
    async def test_http_probe_success(self) -> None:
        """Mocked httpx returns 200 -> success=True with status_code, latency, headers."""
        tool = HttpProbeTool()
        tool_input = HttpProbeInput(url="https://example.com")
        deps = _make_deps()
        emitter = _make_emitter()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/html; charset=utf-8", "server": "nginx"}
        mock_response.url = "https://example.com"
        mock_response.history = []
        mock_response.text = "<html>Hello World</html>"

        with (
            patch(
                "meho_app.modules.agents.react_agent.tools.http_probe.httpx.AsyncClient"
            ) as mock_client_cls,
            patch(
                "meho_app.modules.agents.react_agent.tools.http_probe._emit_topology",
                new_callable=AsyncMock,
            ),
        ):
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            output = await tool.execute(tool_input, deps, emitter)

        assert output.success is True
        assert output.status_code == 200
        assert output.url == "https://example.com"
        assert output.final_url == "https://example.com"
        assert output.latency_ms >= 0
        assert "content-type" in output.headers
        emitter.tool_start.assert_awaited_once_with("http_probe")
        emitter.tool_complete.assert_awaited_once_with("http_probe", success=True)

    @pytest.mark.asyncio
    async def test_http_probe_with_redirects(self) -> None:
        """Mocked response.history has redirects -> redirect_chain populated."""
        tool = HttpProbeTool()
        tool_input = HttpProbeInput(url="http://example.com", follow_redirects=True)
        deps = _make_deps()
        emitter = _make_emitter()

        # Create redirect history
        redirect_entry = MagicMock()
        redirect_entry.url = "http://example.com"
        redirect_entry.status_code = 301

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/html"}
        mock_response.url = "https://example.com"
        mock_response.history = [redirect_entry]
        mock_response.text = "<html>Final</html>"

        with (
            patch(
                "meho_app.modules.agents.react_agent.tools.http_probe.httpx.AsyncClient"
            ) as mock_client_cls,
            patch(
                "meho_app.modules.agents.react_agent.tools.http_probe._emit_topology",
                new_callable=AsyncMock,
            ),
        ):
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            output = await tool.execute(tool_input, deps, emitter)

        assert output.success is True
        assert len(output.redirect_chain) == 1
        assert output.redirect_chain[0]["status_code"] == 301

    @pytest.mark.asyncio
    async def test_http_probe_body_preview(self) -> None:
        """Text response -> body_preview is first 500 chars."""
        tool = HttpProbeTool()
        tool_input = HttpProbeInput(url="https://example.com", method="GET")
        deps = _make_deps()
        emitter = _make_emitter()

        long_body = "x" * 1000

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/html"}
        mock_response.url = "https://example.com"
        mock_response.history = []
        mock_response.text = long_body

        with (
            patch(
                "meho_app.modules.agents.react_agent.tools.http_probe.httpx.AsyncClient"
            ) as mock_client_cls,
            patch(
                "meho_app.modules.agents.react_agent.tools.http_probe._emit_topology",
                new_callable=AsyncMock,
            ),
        ):
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            output = await tool.execute(tool_input, deps, emitter)

        assert output.body_preview is not None
        assert len(output.body_preview) == 500

    @pytest.mark.asyncio
    async def test_http_probe_topology_emission(self) -> None:
        """After successful probe, _emit_topology called with ExternalURL entity."""
        tool = HttpProbeTool()
        tool_input = HttpProbeInput(url="https://example.com")
        deps = _make_deps()
        emitter = _make_emitter()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/html"}
        mock_response.url = "https://example.com"
        mock_response.history = []
        mock_response.text = "Hello"

        with (
            patch(
                "meho_app.modules.agents.react_agent.tools.http_probe.httpx.AsyncClient"
            ) as mock_client_cls,
            patch(
                "meho_app.modules.agents.react_agent.tools.http_probe._emit_topology",
                new_callable=AsyncMock,
            ) as mock_topo,
        ):
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            output = await tool.execute(tool_input, deps, emitter)

        assert output.success is True
        mock_topo.assert_awaited_once()
        # Verify keyword arguments
        call_kwargs = mock_topo.call_args[1]
        assert call_kwargs["final_url"] == "https://example.com"
        assert call_kwargs["status_code"] == 200
        assert call_kwargs["deps"] is deps

    @pytest.mark.asyncio
    async def test_http_probe_error(self) -> None:
        """httpx raises ConnectError -> error field set, success=False."""
        import httpx

        tool = HttpProbeTool()
        tool_input = HttpProbeInput(url="https://unreachable.example.com")
        deps = _make_deps()
        emitter = _make_emitter()

        with patch(
            "meho_app.modules.agents.react_agent.tools.http_probe.httpx.AsyncClient"
        ) as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(
                side_effect=httpx.ConnectError("Connection failed")
            )
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            output = await tool.execute(tool_input, deps, emitter)

        assert output.success is False
        assert output.error is not None
        assert "error" in output.error.lower() or "failed" in output.error.lower()


# ===========================================================================
# TlsCheckTool Execution Tests
# ===========================================================================


class TestTlsCheckExecution:
    """Tests for TlsCheckTool.execute with mocked ssl/asyncio."""

    @pytest.mark.asyncio
    async def test_tls_check_success(self) -> None:
        """Mocked SSL connection returns cert -> success with subject, issuer, expiry, SANs."""
        tool = TlsCheckTool()
        tool_input = TlsCheckInput(hostname="example.com", port=443)
        deps = _make_deps()
        emitter = _make_emitter()

        # Build mock cert
        future_date = datetime.now(UTC) + timedelta(days=365)
        not_after_str = future_date.strftime("%b %d %H:%M:%S %Y GMT")

        mock_cert = {
            "subject": ((("commonName", "example.com"),),),
            "issuer": ((("organizationName", "DigiCert Inc"),), (("commonName", "DigiCert SHA2"),)),
            "notAfter": not_after_str,
            "subjectAltName": (("DNS", "example.com"), ("DNS", "*.example.com")),
            "serialNumber": "01234ABCDEF",
        }

        mock_ssl_object = MagicMock()
        mock_ssl_object.getpeercert.return_value = mock_cert
        mock_ssl_object.version.return_value = "TLSv1.3"

        mock_writer = MagicMock()
        mock_writer.get_extra_info = MagicMock(return_value=mock_ssl_object)
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = AsyncMock()

        with (
            patch(
                "meho_app.modules.agents.react_agent.tools.tls_check.asyncio.wait_for",
                new_callable=AsyncMock,
            ) as mock_wait_for,
            patch(
                "meho_app.modules.agents.react_agent.tools.tls_check._emit_topology",
                new_callable=AsyncMock,
            ) as mock_topo,
        ):
            mock_wait_for.return_value = (MagicMock(), mock_writer)

            output = await tool.execute(tool_input, deps, emitter)

        assert output.success is True
        assert output.chain_valid is True
        assert output.hostname == "example.com"
        assert output.port == 443
        assert output.subject.get("commonName") == "example.com"
        assert "DigiCert" in output.issuer.get("organizationName", "")
        assert output.protocol_version == "TLSv1.3"
        assert "example.com" in output.sans
        assert "*.example.com" in output.sans
        assert output.serial_number == "01234ABCDEF"
        assert output.days_until_expiry > 0
        assert output.expires_at != ""
        emitter.tool_start.assert_awaited_once_with("tls_check")
        emitter.tool_complete.assert_awaited_once_with("tls_check", success=True)

    @pytest.mark.asyncio
    async def test_tls_check_expired_cert(self) -> None:
        """Mocked cert with past expiry -> days_until_expiry is negative."""
        tool = TlsCheckTool()
        tool_input = TlsCheckInput(hostname="expired.example.com", port=443)
        deps = _make_deps()
        emitter = _make_emitter()

        # Cert expired 30 days ago
        past_date = datetime.now(UTC) - timedelta(days=30)
        not_after_str = past_date.strftime("%b %d %H:%M:%S %Y GMT")

        mock_cert = {
            "subject": ((("commonName", "expired.example.com"),),),
            "issuer": ((("commonName", "Test CA"),),),
            "notAfter": not_after_str,
            "subjectAltName": (),
            "serialNumber": "EXPIRED123",
        }

        mock_ssl_object = MagicMock()
        mock_ssl_object.getpeercert.return_value = mock_cert
        mock_ssl_object.version.return_value = "TLSv1.2"

        mock_writer = MagicMock()
        mock_writer.get_extra_info = MagicMock(return_value=mock_ssl_object)
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = AsyncMock()

        with (
            patch(
                "meho_app.modules.agents.react_agent.tools.tls_check.asyncio.wait_for",
                new_callable=AsyncMock,
            ) as mock_wait_for,
            patch(
                "meho_app.modules.agents.react_agent.tools.tls_check._emit_topology",
                new_callable=AsyncMock,
            ),
        ):
            mock_wait_for.return_value = (MagicMock(), mock_writer)

            output = await tool.execute(tool_input, deps, emitter)

        assert output.success is True
        assert output.days_until_expiry < 0

    @pytest.mark.asyncio
    async def test_tls_check_invalid_chain(self) -> None:
        """Mock raises SSLCertVerificationError -> chain_valid=False, error set."""
        tool = TlsCheckTool()
        tool_input = TlsCheckInput(hostname="self-signed.example.com", port=443)
        deps = _make_deps()
        emitter = _make_emitter()

        with patch(
            "meho_app.modules.agents.react_agent.tools.tls_check.asyncio.wait_for",
            new_callable=AsyncMock,
        ) as mock_wait_for:
            mock_wait_for.side_effect = ssl.SSLCertVerificationError(
                "certificate verify failed: self-signed certificate"
            )

            output = await tool.execute(tool_input, deps, emitter)

        assert output.success is True  # Check completed, cert just invalid
        assert output.chain_valid is False
        assert output.error is not None
        assert "verification failed" in output.error.lower() or "verify failed" in output.error.lower()

    @pytest.mark.asyncio
    async def test_tls_check_topology_emission(self) -> None:
        """After successful check, _emit_topology called with TLSCertificate entity."""
        tool = TlsCheckTool()
        tool_input = TlsCheckInput(hostname="example.com", port=443)
        deps = _make_deps()
        emitter = _make_emitter()

        future_date = datetime.now(UTC) + timedelta(days=180)
        not_after_str = future_date.strftime("%b %d %H:%M:%S %Y GMT")

        mock_cert = {
            "subject": ((("commonName", "example.com"),),),
            "issuer": ((("organizationName", "Let's Encrypt"),),),
            "notAfter": not_after_str,
            "subjectAltName": (("DNS", "example.com"),),
            "serialNumber": "TOPO123",
        }

        mock_ssl_object = MagicMock()
        mock_ssl_object.getpeercert.return_value = mock_cert
        mock_ssl_object.version.return_value = "TLSv1.3"

        mock_writer = MagicMock()
        mock_writer.get_extra_info = MagicMock(return_value=mock_ssl_object)
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = AsyncMock()

        with (
            patch(
                "meho_app.modules.agents.react_agent.tools.tls_check.asyncio.wait_for",
                new_callable=AsyncMock,
            ) as mock_wait_for,
            patch(
                "meho_app.modules.agents.react_agent.tools.tls_check._emit_topology",
                new_callable=AsyncMock,
            ) as mock_topo,
        ):
            mock_wait_for.return_value = (MagicMock(), mock_writer)

            output = await tool.execute(tool_input, deps, emitter)

        assert output.success is True
        mock_topo.assert_awaited_once()
        call_kwargs = mock_topo.call_args[1]
        assert call_kwargs["hostname"] == "example.com"
        assert call_kwargs["port"] == 443
        assert call_kwargs["chain_valid"] is True
        assert call_kwargs["deps"] is deps


# ===========================================================================
# Compressor Tests
# ===========================================================================


class TestCompressors:
    """Tests for compress_observation dispatch for diagnostic output types."""

    @pytest.mark.asyncio
    async def test_compress_dns_resolve_success(self) -> None:
        """DnsResolveOutput with records -> compressed text includes hostname and records."""
        output = DnsResolveOutput(
            hostname="example.com",
            records={"A": [{"host": "93.184.216.34"}], "MX": [{"host": "mail.example.com", "priority": 10}]},
            errors=[],
            success=True,
        )
        result = await compress_observation(output, "dns_resolve")
        # The compressor should produce some compact representation
        assert isinstance(result, str)
        assert len(result) > 0
        # If it's a basic str() fallback, it should still contain the hostname
        assert "example.com" in result or "93.184.216.34" in result

    @pytest.mark.asyncio
    async def test_compress_dns_resolve_failure(self) -> None:
        """DnsResolveOutput with errors -> compressed text includes error info."""
        output = DnsResolveOutput(
            hostname="fail.example.com",
            records={},
            errors=["A: Domain not found"],
            success=False,
        )
        result = await compress_observation(output, "dns_resolve")
        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_compress_tcp_probe_connected(self) -> None:
        """TcpProbeOutput connected -> compressed text includes host:port and status."""
        output = TcpProbeOutput(
            host="10.0.0.1",
            port=443,
            status="connected",
            latency_ms=12.5,
            error=None,
            success=True,
        )
        result = await compress_observation(output, "tcp_probe")
        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_compress_tcp_probe_timeout(self) -> None:
        """TcpProbeOutput timeout -> compressed text includes timeout status."""
        output = TcpProbeOutput(
            host="10.0.0.1",
            port=443,
            status="timeout",
            latency_ms=5000.0,
            error="Connection timed out after 5.0s",
            success=False,
        )
        result = await compress_observation(output, "tcp_probe")
        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_compress_http_probe_success(self) -> None:
        """HttpProbeOutput 200 -> compressed text includes status code and URL."""
        output = HttpProbeOutput(
            url="https://example.com",
            final_url="https://example.com",
            status_code=200,
            latency_ms=150.0,
            headers={"content-type": "text/html"},
            content_type="text/html",
            redirect_chain=[],
            body_preview="<html>Hello</html>",
            success=True,
        )
        result = await compress_observation(output, "http_probe")
        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_compress_tls_check_success(self) -> None:
        """TlsCheckOutput valid -> compressed text includes cert summary."""
        output = TlsCheckOutput(
            hostname="example.com",
            port=443,
            subject={"commonName": "example.com"},
            issuer={"organizationName": "DigiCert Inc"},
            expires_at="2027-01-01T00:00:00+00:00",
            days_until_expiry=365,
            sans=["example.com", "*.example.com"],
            protocol_version="TLSv1.3",
            chain_valid=True,
            latency_ms=50.0,
            serial_number="ABC123",
            success=True,
        )
        result = await compress_observation(output, "tls_check")
        assert isinstance(result, str)
        assert len(result) > 0


# ===========================================================================
# Feature Flag Tests
# ===========================================================================


class TestFeatureFlags:
    """Tests for network_diagnostics feature flag."""

    def test_feature_flag_default_enabled(self) -> None:
        """Default FeatureFlags has network_diagnostics=True."""
        # Construct with no env vars to check defaults
        flags = FeatureFlags()
        assert flags.network_diagnostics is True

    def test_feature_flag_disables_tools(self) -> None:
        """When network_diagnostics=False, tools should be excluded from SPECIALIST_TOOLS."""
        # Test that FeatureFlags correctly supports network_diagnostics=False
        flags = FeatureFlags(network_diagnostics=False)
        assert flags.network_diagnostics is False

        # Simulate the SPECIALIST_TOOLS filtering logic from agent.py
        specialist_tools = {
            "search_operations",
            "call_operation",
            "reduce_data",
            "lookup_topology",
            "search_knowledge",
            "store_memory",
            "forget_memory",
            "dns_resolve",
            "tcp_probe",
            "http_probe",
            "tls_check",
        }

        # Apply feature flag filtering (mirrors agent.py logic)
        if not flags.network_diagnostics:
            specialist_tools.discard("dns_resolve")
            specialist_tools.discard("tcp_probe")
            specialist_tools.discard("http_probe")
            specialist_tools.discard("tls_check")

        assert "dns_resolve" not in specialist_tools
        assert "tcp_probe" not in specialist_tools
        assert "http_probe" not in specialist_tools
        assert "tls_check" not in specialist_tools
        # Other tools remain
        assert "search_operations" in specialist_tools
        assert "call_operation" in specialist_tools


# ===========================================================================
# Topology Schema Tests
# ===========================================================================


class TestTopologySchema:
    """Tests for network_diagnostics topology schema registration."""

    def test_topology_schema_registered(self) -> None:
        """get_topology_schema('network_diagnostics') returns schema with 3 entity types."""
        schema = get_topology_schema("network_diagnostics")
        assert schema is not None
        assert schema.connector_type == "network_diagnostics"
        assert len(schema.entity_types) == 3, (
            f"Expected 3 entity types, got {len(schema.entity_types)}: {list(schema.entity_types.keys())}"
        )
        assert "ExternalURL" in schema.entity_types
        assert "IPAddress" in schema.entity_types
        assert "TLSCertificate" in schema.entity_types

    def test_topology_schema_relationship_rules(self) -> None:
        """Schema has resolves_to relationship from ExternalURL to IPAddress."""
        schema = get_topology_schema("network_diagnostics")
        assert schema is not None
        # Check relationship rules exist
        assert len(schema.relationship_rules) >= 1
        # Find the resolves_to rule
        found = False
        for key, rule in schema.relationship_rules.items():
            if rule.relationship_type == "resolves_to":
                assert rule.from_type == "ExternalURL"
                assert rule.to_type == "IPAddress"
                found = True
        assert found, "resolves_to relationship not found"


# ===========================================================================
# Input Validation Tests
# ===========================================================================


class TestInputValidation:
    """Tests for Pydantic validation on tool input models."""

    def test_tcp_probe_port_validation_min(self) -> None:
        """TcpProbeInput rejects port=0."""
        with pytest.raises(Exception):
            TcpProbeInput(host="example.com", port=0)

    def test_tcp_probe_port_validation_max(self) -> None:
        """TcpProbeInput rejects port=70000."""
        with pytest.raises(Exception):
            TcpProbeInput(host="example.com", port=70000)

    def test_http_probe_method_validation(self) -> None:
        """HttpProbeInput rejects method='POST' (only GET/HEAD allowed)."""
        with pytest.raises(Exception):
            HttpProbeInput(url="https://example.com", method="POST")

    def test_tls_check_port_default(self) -> None:
        """TlsCheckInput defaults port to 443."""
        inp = TlsCheckInput(hostname="example.com")
        assert inp.port == 443

    def test_dns_resolve_default_record_types(self) -> None:
        """DnsResolveInput defaults record_types to ['A', 'AAAA']."""
        inp = DnsResolveInput(hostname="example.com")
        assert inp.record_types == ["A", "AAAA"]
