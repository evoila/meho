# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""OpenTelemetry configuration from environment variables."""

import os
from dataclasses import dataclass, field
from functools import lru_cache


@dataclass
class OTelConfig:
    """OpenTelemetry configuration."""

    # Service identity
    service_name: str = "meho"
    service_version: str = "1.0.0"
    environment: str = "dev"

    # OTLP exporter settings
    otlp_endpoint: str | None = None  # e.g., http://localhost:5341/ingest/otlp
    otlp_protocol: str = "http/protobuf"  # or "grpc"
    otlp_headers: dict[str, str] = field(default_factory=dict)

    # Console output
    console_enabled: bool = True
    console_format: str = "pretty"  # "pretty" or "json"

    # Log level
    log_level: str = "INFO"

    # Trace sampling
    trace_sample_rate: float = 1.0  # 1.0 = 100%

    # Batch processing
    batch_delay_ms: int = 5000
    max_batch_size: int = 512


@lru_cache
def get_otel_config() -> OTelConfig:
    """Load OTEL config from environment."""
    headers: dict[str, str] = {}
    if raw_headers := os.getenv("OTEL_EXPORTER_OTLP_HEADERS"):
        # Parse "key=value,key2=value2" format (skip malformed entries)
        for entry in raw_headers.split(","):
            entry = entry.strip()
            if "=" in entry:
                k, v = entry.split("=", 1)
                headers[k.strip()] = v.strip()

    return OTelConfig(
        service_name=os.getenv("OTEL_SERVICE_NAME", "meho"),
        service_version=os.getenv("OTEL_SERVICE_VERSION", "1.0.0"),
        environment=os.getenv("ENVIRONMENT", os.getenv("ENV", "dev")),
        otlp_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
        otlp_protocol=os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf"),
        otlp_headers=headers,
        console_enabled=os.getenv("OTEL_CONSOLE", "true").lower() == "true",
        console_format=os.getenv("OTEL_CONSOLE_FORMAT", "pretty"),
        log_level=os.getenv("MEHO_LOG_LEVEL", "INFO").upper(),
        trace_sample_rate=float(os.getenv("OTEL_TRACE_SAMPLE_RATE", "1.0")),
        batch_delay_ms=int(os.getenv("OTEL_BATCH_DELAY_MS", "5000")),
        max_batch_size=int(os.getenv("OTEL_MAX_BATCH_SIZE", "512")),
    )
