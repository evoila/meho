# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for router-level enterprise feature gating.

Phase 80: Tests that enterprise routers are conditionally registered
based on LicenseService edition, and that the license API endpoint
is always available without authentication.

These tests verify the gating logic at the module/function level rather
than spinning up full app instances (which require DB, Redis, Keycloak).
For full integration tests of the running app, see tests/integration/.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meho_app.api.routes_license import router as license_router
from meho_app.core.licensing import Edition, LicenseService


# ============================================================================
# License module tests (singleton and config integration)
# ============================================================================


class TestLicenseServiceSingleton:
    """get_license_service() caching and config integration."""

    def test_community_when_no_key(self):
        """No MEHO_LICENSE_KEY -> community mode via get_license_service()."""
        from meho_app.core.licensing import get_license_service

        # Clear LRU cache to force re-init
        get_license_service.cache_clear()

        # Mock get_config to return a config with no license_key
        mock_config = MagicMock()
        mock_config.license_key = None

        with patch("meho_app.core.config.get_config", return_value=mock_config):
            svc = get_license_service()
            assert svc.edition == Edition.COMMUNITY
            assert not svc.is_enterprise

        # Clean up
        get_license_service.cache_clear()

    def test_enterprise_with_valid_key(self, patch_license_public_key, test_license_key):
        """Valid MEHO_LICENSE_KEY -> enterprise mode via get_license_service()."""
        from meho_app.core.licensing import get_license_service

        get_license_service.cache_clear()

        mock_config = MagicMock()
        mock_config.license_key = test_license_key

        with patch("meho_app.core.config.get_config", return_value=mock_config):
            svc = get_license_service()
            assert svc.edition == Edition.ENTERPRISE
            assert svc.is_enterprise

        get_license_service.cache_clear()


# ============================================================================
# Enterprise route inventory verification
# ============================================================================


class TestEnterpriseRouteInventory:
    """Verify the enterprise route modules exist and have correct paths."""

    def test_enterprise_sessions_router_has_team_endpoint(self):
        """routes_enterprise_sessions.py has /chat/sessions/team."""
        from meho_app.api.routes_enterprise_sessions import router

        paths = [r.path for r in router.routes]
        assert any("/chat/sessions/team" in p for p in paths)

    def test_admin_router_exists(self):
        """routes_admin.py router is importable."""
        from meho_app.api.routes_admin import router

        assert router is not None

    def test_tenants_router_exists(self):
        """routes_tenants.py router is importable."""
        from meho_app.api.routes_tenants import router

        assert router is not None

    def test_audit_router_exists(self):
        """routes_audit.py router is importable."""
        from meho_app.api.routes_audit import router

        assert router is not None


# ============================================================================
# Router-level gating logic verification
# ============================================================================


class TestRouterGatingLogic:
    """
    Verify the conditional router registration pattern.

    Instead of creating full app instances, we test the gating logic
    by building minimal FastAPI apps with the same conditional pattern
    used in main.py's create_app().
    """

    def _build_gated_app(self, is_enterprise: bool) -> FastAPI:
        """
        Build a minimal FastAPI app that mimics main.py's gating pattern.

        This replicates the exact conditional logic from create_app()
        without requiring database, Redis, or Keycloak connections.
        """
        from meho_app.api.routes_enterprise_sessions import router as enterprise_sessions_router
        from meho_app.api.routes_license import router as license_router_local

        app = FastAPI()

        # Community router (always included -- just a test endpoint)
        from fastapi import APIRouter

        community_router = APIRouter()

        @community_router.get("/chat/test")
        async def test_chat():
            return {"status": "ok"}

        app.include_router(community_router, prefix="/api", tags=["chat"])

        # Enterprise routers (conditional -- same pattern as main.py)
        if is_enterprise:
            # In the real app, these are admin_router, tenants_router, audit_router
            enterprise_admin = APIRouter()

            @enterprise_admin.get("/users")
            async def admin_users():
                return []

            tenants_api = APIRouter()

            @tenants_api.get("/tenants")
            async def list_tenants():
                return []

            audit_api = APIRouter()

            @audit_api.get("/events")
            async def audit_events():
                return []

            app.include_router(enterprise_admin, prefix="/api/admin", tags=["admin"])
            app.include_router(tenants_api, prefix="/api", tags=["tenants"])
            app.include_router(audit_api, prefix="/api/audit", tags=["audit"])
            app.include_router(
                enterprise_sessions_router, prefix="/api", tags=["enterprise-sessions"]
            )

        # License endpoint (always included)
        app.include_router(license_router_local, prefix="/api/v1", tags=["license"])

        return app

    def test_enterprise_routes_absent_in_community(self):
        """Community app has NO enterprise routes."""
        app = self._build_gated_app(is_enterprise=False)
        paths = [r.path for r in app.routes]

        assert not any("/tenants" in p for p in paths), f"Found /tenants in community: {paths}"
        assert not any("/admin" in p for p in paths), f"Found /admin in community: {paths}"
        assert not any("/audit" in p for p in paths), f"Found /audit in community: {paths}"
        assert not any(
            "/chat/sessions/team" in p for p in paths
        ), f"Found /chat/sessions/team in community: {paths}"

    def test_enterprise_routes_present_in_enterprise(self):
        """Enterprise app HAS enterprise routes."""
        app = self._build_gated_app(is_enterprise=True)
        paths = [r.path for r in app.routes]

        assert any("/tenants" in p for p in paths), f"Missing /tenants in enterprise: {paths}"
        assert any("/admin" in p for p in paths), f"Missing /admin in enterprise: {paths}"
        assert any("/audit" in p for p in paths), f"Missing /audit in enterprise: {paths}"
        assert any(
            "/chat/sessions/team" in p for p in paths
        ), f"Missing /chat/sessions/team in enterprise: {paths}"

    def test_community_routes_always_present(self):
        """Community routes exist in both community and enterprise apps."""
        for is_enterprise in [False, True]:
            app = self._build_gated_app(is_enterprise=is_enterprise)
            paths = [r.path for r in app.routes]
            assert any("/chat/test" in p for p in paths), f"Missing community route: {paths}"

    def test_openapi_community_excludes_enterprise(self):
        """Community OpenAPI spec omits enterprise tags."""
        app = self._build_gated_app(is_enterprise=False)
        schema = app.openapi()
        all_paths = list(schema.get("paths", {}).keys())

        assert not any("/tenants" in p for p in all_paths), f"OpenAPI leaks /tenants: {all_paths}"
        assert not any("/admin" in p for p in all_paths), f"OpenAPI leaks /admin: {all_paths}"
        assert not any("/audit" in p for p in all_paths), f"OpenAPI leaks /audit: {all_paths}"

    def test_openapi_enterprise_includes_all(self):
        """Enterprise OpenAPI spec includes enterprise paths."""
        app = self._build_gated_app(is_enterprise=True)
        schema = app.openapi()
        all_paths = list(schema.get("paths", {}).keys())

        assert any("/tenants" in p for p in all_paths), f"Missing /tenants in OpenAPI: {all_paths}"
        assert any("/admin" in p for p in all_paths), f"Missing /admin in OpenAPI: {all_paths}"
        assert any("/audit" in p for p in all_paths), f"Missing /audit in OpenAPI: {all_paths}"


