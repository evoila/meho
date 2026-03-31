# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Connection and authentication testing operations.

Handles testing connector connections and authentication flows.
"""

# mypy: disable-error-code="no-untyped-def,arg-type,attr-defined"
import time
from datetime import UTC, datetime
from urllib.parse import urljoin

from fastapi import APIRouter, Depends, HTTPException

from meho_app.api.auth import get_current_user
from meho_app.api.connectors.schemas import (
    TestAuthRequest,
    TestAuthResponse,
    TestConnectionRequest,
    TestConnectionResponse,
)
from meho_app.core.auth_context import UserContext
from meho_app.core.otel import get_logger

logger = get_logger(__name__)

router = APIRouter()


@router.post("/{connector_id}/test-connection", response_model=TestConnectionResponse)
async def test_connector_connection(
    connector_id: str,
    request: TestConnectionRequest,
    user: UserContext = Depends(get_current_user),  # noqa: PT028 -- intentional default value
):
    """
    Test connection to a connector.

    For typed connectors (VMware, Proxmox): Uses native SDK connection test
    For REST connectors: Attempts to call a safe test endpoint (GET) to verify:
    - Base URL is correct
    - Credentials work
    - Network connectivity is good

    Can test with:
    - Stored credentials (use_stored_credentials=True)
    - New credentials before saving (provide credentials dict)
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.repositories import ConnectorRepository
    from meho_app.modules.connectors.repositories.credential_repository import (
        UserCredentialRepository,
    )
    from meho_app.modules.connectors.rest.endpoint_testing import OpenAPIService
    from meho_app.modules.connectors.rest.http_client import GenericHTTPClient

    session_maker = create_openapi_session_maker()

    async with session_maker() as session:
        # Check if this is a typed connector (VMware, Proxmox)
        connector_repo = ConnectorRepository(session)
        connector = await connector_repo.get_connector(connector_id, user.tenant_id)

        if not connector:
            raise HTTPException(status_code=404, detail="Connector not found")

        # Handle typed connectors with native SDK
        if connector.connector_type in (
            "vmware",
            "proxmox",
            "gcp",
            "kubernetes",
            "argocd",
            "github",
        ):
            return await _test_typed_connector_connection(
                connector, connector_id, request, user, session
            )

        # REST connector flow
        service = OpenAPIService(session)
        await service.get_connector(connector_id, user.tenant_id)

        test_endpoint = await service.find_test_endpoint(connector_id)

        if not test_endpoint:
            return TestConnectionResponse(
                success=False,
                message="No GET endpoints available to test",
                error_detail="Upload an OpenAPI spec first",
            )

        if request.credentials:
            client = GenericHTTPClient(timeout=10.0)
            connector_schema = service.connector_to_schema(connector)

            start_time = time.time()
            try:
                logger.info(
                    f"Testing connection with custom credentials to {connector.base_url}{test_endpoint.path}"
                )
                status_code, response_data = await client.call_endpoint(
                    connector=connector_schema,
                    endpoint=test_endpoint,
                    path_params={},
                    query_params={},
                    body=None,
                    user_credentials=request.credentials,
                )
                duration_ms = int((time.time() - start_time) * 1000)

            except Exception as e:
                duration_ms = int((time.time() - start_time) * 1000)
                return TestConnectionResponse(
                    success=False,
                    message="Connection failed",
                    response_time_ms=duration_ms,
                    tested_endpoint=f"{test_endpoint.method} {test_endpoint.path}",
                    error_detail=str(e)[:200],
                )

        elif request.use_stored_credentials:
            if connector.credential_strategy != "SYSTEM":
                cred_repo = UserCredentialRepository(session)
                user_creds = await cred_repo.get_credentials(user.user_id, connector_id)
                if not user_creds:
                    return TestConnectionResponse(
                        success=False,
                        message="No credentials configured",
                        error_detail="Please provide credentials or save them first",
                    )

            logger.info(
                f"Testing connection with stored credentials to {connector.base_url}{test_endpoint.path}"
            )
            result = await service.test_endpoint(
                user_context=user, connector_id=connector_id, endpoint_id=str(test_endpoint.id)
            )

            status_code = result.status_code or 0
            response_data = result.data
            duration_ms = int(result.duration_ms or 0)

            if not result.success:
                return TestConnectionResponse(
                    success=False,
                    message=result.error or "Connection failed",
                    response_time_ms=duration_ms,
                    tested_endpoint=f"{test_endpoint.method} {test_endpoint.path}",
                    error_detail=result.error,
                )
        else:
            result = await service.test_endpoint(
                user_context=user, connector_id=connector_id, endpoint_id=str(test_endpoint.id)
            )
            status_code = result.status_code or 0
            response_data = result.data
            duration_ms = int(result.duration_ms or 0)

            if not result.success:
                return TestConnectionResponse(
                    success=False,
                    message=result.error or "Connection failed",
                    response_time_ms=duration_ms,
                    tested_endpoint=f"{test_endpoint.method} {test_endpoint.path}",
                    error_detail=result.error,
                )

        if 200 <= status_code < 400:
            return TestConnectionResponse(
                success=True,
                message=f"Connection successful! Endpoint returned {status_code}",
                response_time_ms=duration_ms,
                tested_endpoint=f"{test_endpoint.method} {test_endpoint.path}",
                status_code=status_code,
            )
        elif status_code in [401, 403]:
            return TestConnectionResponse(
                success=False,
                message="Authentication failed",
                response_time_ms=duration_ms,
                tested_endpoint=f"{test_endpoint.method} {test_endpoint.path}",
                status_code=status_code,
                error_detail="Check your credentials",
            )
        else:
            return TestConnectionResponse(
                success=False,
                message=f"Endpoint returned error {status_code}",
                response_time_ms=duration_ms,
                tested_endpoint=f"{test_endpoint.method} {test_endpoint.path}",
                status_code=status_code,
                error_detail=str(response_data)[:200] if response_data else "",
            )


