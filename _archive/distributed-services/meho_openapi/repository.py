"""
Repository for OpenAPI service database operations.
"""
# mypy: disable-error-code="arg-type,assignment"
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_, update
from meho_openapi.models import (
    ConnectorModel,
    OpenAPISpecModel,
    EndpointDescriptorModel,
    UserConnectorCredentialModel,
    SoapOperationDescriptorModel,
    SoapTypeDescriptorModel,
    ConnectorOperationModel,
    ConnectorTypeModel,
)
from meho_openapi.schemas import *
from typing import Optional, List
import uuid
from datetime import datetime


class ConnectorRepository:
    """Repository for connector operations"""
    
    def __init__(self, session: AsyncSession):
        self.session = session
    
    async def create_connector(self, connector: ConnectorCreate) -> Connector:
        """Create a new connector"""
        db_connector = ConnectorModel(
            id=uuid.uuid4(),
            **connector.model_dump()
        )
        self.session.add(db_connector)
        await self.session.flush()  # Flush changes, don't commit (session managed externally)
        await self.session.refresh(db_connector)
        
        return Connector(
            id=str(db_connector.id),
            **connector.model_dump(),
            is_active=db_connector.is_active,
            created_at=db_connector.created_at,
            updated_at=db_connector.updated_at
        )
    
    async def get_connector(self, connector_id: str, tenant_id: Optional[str] = None) -> Optional[Connector]:
        """Get connector by ID"""
        try:
            query = select(ConnectorModel).where(ConnectorModel.id == uuid.UUID(connector_id))
            if tenant_id:
                query = query.where(ConnectorModel.tenant_id == tenant_id)
            
            result = await self.session.execute(query)
            db_connector = result.scalar_one_or_none()
            
            if not db_connector:
                return None
            
            return Connector(
                id=str(db_connector.id),
                tenant_id=db_connector.tenant_id,
                name=db_connector.name,
                description=db_connector.description,
                base_url=db_connector.base_url,
                auth_type=db_connector.auth_type,
                auth_config=db_connector.auth_config,
                credential_strategy=db_connector.credential_strategy,
                # Connector type - single source of truth
                connector_type=getattr(db_connector, 'connector_type', 'rest') or 'rest',
                protocol_config=db_connector.protocol_config,
                # SESSION auth fields
                login_url=db_connector.login_url,
                login_method=db_connector.login_method,
                login_config=db_connector.login_config,
                # Safety policies
                allowed_methods=db_connector.allowed_methods,
                blocked_methods=db_connector.blocked_methods,
                default_safety_level=db_connector.default_safety_level,
                is_active=db_connector.is_active,
                created_at=db_connector.created_at,
                updated_at=db_connector.updated_at
            )
        except ValueError:
            return None
    
    async def list_connectors(self, tenant_id: str, active_only: bool = True) -> List[Connector]:
        """List connectors for a tenant"""
        query = select(ConnectorModel).where(ConnectorModel.tenant_id == tenant_id)
        if active_only:
            query = query.where(ConnectorModel.is_active == True)
        query = query.order_by(ConnectorModel.created_at.desc())
        
        result = await self.session.execute(query)
        db_connectors = result.scalars().all()
        
        # Convert database models to Pydantic schemas (manually to handle UUID → str conversion)
        connectors = []
        for c in db_connectors:
            connector = Connector(
                id=str(c.id),  # Convert UUID to string here!
                tenant_id=c.tenant_id,
                name=c.name,
                description=c.description,
                base_url=c.base_url,
                auth_type=c.auth_type,
                auth_config=c.auth_config or {},
                credential_strategy=c.credential_strategy or "SYSTEM",
                # Connector type - single source of truth
                connector_type=getattr(c, 'connector_type', 'rest') or 'rest',
                protocol_config=c.protocol_config,
                # SESSION auth fields
                login_url=c.login_url,
                login_method=c.login_method,
                login_config=c.login_config,
                # Safety policies
                allowed_methods=c.allowed_methods or ["GET", "POST", "PUT", "PATCH", "DELETE"],
                blocked_methods=c.blocked_methods or [],
                default_safety_level=c.default_safety_level or "safe",
                is_active=c.is_active,
                created_at=c.created_at,
                updated_at=c.updated_at
            )
            connectors.append(connector)
        
        return connectors
    
    async def update_connector(self, connector_id: str, update: ConnectorUpdate, tenant_id: Optional[str] = None) -> Optional[Connector]:
        """Update connector configuration (Task 22)"""
        try:
            query = select(ConnectorModel).where(ConnectorModel.id == uuid.UUID(connector_id))
            if tenant_id:
                query = query.where(ConnectorModel.tenant_id == tenant_id)
            
            result = await self.session.execute(query)
            db_connector = result.scalar_one_or_none()
            
            if not db_connector:
                return None
            
            # Update fields
            update_data = update.model_dump(exclude_unset=True)
            for key, value in update_data.items():
                setattr(db_connector, key, value)
            
            db_connector.updated_at = datetime.utcnow()
            
            await self.session.flush()  # Flush changes, don't commit (session managed externally)
            await self.session.refresh(db_connector)
            
            return Connector.model_validate(db_connector)
        except ValueError:
            return None
    
    async def delete_connector(self, connector_id: str, tenant_id: Optional[str] = None) -> bool:
        """
        Delete a connector.
        
        Returns True if deleted, False if not found.
        
        Note: This will cascade delete related records (OpenAPI specs, endpoints, credentials)
        due to foreign key constraints with ON DELETE CASCADE.
        """
        try:
            query = select(ConnectorModel).where(ConnectorModel.id == uuid.UUID(connector_id))
            if tenant_id:
                query = query.where(ConnectorModel.tenant_id == tenant_id)
            
            result = await self.session.execute(query)
            db_connector = result.scalar_one_or_none()
            
            if not db_connector:
                return False
            
            await self.session.delete(db_connector)
            await self.session.flush()  # Flush changes, don't commit (session managed externally)
            
            return True
        except ValueError:
            return False


