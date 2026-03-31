# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for connector utility functions.

Tests the extract_target_host() function used for topology matching.
"""

from meho_app.modules.connectors.utils import extract_target_host


class TestExtractTargetHost:
    """Tests for extract_target_host utility function."""

    def test_extract_from_https_url(self):
        """Test extraction from standard HTTPS URL."""
        result = extract_target_host("https://api.myapp.com/v1")
        assert result == "api.myapp.com"

    def test_extract_from_http_url(self):
        """Test extraction from HTTP URL."""
        result = extract_target_host("http://api.example.com/api")
        assert result == "api.example.com"

    def test_extract_from_url_with_port(self):
        """Test extraction from URL with explicit port."""
        result = extract_target_host("https://192.168.1.10:8080/api")
        assert result == "192.168.1.10"

    def test_extract_from_ip_address_url(self):
        """Test extraction from URL with IP address."""
        result = extract_target_host("https://192.168.1.100/")
        assert result == "192.168.1.100"

    def test_extract_from_bare_hostname(self):
        """Test extraction from bare hostname (no scheme)."""
        result = extract_target_host("vcenter.example.com")
        assert result == "vcenter.example.com"

    def test_extract_from_bare_hostname_with_port(self):
        """Test extraction from bare hostname with port."""
        result = extract_target_host("proxmox.local:8006")
        assert result == "proxmox.local"

    def test_extract_from_url_with_path(self):
        """Test extraction from URL with complex path."""
        result = extract_target_host("https://api.example.com/v1/users/123")
        assert result == "api.example.com"

    def test_extract_from_url_with_query_params(self):
        """Test extraction from URL with query parameters."""
        result = extract_target_host("https://api.example.com/search?q=test")
        assert result == "api.example.com"

    def test_extract_from_subdomain(self):
        """Test extraction preserves subdomains."""
        result = extract_target_host("https://api.v2.staging.myapp.com/")
        assert result == "api.v2.staging.myapp.com"

    def test_extract_from_localhost(self):
        """Test extraction from localhost URL."""
        result = extract_target_host("http://localhost:3000")
        assert result == "localhost"

    def test_extract_from_bare_ip(self):
        """Test extraction from bare IP address."""
        result = extract_target_host("10.0.0.1")
        assert result == "10.0.0.1"

    def test_extract_from_hostname_with_path_no_scheme(self):
        """Test extraction from hostname with path but no scheme."""
        result = extract_target_host("api.example.com/v1/endpoint")
        assert result == "api.example.com"
