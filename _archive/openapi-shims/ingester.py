"""
SOAP Schema Ingester

Parses WSDL files to discover SOAP operations and convert them
to MEHO's unified Operation model for search and execution.

TASK-126: Also creates knowledge_chunk entries with embeddings for hybrid search.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING
from uuid import UUID
from datetime import datetime

from meho_app.modules.openapi.soap.models import (
    SOAPOperation,
    SOAPConnectorConfig,
    SOAPStyle,
    WSDLMetadata,
    # TASK-96: Type definitions
    SOAPProperty,
    SOAPTypeDefinition,
)

if TYPE_CHECKING:
    from meho_app.modules.knowledge.knowledge_store import KnowledgeStore

logger = logging.getLogger(__name__)


class SOAPSchemaIngester:
    """Ingest SOAP schemas from WSDL files
    
    This class parses WSDL documents using the zeep library and converts
    SOAP operations into MEHO's unified Operation model. Operations are
    then indexed for BM25/hybrid search alongside REST endpoints.
    
    Example:
        ingester = SOAPSchemaIngester()
        operations = await ingester.ingest_wsdl(
            wsdl_url="https://vcenter.local/sdk/vimService.wsdl",
            connector_id=uuid4(),
            tenant_id="demo-tenant"
        )
        # Returns list of SOAPOperation objects
    """
    
    def __init__(self, config: Optional[SOAPConnectorConfig] = None):
        self.config = config or SOAPConnectorConfig(wsdl_url="")
        self._client = None
        self._wsdl = None
    
    async def ingest_wsdl(
        self,
        wsdl_url: str,
        connector_id: UUID,
        tenant_id: str,
        auth: Optional[Dict[str, str]] = None,
        extract_types: bool = True,
    ) -> Tuple[List[SOAPOperation], WSDLMetadata, List[SOAPTypeDefinition]]:
        """Parse WSDL and extract all operations and type definitions
        
        Args:
            wsdl_url: URL or file path to WSDL
            connector_id: UUID of the connector
            tenant_id: Tenant identifier
            auth: Optional auth credentials for fetching WSDL
            extract_types: Whether to extract type definitions (TASK-96)
            
        Returns:
            Tuple of (list of SOAPOperation, WSDLMetadata, list of SOAPTypeDefinition)
            The third element (type definitions) is empty if extract_types=False
        """
        from zeep import Client
        from zeep.transports import Transport
        from zeep.wsdl import Document
        import requests
        
        logger.info(f"🔍 Ingesting WSDL from: {wsdl_url}")
        
        # Create requests session (zeep requires requests.Session, not httpx)
        session = requests.Session()
        session.verify = self.config.verify_ssl
        if auth:
            if auth.get("username") and auth.get("password"):
                session.auth = (auth["username"], auth["password"])
        
        transport = Transport(session=session, timeout=self.config.timeout)
        
        # Parse WSDL
        try:
            client = Client(wsdl_url, transport=transport)
            wsdl = client.wsdl
        except Exception as e:
            logger.error(f"❌ Failed to parse WSDL: {e}")
            raise ValueError(f"Failed to parse WSDL from {wsdl_url}: {e}")
        
        # Extract metadata
        services = list(wsdl.services.keys())
        ports: List[str] = []
        for service in wsdl.services.values():
            ports.extend(service.ports.keys())
        
        operations: List[SOAPOperation] = []
        
        # Iterate through services → ports → operations
        for service_name, service in wsdl.services.items():
            for port_name, port in service.ports.items():
                binding = port.binding
                
                for op_name, operation in binding._operations.items():
                    try:
                        soap_op = self._parse_operation(
                            wsdl_url=wsdl_url,
                            service_name=service_name,
                            port_name=port_name,
                            operation_name=op_name,
                            operation=operation,
                            binding=binding,
                            connector_id=connector_id,
                            tenant_id=tenant_id,
                        )
                        operations.append(soap_op)
                    except Exception as e:
                        logger.warning(
                            f"⚠️ Failed to parse operation {service_name}.{op_name}: {e}"
                        )
                        continue
        
        # TASK-96: Extract type definitions
        type_definitions: List[SOAPTypeDefinition] = []
        if extract_types:
            type_definitions = self._extract_type_definitions(
                client=client,
                connector_id=connector_id,
                tenant_id=tenant_id,
            )
            logger.info(f"📦 Extracted {len(type_definitions)} type definitions")
        
        metadata = WSDLMetadata(
            wsdl_url=wsdl_url,
            target_namespace=str(wsdl.types.prefix_map.get("tns", "")),
            services=services,
            ports=ports,
            operation_count=len(operations),
        )
        
        logger.info(
            f"✅ Parsed {len(operations)} SOAP operations from "
            f"{len(services)} service(s)"
        )
        
        return operations, metadata, type_definitions
    
    def _parse_operation(
        self,
        wsdl_url: str,
        service_name: str,
        port_name: str,
        operation_name: str,
        operation: Any,
        binding: Any,
        connector_id: UUID,
        tenant_id: str,
    ) -> SOAPOperation:
        """Parse a single SOAP operation"""
        
        # Get operation documentation
        description = None
        if hasattr(operation, 'documentation') and operation.documentation:
            description = str(operation.documentation)
        
        # Get SOAP action
        soap_action = None
        if hasattr(operation, 'soapaction'):
            soap_action = operation.soapaction
        
        # Get binding style
        style = SOAPStyle.DOCUMENT
        if hasattr(binding, 'style') and binding.style:
            style = SOAPStyle(binding.style.lower())
        
        # Get namespace
        namespace = ""
        if hasattr(operation, 'input') and operation.input:
            if hasattr(operation.input, 'body') and operation.input.body:
                if hasattr(operation.input.body, 'namespace'):
                    namespace = operation.input.body.namespace or ""
        
        # Extract input schema
        input_schema = self._element_to_schema(
            operation.input.body if hasattr(operation, 'input') and operation.input else None
        )
        
        # Extract output schema
        output_schema = self._element_to_schema(
            operation.output.body if hasattr(operation, 'output') and operation.output else None
        )
        
        # Build search content for BM25
        search_content = self._build_search_content(
            service_name=service_name,
            port_name=port_name,
            operation_name=operation_name,
            description=description,
            input_schema=input_schema,
        )
        
        return SOAPOperation(
            connector_id=connector_id,
            tenant_id=tenant_id,
            service_name=service_name,
            port_name=port_name,
            operation_name=operation_name,
            name=f"{service_name}.{operation_name}",
            description=description,
            soap_action=soap_action,
            style=style,
            namespace=namespace,
            input_schema=input_schema,
            output_schema=output_schema,
            search_content=search_content,
            protocol_details={
                "protocol": "soap",
                "wsdl_url": wsdl_url,
                "service": service_name,
                "port": port_name,
                "operation": operation_name,
                "soap_action": soap_action,
                "style": style.value,
                "namespace": namespace,
            },
        )
    
    def _element_to_schema(self, element: Any) -> Dict[str, Any]:
        """Convert zeep Element to JSON Schema-like dict
        
        Maps XSD types to JSON Schema types for unified handling
        across REST and SOAP operations.
        """
        if element is None:
            return {}
        
        try:
            # Handle body element
            if not hasattr(element, 'type'):
                return {}
            
            element_type = element.type
            if element_type is None:
                return {}
            
            schema: Dict[str, Any] = {
                "type": "object",
                "properties": {},
            }
            required: List[str] = []
            
            # Extract elements from complex type
            if hasattr(element_type, 'elements'):
                for child in element_type.elements:
                    if isinstance(child, tuple) and len(child) >= 2:
                        name = child[0]
                        child_element = child[1]
                        
                        # Get type info
                        child_schema = self._xsd_element_to_json_schema(child_element)
                        schema["properties"][name] = child_schema
                        
                        # Check if required
                        if hasattr(child_element, 'min_occurs'):
                            if child_element.min_occurs and child_element.min_occurs > 0:
                                required.append(name)
            
            if required:
                schema["required"] = required
            
            return schema
            
        except Exception as e:
            logger.warning(f"⚠️ Failed to convert element to schema: {e}")
            return {}
    
    def _xsd_element_to_json_schema(self, element: Any) -> Dict[str, Any]:
        """Convert a single XSD element to JSON Schema"""
        schema: Dict[str, Any] = {}
        
        # Get description if available
        if hasattr(element, 'documentation') and element.documentation:
            schema["description"] = str(element.documentation)
        
        # Get type
        if hasattr(element, 'type') and element.type:
            xsd_type = element.type
            
            # Check if it's a complex type with children
            if hasattr(xsd_type, 'elements') and list(xsd_type.elements):
                # Recursive complex type
                schema["type"] = "object"
                schema["properties"] = {}
                for child in xsd_type.elements:
                    if isinstance(child, tuple) and len(child) >= 2:
                        name = child[0]
                        child_element = child[1]
                        schema["properties"][name] = self._xsd_element_to_json_schema(
                            child_element
                        )
            else:
                # Simple type - map to JSON type
                type_name = str(xsd_type.name) if hasattr(xsd_type, 'name') else "string"
                schema["type"] = self._xsd_to_json_type(type_name)
        else:
            schema["type"] = "string"
        
        # Handle arrays (maxOccurs > 1)
        if hasattr(element, 'max_occurs'):
            max_occurs = element.max_occurs
            if max_occurs == 'unbounded' or (max_occurs and int(max_occurs) > 1):
                schema = {
                    "type": "array",
                    "items": schema,
                }
        
        return schema
    
    def _xsd_to_json_type(self, xsd_type: str) -> str:
        """Map XSD types to JSON Schema types"""
        type_map = {
            # Strings
            "string": "string",
            "normalizedString": "string",
            "token": "string",
            "language": "string",
            "Name": "string",
            "NCName": "string",
            "ID": "string",
            "IDREF": "string",
            "NMTOKEN": "string",
            "anyURI": "string",
            "QName": "string",
            
            # Numbers
            "int": "integer",
            "integer": "integer",
            "long": "integer",
            "short": "integer",
            "byte": "integer",
            "unsignedInt": "integer",
            "unsignedLong": "integer",
            "unsignedShort": "integer",
            "unsignedByte": "integer",
            "positiveInteger": "integer",
            "negativeInteger": "integer",
            "nonPositiveInteger": "integer",
            "nonNegativeInteger": "integer",
            
            # Floats
            "float": "number",
            "double": "number",
            "decimal": "number",
            
            # Boolean
            "boolean": "boolean",
            
            # Date/Time (stored as strings in JSON)
            "dateTime": "string",
            "date": "string",
            "time": "string",
            "duration": "string",
            "gYearMonth": "string",
            "gYear": "string",
            "gMonthDay": "string",
            "gDay": "string",
            "gMonth": "string",
            
            # Binary (base64 encoded string)
            "base64Binary": "string",
            "hexBinary": "string",
            
            # Any type
            "anyType": "object",
            "anySimpleType": "string",
        }
        
        # Extract local name if qualified
        local_name = xsd_type.split(":")[-1] if ":" in xsd_type else xsd_type
        
        return type_map.get(local_name, "string")
    
    def _build_search_content(
        self,
        service_name: str,
        port_name: str,
        operation_name: str,
        description: Optional[str],
        input_schema: Dict[str, Any],
    ) -> str:
        """Build searchable content for BM25 indexing
        
        Creates a text representation of the operation for full-text search,
        similar to how we index REST endpoints.
        """
        parts = [
            f"SOAP operation {operation_name}",
            f"service {service_name}",
            f"port {port_name}",
        ]
        
        if description:
            parts.append(description)
        
        # Include parameter names for search
        if input_schema and "properties" in input_schema:
            param_names = list(input_schema["properties"].keys())
            if param_names:
                parts.append(f"parameters: {', '.join(param_names)}")
        
        return " ".join(parts)
    
    # =========================================================================
    # TASK-96: Type Definition Extraction
    # =========================================================================
    
    def _extract_type_definitions(
        self,
        client: Any,
        connector_id: UUID,
        tenant_id: str,
    ) -> List[SOAPTypeDefinition]:
        """Extract complex type definitions from WSDL schema.
        
        This enables the agent to discover what types exist in a SOAP service
        and what properties each type has. Types are generic and work with
        any SOAP service (VMware, ServiceNow, SAP, etc.).
        
        Args:
            client: Zeep Client instance
            connector_id: Connector UUID for ownership tracking
            tenant_id: Tenant identifier
            
        Returns:
            List of SOAPTypeDefinition objects
        """
        from zeep.xsd.types import ComplexType
        
        type_definitions: List[SOAPTypeDefinition] = []
        
        # Get all types from the WSDL schema
        if not hasattr(client, 'wsdl') or not hasattr(client.wsdl, 'types'):
            logger.warning("WSDL has no types section")
            return type_definitions
        
        types_container = client.wsdl.types
        
        # Debug: log what attributes are available
        public_attrs = [a for a in dir(types_container) if not a.startswith('_')]
        logger.info(f"🔍 types_container attributes: {public_attrs}")
        
        # Collect all types into a dict
        type_dict: Dict[Any, Any] = {}
        
        # Method 1: types._types (internal dict) - main zeep storage
        internal_types = getattr(types_container, '_types', None)
        if internal_types and isinstance(internal_types, dict):
            type_dict.update(internal_types)
            logger.info(f"🔍 _types has {len(internal_types)} entries")
        
        # Method 2: types.types (public property if exists) 
        # This might be a generator, so convert to dict
        if hasattr(types_container, 'types'):
            try:
                public_types = types_container.types
                if public_types is not None:
                    if hasattr(public_types, 'items'):
                        # It's a dict-like
                        type_dict.update(public_types)
                        logger.info(f"🔍 .types (dict) has {len(public_types)} entries")
                    else:
                        # It might be a generator/iterator of tuples or QName->Type pairs
                        types_list = list(public_types)
                        logger.info(f"🔍 .types (iter) has {len(types_list)} entries")
                        
                        # Debug: log first few items
                        for i, item in enumerate(types_list[:3]):
                            logger.info(f"🔍 Sample type {i}: type={type(item).__name__}, item={str(item)[:100]}")
                        
                        for item in types_list:
                            if isinstance(item, tuple) and len(item) >= 2:
                                type_dict[item[0]] = item[1]
                            else:
                                # It might be a direct type object with a qname
                                if hasattr(item, 'qname') and item.qname is not None:
                                    type_dict[item.qname] = item
                                elif hasattr(item, 'name') and item.name is not None:
                                    type_dict[item.name] = item
            except Exception as e:
                logger.warning(f"🔍 Error accessing .types: {e}")
        
        # Method 3: iterate through schema elements  
        if not type_dict:
            # Try to get types from schemas
            schemas = getattr(types_container, '_schemas', None)
            if schemas:
                schemas_list = list(schemas) if hasattr(schemas, '__iter__') else []
                logger.info(f"🔍 Found {len(schemas_list)} schemas")
                for schema in schemas_list:
                    if hasattr(schema, 'types'):
                        try:
                            schema_types = schema.types
                            if schema_types is not None:
                                if hasattr(schema_types, 'items'):
                                    type_dict.update(schema_types)
                                    logger.info(f"🔍 Schema types (dict): {len(schema_types)}")
                                else:
                                    for item in list(schema_types):
                                        if isinstance(item, tuple) and len(item) >= 2:
                                            type_dict[item[0]] = item[1]
                        except Exception as e:
                            logger.warning(f"🔍 Error processing schema types: {e}")
        
        logger.info(f"🔍 Total collected types: {len(type_dict)}")
        
        for type_qname, type_obj in type_dict.items():
            try:
                # Only process complex types (not simple types like string)
                if not isinstance(type_obj, ComplexType):
                    continue
                
                # Skip anonymous/internal types
                type_name = str(type_qname.localname) if hasattr(type_qname, 'localname') else str(type_qname)
                if not type_name or type_name.startswith('_'):
                    continue
                
                # Get namespace
                namespace = ""
                if hasattr(type_qname, 'namespace'):
                    namespace = str(type_qname.namespace) or ""
                
                # Extract properties from the complex type
                properties = self._extract_type_properties(type_obj)
                
                # Get base type if this extends another type
                base_type = self._get_base_type(type_obj)
                
                # Get description from documentation if available
                description = None
                if hasattr(type_obj, 'documentation') and type_obj.documentation:
                    description = str(type_obj.documentation)
                
                type_def = SOAPTypeDefinition(
                    name=type_name,
                    namespace=namespace,
                    base_type=base_type,
                    properties=properties,
                    description=description,
                    connector_id=connector_id,
                    tenant_id=tenant_id,
                )
                type_definitions.append(type_def)
                
            except Exception as e:
                logger.warning(f"⚠️ Failed to extract type {type_qname}: {e}")
                continue
        
        return type_definitions
    
    def _extract_type_properties(self, complex_type: Any) -> List[SOAPProperty]:
        """Extract properties from a zeep ComplexType.
        
        Handles both elements and attributes on the type.
        """
        properties: List[SOAPProperty] = []
        
        # Get elements from the complex type
        # Zeep stores elements as list of (name, element) tuples
        if hasattr(complex_type, 'elements'):
            try:
                elements = list(complex_type.elements)
                for item in elements:
                    if isinstance(item, tuple) and len(item) >= 2:
                        name, elem = item
                        prop = self._element_to_property(name, elem)
                        if prop:
                            properties.append(prop)
            except Exception as e:
                logger.debug(f"Could not extract elements: {e}")
        
        # Also get attributes if present
        if hasattr(complex_type, 'attributes'):
            try:
                attrs = list(complex_type.attributes)
                for item in attrs:
                    if isinstance(item, tuple) and len(item) >= 2:
                        name, attr = item
                        prop = self._attribute_to_property(name, attr)
                        if prop:
                            properties.append(prop)
            except Exception as e:
                logger.debug(f"Could not extract attributes: {e}")
        
        return properties
    
    def _element_to_property(self, name: str, element: Any) -> Optional[SOAPProperty]:
        """Convert a zeep Element to SOAPProperty."""
        try:
            # Get type name
            type_name = "any"
            if hasattr(element, 'type') and element.type:
                type_obj = element.type
                if hasattr(type_obj, 'name') and type_obj.name:
                    type_name = str(type_obj.name)
                elif hasattr(type_obj, '__class__'):
                    type_name = type_obj.__class__.__name__
            
            # Check if array (maxOccurs > 1 or unbounded)
            is_array = False
            if hasattr(element, 'max_occurs'):
                max_occurs = element.max_occurs
                if max_occurs == 'unbounded' or (max_occurs and str(max_occurs) != '1'):
                    is_array = True
            
            # Check if required (minOccurs > 0)
            is_required = False
            if hasattr(element, 'min_occurs'):
                min_occurs = element.min_occurs
                if min_occurs and int(min_occurs) > 0:
                    is_required = True
            
            # Get description
            description = None
            if hasattr(element, 'documentation') and element.documentation:
                description = str(element.documentation)
            
            return SOAPProperty(
                name=str(name),
                type_name=type_name,
                is_array=is_array,
                is_required=is_required,
                description=description,
            )
            
        except Exception as e:
            logger.debug(f"Could not convert element {name} to property: {e}")
            return None
    
    def _attribute_to_property(self, name: str, attr: Any) -> Optional[SOAPProperty]:
        """Convert a zeep Attribute to SOAPProperty."""
        try:
            type_name = "string"
            if hasattr(attr, 'type') and attr.type:
                type_obj = attr.type
                if hasattr(type_obj, 'name') and type_obj.name:
                    type_name = str(type_obj.name)
            
            # Attributes are never arrays
            is_array = False
            
            # Check if required
            is_required = False
            if hasattr(attr, 'use') and attr.use == 'required':
                is_required = True
            
            return SOAPProperty(
                name=str(name),
                type_name=type_name,
                is_array=is_array,
                is_required=is_required,
                description=None,
            )
            
        except Exception as e:
            logger.debug(f"Could not convert attribute {name} to property: {e}")
            return None
    
    def _get_base_type(self, complex_type: Any) -> Optional[str]:
        """Get the base type name if this type extends another."""
        try:
            # Zeep stores extension/restriction info
            if hasattr(complex_type, 'extension'):
                ext = complex_type.extension
                if ext and hasattr(ext, 'name'):
                    return str(ext.name)
            
            # Alternative: check _base attribute
            if hasattr(complex_type, '_base') and complex_type._base:
                base = complex_type._base
                if hasattr(base, 'name') and base.name:
                    return str(base.name)
            
        except Exception as e:
            logger.debug(f"Could not get base type: {e}")
        
        return None
    
    # =========================================================================
    # TASK-126: Knowledge Chunk Creation for Hybrid Search
    # =========================================================================
    
    async def create_knowledge_chunks_for_operations(
        self,
        operations: List[SOAPOperation],
        knowledge_store: "KnowledgeStore",
        connector_id: UUID,
        connector_name: str,
        tenant_id: str,
    ) -> int:
        """
        Create knowledge_chunk entries for SOAP operations.
        
        TASK-126: Enables hybrid search (BM25 + semantic) for SOAP operations.
        
        Args:
            operations: List of SOAPOperation objects from ingest_wsdl()
            knowledge_store: KnowledgeStore for creating chunks with embeddings
            connector_id: UUID of the connector
            connector_name: Display name for text formatting
            tenant_id: Tenant ID
            
        Returns:
            Number of knowledge chunks created
        """
        from meho_app.modules.knowledge.schemas import KnowledgeChunkCreate, KnowledgeType
        from meho_app.modules.knowledge.models import KnowledgeChunkModel
        from sqlalchemy import delete
        
        # First, delete existing knowledge chunks for this connector
        try:
            stmt = delete(KnowledgeChunkModel).where(
                KnowledgeChunkModel.tenant_id == tenant_id,
                KnowledgeChunkModel.search_metadata["connector_id"].astext == str(connector_id),
                KnowledgeChunkModel.search_metadata["source_type"].astext == "connector_operation",
                KnowledgeChunkModel.search_metadata["connector_type"].astext == "soap",
            )
            await knowledge_store.repository.session.execute(stmt)
            logger.debug(f"Deleted existing SOAP knowledge chunks for connector {connector_id}")
        except Exception as e:
            logger.warning(f"Failed to delete existing chunks (may not exist): {e}")
        
        chunks_created = 0
        
        for op in operations:
            try:
                # Format operation as rich searchable text
                text = self._format_soap_operation_as_text(op, connector_name)
                
                # Create knowledge chunk with metadata for filtering
                # Note: search_metadata accepts ChunkMetadata with extra fields (extra="allow")
                from meho_app.modules.knowledge.schemas import ChunkMetadata
                
                # Build metadata dict and validate (ChunkMetadata has extra="allow")
                metadata_dict = {
                    "resource_type": "soap_operation",
                    "keywords": [op.operation_name, op.service_name or "", op.port_name or ""],
                    "source_type": "connector_operation",
                    "connector_id": str(connector_id),
                    "connector_type": "soap",
                    "operation_name": op.operation_name,
                    "service_name": op.service_name,
                    "port_name": op.port_name,
                    "soap_action": op.soap_action,
                }
                chunk_metadata = ChunkMetadata.model_validate(metadata_dict)
                
                chunk_create = KnowledgeChunkCreate(
                    text=text,
                    tenant_id=tenant_id,
                    tags=["api", "operation", "soap", op.service_name or "", op.port_name or ""],
                    knowledge_type=KnowledgeType.DOCUMENTATION,
                    priority=5,  # Medium priority
                    search_metadata=chunk_metadata,
                    source_uri=f"connector://{connector_id}/operation/{op.operation_name}"
                )
                
                # Add chunk (generates embedding automatically)
                await knowledge_store.add_chunk(chunk_create)
                chunks_created += 1
                logger.debug(f"  Created knowledge chunk for operation: {op.operation_name}")
                
            except Exception as e:
                logger.error(f"Failed to create knowledge chunk for {op.operation_name}: {e}")
                continue
        
        logger.info(f"Created {chunks_created} knowledge chunks for SOAP connector {connector_id}")
        return chunks_created
    
    def _format_soap_operation_as_text(
        self,
        op: SOAPOperation,
        connector_name: str,
    ) -> str:
        """
        Format SOAP operation as rich searchable text.
        
        TASK-126: Creates text optimized for BM25 + semantic search.
        Similar to _format_endpoint_as_text() for REST endpoints.
        
        Args:
            op: SOAP operation
            connector_name: Name of the connector for context
            
        Returns:
            Formatted text for embedding and BM25 indexing
        """
        parts = []
        
        # Header: Operation name
        parts.append(f"{op.operation_name}")
        parts.append("")
        
        # Service and port context
        parts.append(f"Service: {op.service_name}")
        parts.append(f"Port: {op.port_name}")
        parts.append(f"Connector: {connector_name}")
        parts.append("")
        
        # Description
        if op.description:
            parts.append(f"Description: {op.description}")
            parts.append("")
        
        # SOAP details
        parts.append(f"Style: {op.style.value if hasattr(op.style, 'value') else op.style}")
        if op.soap_action:
            parts.append(f"SOAPAction: {op.soap_action}")
        if op.namespace:
            parts.append(f"Namespace: {op.namespace}")
        parts.append("")
        
        # Input parameters
        if op.input_schema and op.input_schema.get("properties"):
            parts.append("Input Parameters:")
            for param_name, param_schema in op.input_schema["properties"].items():
                param_type = param_schema.get("type", "any")
                param_desc = param_schema.get("description", "")
                parts.append(f"  - {param_name} ({param_type}): {param_desc}")
            parts.append("")
        
        # Output schema summary
        if op.output_schema and op.output_schema.get("properties"):
            parts.append("Output:")
            output_fields = list(op.output_schema["properties"].keys())[:5]
            parts.append(f"  Fields: {', '.join(output_fields)}")
            parts.append("")
        
        # Search keywords for better BM25 matching
        keywords = self._generate_soap_search_keywords(op)
        if keywords:
            parts.append(f"Search: {keywords}")
        
        return "\n".join(parts)
    
    def _generate_soap_search_keywords(self, op: SOAPOperation) -> str:
        """
        Generate search keywords for better BM25 matching.
        
        Args:
            op: SOAP operation
            
        Returns:
            Space-separated search keywords
        """
        keywords = set()
        
        # Add operation name parts
        # CamelCase splitting: "GetHostInfo" → ["Get", "Host", "Info"]
        import re
        name_parts = re.findall(r'[A-Z][a-z]*|[a-z]+', op.operation_name)
        keywords.update([p.lower() for p in name_parts])
        
        # Add service and port names (also split)
        for name in [op.service_name, op.port_name]:
            if name:
                name_parts = re.findall(r'[A-Z][a-z]*|[a-z]+', name)
                keywords.update([p.lower() for p in name_parts])
        
        # Common SOAP/Web service terms
        keywords.add("soap")
        keywords.add("web")
        keywords.add("service")
        
        # Action word detection
        op_lower = op.operation_name.lower()
        if "get" in op_lower or "retrieve" in op_lower:
            keywords.update(["get", "retrieve", "fetch", "read"])
        if "set" in op_lower or "update" in op_lower:
            keywords.update(["set", "update", "modify", "change"])
        if "create" in op_lower or "add" in op_lower:
            keywords.update(["create", "add", "new", "insert"])
        if "delete" in op_lower or "remove" in op_lower:
            keywords.update(["delete", "remove", "destroy"])
        if "list" in op_lower or "query" in op_lower:
            keywords.update(["list", "query", "search", "find", "all"])
        
        return " ".join(sorted(keywords))
    
    def get_operation_count_estimate(self, wsdl_url: str) -> int:
        """Get estimated operation count without full parsing
        
        Useful for progress bars or validation before full ingestion.
        """
        # This would do a quick WSDL parse to count operations
        # For now, return a placeholder
        return 0


class SOAPSchemaIngesterError(Exception):
    """Error during SOAP schema ingestion"""
    pass