class OpenAPISpecRepository:
    """Repository for OpenAPI spec operations"""
    
    def __init__(self, session: AsyncSession):
        self.session = session
    
    async def create_spec(self, connector_id: str, storage_uri: str, version: Optional[str] = None, spec_version: Optional[str] = None) -> "OpenAPISpec":
        """Create OpenAPI spec record"""
        from meho_openapi.models import OpenAPISpecModel
        from meho_openapi.schemas import OpenAPISpec
        
        db_spec = OpenAPISpecModel(
            id=uuid.uuid4(),
            connector_id=uuid.UUID(connector_id),
            storage_uri=storage_uri,
            version=version,
            spec_version=spec_version
        )
        self.session.add(db_spec)
        await self.session.flush()  # Flush changes, don't commit (session managed externally)
        await self.session.refresh(db_spec)
        
        return OpenAPISpec(
            id=str(db_spec.id),
            connector_id=str(db_spec.connector_id),
            storage_uri=db_spec.storage_uri,
            version=db_spec.version,
            spec_version=db_spec.spec_version,
            created_at=db_spec.created_at
        )
    
    async def get_spec_by_connector(self, connector_id: str) -> Optional["OpenAPISpec"]:
        """Get the most recent OpenAPI spec for a connector"""
        from meho_openapi.models import OpenAPISpecModel
        from meho_openapi.schemas import OpenAPISpec
        
        try:
            connector_uuid = uuid.UUID(connector_id)
        except ValueError:
            return None
        
        result = await self.session.execute(
            select(OpenAPISpecModel)
            .where(OpenAPISpecModel.connector_id == connector_uuid)
            .order_by(OpenAPISpecModel.created_at.desc())
            .limit(1)
        )
        db_spec = result.scalar_one_or_none()
        
        if not db_spec:
            return None
        
        return OpenAPISpec(
            id=str(db_spec.id),
            connector_id=str(db_spec.connector_id),
            storage_uri=db_spec.storage_uri,
            version=db_spec.version,
            spec_version=db_spec.spec_version,
            created_at=db_spec.created_at
        )


