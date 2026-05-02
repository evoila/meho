# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for GCP Helpers (TASK-102)

Tests utility functions for GCP operations.
"""

from meho_app.modules.connectors.gcp.helpers import (
    build_filter_string,
    extract_name_from_url,
    extract_region_from_zone,
    extract_zone_from_url,
    format_bytes,
    format_timestamp,
    get_status_color,
    parse_labels,
    parse_machine_type,
)


class TestExtractZoneFromUrl:
    """Test zone extraction from URLs."""

    def test_full_zone_url(self):
        """Test extraction from full zone URL."""
        url = "https://www.googleapis.com/compute/v1/projects/my-project/zones/us-central1-a"
        assert extract_zone_from_url(url) == "us-central1-a"

    def test_empty_url(self):
        """Test handling of empty URL."""
        assert extract_zone_from_url("") == ""
        assert extract_zone_from_url(None) == ""

    def test_various_zones(self):
        """Test various zone formats."""
        zones = [
            ("zones/us-west1-b", "us-west1-b"),
            ("projects/p/zones/europe-west1-c", "europe-west1-c"),
            ("asia-east1-a", "asia-east1-a"),
        ]
        for url, expected in zones:
            assert extract_zone_from_url(url) == expected


class TestExtractRegionFromZone:
    """Test region extraction from zone names."""

    def test_standard_zone(self):
        """Test extraction from standard zone."""
        assert extract_region_from_zone("us-central1-a") == "us-central1"
        assert extract_region_from_zone("europe-west1-b") == "europe-west1"
        assert extract_region_from_zone("asia-east1-c") == "asia-east1"

    def test_empty_zone(self):
        """Test handling of empty zone."""
        assert extract_region_from_zone("") == ""
        assert extract_region_from_zone(None) == ""


class TestExtractNameFromUrl:
    """Test resource name extraction from URLs."""

    def test_full_url(self):
        """Test extraction from full resource URL."""
        url = "https://www.googleapis.com/compute/v1/projects/my-project/global/networks/default"
        assert extract_name_from_url(url) == "default"

    def test_partial_url(self):
        """Test extraction from partial URL."""
        assert extract_name_from_url("networks/my-vpc") == "my-vpc"
        assert extract_name_from_url("instances/my-vm") == "my-vm"

    def test_empty_url(self):
        """Test handling of empty URL."""
        assert extract_name_from_url("") == ""
        assert extract_name_from_url(None) == ""


class TestFormatBytes:
    """Test byte formatting."""

    def test_bytes(self):
        """Test byte values."""
        assert format_bytes(100) == "100.0 B"

    def test_kilobytes(self):
        """Test kilobyte values."""
        assert format_bytes(1024) == "1.0 KB"
        assert format_bytes(2048) == "2.0 KB"

    def test_megabytes(self):
        """Test megabyte values."""
        assert format_bytes(1024 * 1024) == "1.0 MB"

    def test_gigabytes(self):
        """Test gigabyte values."""
        assert format_bytes(1024 * 1024 * 1024) == "1.0 GB"
        assert format_bytes(100 * 1024 * 1024 * 1024) == "100.0 GB"

    def test_terabytes(self):
        """Test terabyte values."""
        assert format_bytes(1024 * 1024 * 1024 * 1024) == "1.0 TB"

    def test_none(self):
        """Test None handling."""
        assert format_bytes(None) == "Unknown"


class TestParseMachineType:
    """Test machine type parsing."""

    def test_full_url(self):
        """Test parsing full machine type URL."""
        url = "zones/us-central1-a/machineTypes/n1-standard-4"
        result = parse_machine_type(url)
        assert result["zone"] == "us-central1-a"
        assert result["machine_type"] == "n1-standard-4"

    def test_empty_url(self):
        """Test empty URL handling."""
        result = parse_machine_type("")
        assert result["zone"] == ""
        assert result["machine_type"] == ""


class TestGetStatusColor:
    """Test status color mapping."""

    def test_running_status(self):
        """Test running status."""
        assert get_status_color("RUNNING") == "green"
        assert get_status_color("running") == "green"

    def test_stopped_status(self):
        """Test stopped status."""
        assert get_status_color("STOPPED") == "red"
        assert get_status_color("TERMINATED") == "red"

    def test_transitional_status(self):
        """Test transitional statuses."""
        assert get_status_color("STAGING") == "yellow"
        assert get_status_color("STOPPING") == "yellow"
        assert get_status_color("PROVISIONING") == "yellow"

    def test_unknown_status(self):
        """Test unknown status."""
        assert get_status_color("UNKNOWN_STATUS") == "gray"


class TestParseLabels:
    """Test label parsing."""

    def test_normal_labels(self):
        """Test normal label dict."""
        labels = {"env": "prod", "team": "dev"}
        assert parse_labels(labels) == {"env": "prod", "team": "dev"}

    def test_empty_labels(self):
        """Test empty/None labels."""
        assert parse_labels(None) == {}
        assert parse_labels({}) == {}


class TestFormatTimestamp:
    """Test timestamp formatting."""

    def test_iso_timestamp(self):
        """Test ISO format timestamp."""
        ts = "2024-01-15T10:00:00.000-08:00"
        assert format_timestamp(ts) == ts

    def test_empty_timestamp(self):
        """Test empty/None timestamp."""
        assert format_timestamp(None) == ""
        assert format_timestamp("") == ""


class TestBuildFilterString:
    """Test filter string building."""

    def test_single_filter(self):
        """Test single filter."""
        result = build_filter_string({"status": "RUNNING"})
        assert result == 'status="RUNNING"'

    def test_multiple_filters(self):
        """Test multiple filters."""
        result = build_filter_string({"status": "RUNNING", "zone": "us-central1-a"})
        assert "status=" in result
        assert "zone=" in result
        assert " AND " in result

    def test_boolean_filter(self):
        """Test boolean filter."""
        result = build_filter_string({"enabled": True})
        assert result == "enabled=true"

    def test_empty_filters(self):
        """Test empty filters."""
        assert build_filter_string({}) is None
        assert build_filter_string(None) is None

    def test_none_values_skipped(self):
        """Test that None values are skipped."""
        result = build_filter_string({"status": "RUNNING", "zone": None})
        assert result == 'status="RUNNING"'
