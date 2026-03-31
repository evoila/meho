# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""TLS certificate check tool - Validate TLS certificate for a hostname.

Uses Python's ssl module with asyncio for async TLS handshake. Returns
certificate subject, issuer, expiry, SANs, chain validity, and protocol
version. Emits TLSCertificate topology entities for investigation breadcrumbs.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, Field

from meho_app.modules.agents.base.tool import BaseTool

if TYPE_CHECKING:
    from meho_app.modules.agents.sse.emitter import EventEmitter

logger = logging.getLogger(__name__)


class TlsCheckInput(BaseModel):
    """Input for tls_check tool."""

    hostname: str = Field(description="Hostname to check TLS certificate for")
    port: int = Field(
        default=443,
        ge=1,
        le=65535,
        description="TLS port number (default: 443)",
    )
    timeout_seconds: float = Field(
        default=10.0,
        ge=1.0,
        le=30.0,
        description="Connection timeout in seconds",
    )


class TlsCheckOutput(BaseModel):
    """Output from tls_check tool."""

    hostname: str = Field(description="The checked hostname")
    port: int = Field(description="The checked port")
    subject: dict[str, str] = Field(default_factory=dict, description="Certificate subject fields")
    issuer: dict[str, str] = Field(default_factory=dict, description="Certificate issuer fields")
    expires_at: str = Field(default="", description="Certificate expiry in ISO format")
    days_until_expiry: int = Field(default=0, description="Days until certificate expires")
    sans: list[str] = Field(default_factory=list, description="Subject Alternative Names")
    protocol_version: str = Field(default="", description="TLS protocol version")
    chain_valid: bool = Field(default=False, description="Whether the certificate chain is valid")
    latency_ms: float = Field(default=0.0, description="TLS handshake latency in milliseconds")
    serial_number: str | None = Field(default=None, description="Certificate serial number")
    error: str | None = Field(default=None, description="Error message if check failed")
    success: bool = Field(default=False, description="True if TLS check completed successfully")


@dataclass
class TlsCheckTool(BaseTool[TlsCheckInput, TlsCheckOutput]):
    """Check TLS certificate details for a hostname.

    Attributes:
        TOOL_NAME: Unique identifier.
        TOOL_DESCRIPTION: LLM-facing description.
    """

    TOOL_NAME: ClassVar[str] = "tls_check"
    TOOL_DESCRIPTION: ClassVar[str] = """Check TLS certificate for a hostname.
Returns subject, issuer, expiry date, SANs, chain validity, and protocol version.
Use to diagnose certificate issues, check expiry, or verify TLS configuration."""
    InputSchema: ClassVar[type[BaseModel]] = TlsCheckInput
    OutputSchema: ClassVar[type[BaseModel]] = TlsCheckOutput

    async def execute(
        self,
        tool_input: TlsCheckInput,
        deps: Any,
        emitter: EventEmitter,
    ) -> TlsCheckOutput:
        """Execute TLS certificate check for the given hostname."""
        await emitter.tool_start(self.TOOL_NAME)

        ctx = ssl.create_default_context()

        try:
            start = time.perf_counter()
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    tool_input.hostname,
                    tool_input.port,
                    ssl=ctx,
                    server_hostname=tool_input.hostname,
                ),
                timeout=tool_input.timeout_seconds,
            )
            latency_ms = (time.perf_counter() - start) * 1000

            # Extract certificate
            ssl_object = writer.get_extra_info("ssl_object")
            cert = ssl_object.getpeercert()
            protocol_version = ssl_object.version() or ""

            # Parse subject and issuer from nested tuples
            subject = _parse_cert_dn(cert.get("subject", ()))
            issuer = _parse_cert_dn(cert.get("issuer", ()))

            # Parse SANs
            sans = [
                value for san_type, value in cert.get("subjectAltName", [])
                if san_type == "DNS"
            ]

            # Parse expiry
            not_after = cert.get("notAfter", "")
            expires_at = ""
            days_until_expiry = 0
            if not_after:
                expiry_epoch = ssl.cert_time_to_seconds(not_after)
                expiry_dt = datetime.fromtimestamp(expiry_epoch, tz=UTC)
                expires_at = expiry_dt.isoformat()
                days_until_expiry = (expiry_dt - datetime.now(UTC)).days

            # Serial number
            serial_number = cert.get("serialNumber")

            # Clean shutdown
            writer.close()
            await writer.wait_closed()

            output = TlsCheckOutput(
                hostname=tool_input.hostname,
                port=tool_input.port,
                subject=subject,
                issuer=issuer,
                expires_at=expires_at,
                days_until_expiry=days_until_expiry,
                sans=sans,
                protocol_version=protocol_version,
                chain_valid=True,
                latency_ms=round(latency_ms, 2),
                serial_number=serial_number,
                success=True,
            )

            # Fire-and-forget topology emission
            try:
                await _emit_topology(
                    hostname=tool_input.hostname,
                    port=tool_input.port,
                    subject=subject,
                    issuer=issuer,
                    expires_at=expires_at,
                    days_until_expiry=days_until_expiry,
                    sans=sans,
                    chain_valid=True,
                    protocol_version=protocol_version,
                    deps=deps,
                )
            except Exception:
                logger.debug(
                    "Topology emission failed for tls_check(%s:%d), continuing",
                    tool_input.hostname,
                    tool_input.port,
                    exc_info=True,
                )

            await emitter.tool_complete(self.TOOL_NAME, success=True)
            return output

        except ssl.SSLCertVerificationError as e:
            await emitter.tool_complete(self.TOOL_NAME, success=True)
            return TlsCheckOutput(
                hostname=tool_input.hostname,
                port=tool_input.port,
                chain_valid=False,
                error=f"Certificate verification failed: {e}",
                success=True,  # The check completed, cert is just invalid
            )
        except TimeoutError:
            await emitter.tool_complete(self.TOOL_NAME, success=False)
            return TlsCheckOutput(
                hostname=tool_input.hostname,
                port=tool_input.port,
                error=f"TLS handshake timed out after {tool_input.timeout_seconds}s",
                success=False,
            )
        except OSError as e:
            await emitter.tool_complete(self.TOOL_NAME, success=False)
            return TlsCheckOutput(
                hostname=tool_input.hostname,
                port=tool_input.port,
                error=f"Connection error: {e}",
                success=False,
            )
        except Exception as e:
            await emitter.tool_complete(self.TOOL_NAME, success=False)
            return TlsCheckOutput(
                hostname=tool_input.hostname,
                port=tool_input.port,
                error=f"Unexpected error: {e}",
                success=False,
            )