class EndpointDescriptorRepository:
    """Repository for endpoint descriptor operations"""
    
    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def _to_endpoint_schema(db_endpoint: EndpointDescriptorModel) -> EndpointDescriptor:
        """
        Convert SQLAlchemy model to Pydantic schema.
        Ensures UUID fields are serialized as strings and JSON columns have defaults.
        """
        return EndpointDescriptor(
            id=str(db_endpoint.id),
            connector_id=str(db_endpoint.connector_id),
            method=db_endpoint.method,
            path=db_endpoint.path,
            operation_id=db_endpoint.operation_id,
            summary=db_endpoint.summary,
            description=db_endpoint.description,
            tags=db_endpoint.tags or [],
            required_params=db_endpoint.required_params or [],
            path_params_schema=db_endpoint.path_params_schema or {},
            query_params_schema=db_endpoint.query_params_schema or {},
            body_schema=db_endpoint.body_schema or {},
            response_schema=db_endpoint.response_schema or {},
            parameter_metadata=db_endpoint.parameter_metadata,
            llm_instructions=db_endpoint.llm_instructions,
            is_enabled=db_endpoint.is_enabled,
            safety_level=db_endpoint.safety_level,
            requires_approval=db_endpoint.requires_approval,
            custom_description=db_endpoint.custom_description,
            custom_notes=db_endpoint.custom_notes,
            usage_examples=db_endpoint.usage_examples,
            agent_notes=db_endpoint.agent_notes,
            common_errors=db_endpoint.common_errors,
            success_patterns=db_endpoint.success_patterns,
            created_at=db_endpoint.created_at,
            last_modified_by=db_endpoint.last_modified_by,
            last_modified_at=db_endpoint.last_modified_at
        )
    
    async def create_endpoint(self, endpoint: EndpointDescriptorCreate) -> EndpointDescriptor:
        """Create endpoint descriptor"""
        db_endpoint = EndpointDescriptorModel(
            id=uuid.uuid4(),
            **endpoint.model_dump()
        )
        self.session.add(db_endpoint)
        await self.session.flush()  # Flush changes, don't commit (session managed externally)
        await self.session.refresh(db_endpoint)
        
        return self._to_endpoint_schema(db_endpoint)
    
    async def upsert_endpoint(self, endpoint: EndpointDescriptorCreate) -> EndpointDescriptor:
        """
        Create or update endpoint descriptor.
        
        If an endpoint with the same connector_id + operation_id exists, updates it.
        Otherwise, creates a new endpoint.
        
        This prevents duplicates when re-uploading OpenAPI specs.
        
        Args:
            endpoint: Endpoint data to create/update
            
        Returns:
            Created or updated endpoint descriptor
        """
        # Check if endpoint exists
        existing = await self.get_endpoint_by_operation_id(
            connector_id=endpoint.connector_id,
            operation_id=endpoint.operation_id
        )
        
        if existing:
            # Update existing endpoint
            query = (
                update(EndpointDescriptorModel)
                .where(EndpointDescriptorModel.id == uuid.UUID(existing.id))
                .values(
                    method=endpoint.method,
                    path=endpoint.path,
                    summary=endpoint.summary,
                    description=endpoint.description,
                    tags=endpoint.tags,
                    required_params=endpoint.required_params,
                    path_params_schema=endpoint.path_params_schema,
                    query_params_schema=endpoint.query_params_schema,
                    body_schema=endpoint.body_schema,
                    response_schema=endpoint.response_schema,
                    parameter_metadata=endpoint.parameter_metadata,
                    last_modified_at=datetime.utcnow()
                )
            )
            await self.session.execute(query)
            await self.session.flush()
            
            # Fetch updated record
            updated = await self.get_endpoint(existing.id)
            if updated:
                return updated
        
        # Create new endpoint
        return await self.create_endpoint(endpoint)
    
    async def get_endpoint(self, endpoint_id: str) -> Optional[EndpointDescriptor]:
        """Get endpoint descriptor by ID"""
        try:
            query = select(EndpointDescriptorModel).where(
                EndpointDescriptorModel.id == uuid.UUID(endpoint_id)
            )
            result = await self.session.execute(query)
            db_endpoint = result.scalar_one_or_none()
            
            if not db_endpoint:
                return None
            
            return self._to_endpoint_schema(db_endpoint)
        except ValueError:
            return None
    
    async def get_endpoint_by_operation_id(
        self, 
        connector_id: str, 
        operation_id: str
    ) -> Optional[EndpointDescriptor]:
        """Get endpoint descriptor by connector_id and operation_id"""
        try:
            query = select(EndpointDescriptorModel).where(
                EndpointDescriptorModel.connector_id == uuid.UUID(connector_id),
                EndpointDescriptorModel.operation_id == operation_id
            )
            result = await self.session.execute(query)
            db_endpoint = result.scalar_one_or_none()
            
            if not db_endpoint:
                return None
            
            return self._to_endpoint_schema(db_endpoint)
        except ValueError:
            return None
    
    async def list_endpoints(self, filter: EndpointFilter) -> List[EndpointDescriptor]:
        """List endpoints with filters"""
        query = select(EndpointDescriptorModel)
        
        conditions = []
        if filter.connector_id:
            conditions.append(EndpointDescriptorModel.connector_id == uuid.UUID(filter.connector_id))
        if filter.method:
            conditions.append(EndpointDescriptorModel.method == filter.method.upper())
        if filter.tags:
            for tag in filter.tags:
                conditions.append(EndpointDescriptorModel.tags.contains([tag]))
        if filter.search_text:
            search = f"%{filter.search_text}%"
            conditions.append(or_(
                EndpointDescriptorModel.summary.ilike(search),
                EndpointDescriptorModel.description.ilike(search),
                EndpointDescriptorModel.path.ilike(search)
            ))
        
        # Task 22: Filter by activation and safety
        if filter.is_enabled is not None:
            conditions.append(EndpointDescriptorModel.is_enabled == filter.is_enabled)
        if filter.safety_level:
            conditions.append(EndpointDescriptorModel.safety_level == filter.safety_level)
        
        if conditions:
            query = query.where(and_(*conditions))
        
        query = query.limit(filter.limit).offset(filter.offset).order_by(EndpointDescriptorModel.path)
        
        result = await self.session.execute(query)
        db_endpoints = result.scalars().all()
        
        return [self._to_endpoint_schema(e) for e in db_endpoints]
    
    async def update_endpoint(self, endpoint_id: str, update: EndpointUpdate, modified_by: str) -> Optional[EndpointDescriptor]:
        """Update endpoint configuration (Task 22)"""
        try:
            query = select(EndpointDescriptorModel).where(
                EndpointDescriptorModel.id == uuid.UUID(endpoint_id)
            )
            result = await self.session.execute(query)
            db_endpoint = result.scalar_one_or_none()
            
            if not db_endpoint:
                return None
            
            # Update fields
            update_data = update.model_dump(exclude_unset=True)
            for key, value in update_data.items():
                setattr(db_endpoint, key, value)
            
            # Update audit trail
            db_endpoint.last_modified_by = modified_by
            db_endpoint.last_modified_at = datetime.utcnow()
            
            await self.session.flush()  # Flush changes, don't commit (session managed externally)
            await self.session.refresh(db_endpoint)
            
            return self._to_endpoint_schema(db_endpoint)
        except ValueError:
            return None


