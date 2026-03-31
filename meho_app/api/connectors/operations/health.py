# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Connector reachability health check endpoint.

Probes network reachability of all tenant connectors using the cheapest
meaningful method per connector type (TCP for typed, HTTP for REST/SOAP).
No credentials are used -- this is network reachability only.
"""

# mypy: disable-error-code="no-untyped-def,arg-type,attr-defined"
import asyncio
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends

from meho_app.api.auth import get_current_user
from meho_app.core.auth_context import UserContext
from meho_app.core.otel import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["connectors"])

PROBE_TIMEOUT = 5.0  # seconds per connector probe


async def _tcp_probe(host: str, port: int) -> None:
    """TCP connect probe -- proves host:port is reachable."""
    _reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port),
        timeout=PROBE_TIMEOUT,
    )
    writer.close()
    await writer.wait_closed()


async def _http_probe(url: str) -> None:
    """HTTP GET probe -- proves HTTP server responds."""
    async with httpx.AsyncClient(verify=False, timeout=PROBE_TIMEOUT) as client:  # noqa: S501 -- internal service, self-signed cert
        await client.get(url)


def _parse_host_port(base_url: str, default_port: int) -> tuple[str, int]:
    """Extract host and port from a URL, falling back to default_port."""
    parsed = urlparse(base_url)
    host = parsed.hostname or ""
    port = parsed.port or default_port
    return host, port


async def _probe_connector(connector: Any) -> dict:
    """
    Probe a single connector for network reachability.

    Uses the cheapest meaningful probe per connector type:
    - REST/SOAP: HTTP GET (proves server responds)
    - VMware: TCP 443 (vCenter API port)
    - Proxmox: TCP 8006 (Proxmox API port)
    - Kubernetes: TCP to API server host:port (default 6443)
    - GCP: HTTP GET to googleapis.com (proves Google API gateway reachable)
    """
    start = time.monotonic()
    connector_id = str(connector.id)
    connector_type = connector.connector_type or "rest"
    protocol_config = connector.protocol_config or {}

    try:
        if connector_type == "rest":
            await _http_probe(connector.base_url)

        elif connector_type == "soap":
            # Prefer WSDL URL if available, otherwise base_url
            wsdl_url = protocol_config.get("wsdl_url")
            probe_url = wsdl_url if wsdl_url else connector.base_url
            await _http_probe(probe_url)

        elif connector_type == "vmware":
            host, port = _parse_host_port(connector.base_url, 443)
            await _tcp_probe(host, port)

        elif connector_type == "proxmox":
            host, port = _parse_host_port(connector.base_url, 8006)
            await _tcp_probe(host, port)

        elif connector_type == "kubernetes":
            host, port = _parse_host_port(connector.base_url, 6443)
            await _tcp_probe(host, port)

        elif connector_type == "gcp":
            await _http_probe("https://www.googleapis.com/")

        elif connector_type == "prometheus":
            await _http_probe(connector.base_url + "/api/v1/status/buildinfo")

        elif connector_type == "loki" or connector_type == "tempo":
            await _http_probe(connector.base_url + "/ready")

        elif connector_type == "alertmanager":
            await _http_probe(connector.base_url + "/-/ready")

        elif connector_type == "jira":
            # Probe Jira Cloud via HTTPS (site URL is always HTTPS)
            await _http_probe(connector.base_url + "/rest/api/3/serverInfo")

        elif connector_type == "confluence":
            # Probe Confluence Cloud via HTTPS (same Atlassian site)
            base_url = protocol_config.get("base_url", connector.base_url)
            host, port = _parse_host_port(base_url, 443)
            await _tcp_probe(host, port)

        elif connector_type == "email":
            # Email health: lightweight probe only (Pitfall 8 -- NOT a real email)
            provider_type = protocol_config.get("provider_type", "smtp")
            if provider_type == "smtp":
                # TCP probe to SMTP host:port
                smtp_host = protocol_config.get("smtp_host", "")
                smtp_port = protocol_config.get("smtp_port", 587)
                if smtp_host:
                    await _tcp_probe(smtp_host, smtp_port)
                else:
                    raise ValueError("No smtp_host configured")
            elif provider_type == "sendgrid":
                # SendGrid API reachability (not sending email)
                await _http_probe("https://api.sendgrid.com/v3/")
            elif provider_type == "mailgun":
                # Mailgun API reachability
                mailgun_domain = protocol_config.get("mailgun_domain", "")
                await _http_probe(f"https://api.mailgun.net/v3/{mailgun_domain}")
            elif provider_type == "ses":
                # SES uses SMTP -- probe the regional SMTP endpoint
                ses_region = protocol_config.get("ses_region", "us-east-1")
                await _tcp_probe(f"email-smtp.{ses_region}.amazonaws.com", 587)
            elif provider_type == "generic_http":
                # Probe the configured HTTP endpoint
                endpoint_url = protocol_config.get("endpoint_url", "")
                if endpoint_url:
                    await _http_probe(endpoint_url)
                else:
                    raise ValueError("No endpoint_url configured")

        else:
            # Unknown type -- try HTTP probe on base_url as best effort
            await _http_probe(connector.base_url)

        latency_ms = round((time.monotonic() - start) * 1000)
        return {
            "connector_id": connector_id,
            "name": connector.name,
            "connector_type": connector_type,
            "status": "reachable",
            "latency_ms": latency_ms,
            "error": None,
            "last_checked": datetime.now(UTC).isoformat(),
        }

    except Exception as exc:
        latency_ms = round((time.monotonic() - start) * 1000)
        error_msg = str(exc)[:200]
        logger.warning(
            "Connector %s (%s) unreachable: %s",
            connector.name,
            connector_type,
            error_msg,
        )
        return {
            "connector_id": connector_id,
            "name": connector.name,
            "connector_type": connector_type,
            "status": "unreachable",
            "latency_ms": latency_ms,
            "error": error_msg,
            "last_checked": datetime.now(UTC).isoformat(),
        }


@router.get("/health")
async def check_connectors_health(
    user: UserContext = Depends(get_current_user),
):
    """
    Check reachability of all connectors for the current tenant.

    Returns per-connector status (reachable/unreachable), latency, and error details.
    Probes run in parallel with a 5-second timeout per connector.
    No credentials are used -- this is network reachability only.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.repositories import ConnectorRepository

    session_maker = create_openapi_session_maker()

    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)
        connectors = await connector_repo.list_connectors(
            tenant_id=user.tenant_id,
            active_only=True,
        )

    if not connectors:
        return []

    # Probe all connectors in parallel
    results = await asyncio.gather(
        *[_probe_connector(c) for c in connectors],
        return_exceptions=True,
    )

    # Convert any unexpected exceptions to unreachable entries
    final_results = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            connector = connectors[i]
            final_results.append(
                {
                    "connector_id": str(connector.id),
                    "name": connector.name,
                    "connector_type": connector.connector_type or "rest",
                    "status": "unreachable",
                    "latency_ms": None,
                    "error": str(result)[:200],
                    "last_checked": datetime.now(UTC).isoformat(),
                }
            )
        else:
            final_results.append(result)

    return final_results
