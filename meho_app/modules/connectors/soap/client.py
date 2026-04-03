# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
SOAP Client

Executes SOAP operations using the zeep library with support for:
- Multiple authentication types (Basic, Session, WS-Security)
- Async execution
- Session management (for VMware-style auth)
- Error handling with SOAP fault extraction
"""

import time
from datetime import UTC
from typing import Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.soap.models import (
    SOAPAuthType,
    SOAPConnectorConfig,
    SOAPOperation,
    SOAPResponse,
)

logger = get_logger(__name__)


class SOAPClient:
    """Execute SOAP operations using zeep

    This client handles SOAP communication with support for various
    authentication patterns including VMware-style session login.

    Example:
        config = SOAPConnectorConfig(
            wsdl_url="https://vcenter.local/sdk/vimService.wsdl",
            auth_type=SOAPAuthType.SESSION,
            login_operation="SessionManager.Login",
        )
        client = SOAPClient(config)

        async with client:
            result = await client.call(
                operation_name="RetrieveProperties",
                params={"specSet": [...]}
            )
    """

    def __init__(self, config: SOAPConnectorConfig) -> None:
        self.config = config
        self._client: Any = None  # zeep.Client when connected
        self._session_token: str | None = None
        self._session_cookies: dict[str, str] = {}
        self._is_connected = False

    async def __aenter__(self) -> "SOAPClient":
        """Async context manager entry"""
        self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit"""
        self.disconnect()

    def connect(self) -> None:
        """Initialize the SOAP client and authenticate if needed"""
        import requests
        from zeep import Client
        from zeep.transports import Transport

        if self._client is not None:
            return

        logger.info(f"🔌 Connecting to SOAP service: {self.config.wsdl_url}")

        # Create requests session (zeep requires requests.Session, not httpx)
        session = requests.Session()
        session.verify = self.config.verify_ssl

        # Apply basic auth if configured
        if self.config.auth_type == SOAPAuthType.BASIC:  # noqa: SIM102 -- readability preferred over collapse
            if self.config.username and self.config.password:
                session.auth = (self.config.username, self.config.password)

        transport = Transport(session=session, timeout=self.config.timeout)

        # Add WS-Security if configured
        plugins = []
        if self.config.auth_type == SOAPAuthType.WS_SECURITY:
            plugins.append(self._create_ws_security_plugin())

        # Create zeep client
        self._client = Client(
            self.config.wsdl_url,
            transport=transport,
            plugins=plugins,
        )

        # Override endpoint if needed (many WSDLs define localhost as endpoint)
        endpoint_url = self.config.get_endpoint_url()
        if endpoint_url:
            logger.info(f"🔗 Overriding SOAP endpoint to: {endpoint_url}")
            # Override the endpoint address for all services
            for service in self._client.wsdl.services.values():
                for port in service.ports.values():
                    port.binding_options["address"] = endpoint_url

        # Perform login if session-based auth
        if self.config.auth_type == SOAPAuthType.SESSION:
            self._session_login()

        self._is_connected = True
        logger.info("✅ SOAP client connected")

    def disconnect(self) -> None:
        """Disconnect and cleanup"""
        if not self._is_connected:
            return

        # Logout if session-based
        if self.config.auth_type == SOAPAuthType.SESSION:
            self._session_logout()

        self._client = None
        self._session_token = None
        self._session_cookies = {}
        self._is_connected = False

        logger.info("🔌 SOAP client disconnected")

    def call(
        self,
        operation_name: str,
        params: dict[str, Any],
        service_name: str | None = None,
        port_name: str | None = None,
    ) -> SOAPResponse:
        """Execute a SOAP operation

        Args:
            operation_name: Name of the SOAP operation
            params: Parameters for the operation
            service_name: Override service (uses default if not provided)
            port_name: Override port (uses default if not provided)

        Returns:
            SOAPResponse with result or error
        """
        if self._client is None:
            self.connect()

        # Assert client is now connected (for type checker)
        assert self._client is not None, "SOAP client failed to connect"  # noqa: S101 -- runtime assertion for invariant checking

        start_time = time.time()

        try:
            # Get the service
            service = self._client.service

            # Bind to specific service/port if provided
            if service_name and port_name:
                service = self._client.bind(service_name, port_name)

            # Get the operation
            soap_operation = getattr(service, operation_name, None)
            if soap_operation is None:
                return SOAPResponse(
                    success=False,
                    status_code=404,
                    fault_string=f"Operation '{operation_name}' not found",
                )

            # Execute the operation
            logger.debug(f"📤 Calling SOAP operation: {operation_name}")
            result = soap_operation(**params)

            # Convert result to dict
            body = self._serialize_result(result)

            duration_ms = (time.time() - start_time) * 1000

            logger.debug(f"📥 SOAP response received: {operation_name} ({duration_ms:.2f}ms)")

            return SOAPResponse(
                success=True,
                status_code=200,
                body=body,
                operation_name=operation_name,
                duration_ms=duration_ms,
            )

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000

            # Extract SOAP fault details
            fault_code, fault_string, fault_detail = self._extract_fault(e)

            logger.error(f"❌ SOAP operation failed: {operation_name} - {fault_string}")

            return SOAPResponse(
                success=False,
                status_code=500,
                body={"error": str(e)},
                fault_code=fault_code,
                fault_string=fault_string,
                fault_detail=fault_detail,
                operation_name=operation_name,
                duration_ms=duration_ms,
            )

    def call_operation(
        self,
        operation: SOAPOperation,
        params: dict[str, Any],
    ) -> SOAPResponse:
        """Execute a SOAP operation using SOAPOperation object

        Convenience method that extracts operation details from
        the SOAPOperation model.
        """
        return self.call(
            operation_name=operation.operation_name,
            params=params,
            service_name=operation.service_name,
            port_name=operation.port_name,
        )

    def _session_login(self) -> None:
        """Perform session-based login (e.g., VMware VIM)

        This handles the login flow for systems that require authentication
        before calling other operations. The session token/cookie is then
        used for subsequent calls.
        """
        if not self.config.login_operation:
            logger.warning("⚠️ Session auth configured but no login_operation specified")
            return

        if not (self.config.username and self.config.password):
            logger.warning("⚠️ Session auth requires username and password")
            return

        logger.info(f"🔐 Performing session login: {self.config.login_operation}")

        try:
            # For VMware VIM API, login requires specific flow:
            # 1. Get ServiceInstance
            # 2. Get SessionManager from ServiceContent
            # 3. Call Login on SessionManager

            # This is a generic implementation - VMware-specific handling
            # would need to be added based on the login_operation format

            parts = self.config.login_operation.split(".")
            if len(parts) == 2:
                _service_method, operation = parts
            else:
                operation = self.config.login_operation

            # Attempt login
            result = self.call(
                operation_name=operation,
                params={
                    "userName": self.config.username,
                    "password": self.config.password,
                },
            )

            if result.success:
                # Store session info
                if "key" in result.body:
                    self._session_token = result.body["key"]
                logger.info("✅ Session login successful")
            else:
                logger.error(f"❌ Session login failed: {result.fault_string}")

        except Exception as e:
            logger.error(f"❌ Session login error: {e}")

    def _session_logout(self) -> None:
        """Perform session logout"""
        if not self.config.logout_operation:
            return

        try:
            logger.info(f"🔐 Performing session logout: {self.config.logout_operation}")
            self.call(
                operation_name=self.config.logout_operation,
                params={},
            )
            logger.info("✅ Session logout successful")
        except Exception as e:
            logger.warning(f"⚠️ Session logout error: {e}")

    def _create_ws_security_plugin(self) -> Any:
        """
        Create WS-Security plugin for zeep.

        Supports:
        - UsernameToken with plain or digest password
        - Timestamp element (prevents replay attacks)
        - Nonce (additional replay attack protection)

        Configuration via SOAPConnectorConfig:
        - ws_security_username: Username for authentication
        - ws_security_password: Password for authentication
        - ws_security_use_digest: Use password digest (True) or plain (False)
        - ws_security_use_timestamp: Add Timestamp element (recommended)
        - ws_security_timestamp_ttl: Timestamp validity in seconds
        - ws_security_use_nonce: Add Nonce element
        """
        from datetime import datetime

        from zeep.wsse.username import UsernameToken

        # Create UsernameToken with optional features
        username_token = UsernameToken(
            username=self.config.ws_security_username,
            password=self.config.ws_security_password,
            use_digest=self.config.ws_security_use_digest,
            nonce=self.config.ws_security_use_nonce,
            created=(datetime.now(tz=UTC) if self.config.ws_security_use_timestamp else None),
        )

        logger.debug(
            f"🔐 WS-Security configured: digest={self.config.ws_security_use_digest}, "
            f"timestamp={self.config.ws_security_use_timestamp}, "
            f"nonce={self.config.ws_security_use_nonce}"
        )

        return username_token

    def _serialize_result(self, result: Any) -> dict[str, Any]:
        """Convert zeep result object to JSON-serializable dict"""
        from zeep.helpers import serialize_object

        if result is None:
            return {}

        try:
            serialized = serialize_object(result)
            if isinstance(serialized, dict):
                return serialized
            return {"result": serialized}
        except Exception:
            # Fallback for non-serializable objects
            return {"raw": str(result)}

    def _extract_fault(self, exception: Exception) -> tuple:
        """Extract SOAP fault details from exception

        Returns:
            Tuple of (fault_code, fault_string, fault_detail)
        """
        fault_code = None
        fault_string = str(exception)
        fault_detail = None

        # Check for zeep Fault exception
        if hasattr(exception, "fault"):
            fault = exception.fault
            if hasattr(fault, "faultcode"):
                fault_code = str(fault.faultcode)
            if hasattr(fault, "faultstring"):
                fault_string = str(fault.faultstring)
            if hasattr(fault, "detail"):
                fault_detail = str(fault.detail)

        return fault_code, fault_string, fault_detail

    @property
    def is_connected(self) -> bool:
        """Check if client is connected"""
        return self._is_connected

    @property
    def session_token(self) -> str | None:
        """Get current session token"""
        return self._session_token