# ============================================================================
# SOAP Repositories (TASK-96: SOAP Type Support)
# ============================================================================

class SoapOperationRepository:
    """Repository for SOAP operation descriptor operations"""
    
    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def _to_schema(db_op: SoapOperationDescriptorModel) -> SoapOperationDescriptor:
        """Convert SQLAlchemy model to Pydantic schema"""
        return SoapOperationDescriptor(
            id=str(db_op.id),
            connector_id=str(db_op.connector_id),
            tenant_id=db_op.tenant_id,
            service_name=db_op.service_name,
            port_name=db_op.port_name,
            operation_name=db_op.operation_name,
            name=db_op.name,
            description=db_op.description,
            soap_action=db_op.soap_action,
            namespace=db_op.namespace,
            style=db_op.style,
            input_schema=db_op.input_schema or {},
            output_schema=db_op.output_schema or {},
            protocol_details=db_op.protocol_details or {},
            search_content=db_op.search_content,
            is_enabled=db_op.is_enabled,
            safety_level=db_op.safety_level,
            requires_approval=db_op.requires_approval,
            created_at=db_op.created_at,
            updated_at=db_op.updated_at,
        )

    async def create_operation(self, operation: SoapOperationDescriptorCreate) -> SoapOperationDescriptor:
        """Create SOAP operation descriptor"""
        db_op = SoapOperationDescriptorModel(
            id=uuid.uuid4(),
            **operation.model_dump()
        )
        self.session.add(db_op)
        await self.session.flush()
        await self.session.refresh(db_op)
        return self._to_schema(db_op)

    async def create_operations_bulk(
        self, 
        operations: List[SoapOperationDescriptorCreate]
    ) -> int:
        """Bulk create SOAP operations. Returns count of created operations."""
        for op in operations:
            db_op = SoapOperationDescriptorModel(
                id=uuid.uuid4(),
                **op.model_dump()
            )
            self.session.add(db_op)
        await self.session.flush()
        return len(operations)

    async def delete_by_connector(self, connector_id: str) -> int:
        """Delete all SOAP operations for a connector. Returns count deleted."""
        from sqlalchemy import delete
        query = delete(SoapOperationDescriptorModel).where(
            SoapOperationDescriptorModel.connector_id == uuid.UUID(connector_id)
        )
        result = await self.session.execute(query)
        return int(getattr(result, 'rowcount', 0) or 0)

    async def get_operation(self, operation_id: str) -> Optional[SoapOperationDescriptor]:
        """Get SOAP operation by ID"""
        try:
            query = select(SoapOperationDescriptorModel).where(
                SoapOperationDescriptorModel.id == uuid.UUID(operation_id)
            )
            result = await self.session.execute(query)
            db_op = result.scalar_one_or_none()
            return self._to_schema(db_op) if db_op else None
        except ValueError:
            return None

    async def list_operations(
        self, 
        connector_id: str,
        service_name: Optional[str] = None,
        is_enabled: Optional[bool] = None,
        limit: int = 100,
        offset: int = 0
    ) -> List[SoapOperationDescriptor]:
        """List SOAP operations with filters"""
        query = select(SoapOperationDescriptorModel).where(
            SoapOperationDescriptorModel.connector_id == uuid.UUID(connector_id)
        )
        
        if service_name:
            query = query.where(SoapOperationDescriptorModel.service_name == service_name)
        if is_enabled is not None:
            query = query.where(SoapOperationDescriptorModel.is_enabled == is_enabled)
        
        query = query.limit(limit).offset(offset).order_by(SoapOperationDescriptorModel.name)
        
        result = await self.session.execute(query)
        db_ops = result.scalars().all()
        return [self._to_schema(op) for op in db_ops]

    async def search_operations(
        self, 
        connector_id: str, 
        query_text: str, 
        limit: int = 10
    ) -> List[SoapOperationDescriptor]:
        """
        Search SOAP operations using SQL ILIKE.
        
        For full BM25 search, use the BM25Service with this repository's data.
        This method provides fast, basic search capability.
        """
        search = f"%{query_text}%"
        query = select(SoapOperationDescriptorModel).where(
            and_(
                SoapOperationDescriptorModel.connector_id == uuid.UUID(connector_id),
                or_(
                    SoapOperationDescriptorModel.name.ilike(search),
                    SoapOperationDescriptorModel.operation_name.ilike(search),
                    SoapOperationDescriptorModel.description.ilike(search),
                    SoapOperationDescriptorModel.search_content.ilike(search),
                )
            )
        ).limit(limit)
        
        result = await self.session.execute(query)
        db_ops = result.scalars().all()
        return [self._to_schema(op) for op in db_ops]

    async def get_all_for_bm25(self, connector_id: str) -> List[dict]:
        """
        Get all operations for a connector in format suitable for BM25 indexing.
        
        Returns list of dicts with 'id' and 'text' for BM25 corpus building.
        """
        query = select(SoapOperationDescriptorModel).where(
            SoapOperationDescriptorModel.connector_id == uuid.UUID(connector_id),
            SoapOperationDescriptorModel.is_enabled == True
        )
        result = await self.session.execute(query)
        db_ops = result.scalars().all()
        
        return [
            {
                "id": str(op.id),
                "text": op.search_content or f"{op.name} {op.description or ''}"
            }
            for op in db_ops
        ]