# ============================================================================
# License endpoint tests (using a minimal FastAPI app)
# ============================================================================


class TestLicenseEndpoint:
    """
    /api/v1/license endpoint tests using a minimal FastAPI app.

    Verifies the endpoint is public (no auth), returns correct shape,
    and reflects the current edition.
    """

    def _build_license_app(self, license_svc: LicenseService) -> FastAPI:
        """Build a minimal app with just the license endpoint."""
        from meho_app.core.licensing import get_license_service

        app = FastAPI()
        app.include_router(license_router, prefix="/api/v1", tags=["license"])

        # Override the dependency to use our test service
        app.dependency_overrides[get_license_service] = lambda: license_svc
        return app

    def test_license_endpoint_no_auth(self):
        """GET /api/v1/license returns 200 without Authorization header."""
        svc = LicenseService()  # community
        app = self._build_license_app(svc)
        client = TestClient(app)

        response = client.get("/api/v1/license")
        assert response.status_code == 200

    def test_license_endpoint_community(self):
        """Community license endpoint returns correct shape and values."""
        svc = LicenseService()
        app = self._build_license_app(svc)
        client = TestClient(app)

        response = client.get("/api/v1/license")
        assert response.status_code == 200

        data = response.json()
        assert data["edition"] == "community"
        assert data["features"] == []
        assert data["org"] is None
        assert data["expires_at"] is None
        assert data["in_grace_period"] is False

    def test_license_endpoint_enterprise(self, patch_license_public_key, test_license_key):
        """Enterprise license endpoint returns correct shape and values."""
        svc = LicenseService(license_key=test_license_key)
        app = self._build_license_app(svc)
        client = TestClient(app)

        response = client.get("/api/v1/license")
        assert response.status_code == 200

        data = response.json()
        assert data["edition"] == "enterprise"
        assert "multi_tenancy" in data["features"]
        assert data["org"] == "Test Org"
        assert data["in_grace_period"] is False

    def test_license_endpoint_response_shape(self):
        """Response contains exactly the expected keys."""
        svc = LicenseService()
        app = self._build_license_app(svc)
        client = TestClient(app)

        response = client.get("/api/v1/license")
        data = response.json()

        expected_keys = {"edition", "features", "org", "expires_at", "in_grace_period"}
        assert set(data.keys()) == expected_keys


# ============================================================================
# Main.py conditional pattern verification
# ============================================================================


class TestMainPyGatingPattern:
    """
    Verify main.py contains the correct gating patterns.

    Uses file reading instead of module import because importing main.py
    triggers create_app() which requires full config (DB URL, API keys).
    """

    @pytest.fixture(autouse=True)
    def _load_main_source(self):
        """Load main.py source once for all tests in this class."""
        from pathlib import Path

        main_path = Path(__file__).parent.parent.parent / "meho_app" / "main.py"
        self._main_source = main_path.read_text()

    def test_main_imports_licensing(self):
        """main.py imports get_license_service."""
        assert "from meho_app.core.licensing import get_license_service" in self._main_source

    def test_main_has_enterprise_conditional(self):
        """main.py has the is_enterprise conditional for router gating."""
        assert "license_svc.is_enterprise" in self._main_source

    def test_main_has_license_router(self):
        """main.py registers the license router."""
        assert "license_router" in self._main_source
        assert 'prefix="/api/v1"' in self._main_source

    def test_team_endpoint_removed_from_chat_sessions(self):
        """routes_chat_sessions.py no longer has list_team_sessions."""
        import inspect

        from meho_app.api import routes_chat_sessions

        source = inspect.getsource(routes_chat_sessions)
        assert "def list_team_sessions" not in source

    def test_audit_purge_gated(self):
        """Audit purge in lifespan is gated behind enterprise check."""
        assert "_license_svc.is_enterprise" in self._main_source