class VMwareSOAPClient(SOAPClient):
    """Specialized SOAP client for VMware VIM API

    Handles VMware-specific authentication flow:
    1. RetrieveServiceContent to get SessionManager
    2. Login via SessionManager
    3. Track vmware_soap_session cookie

    Example:
        config = SOAPConnectorConfig(
            wsdl_url="https://vcenter.local/sdk/vimService.wsdl",
            auth_type=SOAPAuthType.SESSION,
            username="admin@vsphere.local",
            password="***",
        )
        client = VMwareSOAPClient(config)

        async with client:
            # Client is now logged in
            vms = await client.call("RetrieveProperties", {...})
    """

    def _create_mor(self, mor_type: str, mor_value: str) -> Any:
        """Create a VMware ManagedObjectReference using zeep's type factory.

        VMware VIM API requires proper ManagedObjectReference types, not plain dicts.
        """
        if self._client is None:
            raise RuntimeError("Client not connected - cannot create MOR types")

        # Get the ManagedObjectReference type from zeep
        MOR = self._client.get_type("{urn:vim25}ManagedObjectReference")
        return MOR(_value_1=mor_value, type=mor_type)

    def _convert_mor_params(
        self, params: dict[str, Any]
    ) -> dict[str, Any]:  # NOSONAR (cognitive complexity)
        """Convert MOR-like dicts in params to proper zeep ManagedObjectReference types.

        Detects params that look like MORs and converts them:
        - {"type": "ClusterComputeResource", "_value_1": "domain-c9"} -> MOR object
        - {"type": "...", "value": "..."} -> MOR object (alternative format)
        """
        if self._client is None:
            return params

        converted = {}
        for key, value in params.items():
            if isinstance(value, dict):
                # Check if this looks like a MOR
                mor_type = value.get("type")
                mor_value = value.get("_value_1") or value.get("value")

                if mor_type and mor_value:
                    # This is a MOR - convert it
                    logger.debug(
                        f"Converting MOR param '{key}': type={mor_type}, value={mor_value}"
                    )
                    converted[key] = self._create_mor(mor_type, mor_value)
                else:
                    # Recurse for nested dicts
                    converted[key] = self._convert_mor_params(value)
            elif isinstance(value, list):
                # Handle lists of MORs
                converted_list = []
                for item in value:
                    if isinstance(item, dict):
                        item_type = item.get("type")
                        item_value = item.get("_value_1") or item.get("value")
                        if item_type and item_value:
                            converted_list.append(self._create_mor(str(item_type), str(item_value)))
                        else:
                            converted_list.append(item)
                    else:
                        converted_list.append(item)
                converted[key] = converted_list
            else:
                converted[key] = value

        return converted

    def call(
        self,
        operation_name: str,
        params: dict[str, Any],
        service_name: str | None = None,
        port_name: str | None = None,
    ) -> SOAPResponse:
        """Execute a SOAP operation with VMware MOR conversion.

        Overrides parent to convert MOR-like dicts to proper zeep types.
        """
        # Convert MOR params before calling
        converted_params = self._convert_mor_params(params)

        # Call parent implementation with converted params
        return super().call(
            operation_name=operation_name,
            params=converted_params,
            service_name=service_name,
            port_name=port_name,
        )

    def _session_login(self) -> None:
        """VMware-specific login flow"""
        if not (self.config.username and self.config.password):
            logger.warning("⚠️ VMware auth requires username and password")
            return

        logger.info("🔐 Performing VMware VIM API login")

        try:
            # Create ServiceInstance MOR using zeep's type factory
            service_instance = self._create_mor("ServiceInstance", "ServiceInstance")

            # Step 1: Get ServiceContent (call service directly to avoid our wrapper)
            logger.debug("📤 Calling RetrieveServiceContent...")
            result = self._client.service.RetrieveServiceContent(_this=service_instance)

            if not result:
                logger.error("❌ Failed to retrieve ServiceContent")
                return

            # Serialize the result for easier access
            from zeep.helpers import serialize_object

            service_content = serialize_object(result)

            session_manager_ref = service_content.get("sessionManager")
            if not session_manager_ref:
                logger.error("❌ SessionManager not found in ServiceContent")
                return

            # Create SessionManager MOR
            session_manager = self._create_mor(
                session_manager_ref.get("type", "SessionManager"),
                session_manager_ref.get(
                    "_value_1", session_manager_ref.get("value", "SessionManager")
                ),
            )

            # Step 2: Login via SessionManager
            logger.debug(f"📤 Calling Login for user: {self.config.username}")
            login_result = self._client.service.Login(
                _this=session_manager,
                userName=self.config.username,
                password=self.config.password,
            )

            if login_result:
                login_body = serialize_object(login_result)
                self._session_token = login_body.get("key")
                if self._session_token:
                    logger.info(
                        f"✅ VMware login successful, session: {self._session_token[:8]}..."
                    )
                else:
                    logger.warning("VMware login succeeded but no session token returned")
            else:
                logger.error("❌ VMware login failed: No response")

        except Exception as e:
            logger.error(f"❌ VMware login error: {e}")

    def _session_logout(self) -> None:
        """VMware-specific logout"""
        if not self._session_token or not self._client:
            return

        try:
            from zeep.helpers import serialize_object

            # Get SessionManager reference
            service_instance = self._create_mor("ServiceInstance", "ServiceInstance")
            result = self._client.service.RetrieveServiceContent(_this=service_instance)

            if result:
                service_content = serialize_object(result)
                session_manager_ref = service_content.get("sessionManager")
                if session_manager_ref:
                    session_manager = self._create_mor(
                        session_manager_ref.get("type", "SessionManager"),
                        session_manager_ref.get(
                            "_value_1",
                            session_manager_ref.get("value", "SessionManager"),
                        ),
                    )
                    self._client.service.Logout(_this=session_manager)
                    logger.info("✅ VMware logout successful")
        except Exception as e:
            logger.warning(f"⚠️ VMware logout error: {e}")