class SoapTypeRepository:
    """Repository for SOAP type descriptor operations"""
    
    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def _to_schema(db_type: SoapTypeDescriptorModel) -> SoapTypeDescriptor:
        """Convert SQLAlchemy model to Pydantic schema"""
        return SoapTypeDescriptor(
            id=str(db_type.id),
            connector_id=str(db_type.connector_id),
            tenant_id=db_type.tenant_id,
            type_name=db_type.type_name,
            namespace=db_type.namespace,
            base_type=db_type.base_type,
            properties=db_type.properties or [],
            description=db_type.description,
            search_content=db_type.search_content,
            created_at=db_type.created_at,
            updated_at=db_type.updated_at,
        )

    async def create_type(self, type_def: SoapTypeDescriptorCreate) -> SoapTypeDescriptor:
        """Create SOAP type descriptor"""
        db_type = SoapTypeDescriptorModel(
            id=uuid.uuid4(),
            **type_def.model_dump()
        )
        self.session.add(db_type)
        await self.session.flush()
        await self.session.refresh(db_type)
        return self._to_schema(db_type)

    async def create_types_bulk(self, types: List[SoapTypeDescriptorCreate]) -> int:
        """Bulk create SOAP types. Returns count of created types."""
        for type_def in types:
            db_type = SoapTypeDescriptorModel(
                id=uuid.uuid4(),
                **type_def.model_dump()
            )
            self.session.add(db_type)
        await self.session.flush()
        return len(types)

    async def delete_by_connector(self, connector_id: str) -> int:
        """Delete all SOAP types for a connector. Returns count deleted."""
        from sqlalchemy import delete
        query = delete(SoapTypeDescriptorModel).where(
            SoapTypeDescriptorModel.connector_id == uuid.UUID(connector_id)
        )
        result = await self.session.execute(query)
        return int(getattr(result, 'rowcount', 0) or 0)

    async def get_type(self, type_id: str) -> Optional[SoapTypeDescriptor]:
        """Get SOAP type by ID"""
        try:
            query = select(SoapTypeDescriptorModel).where(
                SoapTypeDescriptorModel.id == uuid.UUID(type_id)
            )
            result = await self.session.execute(query)
            db_type = result.scalar_one_or_none()
            return self._to_schema(db_type) if db_type else None
        except ValueError:
            return None

    async def get_type_by_name(
        self, 
        connector_id: str, 
        type_name: str
    ) -> Optional[SoapTypeDescriptor]:
        """Get SOAP type by name within a connector"""
        query = select(SoapTypeDescriptorModel).where(
            and_(
                SoapTypeDescriptorModel.connector_id == uuid.UUID(connector_id),
                SoapTypeDescriptorModel.type_name == type_name
            )
        )
        result = await self.session.execute(query)
        db_type = result.scalar_one_or_none()
        return self._to_schema(db_type) if db_type else None

    async def list_types(
        self, 
        connector_id: str,
        base_type: Optional[str] = None,
        limit: int = 100,
        offset: int = 0
    ) -> List[SoapTypeDescriptor]:
        """List SOAP types with filters"""
        query = select(SoapTypeDescriptorModel).where(
            SoapTypeDescriptorModel.connector_id == uuid.UUID(connector_id)
        )
        
        if base_type:
            query = query.where(SoapTypeDescriptorModel.base_type == base_type)
        
        query = query.limit(limit).offset(offset).order_by(SoapTypeDescriptorModel.type_name)
        
        result = await self.session.execute(query)
        db_types = result.scalars().all()
        return [self._to_schema(t) for t in db_types]

    async def search_types(
        self, 
        connector_id: str, 
        query_text: str, 
        limit: int = 10
    ) -> List[SoapTypeDescriptor]:
        """
        Search SOAP types using SQL ILIKE.
        
        For full BM25 search, use the BM25Service with this repository's data.
        """
        search = f"%{query_text}%"
        query = select(SoapTypeDescriptorModel).where(
            and_(
                SoapTypeDescriptorModel.connector_id == uuid.UUID(connector_id),
                or_(
                    SoapTypeDescriptorModel.type_name.ilike(search),
                    SoapTypeDescriptorModel.description.ilike(search),
                    SoapTypeDescriptorModel.search_content.ilike(search),
                )
            )
        ).limit(limit)
        
        result = await self.session.execute(query)
        db_types = result.scalars().all()
        return [self._to_schema(t) for t in db_types]

    async def get_all_for_bm25(self, connector_id: str) -> List[dict]:
        """
        Get all types for a connector in format suitable for BM25 indexing.
        
        Returns list of dicts with 'id' and 'text' for BM25 corpus building.
        """
        query = select(SoapTypeDescriptorModel).where(
            SoapTypeDescriptorModel.connector_id == uuid.UUID(connector_id)
        )
        result = await self.session.execute(query)
        db_types = result.scalars().all()
        
        return [
            {
                "id": str(t.id),
                "text": t.search_content or f"{t.type_name} {t.base_type or ''} {t.description or ''}"
            }
            for t in db_types
        ]


