"""SOAP connector implementing BaseConnector with zeep.

Registered as "soap" in the connector registry. Parses WSDL to discover
operations and executes them via zeep CachingClient service proxy.
Uses asyncio.to_thread() for sync zeep calls.
"""

from __future__ import annotations

import asyncio
from typing import Any

from zeep import CachingClient, Client, Settings
from zeep.helpers import serialize_object

from meho_claude.core.connectors.base import BaseConnector
from meho_claude.core.connectors.models import ConnectorConfig, Operation
from meho_claude.core.connectors.registry import register_connector
from meho_claude.core.connectors.wsdl_parser import parse_wsdl


@register_connector("soap")
class SOAPConnector(BaseConnector):
    """SOAP connector using zeep for WSDL parsing and operation execution.

    Uses CachingClient for execute() calls (SqliteCache avoids re-parsing
    WSDL on every invocation). Creates fresh client per execute() call
    (per-call connection pattern matching K8s/VMware connectors).
    """

    def __init__(self, config: ConnectorConfig, credentials: dict | None = None) -> None:
        super().__init__(config, credentials)

    def _get_wsdl_source(self) -> str:
        """Get the WSDL source URL or file path from config.

        Uses spec_url (preferred) or spec_path for WSDL location.
        Raises ValueError if neither is set.
        """
        if self.config.spec_url:
            return self.config.spec_url
        if self.config.spec_path:
            return self.config.spec_path
        raise ValueError(
            f"SOAP connector '{self.config.name}' requires spec_url or spec_path "
            "for WSDL location"
        )

    def _build_zeep_settings(self) -> Settings:
        """Build zeep Settings with strict=False for enterprise WSDL compatibility."""
        return Settings(strict=False)

    async def test_connection(self) -> dict[str, Any]:
        """Test connectivity by parsing the WSDL document.

        Creates a zeep Client (not CachingClient) to verify the WSDL
        endpoint is reachable and parseable.

        Returns:
            Dict with status and service count on success,
            or status and error message on failure.
        """
        try:
            wsdl_source = self._get_wsdl_source()

            def _test() -> dict[str, Any]:
                client = Client(wsdl_source, settings=self._build_zeep_settings())
                service_count = len(client.wsdl.services)
                del client
                return {"status": "ok", "services": service_count}

            return await asyncio.to_thread(_test)
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    async def discover_operations(self) -> list[Operation]:
        """Discover all SOAP operations by parsing the WSDL.

        Delegates to parse_wsdl() which mirrors the openapi_parser pattern.
        """
        wsdl_source = self._get_wsdl_source()
        return await asyncio.to_thread(parse_wsdl, wsdl_source, self.config.name)

    async def execute(self, operation: Operation, params: dict[str, Any]) -> dict[str, Any]:
        """Execute a SOAP operation via zeep CachingClient.

        Creates a fresh CachingClient per call (CachingClient uses SqliteCache
        so WSDL re-parse is fast after first call). Parses operation_id to
        extract service and operation names, then calls via service proxy.

        Args:
            operation: The Operation model to execute.
            params: Parameters to pass to the SOAP operation.

        Returns:
            Dict with serialized response data.
        """
        wsdl_source = self._get_wsdl_source()

        def _execute() -> dict[str, Any]:
            client = CachingClient(wsdl_source, settings=self._build_zeep_settings())

            # Parse operation_id: "ServiceName.OperationName"
            parts = operation.operation_id.split(".", 1)
            if len(parts) == 2:
                op_name = parts[1]
            else:
                op_name = parts[0]

            # Call operation via service proxy
            service_proxy = client.service
            op_callable = getattr(service_proxy, op_name)
            result = op_callable(**params)

            # Serialize zeep CompoundValue to plain dict
            result_dict = serialize_object(result, target_cls=dict)

            del client
            return {"data": result_dict}

        return await asyncio.to_thread(_execute)

    def get_trust_tier(self, operation: Operation) -> str:
        """Determine trust tier, checking config overrides first.

        Mirrors vmware.py pattern -- trust_overrides take precedence
        over the operation's inferred trust tier.
        """
        override_map = {o.operation_id: o.trust_tier for o in self.config.trust_overrides}
        if operation.operation_id in override_map:
            return override_map[operation.operation_id]
        return operation.trust_tier

    def close(self) -> None:
        """No-op -- SOAP client created per-execute call."""
        pass