def _parse_cert_dn(dn_tuples: tuple[Any, ...]) -> dict[str, str]:
    """Parse certificate subject/issuer from nested tuple structure.

    ssl.getpeercert() returns DN as ((('commonName', 'example.com'),),)
    We flatten it to {'commonName': 'example.com', 'organizationName': '...'}.
    """
    result: dict[str, str] = {}
    for rdn in dn_tuples:
        for attr_type, attr_value in rdn:
            result[attr_type] = attr_value
    return result


async def _emit_topology(
    *,
    hostname: str,
    port: int,
    subject: dict[str, str],
    issuer: dict[str, str],
    expires_at: str,
    days_until_expiry: int,
    sans: list[str],
    chain_valid: bool,
    protocol_version: str,
    deps: Any,
) -> None:
    """Emit TLSCertificate topology entity (fire-and-forget)."""
    meho_deps = getattr(deps, "meho_deps", None)
    if not meho_deps:
        return

    session = getattr(meho_deps, "db_session", None)
    if not session:
        return

    tenant_id = getattr(meho_deps, "tenant_id", "default")

    from meho_app.modules.topology.schemas import (
        StoreDiscoveryInput,
        TopologyEntityCreate,
    )
    from meho_app.modules.topology.service import TopologyService

    now_iso = datetime.now(UTC).isoformat()
    subject_cn = subject.get("commonName", hostname)
    issuer_org = issuer.get("organizationName", issuer.get("commonName", "unknown"))

    entity = TopologyEntityCreate(
        name=f"TLS:{hostname}:{port}",
        entity_type="TLSCertificate",
        connector_type="network_diagnostics",
        canonical_id=f"{hostname}:{port}",
        description=(
            f"TLS certificate for {hostname}:{port} "
            f"(CN={subject_cn}, issuer={issuer_org}, "
            f"expires={expires_at}, days_left={days_until_expiry})"
        ),
        raw_attributes={
            "subject": subject_cn,
            "issuer": issuer_org,
            "expires_at": expires_at,
            "days_until_expiry": days_until_expiry,
            "sans": sans,
            "chain_valid": chain_valid,
            "protocol_version": protocol_version,
            "last_checked": now_iso,
        },
    )

    service = TopologyService(session)
    await service.store_discovery(
        input=StoreDiscoveryInput(
            connector_type="network_diagnostics",
            entities=[entity],
        ),
        tenant_id=tenant_id,
    )