# ============================================================================
# Typed Connector Repositories (TASK-97: VMware/Kubernetes/etc)
# ============================================================================

class ConnectorOperationRepository:
    """
    Repository for typed connector operations (TASK-97).
    
    Used by VMware, Kubernetes, and other typed connectors.
    Provides uniform search/list operations regardless of connector type.
    """
    
    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def _to_schema(db_op: ConnectorOperationModel) -> "ConnectorOperationDescriptor":
        """Convert SQLAlchemy model to Pydantic schema"""
        from meho_openapi.schemas import ConnectorOperationDescriptor
        return ConnectorOperationDescriptor(
            id=str(db_op.id),
            connector_id=str(db_op.connector_id),
            tenant_id=db_op.tenant_id,
            operation_id=db_op.operation_id,
            name=db_op.name,
            description=db_op.description,
            category=db_op.category,
            parameters=db_op.parameters or [],
            example=db_op.example,
            search_content=db_op.search_content,
            is_enabled=db_op.is_enabled,
            safety_level=db_op.safety_level,
            requires_approval=db_op.requires_approval,
            created_at=db_op.created_at,
            updated_at=db_op.updated_at,
        )

    async def create_operation(self, operation: "ConnectorOperationCreate") -> "ConnectorOperationDescriptor":
        """Create connector operation descriptor"""
        db_op = ConnectorOperationModel(
            id=uuid.uuid4(),
            **operation.model_dump()
        )
        self.session.add(db_op)
        await self.session.flush()
        await self.session.refresh(db_op)
        return self._to_schema(db_op)

    async def create_operations_bulk(self, operations: List["ConnectorOperationCreate"]) -> int:
        """Bulk create operations. Returns count of created operations."""
        for op in operations:
            db_op = ConnectorOperationModel(
                id=uuid.uuid4(),
                **op.model_dump()
            )
            self.session.add(db_op)
        await self.session.flush()
        return len(operations)

    async def delete_by_connector(self, connector_id: str) -> int:
        """Delete all operations for a connector. Returns count deleted."""
        from sqlalchemy import delete
        query = delete(ConnectorOperationModel).where(
            ConnectorOperationModel.connector_id == uuid.UUID(connector_id)
        )
        result = await self.session.execute(query)
        return int(getattr(result, 'rowcount', 0) or 0)

    async def update_operation(
        self,
        connector_id: str,
        operation_id: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
        category: Optional[str] = None,
        parameters: Optional[List[dict]] = None,
        example: Optional[str] = None,
        search_content: Optional[str] = None,
    ) -> bool:
        """
        Update an existing operation by connector_id and operation_id.
        
        Returns True if operation was found and updated, False otherwise.
        """
        query = select(ConnectorOperationModel).where(
            and_(
                ConnectorOperationModel.connector_id == uuid.UUID(connector_id),
                ConnectorOperationModel.operation_id == operation_id
            )
        )
        result = await self.session.execute(query)
        db_op = result.scalar_one_or_none()
        
        if not db_op:
            return False
        
        if name is not None:
            db_op.name = name
        if description is not None:
            db_op.description = description
        if category is not None:
            db_op.category = category
        if parameters is not None:
            db_op.parameters = parameters
        if example is not None:
            db_op.example = example
        if search_content is not None:
            db_op.search_content = search_content
        
        await self.session.flush()
        return True

    async def get_operation(self, operation_id: str) -> Optional["ConnectorOperationDescriptor"]:
        """Get operation by ID"""
        try:
            query = select(ConnectorOperationModel).where(
                ConnectorOperationModel.id == uuid.UUID(operation_id)
            )
            result = await self.session.execute(query)
            db_op = result.scalar_one_or_none()
            return self._to_schema(db_op) if db_op else None
        except ValueError:
            return None

    async def get_operation_by_op_id(
        self, 
        connector_id: str, 
        operation_id: str
    ) -> Optional["ConnectorOperationDescriptor"]:
        """Get operation by connector_id and operation_id"""
        query = select(ConnectorOperationModel).where(
            and_(
                ConnectorOperationModel.connector_id == uuid.UUID(connector_id),
                ConnectorOperationModel.operation_id == operation_id
            )
        )
        result = await self.session.execute(query)
        db_op = result.scalar_one_or_none()
        return self._to_schema(db_op) if db_op else None

    async def list_operations(
        self, 
        connector_id: str,
        category: Optional[str] = None,
        is_enabled: Optional[bool] = None,
        limit: int = 100,
        offset: int = 0
    ) -> List["ConnectorOperationDescriptor"]:
        """List operations with filters"""
        query = select(ConnectorOperationModel).where(
            ConnectorOperationModel.connector_id == uuid.UUID(connector_id)
        )
        
        if category:
            query = query.where(ConnectorOperationModel.category == category)
        if is_enabled is not None:
            query = query.where(ConnectorOperationModel.is_enabled == is_enabled)
        
        query = query.limit(limit).offset(offset).order_by(ConnectorOperationModel.name)
        
        result = await self.session.execute(query)
        db_ops = result.scalars().all()
        return [self._to_schema(op) for op in db_ops]

    async def search_operations(
        self, 
        connector_id: str, 
        query_text: str, 
        limit: int = 10
    ) -> List["ConnectorOperationDescriptor"]:
        """Search operations using SQL ILIKE"""
        search = f"%{query_text}%"
        query = select(ConnectorOperationModel).where(
            and_(
                ConnectorOperationModel.connector_id == uuid.UUID(connector_id),
                or_(
                    ConnectorOperationModel.name.ilike(search),
                    ConnectorOperationModel.operation_id.ilike(search),
                    ConnectorOperationModel.description.ilike(search),
                    ConnectorOperationModel.search_content.ilike(search),
                )
            )
        ).limit(limit)
        
        result = await self.session.execute(query)
        db_ops = result.scalars().all()
        return [self._to_schema(op) for op in db_ops]

    async def get_all_for_bm25(self, connector_id: str) -> List[dict]:
        """
        Get all operations for BM25 indexing with rich search content.
        
        Returns all fields needed for:
        1. BM25 search (text field with operation_id, name, description, category)
        2. Agent response (operation_id, name, description, parameters, example)
        
        The 'text' field combines multiple fields for better stemming matches:
        - "list_virtual_machines" becomes searchable via "list", "virtual", "machine"
        - Category helps match "compute", "storage", "networking" queries
        """
        query = select(ConnectorOperationModel).where(
            ConnectorOperationModel.connector_id == uuid.UUID(connector_id),
            ConnectorOperationModel.is_enabled == True
        )
        result = await self.session.execute(query)
        db_ops = result.scalars().all()
        
        return [
            {
                # ID for cache key generation
                "id": str(op.id),
                # Rich text for BM25 indexing (all searchable content)
                "text": " ".join([
                    str(op.operation_id) if op.operation_id else "",
                    str(op.name) if op.name else "",
                    str(op.description) if op.description else "",
                    str(op.category) if op.category else "",
                    str(op.search_content) if op.search_content else "",
                ]),
                # Fields needed for agent response
                "operation_id": op.operation_id,
                "name": op.name,
                "description": op.description,
                "category": op.category,
                "parameters": op.parameters,
                "example": op.example,
            }
            for op in db_ops
        ]