@router.post("/{connector_id}/test-auth", response_model=TestAuthResponse)
async def test_connector_auth(
    connector_id: str,
    request: TestAuthRequest,
    user: UserContext = Depends(get_current_user),  # noqa: PT028 -- intentional default value
):
    """
    Test authentication flow for a connector.

    For SESSION auth: Tests login endpoint and returns session token info
    For BASIC/API_KEY/OAUTH2: Validates credentials are configured
    For NONE: Returns success (no auth needed)
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.repositories import ConnectorRepository
    from meho_app.modules.connectors.repositories.credential_repository import (
        UserCredentialRepository,
    )

    start_time = time.time()

    session_maker = create_openapi_session_maker()

    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)
        cred_repo = UserCredentialRepository(session)

        connector = await connector_repo.get_connector(connector_id, user.tenant_id)
        if not connector:
            raise HTTPException(status_code=404, detail="Connector not found")

        logger.info(
            f"Testing auth for connector {connector.name} (auth_type={connector.auth_type})"
        )

        try:
            if connector.auth_type == "NONE":
                return TestAuthResponse(
                    success=True,
                    message="No authentication required",
                    auth_type=connector.auth_type,
                )

            elif connector.auth_type == "SESSION":
                return await _test_session_auth(
                    connector, connector_id, request, user, cred_repo, start_time
                )

            elif connector.auth_type in ["BASIC", "API_KEY", "OAUTH2"]:
                return await _test_standard_auth(connector, connector_id, request, user, cred_repo)

            else:
                return TestAuthResponse(
                    success=False,
                    message=f"Unknown auth type: {connector.auth_type}",
                    auth_type=connector.auth_type,
                    error_detail="Unsupported auth type",
                )

        except Exception as e:
            logger.error(f"❌ Auth test failed: {e}", exc_info=True)
            return TestAuthResponse(
                success=False,
                message="Auth test failed",
                auth_type=connector.auth_type,
                error_detail=str(e)[:200],
            )


async def _test_typed_connector_connection(connector, connector_id, request, user, session):
    """Test connection for typed connectors (VMware, Proxmox)."""
    from meho_app.modules.connectors.repositories.credential_repository import (
        UserCredentialRepository,
    )

    start_time = time.time()
    cred_repo = UserCredentialRepository(session)

    # Get credentials
    credentials_to_use = request.credentials
    if not credentials_to_use and request.use_stored_credentials:
        credentials_to_use = await cred_repo.get_credentials(user.user_id, connector_id)

    if not credentials_to_use:
        return TestConnectionResponse(
            success=False,
            message="No credentials configured",
            error_detail="Please provide or save credentials first",
        )

    protocol_config = connector.protocol_config or {}

    try:
        if connector.connector_type == "vmware":
            from meho_app.modules.connectors.vmware import VMwareConnector

            typed_connector = VMwareConnector(
                connector_id=connector_id,
                config=protocol_config,
                credentials=credentials_to_use,
            )
            connector_label = "vCenter"
        elif connector.connector_type == "proxmox":
            from meho_app.modules.connectors.proxmox import ProxmoxConnector

            typed_connector = ProxmoxConnector(
                connector_id=connector_id,
                config=protocol_config,
                credentials=credentials_to_use,
            )
            connector_label = "Proxmox VE"
        elif connector.connector_type == "gcp":
            from meho_app.modules.connectors.gcp import GCPConnector

            typed_connector = GCPConnector(
                connector_id=connector_id,
                config=protocol_config,
                credentials=credentials_to_use,
            )
            connector_label = "Google Cloud Platform"
        elif connector.connector_type == "kubernetes":
            from meho_app.modules.connectors.kubernetes import KubernetesConnector

            typed_connector = KubernetesConnector(
                connector_id=connector_id,
                config=protocol_config,
                credentials=credentials_to_use,
            )
            connector_label = "Kubernetes"
        elif connector.connector_type == "argocd":
            from meho_app.modules.connectors.argocd import ArgoConnector

            typed_connector = ArgoConnector(
                connector_id=connector_id,
                config=protocol_config,
                credentials=credentials_to_use,
            )
            connector_label = "ArgoCD"
        elif connector.connector_type == "github":
            from meho_app.modules.connectors.github import GitHubConnector

            typed_connector = GitHubConnector(
                connector_id=connector_id,
                config=protocol_config,
                credentials=credentials_to_use,
            )
            connector_label = "GitHub"
        else:
            return TestConnectionResponse(
                success=False,
                message=f"Unknown typed connector: {connector.connector_type}",
                error_detail="Unsupported connector type",
            )

        await typed_connector.connect()
        is_connected = await typed_connector.test_connection()
        await typed_connector.disconnect()

        duration_ms = int((time.time() - start_time) * 1000)

        if is_connected:
            return TestConnectionResponse(
                success=True,
                message=f"{connector_label} connection successful",
                response_time_ms=duration_ms,
                tested_endpoint=f"{connector_label} API",
            )
        else:
            return TestConnectionResponse(
                success=False,
                message=f"{connector_label} connection failed",
                response_time_ms=duration_ms,
                error_detail="Connection test returned false",
            )
    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)
        logger.error(f"❌ {connector.connector_type} connection test failed: {e}")
        return TestConnectionResponse(
            success=False,
            message=f"{connector.connector_type.title()} connection failed",
            response_time_ms=duration_ms,
            error_detail=str(e)[:200],
        )


async def _test_session_auth(connector, connector_id, request, user, cred_repo, start_time):
    """Test SESSION authentication flow."""

    credentials_to_use = request.credentials
    if not credentials_to_use:
        logger.info(
            f"No credentials in request, fetching stored credentials for user {user.user_id}"
        )
        stored_cred = await cred_repo.get_credentials(user.user_id, connector_id)
        if not stored_cred:
            return TestAuthResponse(
                success=False,
                message="No credentials configured for this connector",
                auth_type=connector.auth_type,
                error_detail="Please configure credentials in the Credentials tab",
            )
        credentials_to_use = stored_cred
        logger.info("Using stored credentials for SESSION auth")

    if not credentials_to_use:
        return TestAuthResponse(
            success=False,
            message="Credentials required for SESSION auth",
            auth_type=connector.auth_type,
            error_detail="Please provide username and password",
        )

    # Handle SOAP session auth
    if connector.connector_type == "soap":
        return await _test_soap_session_auth(connector, credentials_to_use, start_time)

    # Handle VMware session auth
    elif connector.connector_type == "vmware":
        return await _test_vmware_session_auth(
            connector, connector_id, credentials_to_use, start_time
        )

    # Handle Proxmox session auth
    elif connector.connector_type == "proxmox":
        return await _test_proxmox_session_auth(
            connector, connector_id, credentials_to_use, start_time
        )

    # Handle GCP session auth (service account)
    elif connector.connector_type == "gcp":
        return await _test_gcp_session_auth(connector, connector_id, credentials_to_use, start_time)

    # Handle Kubernetes session auth (token)
    elif connector.connector_type == "kubernetes":
        return await _test_kubernetes_session_auth(
            connector, connector_id, credentials_to_use, start_time
        )

    # Handle REST SESSION auth
    else:
        return await _test_rest_session_auth(connector, credentials_to_use, start_time)


async def _test_soap_session_auth(connector, credentials_to_use, start_time):
    """Test SOAP session authentication."""
    try:
        from meho_app.modules.connectors.soap import SOAPAuthType, SOAPConnectorConfig
        from meho_app.modules.connectors.soap.client import VMwareSOAPClient

        protocol_config = connector.protocol_config or {}
        soap_config = SOAPConnectorConfig(
            wsdl_url=protocol_config.get("wsdl_url", ""),
            auth_type=SOAPAuthType.SESSION,
            username=credentials_to_use.get("username"),
            password=credentials_to_use.get("password"),
            login_operation="Login",
            logout_operation="Logout",
            verify_ssl=protocol_config.get("verify_ssl", True),
            timeout=protocol_config.get("timeout", 30),
        )

        async with VMwareSOAPClient(soap_config) as soap_client:
            if soap_client.session_token:
                duration_ms = int((time.time() - start_time) * 1000)
                return TestAuthResponse(
                    success=True,
                    message="SOAP session authentication successful",
                    auth_type=connector.auth_type,
                    session_token_obtained=True,
                    response_time_ms=duration_ms,
                )
            else:
                raise Exception("Login succeeded but no session token returned")
    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)
        logger.error(f"❌ SOAP SESSION auth test failed: {e}")
        return TestAuthResponse(
            success=False,
            message="SOAP authentication failed",
            auth_type=connector.auth_type,
            error_detail=str(e)[:200],
            response_time_ms=duration_ms,
        )


async def _test_vmware_session_auth(connector, connector_id, credentials_to_use, start_time):
    """Test VMware vSphere session authentication."""
    try:
        from meho_app.modules.connectors.vmware import VMwareConnector

        protocol_config = connector.protocol_config or {}
        vmware = VMwareConnector(
            connector_id=connector_id,
            config=protocol_config,
            credentials=credentials_to_use,
        )

        await vmware.connect()
        is_connected = await vmware.test_connection()
        await vmware.disconnect()

        duration_ms = int((time.time() - start_time) * 1000)

        if is_connected:
            return TestAuthResponse(
                success=True,
                message="vCenter connection successful",
                auth_type=connector.auth_type,
                session_token_obtained=True,
                response_time_ms=duration_ms,
            )
        else:
            return TestAuthResponse(
                success=False,
                message="vCenter connection failed",
                auth_type=connector.auth_type,
                error_detail="Connection test returned false",
                response_time_ms=duration_ms,
            )
    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)
        logger.error(f"❌ VMware SESSION auth test failed: {e}")
        return TestAuthResponse(
            success=False,
            message="vCenter authentication failed",
            auth_type=connector.auth_type,
            error_detail=str(e)[:200],
            response_time_ms=duration_ms,
        )


async def _test_proxmox_session_auth(connector, connector_id, credentials_to_use, start_time):
    """Test Proxmox VE authentication."""
    try:
        from meho_app.modules.connectors.proxmox import ProxmoxConnector

        protocol_config = connector.protocol_config or {}
        proxmox = ProxmoxConnector(
            connector_id=connector_id,
            config=protocol_config,
            credentials=credentials_to_use,
        )

        await proxmox.connect()
        is_connected = await proxmox.test_connection()
        await proxmox.disconnect()

        duration_ms = int((time.time() - start_time) * 1000)

        if is_connected:
            return TestAuthResponse(
                success=True,
                message="Proxmox VE connection successful",
                auth_type=connector.auth_type,
                session_token_obtained=True,
                response_time_ms=duration_ms,
            )
        else:
            return TestAuthResponse(
                success=False,
                message="Proxmox VE connection failed",
                auth_type=connector.auth_type,
                error_detail="Connection test returned false",
                response_time_ms=duration_ms,
            )
    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)
        logger.error(f"❌ Proxmox auth test failed: {e}")
        return TestAuthResponse(
            success=False,
            message="Proxmox VE authentication failed",
            auth_type=connector.auth_type,
            error_detail=str(e)[:200],
            response_time_ms=duration_ms,
        )


async def _test_gcp_session_auth(connector, connector_id, credentials_to_use, start_time):
    """Test GCP service account authentication."""
    try:
        from meho_app.modules.connectors.gcp import GCPConnector

        protocol_config = connector.protocol_config or {}
        gcp = GCPConnector(
            connector_id=connector_id,
            config=protocol_config,
            credentials=credentials_to_use,
        )

        await gcp.connect()
        is_connected = await gcp.test_connection()
        await gcp.disconnect()

        duration_ms = int((time.time() - start_time) * 1000)

        if is_connected:
            return TestAuthResponse(
                success=True,
                message="Google Cloud Platform connection successful",
                auth_type=connector.auth_type,
                session_token_obtained=True,
                response_time_ms=duration_ms,
            )
        else:
            return TestAuthResponse(
                success=False,
                message="Google Cloud Platform connection failed",
                auth_type=connector.auth_type,
                error_detail="Connection test returned false",
                response_time_ms=duration_ms,
            )
    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)
        logger.error(f"❌ GCP auth test failed: {e}")
        return TestAuthResponse(
            success=False,
            message="Google Cloud Platform authentication failed",
            auth_type=connector.auth_type,
            error_detail=str(e)[:200],
            response_time_ms=duration_ms,
        )


async def _test_kubernetes_session_auth(connector, connector_id, credentials_to_use, start_time):
    """Test Kubernetes token authentication."""
    try:
        from meho_app.modules.connectors.kubernetes import KubernetesConnector

        protocol_config = connector.protocol_config or {}
        k8s = KubernetesConnector(
            connector_id=connector_id,
            config=protocol_config,
            credentials=credentials_to_use,
        )

        await k8s.connect()
        is_connected = await k8s.test_connection()
        await k8s.disconnect()

        duration_ms = int((time.time() - start_time) * 1000)

        if is_connected:
            version_info = f" (v{k8s.kubernetes_version})" if k8s.kubernetes_version else ""
            return TestAuthResponse(
                success=True,
                message=f"Kubernetes connection successful{version_info}",
                auth_type=connector.auth_type,
                session_token_obtained=True,
                response_time_ms=duration_ms,
            )
        else:
            return TestAuthResponse(
                success=False,
                message="Kubernetes connection failed",
                auth_type=connector.auth_type,
                error_detail="Connection test returned false",
                response_time_ms=duration_ms,
            )
    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)
        logger.error(f"❌ Kubernetes auth test failed: {e}")
        return TestAuthResponse(
            success=False,
            message="Kubernetes authentication failed",
            auth_type=connector.auth_type,
            error_detail=str(e)[:200],
            response_time_ms=duration_ms,
        )


async def _test_rest_session_auth(connector, credentials_to_use, start_time):
    """Test REST SESSION authentication."""
    from meho_app.modules.connectors.rest.session_manager import SessionManager

    if not connector.login_url:
        return TestAuthResponse(
            success=False,
            message="Connector not configured for SESSION auth",
            auth_type=connector.auth_type,
            error_detail="login_url not configured",
        )

    if not connector.login_config:
        return TestAuthResponse(
            success=False,
            message="Connector not configured for SESSION auth",
            auth_type=connector.auth_type,
            error_detail="login_config not configured",
        )

    session_manager = SessionManager()

    login_url = urljoin(connector.base_url, connector.login_url.lstrip("/"))
    login_method = connector.login_method or "POST"

    try:
        (
            session_token,
            refresh_token,
            expires_at,
            refresh_expires_at,
            _session_state,
        ) = await session_manager.login(connector=connector, credentials=credentials_to_use)

        duration_ms = int((time.time() - start_time) * 1000)

        logger.info(f"✅ SESSION auth test successful for {connector.name}")
        logger.info(f"   Session token obtained: {session_token[:20]}...")
        logger.info(f"   Expires at: {expires_at}")

        message = f"Authentication successful (session valid for {(expires_at - datetime.now(tz=UTC)).total_seconds():.0f}s)"
        if refresh_token:
            if refresh_expires_at:
                refresh_valid_secs = (refresh_expires_at - datetime.now(tz=UTC)).total_seconds()
                message += f", refresh token valid for {refresh_valid_secs:.0f}s"
            else:
                message += ", refresh token available"

        return TestAuthResponse(
            success=True,
            message=message,
            auth_type=connector.auth_type,
            session_token_obtained=True,
            session_expires_at=expires_at,
            request_url=login_url,
            request_method=login_method,
            response_status=200,
            response_time_ms=duration_ms,
        )

    except ValueError as e:
        duration_ms = int((time.time() - start_time) * 1000)
        logger.error(f"❌ SESSION auth test failed: {e}")
        return TestAuthResponse(
            success=False,
            message="Authentication failed",
            auth_type=connector.auth_type,
            session_token_obtained=False,
            error_detail=str(e)[:500],
            request_url=login_url,
            request_method=login_method,
            response_time_ms=duration_ms,
        )


async def _test_standard_auth(connector, connector_id, request, user, cred_repo):
    """Test BASIC, API_KEY, or OAUTH2 authentication."""
    credentials = request.credentials

    if not credentials:
        stored_creds = await cred_repo.get_credentials(user.user_id, connector_id)
        if stored_creds:
            credentials = stored_creds
            logger.info(f"Using stored {connector.auth_type} credentials for user {user.user_id}")

    if not credentials and connector.credential_strategy == "USER_PROVIDED":
        return TestAuthResponse(
            success=False,
            message=f"Credentials required for {connector.auth_type} auth",
            auth_type=connector.auth_type,
            error_detail="Please provide credentials",
        )

    if connector.credential_strategy == "SYSTEM" and not connector.auth_config:
        return TestAuthResponse(
            success=False,
            message=f"Connector not configured with {connector.auth_type} credentials",
            auth_type=connector.auth_type,
            error_detail="auth_config is empty",
        )

    creds = credentials or connector.auth_config or {}

    if connector.auth_type == "BASIC":
        if not creds.get("username") or not creds.get("password"):
            return TestAuthResponse(
                success=False,
                message="BASIC auth requires username and password",
                auth_type=connector.auth_type,
                error_detail="Missing username or password",
            )

    elif connector.auth_type == "API_KEY":
        if not creds.get("api_key"):
            return TestAuthResponse(
                success=False,
                message="API_KEY auth requires api_key",
                auth_type=connector.auth_type,
                error_detail="Missing api_key",
            )

    elif connector.auth_type == "OAUTH2" and not creds.get("access_token"):
        return TestAuthResponse(
            success=False,
            message="OAUTH2 auth requires access_token",
            auth_type=connector.auth_type,
            error_detail="Missing access_token",
        )

    return TestAuthResponse(
        success=True,
        message=f"{connector.auth_type} credentials configured",
        auth_type=connector.auth_type,
    )
