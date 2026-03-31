# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""SDDC Manager operations for VMware connector (VCF lifecycle visibility).

Provides read-only lifecycle visibility operations via the SDDC Manager REST
API (/v1/) -- workload domains, hosts, clusters, update compliance, and
certificate health.  All methods reference ``self._sddc_client`` -- a
:class:`VMwareRESTClient` instance whose lifecycle is managed by
:class:`VMwareConnector` (Plan 04).
"""

from __future__ import annotations

import time
from typing import Any

from meho_app.core.otel import get_logger

logger = get_logger(__name__)

_SDDC_NOT_CONFIGURED = (
    "SDDC Manager not configured. "
    "Add sddc_host, sddc_username, sddc_password to this connector's credentials."
)

# Token is considered expired after 55 minutes (5-min buffer on 60-min TTL)
_SDDC_TOKEN_TTL_SECONDS = 55 * 60


class SddcHandlerMixin:
    """SDDC Manager lifecycle visibility operations (read-only per D-18).

    Covers system info, workload domains, hosts, clusters, update history,
    certificates, and prechecks.  Token-based authentication with 55-minute
    expiry and auto-refresh.  Graceful degradation when SDDC Manager not
    configured.
    """

    # Instance variables set by VMwareConnector.__init__ (Plan 04).
    # Declared here for IDE type-checking only.
    _sddc_client: Any
    _sddc_auth_client: Any
    _sddc_access_token: str | None
    _sddc_refresh_token_id: str | None
    _sddc_token_time: float | None
    _sddc_username: str | None
    _sddc_password: str | None

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    async def _ensure_sddc_token(self) -> None:
        """Ensure a valid SDDC Manager access token is available.

        If the token is missing or expired (>55 minutes old), authenticate
        via POST /v1/tokens and update the data-plane client headers with
        the new Bearer token.

        Raises:
            RuntimeError: If token acquisition fails.
        """
        now = time.monotonic()

        # Check if current token is still valid
        if (
            self._sddc_access_token
            and self._sddc_token_time
            and (now - self._sddc_token_time) < _SDDC_TOKEN_TTL_SECONDS
        ):
            return

        logger.info("Acquiring new SDDC Manager access token")
        try:
            token_data = await self._sddc_auth_client.post(
                "/v1/tokens",
                json={
                    "username": self._sddc_username,
                    "password": self._sddc_password,
                },
            )
            self._sddc_access_token = token_data["accessToken"]
            self._sddc_refresh_token_id = token_data.get("refreshToken", {}).get("id")
            self._sddc_token_time = now

            # Update the data-plane client with the new Bearer token
            await self._sddc_client.update_headers(
                {
                    "Authorization": f"Bearer {self._sddc_access_token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                }
            )
            logger.info("SDDC Manager access token acquired successfully")
        except Exception as e:
            # nosemgrep: python-logger-credential-disclosure -- logs exception message, not the token itself
            logger.error("Failed to acquire SDDC Manager token: %s", e, exc_info=True)
            raise RuntimeError(f"SDDC Manager token acquisition failed: {e}") from e

    def _sddc_available(self) -> bool:
        """Return True if the SDDC REST client is connected."""
        return bool(self._sddc_client and self._sddc_client.is_connected)

    # ------------------------------------------------------------------
    # 1. System Info
    # ------------------------------------------------------------------

    async def _get_sddc_system_info(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get SDDC Manager version, build, hostname, and FQDN."""
        if not self._sddc_available():
            return {"error": _SDDC_NOT_CONFIGURED}

        try:
            await self._ensure_sddc_token()
            data = await self._sddc_client.get("/v1/system")
            return {
                "id": data.get("id"),
                "version": data.get("version"),
                "build": data.get("build"),
                "fqdn": data.get("fqdn"),
                "hostname": data.get("hostname"),
                "ip_address": data.get("ipAddress"),
                "dns_name": data.get("dnsName"),
                "domain": data.get("domain"),
            }
        except Exception as e:
            logger.error("Failed to get SDDC system info: %s", e, exc_info=True)
            raise RuntimeError(f"SDDC get_system_info failed: {e}") from e

    # ------------------------------------------------------------------
    # 2. Workload Domains (list)
    # ------------------------------------------------------------------

    async def _list_sddc_workload_domains(
        self, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List VCF workload domains to understand infrastructure topology."""
        if not self._sddc_available():
            return [{"error": _SDDC_NOT_CONFIGURED}]

        try:
            await self._ensure_sddc_token()
            data = await self._sddc_client.get("/v1/domains")
            elements = data.get("elements", [])
            return [
                {
                    "id": d.get("id"),
                    "name": d.get("name"),
                    "type": d.get("type"),
                    "status": d.get("status"),
                    "vcenters": [
                        vc.get("fqdn") for vc in d.get("vcenters", [])
                    ],
                    "clusters": [
                        {
                            "id": c.get("id"),
                            "name": c.get("name"),
                            "host_count": len(c.get("hosts", [])),
                        }
                        for c in d.get("clusters", [])
                    ],
                }
                for d in elements
            ]
        except Exception as e:
            logger.error("Failed to list SDDC workload domains: %s", e, exc_info=True)
            raise RuntimeError(f"SDDC list_workload_domains failed: {e}") from e

    # ------------------------------------------------------------------
    # 3. Workload Domain (get by ID)
    # ------------------------------------------------------------------

    async def _get_sddc_workload_domain(
        self, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Get full details of a specific VCF workload domain."""
        if not self._sddc_available():
            return {"error": _SDDC_NOT_CONFIGURED}

        domain_id = params.get("domain_id")
        if not domain_id:
            raise ValueError("domain_id is required")

        try:
            await self._ensure_sddc_token()
            d = await self._sddc_client.get(f"/v1/domains/{domain_id}")
            return {
                "id": d.get("id"),
                "name": d.get("name"),
                "type": d.get("type"),
                "status": d.get("status"),
                "vcenters": d.get("vcenters", []),
                "clusters": d.get("clusters", []),
                "nsx_cluster": d.get("nsxCluster"),
                "network_pools": d.get("networkPools", []),
                "sso_id": d.get("ssoId"),
                "sso_name": d.get("ssoName"),
                "is_management_sso_domain": d.get("isManagementSsoDomain"),
            }
        except Exception as e:
            logger.error(
                "Failed to get SDDC workload domain %s: %s",
                domain_id, e, exc_info=True,
            )
            raise RuntimeError(
                f"SDDC get_workload_domain failed for {domain_id}: {e}"
            ) from e

    # ------------------------------------------------------------------
    # 4. Hosts
    # ------------------------------------------------------------------

    async def _list_sddc_hosts(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """List VCF-managed hosts with assignment status and hardware info."""
        if not self._sddc_available():
            return [{"error": _SDDC_NOT_CONFIGURED}]

        try:
            await self._ensure_sddc_token()
            data = await self._sddc_client.get("/v1/hosts")
            elements = data.get("elements", [])
            return [
                {
                    "id": h.get("id"),
                    "fqdn": h.get("fqdn"),
                    "status": h.get("status"),
                    "domain": h.get("domain", {}).get("name") if isinstance(h.get("domain"), dict) else h.get("domain"),
                    "cluster": h.get("cluster", {}).get("name") if isinstance(h.get("cluster"), dict) else h.get("cluster"),
                    "hardware": {
                        "model": h.get("hardwareModel", h.get("hardware", {}).get("model")),
                        "vendor": h.get("hardwareVendor", h.get("hardware", {}).get("vendor")),
                    },
                    "ip_addresses": h.get("ipAddresses", []),
                }
                for h in elements
            ]
        except Exception as e:
            logger.error("Failed to list SDDC hosts: %s", e, exc_info=True)
            raise RuntimeError(f"SDDC list_hosts failed: {e}") from e

    # ------------------------------------------------------------------
    # 5. Clusters
    # ------------------------------------------------------------------

    async def _list_sddc_clusters(
        self, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List VCF clusters with host counts and datastore configuration."""
        if not self._sddc_available():
            return [{"error": _SDDC_NOT_CONFIGURED}]

        try:
            await self._ensure_sddc_token()
            data = await self._sddc_client.get("/v1/clusters")
            elements = data.get("elements", [])
            return [
                {
                    "id": c.get("id"),
                    "name": c.get("name"),
                    "primary_datastore_name": c.get("primaryDatastoreName"),
                    "primary_datastore_type": c.get("primaryDatastoreType"),
                    "host_count": len(c.get("hosts", [])),
                    "is_stretched": c.get("isStretched", False),
                    "is_default": c.get("isDefault", False),
                }
                for c in elements
            ]
        except Exception as e:
            logger.error("Failed to list SDDC clusters: %s", e, exc_info=True)
            raise RuntimeError(f"SDDC list_clusters failed: {e}") from e

    # ------------------------------------------------------------------
    # 6. Update History
    # ------------------------------------------------------------------

    async def _get_sddc_update_history(
        self, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Get VCF upgrade/update history to check what versions have been applied."""
        if not self._sddc_available():
            return [{"error": _SDDC_NOT_CONFIGURED}]

        try:
            await self._ensure_sddc_token()
            data = await self._sddc_client.get("/v1/upgrades")
            elements = data.get("elements", [])
            return [
                {
                    "id": u.get("id"),
                    "status": u.get("status"),
                    "description": u.get("description"),
                    "created_at": u.get("createdAt"),
                    "release_version": u.get("releaseVersion"),
                    "upgrade_type": u.get("upgradeType"),
                }
                for u in elements
            ]
        except Exception as e:
            logger.error("Failed to get SDDC update history: %s", e, exc_info=True)
            raise RuntimeError(f"SDDC get_update_history failed: {e}") from e

    # ------------------------------------------------------------------
    # 7. Certificates
    # ------------------------------------------------------------------

    async def _get_sddc_certificates(
        self, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Check certificate health to diagnose TLS issues in the VCF environment."""
        if not self._sddc_available():
            return [{"error": _SDDC_NOT_CONFIGURED}]

        try:
            await self._ensure_sddc_token()
            data = await self._sddc_client.get("/v1/certificates")
            elements = data.get("elements", [])
            return [
                {
                    "subject": cert.get("subject"),
                    "issuer": cert.get("issuer"),
                    "valid_from": cert.get("notBefore", cert.get("validFrom")),
                    "valid_to": cert.get("notAfter", cert.get("validTo")),
                    "thumbprint": cert.get("thumbprint"),
                    "is_installed": cert.get("isInstalled"),
                    "certificate_type": cert.get("certificateType"),
                    "domain": cert.get("domain"),
                }
                for cert in elements
            ]
        except Exception as e:
            logger.error("Failed to get SDDC certificates: %s", e, exc_info=True)
            raise RuntimeError(f"SDDC get_certificates failed: {e}") from e

    # ------------------------------------------------------------------
    # 8. Prechecks
    # ------------------------------------------------------------------

    async def _get_sddc_prechecks(
        self, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Get compliance/precheck results to verify upgrade readiness."""
        if not self._sddc_available():
            return [{"error": _SDDC_NOT_CONFIGURED}]

        try:
            await self._ensure_sddc_token()
            data = await self._sddc_client.get("/v1/system/prechecks")
            elements = data.get("elements", [])
            return [
                {
                    "status": pc.get("status"),
                    "precheck_type": pc.get("precheckType"),
                    "description": pc.get("description"),
                    "result": pc.get("result"),
                    "resource_name": pc.get("resourceName"),
                    "resource_type": pc.get("resourceType"),
                }
                for pc in elements
            ]
        except Exception as e:
            logger.error("Failed to get SDDC prechecks: %s", e, exc_info=True)
            raise RuntimeError(f"SDDC get_prechecks failed: {e}") from e