class ConnectorTypeRepository:
    """
    Repository for typed connector entity types (TASK-97).
    
    Stores entity type definitions (VirtualMachine, Cluster, Pod, etc.)
    for typed connectors. Used by agent to understand available entities.
    """
    
    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def _to_schema(db_type: ConnectorTypeModel) -> "ConnectorEntityType":
        """Convert SQLAlchemy model to Pydantic schema"""
        from meho_openapi.schemas import ConnectorEntityType
        return ConnectorEntityType(
            id=str(db_type.id),
            connector_id=str(db_type.connector_id),
            tenant_id=db_type.tenant_id,
            type_name=db_type.type_name,
            description=db_type.description,
            category=db_type.category,
            properties=db_type.properties or [],
            search_content=db_type.search_content,
            created_at=db_type.created_at,
            updated_at=db_type.updated_at,
        )

    async def create_type(self, type_def: "ConnectorEntityTypeCreate") -> "ConnectorEntityType":
        """Create connector entity type"""
        db_type = ConnectorTypeModel(
            id=uuid.uuid4(),
            **type_def.model_dump()
        )
        self.session.add(db_type)
        await self.session.flush()
        await self.session.refresh(db_type)
        return self._to_schema(db_type)

    async def create_types_bulk(self, types: List["ConnectorEntityTypeCreate"]) -> int:
        """Bulk create entity types. Returns count created."""
        for type_def in types:
            db_type = ConnectorTypeModel(
                id=uuid.uuid4(),
                **type_def.model_dump()
            )
            self.session.add(db_type)
        await self.session.flush()
        return len(types)

    async def delete_by_connector(self, connector_id: str) -> int:
        """Delete all types for a connector. Returns count deleted."""
        from sqlalchemy import delete
        query = delete(ConnectorTypeModel).where(
            ConnectorTypeModel.connector_id == uuid.UUID(connector_id)
        )
        result = await self.session.execute(query)
        return int(getattr(result, 'rowcount', 0) or 0)

    async def get_type(self, type_id: str) -> Optional["ConnectorEntityType"]:
        """Get entity type by ID"""
        try:
            query = select(ConnectorTypeModel).where(
                ConnectorTypeModel.id == uuid.UUID(type_id)
            )
            result = await self.session.execute(query)
            db_type = result.scalar_one_or_none()
            return self._to_schema(db_type) if db_type else None
        except ValueError:
            return None

    async def get_type_by_name(
        self, 
        connector_id: str, 
        type_name: str
    ) -> Optional["ConnectorEntityType"]:
        """Get entity type by name within a connector"""
        query = select(ConnectorTypeModel).where(
            and_(
                ConnectorTypeModel.connector_id == uuid.UUID(connector_id),
                ConnectorTypeModel.type_name == type_name
            )
        )
        result = await self.session.execute(query)
        db_type = result.scalar_one_or_none()
        return self._to_schema(db_type) if db_type else None

    async def list_types(
        self, 
        connector_id: str,
        category: Optional[str] = None,
        limit: int = 100,
        offset: int = 0
    ) -> List["ConnectorEntityType"]:
        """List entity types with filters"""
        query = select(ConnectorTypeModel).where(
            ConnectorTypeModel.connector_id == uuid.UUID(connector_id)
        )
        
        if category:
            query = query.where(ConnectorTypeModel.category == category)
        
        query = query.limit(limit).offset(offset).order_by(ConnectorTypeModel.type_name)
        
        result = await self.session.execute(query)
        db_types = result.scalars().all()
        return [self._to_schema(t) for t in db_types]

    async def search_types(
        self, 
        connector_id: str, 
        query_text: str, 
        limit: int = 10
    ) -> List["ConnectorEntityType"]:
        """Search entity types using SQL ILIKE"""
        search = f"%{query_text}%"
        query = select(ConnectorTypeModel).where(
            and_(
                ConnectorTypeModel.connector_id == uuid.UUID(connector_id),
                or_(
                    ConnectorTypeModel.type_name.ilike(search),
                    ConnectorTypeModel.description.ilike(search),
                    ConnectorTypeModel.search_content.ilike(search),
                )
            )
        ).limit(limit)
        
        result = await self.session.execute(query)
        db_types = result.scalars().all()
        return [self._to_schema(t) for t in db_types]

    async def get_all_for_bm25(self, connector_id: str) -> List[dict]:
        """Get all entity types for BM25 indexing"""
        query = select(ConnectorTypeModel).where(
            ConnectorTypeModel.connector_id == uuid.UUID(connector_id)
        )
        result = await self.session.execute(query)
        db_types = result.scalars().all()
        
        return [
            {
                "id": str(t.id),
                "text": t.search_content or f"{t.type_name} {t.description or ''}"
            }
            for t in db_types
        ]

