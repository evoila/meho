# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Initial consolidated schema for the MEHO monolith.

Concatenates the previous nine module-level squash migrations
(topology, connectors, knowledge, memory, agents, ingestion,
scheduled_tasks, orchestrator_skills, audit) into a single linear
history. Tables are created in foreign-key dependency order so a fresh
database can run ``alembic upgrade head`` end-to-end without errors.

Existing deployments running the legacy multi-tree layout MUST run
``scripts/migrate_to_unified_alembic.py`` *before* upgrading. The rescue
script verifies the legacy ``alembic_version_meho_*`` revisions, stamps
the new unified ``alembic_version`` table, and drops the legacy tables.
This file therefore assumes a clean database and contains no "table
exists" probe blocks.

Known autogenerate residue
--------------------------

``alembic check`` reports a non-empty diff against ``Base.metadata``
after this migration runs. That divergence existed in the original
nine squash migrations as well (this file is a faithful concatenation,
not a re-derivation from metadata). The differences are cosmetic:

* per-column ``index=True`` declarations on models that the squash
  migrations never emitted as separate ``CREATE INDEX`` statements
  (e.g. ``ix_audit_event_tenant_id``);
* foreign keys created with the PostgreSQL default name pattern
  ``<table>_<col>_fkey`` rather than an explicit Alembic-named
  constraint;
* HNSW vector indexes (``USING hnsw``) and GIN JSONB indexes that are
  raw DDL and not declared on the SQLAlchemy model side.

None of these affect query behavior, FK semantics, or test fixtures.
Closing the residue is a separate alignment task tracked outside
Goal #294.

Revision ID: 0001_init
Revises:
Create Date: 2026-04-27
"""

from alembic import op

revision = "0001_init"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the full MEHO schema (every module) on a fresh database."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ------------------------------------------------------------------------
    # topology -- discovered system entities, relationships, embeddings
    # ------------------------------------------------------------------------
    op.execute("""
        CREATE TABLE topology_entities (
            id UUID PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            connector_id UUID,
            connector_name VARCHAR(255),
            entity_type VARCHAR(100) NOT NULL,
            connector_type VARCHAR(50) NOT NULL,
            scope JSONB DEFAULT '{}',
            canonical_id VARCHAR(500) NOT NULL,
            description TEXT NOT NULL,
            raw_attributes JSONB DEFAULT '{}',
            discovered_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_verified_at TIMESTAMP WITH TIME ZONE,
            stale_at TIMESTAMP WITH TIME ZONE,
            tenant_id VARCHAR(100) NOT NULL
        )
    """)
    op.execute("CREATE INDEX idx_topology_entities_tenant ON topology_entities (tenant_id)")
    op.execute("CREATE INDEX idx_topology_entities_connector ON topology_entities (connector_id)")
    op.execute("CREATE INDEX idx_topology_entities_name ON topology_entities (name)")
    op.execute("CREATE INDEX idx_topology_entities_type ON topology_entities (entity_type)")
    op.execute(
        "CREATE INDEX idx_topology_entities_connector_type ON topology_entities (connector_type)"
    )
    op.execute(
        "CREATE UNIQUE INDEX idx_topology_entity_identity "
        "ON topology_entities (tenant_id, connector_id, entity_type, canonical_id)"
    )

    op.execute("""
        CREATE TABLE topology_embeddings (
            entity_id UUID PRIMARY KEY REFERENCES topology_entities(id) ON DELETE CASCADE,
            embedding vector(1024)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_topology_embeddings_hnsw
        ON topology_embeddings
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """)

    op.execute("""
        CREATE TABLE topology_relationships (
            id UUID PRIMARY KEY,
            from_entity_id UUID NOT NULL REFERENCES topology_entities(id) ON DELETE CASCADE,
            to_entity_id UUID NOT NULL REFERENCES topology_entities(id) ON DELETE CASCADE,
            relationship_type VARCHAR(100) NOT NULL,
            discovered_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_verified_at TIMESTAMP WITH TIME ZONE
        )
    """)
    op.execute(
        "CREATE INDEX idx_topology_relationships_from ON topology_relationships (from_entity_id)"
    )
    op.execute(
        "CREATE INDEX idx_topology_relationships_to ON topology_relationships (to_entity_id)"
    )
    op.execute(
        "CREATE UNIQUE INDEX idx_topology_relationships_unique "
        "ON topology_relationships (from_entity_id, to_entity_id, relationship_type)"
    )

    op.execute("""
        CREATE TABLE topology_same_as (
            id UUID PRIMARY KEY,
            entity_a_id UUID NOT NULL REFERENCES topology_entities(id) ON DELETE CASCADE,
            entity_b_id UUID NOT NULL REFERENCES topology_entities(id) ON DELETE CASCADE,
            similarity_score FLOAT NOT NULL,
            verified_via TEXT[] NOT NULL,
            discovered_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_verified_at TIMESTAMP WITH TIME ZONE,
            tenant_id VARCHAR(100) NOT NULL
        )
    """)
    op.execute("CREATE INDEX idx_topology_same_as_tenant ON topology_same_as (tenant_id)")
    op.execute(
        "CREATE UNIQUE INDEX idx_topology_same_as_unique "
        "ON topology_same_as (entity_a_id, entity_b_id, tenant_id)"
    )

    op.execute("""
        CREATE TABLE topology_same_as_suggestion (
            id UUID PRIMARY KEY,
            entity_a_id UUID NOT NULL REFERENCES topology_entities(id) ON DELETE CASCADE,
            entity_b_id UUID NOT NULL REFERENCES topology_entities(id) ON DELETE CASCADE,
            confidence FLOAT NOT NULL,
            match_type VARCHAR(50) NOT NULL,
            match_details TEXT,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            suggested_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            resolved_at TIMESTAMP WITH TIME ZONE,
            resolved_by VARCHAR(255),
            llm_verification_attempted BOOLEAN NOT NULL DEFAULT false,
            llm_verification_result JSONB,
            tenant_id VARCHAR(100) NOT NULL
        )
    """)
    op.execute(
        "CREATE INDEX idx_topology_suggestion_tenant ON topology_same_as_suggestion (tenant_id)"
    )
    op.execute(
        "CREATE INDEX idx_topology_suggestion_status ON topology_same_as_suggestion (status)"
    )
    op.execute(
        "CREATE UNIQUE INDEX idx_topology_suggestion_unique "
        "ON topology_same_as_suggestion (entity_a_id, entity_b_id)"
    )

    # ------------------------------------------------------------------------
    # connectors -- core connector configuration + REST/SOAP descriptors
    # ------------------------------------------------------------------------
    op.execute("""
        CREATE TABLE connector (
            id UUID PRIMARY KEY,
            tenant_id VARCHAR NOT NULL,
            name VARCHAR NOT NULL,
            description TEXT,
            base_url VARCHAR NOT NULL,
            connector_type VARCHAR NOT NULL DEFAULT 'rest',
            protocol_config JSONB,
            auth_type VARCHAR NOT NULL,
            auth_config JSONB NOT NULL DEFAULT '{}',
            credential_strategy VARCHAR NOT NULL DEFAULT 'SYSTEM',
            login_url VARCHAR,
            login_method VARCHAR,
            login_config JSONB,
            allowed_methods JSONB NOT NULL DEFAULT '["GET", "POST", "PUT", "PATCH", "DELETE"]',
            blocked_methods JSONB NOT NULL DEFAULT '[]',
            default_safety_level VARCHAR NOT NULL DEFAULT 'safe',
            related_connector_ids JSONB DEFAULT '[]',
            topology_entity_id UUID REFERENCES topology_entities(id) ON DELETE SET NULL,
            is_active BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            routing_description TEXT,
            skill_name VARCHAR,
            generated_skill TEXT,
            custom_skill TEXT,
            skill_quality_score INTEGER,
            automation_enabled BOOLEAN NOT NULL DEFAULT true
        )
    """)
    op.execute("CREATE INDEX ix_connector_tenant_id ON connector (tenant_id)")
    op.execute("CREATE INDEX ix_connector_tenant_name ON connector (tenant_id, name)")
    op.execute("CREATE INDEX ix_connector_topology_entity_id ON connector (topology_entity_id)")

    op.execute("""
        CREATE TABLE openapi_spec (
            id UUID PRIMARY KEY,
            connector_id UUID NOT NULL REFERENCES connector(id) ON DELETE CASCADE,
            storage_uri TEXT NOT NULL,
            version VARCHAR,
            spec_version VARCHAR,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)

    op.execute("""
        CREATE TABLE endpoint_descriptor (
            id UUID PRIMARY KEY,
            connector_id UUID NOT NULL REFERENCES connector(id) ON DELETE CASCADE,
            method VARCHAR NOT NULL,
            path VARCHAR NOT NULL,
            operation_id VARCHAR,
            summary TEXT,
            description TEXT,
            tags JSONB NOT NULL DEFAULT '[]',
            required_params JSONB NOT NULL DEFAULT '[]',
            path_params_schema JSONB NOT NULL DEFAULT '{}',
            query_params_schema JSONB NOT NULL DEFAULT '{}',
            body_schema JSONB NOT NULL DEFAULT '{}',
            response_schema JSONB NOT NULL DEFAULT '{}',
            parameter_metadata JSONB,
            llm_instructions JSONB,
            is_enabled BOOLEAN NOT NULL DEFAULT true,
            safety_level VARCHAR NOT NULL DEFAULT 'safe',
            requires_approval BOOLEAN NOT NULL DEFAULT false,
            custom_description TEXT,
            custom_notes TEXT,
            usage_examples JSONB,
            agent_notes TEXT,
            common_errors JSONB,
            success_patterns JSONB,
            last_modified_by VARCHAR,
            last_modified_at TIMESTAMP WITH TIME ZONE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    op.execute(
        "CREATE INDEX ix_endpoint_connector_method_path "
        "ON endpoint_descriptor (connector_id, method, path)"
    )
    op.execute("CREATE INDEX ix_endpoint_connector_tags ON endpoint_descriptor (connector_id)")
    op.execute("CREATE INDEX ix_endpoint_enabled ON endpoint_descriptor (connector_id, is_enabled)")
    op.execute(
        "CREATE INDEX ix_endpoint_safety ON endpoint_descriptor (connector_id, safety_level)"
    )

    op.execute("""
        CREATE TABLE user_connector_credential (
            id UUID PRIMARY KEY,
            user_id VARCHAR NOT NULL,
            connector_id UUID NOT NULL REFERENCES connector(id) ON DELETE CASCADE,
            credential_type VARCHAR NOT NULL,
            encrypted_credentials TEXT NOT NULL,
            oauth2_refresh_token TEXT,
            oauth2_token_expires_at TIMESTAMP WITH TIME ZONE,
            session_token TEXT,
            session_token_expires_at TIMESTAMP WITH TIME ZONE,
            session_refresh_token TEXT,
            session_refresh_expires_at TIMESTAMP WITH TIME ZONE,
            session_state VARCHAR,
            is_active BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_used_at TIMESTAMP WITH TIME ZONE,
            credential_health VARCHAR(20),
            credential_health_message VARCHAR(500),
            credential_health_checked_at TIMESTAMP WITH TIME ZONE
        )
    """)
    op.execute(
        "CREATE INDEX ix_user_connector_credential_user_id ON user_connector_credential (user_id)"
    )
    op.execute(
        "CREATE UNIQUE INDEX ix_user_connector ON user_connector_credential (user_id, connector_id)"
    )

    op.execute("""
        CREATE TABLE soap_operation_descriptor (
            id UUID PRIMARY KEY,
            connector_id UUID NOT NULL REFERENCES connector(id) ON DELETE CASCADE,
            tenant_id VARCHAR NOT NULL,
            service_name VARCHAR NOT NULL,
            port_name VARCHAR NOT NULL,
            operation_name VARCHAR NOT NULL,
            name VARCHAR NOT NULL,
            description TEXT,
            soap_action VARCHAR,
            namespace VARCHAR,
            style VARCHAR NOT NULL DEFAULT 'document',
            input_schema JSONB NOT NULL DEFAULT '{}',
            output_schema JSONB NOT NULL DEFAULT '{}',
            protocol_details JSONB NOT NULL DEFAULT '{}',
            search_content TEXT,
            is_enabled BOOLEAN NOT NULL DEFAULT true,
            safety_level VARCHAR NOT NULL DEFAULT 'caution',
            requires_approval BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    op.execute("CREATE INDEX ix_soap_op_connector ON soap_operation_descriptor (connector_id)")
    op.execute(
        "CREATE INDEX ix_soap_op_connector_service "
        "ON soap_operation_descriptor (connector_id, service_name)"
    )
    op.execute(
        "CREATE INDEX ix_soap_op_connector_operation "
        "ON soap_operation_descriptor (connector_id, operation_name)"
    )
    op.execute("CREATE INDEX ix_soap_op_tenant ON soap_operation_descriptor (tenant_id)")

    op.execute("""
        CREATE TABLE soap_type_descriptor (
            id UUID PRIMARY KEY,
            connector_id UUID NOT NULL REFERENCES connector(id) ON DELETE CASCADE,
            tenant_id VARCHAR NOT NULL,
            type_name VARCHAR NOT NULL,
            namespace VARCHAR,
            base_type VARCHAR,
            properties JSONB NOT NULL DEFAULT '[]',
            description TEXT,
            search_content TEXT,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    op.execute("CREATE INDEX ix_soap_type_connector ON soap_type_descriptor (connector_id)")
    op.execute(
        "CREATE INDEX ix_soap_type_connector_name ON soap_type_descriptor (connector_id, type_name)"
    )
    op.execute("CREATE INDEX ix_soap_type_tenant ON soap_type_descriptor (tenant_id)")

    op.execute("""
        CREATE TABLE connector_operation (
            id UUID PRIMARY KEY,
            connector_id UUID NOT NULL REFERENCES connector(id) ON DELETE CASCADE,
            tenant_id VARCHAR NOT NULL,
            operation_id VARCHAR NOT NULL,
            name VARCHAR NOT NULL,
            description TEXT,
            category VARCHAR,
            parameters JSONB NOT NULL DEFAULT '[]',
            example VARCHAR,
            search_content TEXT,
            is_enabled BOOLEAN NOT NULL DEFAULT true,
            safety_level VARCHAR NOT NULL DEFAULT 'safe',
            requires_approval BOOLEAN NOT NULL DEFAULT false,
            response_entity_type VARCHAR,
            response_identifier_field VARCHAR,
            response_display_name_field VARCHAR,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            source VARCHAR(20) NOT NULL DEFAULT 'type',
            type_operation_id UUID,
            is_enabled_override BOOLEAN
        )
    """)
    op.execute("CREATE INDEX ix_conn_op_connector ON connector_operation (connector_id)")
    op.execute(
        "CREATE INDEX ix_conn_op_connector_operation "
        "ON connector_operation (connector_id, operation_id)"
    )
    op.execute("CREATE INDEX ix_conn_op_tenant ON connector_operation (tenant_id)")
    op.execute("CREATE INDEX ix_conn_op_category ON connector_operation (connector_id, category)")

    op.execute("""
        CREATE TABLE connector_type (
            id UUID PRIMARY KEY,
            connector_id UUID NOT NULL REFERENCES connector(id) ON DELETE CASCADE,
            tenant_id VARCHAR NOT NULL,
            type_name VARCHAR NOT NULL,
            description TEXT,
            category VARCHAR,
            properties JSONB NOT NULL DEFAULT '[]',
            search_content TEXT,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    op.execute("CREATE INDEX ix_conn_type_connector ON connector_type (connector_id)")
    op.execute(
        "CREATE INDEX ix_conn_type_connector_name ON connector_type (connector_id, type_name)"
    )
    op.execute("CREATE INDEX ix_conn_type_tenant ON connector_type (tenant_id)")

    op.execute("""
        CREATE TABLE email_delivery_log (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            connector_id UUID NOT NULL REFERENCES connector(id) ON DELETE CASCADE,
            tenant_id VARCHAR NOT NULL,
            from_email VARCHAR NOT NULL,
            to_emails JSONB NOT NULL,
            subject VARCHAR(500) NOT NULL,
            provider_type VARCHAR(20) NOT NULL,
            provider_message_id VARCHAR,
            status VARCHAR(20) NOT NULL,
            error_message TEXT,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    op.execute("CREATE INDEX ix_email_log_connector ON email_delivery_log (connector_id)")
    op.execute("CREATE INDEX ix_email_log_tenant ON email_delivery_log (tenant_id)")
    op.execute("CREATE INDEX ix_email_log_created ON email_delivery_log (created_at)")

    # webhook_registration / webhook_event tables are created here under their
    # original names; later migrations rename them to event_registration /
    # event_history (see 0004_webhook_to_event).
    op.execute("""
        CREATE TABLE webhook_registration (
            id UUID PRIMARY KEY,
            connector_id UUID NOT NULL REFERENCES connector(id) ON DELETE CASCADE,
            tenant_id VARCHAR NOT NULL,
            name VARCHAR(255) NOT NULL,
            description TEXT,
            encrypted_secret TEXT NOT NULL,
            prompt_template TEXT NOT NULL,
            rate_limit_per_hour INTEGER NOT NULL DEFAULT 10,
            is_active BOOLEAN NOT NULL DEFAULT true,
            total_events_received INTEGER NOT NULL DEFAULT 0,
            total_events_processed INTEGER NOT NULL DEFAULT 0,
            total_events_deduplicated INTEGER NOT NULL DEFAULT 0,
            last_event_at TIMESTAMP WITH TIME ZONE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            require_signature BOOLEAN NOT NULL DEFAULT true,
            created_by_user_id VARCHAR,
            allowed_connector_ids JSONB,
            delegate_credentials BOOLEAN NOT NULL DEFAULT false,
            delegation_active BOOLEAN NOT NULL DEFAULT true,
            notification_targets JSONB
        )
    """)
    op.execute("CREATE INDEX ix_webhook_reg_connector ON webhook_registration (connector_id)")
    op.execute("CREATE INDEX ix_webhook_reg_tenant ON webhook_registration (tenant_id)")
    op.execute(
        "CREATE INDEX ix_webhook_reg_created_by ON webhook_registration (created_by_user_id)"
    )

    op.execute("""
        CREATE TABLE webhook_event (
            id UUID PRIMARY KEY,
            webhook_id UUID NOT NULL REFERENCES webhook_registration(id) ON DELETE CASCADE,
            tenant_id VARCHAR NOT NULL,
            status VARCHAR(20) NOT NULL,
            payload_hash VARCHAR(64) NOT NULL,
            payload_size_bytes INTEGER NOT NULL,
            session_id UUID,
            error_message TEXT,
            duplicates_suppressed INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    op.execute("CREATE INDEX ix_webhook_event_webhook ON webhook_event (webhook_id)")
    op.execute("CREATE INDEX ix_webhook_event_created ON webhook_event (created_at)")

    # ------------------------------------------------------------------------
    # knowledge -- vector-searchable chunks + ingestion job tracking
    # ------------------------------------------------------------------------
    op.execute("""
        CREATE TABLE knowledge_chunk (
            id UUID PRIMARY KEY,
            tenant_id VARCHAR,
            user_id VARCHAR,
            roles JSONB NOT NULL DEFAULT '[]',
            groups JSONB NOT NULL DEFAULT '[]',
            text TEXT NOT NULL,
            tags JSONB NOT NULL DEFAULT '[]',
            source_uri TEXT,
            search_metadata JSONB DEFAULT '{}',
            expires_at TIMESTAMP WITH TIME ZONE,
            knowledge_type VARCHAR(50) NOT NULL DEFAULT 'documentation',
            priority INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            connector_id UUID REFERENCES connector(id) ON DELETE CASCADE,
            scope_type VARCHAR(20) NOT NULL DEFAULT 'instance',
            connector_type_scope VARCHAR(50),
            embedding vector(1024),
            CONSTRAINT ck_knowledge_chunk_scope_connector CHECK (
                (scope_type = 'instance' AND connector_id IS NOT NULL) OR
                (scope_type IN ('global', 'type') AND connector_id IS NULL)
            )
        )
    """)
    op.execute("CREATE INDEX ix_knowledge_chunk_tenant_id ON knowledge_chunk (tenant_id)")
    op.execute("CREATE INDEX ix_knowledge_chunk_user_id ON knowledge_chunk (user_id)")
    op.execute("CREATE INDEX idx_knowledge_expires_at ON knowledge_chunk (expires_at)")
    op.execute("CREATE INDEX idx_knowledge_type ON knowledge_chunk (knowledge_type)")
    op.execute(
        "CREATE INDEX idx_knowledge_type_expires ON knowledge_chunk (knowledge_type, expires_at)"
    )
    op.execute("CREATE INDEX ix_knowledge_chunk_connector ON knowledge_chunk (connector_id)")
    op.execute(
        "CREATE INDEX ix_knowledge_chunk_tenant_connector "
        "ON knowledge_chunk (tenant_id, connector_id)"
    )
    op.execute(
        "CREATE INDEX ix_knowledge_chunk_tenant_user ON knowledge_chunk (tenant_id, user_id)"
    )
    op.execute("CREATE INDEX ix_knowledge_chunk_scope ON knowledge_chunk (tenant_id, scope_type)")
    op.execute(
        "CREATE INDEX ix_knowledge_chunk_type_scope "
        "ON knowledge_chunk (tenant_id, connector_type_scope) "
        "WHERE scope_type = 'type'"
    )
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_knowledge_chunk_embedding_hnsw
        ON knowledge_chunk
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_knowledge_chunk_text_fts
        ON knowledge_chunk
        USING gin (to_tsvector('english', text))
    """)

    op.execute("""
        CREATE TABLE ingestion_jobs (
            id UUID PRIMARY KEY,
            job_type VARCHAR(50) NOT NULL,
            status VARCHAR(50) NOT NULL DEFAULT 'pending',
            tenant_id VARCHAR NOT NULL,
            filename VARCHAR(255),
            file_size INTEGER,
            knowledge_type VARCHAR(50),
            tags JSONB NOT NULL DEFAULT '[]',
            total_chunks INTEGER,
            chunks_processed INTEGER NOT NULL DEFAULT 0,
            chunks_created INTEGER NOT NULL DEFAULT 0,
            current_stage VARCHAR(50),
            stage_progress FLOAT DEFAULT 0.0,
            overall_progress FLOAT DEFAULT 0.0,
            status_message TEXT,
            stage_started_at TIMESTAMP WITH TIME ZONE,
            estimated_completion TIMESTAMP WITH TIME ZONE,
            error_stage VARCHAR(50),
            error_chunk_index INTEGER,
            error_details JSONB,
            error TEXT,
            retention_until TIMESTAMP WITH TIME ZONE,
            chunk_ids JSONB,
            started_at TIMESTAMP WITH TIME ZONE,
            completed_at TIMESTAMP WITH TIME ZONE,
            connector_id UUID REFERENCES connector(id) ON DELETE CASCADE
        )
    """)
    op.execute("CREATE INDEX idx_ingestion_jobs_status ON ingestion_jobs (status)")
    op.execute("CREATE INDEX idx_ingestion_jobs_tenant ON ingestion_jobs (tenant_id)")
    op.execute(
        "CREATE INDEX idx_ingestion_jobs_tenant_status ON ingestion_jobs (tenant_id, status)"
    )
    op.execute("CREATE INDEX idx_ingestion_jobs_retention ON ingestion_jobs (retention_until)")

    # ------------------------------------------------------------------------
    # memory -- connector-scoped operator memories
    # ------------------------------------------------------------------------
    op.execute("""
        CREATE TABLE connector_memory (
            id UUID PRIMARY KEY,
            tenant_id VARCHAR NOT NULL,
            connector_id UUID NOT NULL REFERENCES connector(id) ON DELETE CASCADE,
            title VARCHAR(500) NOT NULL,
            body TEXT NOT NULL,
            memory_type VARCHAR(50) NOT NULL,
            tags JSONB NOT NULL DEFAULT '[]',
            confidence_level VARCHAR(50) NOT NULL DEFAULT 'auto_extracted',
            source_type VARCHAR(50) NOT NULL DEFAULT 'extraction',
            created_by VARCHAR,
            provenance_trail JSONB NOT NULL DEFAULT '[]',
            occurrence_count INTEGER NOT NULL DEFAULT 1,
            last_accessed TIMESTAMP WITH TIME ZONE,
            last_seen TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            embedding vector(1024)
        )
    """)
    op.execute("CREATE INDEX ix_connector_memory_tenant_id ON connector_memory (tenant_id)")
    op.execute("CREATE INDEX ix_connector_memory_connector_id ON connector_memory (connector_id)")
    op.execute("CREATE INDEX ix_connector_memory_memory_type ON connector_memory (memory_type)")
    op.execute("CREATE INDEX ix_connector_memory_confidence ON connector_memory (confidence_level)")
    op.execute(
        "CREATE INDEX ix_connector_memory_tenant_connector "
        "ON connector_memory (tenant_id, connector_id)"
    )
    op.execute(
        "CREATE INDEX ix_connector_memory_connector_type "
        "ON connector_memory (connector_id, memory_type)"
    )
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_connector_memory_embedding_hnsw
        ON connector_memory
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """)

    # ------------------------------------------------------------------------
    # agents -- chat sessions, messages, recipes, approvals, transcripts
    # ------------------------------------------------------------------------
    op.execute("""
        CREATE TABLE chat_session (
            id UUID PRIMARY KEY,
            tenant_id VARCHAR NOT NULL,
            user_id VARCHAR NOT NULL,
            title VARCHAR,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            visibility VARCHAR(20) NOT NULL DEFAULT 'private',
            created_by_name VARCHAR(255),
            trigger_source VARCHAR(100),
            session_mode VARCHAR(10) NOT NULL DEFAULT 'agent'
        )
    """)
    op.execute("CREATE INDEX ix_chat_session_tenant_id ON chat_session (tenant_id)")
    op.execute("CREATE INDEX ix_chat_session_user_id ON chat_session (user_id)")
    op.execute(
        "CREATE INDEX ix_chat_session_tenant_visibility "
        "ON chat_session (tenant_id, visibility) "
        "WHERE visibility != 'private'"
    )

    op.execute("""
        CREATE TABLE chat_message (
            id UUID PRIMARY KEY,
            session_id UUID NOT NULL REFERENCES chat_session(id) ON DELETE CASCADE,
            role VARCHAR NOT NULL,
            content TEXT NOT NULL,
            message_data JSON,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            sender_id VARCHAR(255),
            sender_name VARCHAR(255)
        )
    """)
    op.execute("CREATE INDEX ix_chat_message_session_id ON chat_message (session_id)")

    op.execute("""
        CREATE TABLE recipe (
            id UUID PRIMARY KEY,
            tenant_id VARCHAR NOT NULL,
            name VARCHAR NOT NULL,
            description TEXT,
            tags JSONB NOT NULL DEFAULT '[]',
            connector_id UUID NOT NULL,
            endpoint_id UUID,
            original_question TEXT NOT NULL,
            parameters JSONB NOT NULL DEFAULT '[]',
            query_template JSONB NOT NULL,
            interpretation_prompt TEXT,
            execution_count INTEGER NOT NULL DEFAULT 0,
            last_executed_at TIMESTAMP WITH TIME ZONE,
            is_public BOOLEAN NOT NULL DEFAULT false,
            created_by VARCHAR,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    op.execute("CREATE INDEX ix_recipe_tenant_id ON recipe (tenant_id)")
    op.execute("CREATE INDEX ix_recipe_connector_id ON recipe (connector_id)")
    op.execute("CREATE INDEX ix_recipe_endpoint_id ON recipe (endpoint_id)")

    op.execute("""
        CREATE TABLE recipe_execution (
            id UUID PRIMARY KEY,
            recipe_id UUID NOT NULL REFERENCES recipe(id) ON DELETE CASCADE,
            tenant_id VARCHAR NOT NULL,
            parameter_values JSONB NOT NULL DEFAULT '{}',
            status VARCHAR NOT NULL DEFAULT 'pending',
            error_message TEXT,
            result_count INTEGER,
            result_summary TEXT,
            aggregates JSONB NOT NULL DEFAULT '{}',
            started_at TIMESTAMP WITH TIME ZONE,
            completed_at TIMESTAMP WITH TIME ZONE,
            duration_ms INTEGER,
            triggered_by VARCHAR
        )
    """)
    op.execute("CREATE INDEX ix_recipe_execution_recipe_id ON recipe_execution (recipe_id)")
    op.execute("CREATE INDEX ix_recipe_execution_tenant_id ON recipe_execution (tenant_id)")

    op.execute("""
        CREATE TABLE tenant_agent_config (
            id UUID PRIMARY KEY,
            tenant_id VARCHAR NOT NULL UNIQUE,
            display_name VARCHAR(255),
            is_active BOOLEAN NOT NULL DEFAULT true,
            subscription_tier VARCHAR(50) NOT NULL DEFAULT 'free',
            email_domains JSONB DEFAULT '[]',
            max_connectors INTEGER,
            max_knowledge_chunks INTEGER,
            max_workflows_per_day INTEGER,
            installation_context TEXT,
            model_override VARCHAR(100),
            temperature_override JSONB,
            features JSONB NOT NULL DEFAULT '{}',
            updated_by VARCHAR,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    op.execute("CREATE INDEX ix_tenant_agent_config_tenant_id ON tenant_agent_config (tenant_id)")
    op.execute("CREATE INDEX ix_tenant_agent_config_is_active ON tenant_agent_config (is_active)")
    op.execute("""
        CREATE INDEX ix_tenant_agent_config_email_domains
        ON tenant_agent_config
        USING GIN (email_domains jsonb_path_ops)
    """)

    op.execute("""
        CREATE TABLE tenant_agent_config_audit (
            id UUID PRIMARY KEY,
            tenant_id VARCHAR NOT NULL,
            field_changed VARCHAR(100) NOT NULL,
            old_value TEXT,
            new_value TEXT,
            changed_by VARCHAR NOT NULL,
            changed_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    op.execute(
        "CREATE INDEX ix_tenant_agent_config_audit_tenant_id "
        "ON tenant_agent_config_audit (tenant_id)"
    )

    op.execute("""
        CREATE TABLE approval_request (
            id UUID PRIMARY KEY,
            session_id UUID NOT NULL REFERENCES chat_session(id) ON DELETE CASCADE,
            tenant_id VARCHAR NOT NULL,
            user_id VARCHAR NOT NULL,
            tool_name VARCHAR(100) NOT NULL,
            tool_args JSONB NOT NULL,
            tool_args_hash VARCHAR(64) NOT NULL,
            danger_level VARCHAR(20) NOT NULL,
            http_method VARCHAR(10),
            endpoint_path VARCHAR(500),
            description TEXT,
            impact_message TEXT,
            user_message TEXT NOT NULL,
            conversation_history JSONB,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            decided_by VARCHAR,
            decided_at TIMESTAMP WITH TIME ZONE,
            decision_reason TEXT,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP WITH TIME ZONE
        )
    """)
    op.execute("CREATE INDEX ix_approval_request_session_id ON approval_request (session_id)")
    op.execute("CREATE INDEX ix_approval_request_tenant_id ON approval_request (tenant_id)")
    op.execute("CREATE INDEX ix_approval_request_user_id ON approval_request (user_id)")

    op.execute("""
        CREATE TABLE approval_audit (
            id UUID PRIMARY KEY,
            approval_request_id UUID REFERENCES approval_request(id) ON DELETE SET NULL,
            session_id UUID NOT NULL,
            tenant_id VARCHAR NOT NULL,
            action VARCHAR(50) NOT NULL,
            actor_id VARCHAR,
            tool_name VARCHAR(100) NOT NULL,
            tool_args JSONB,
            danger_level VARCHAR(20),
            http_method VARCHAR(10),
            endpoint_path VARCHAR(500),
            ip_address VARCHAR(45),
            user_agent TEXT,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            outcome_status VARCHAR(20),
            outcome_summary TEXT
        )
    """)
    op.execute("CREATE INDEX ix_approval_audit_session_id ON approval_audit (session_id)")
    op.execute("CREATE INDEX ix_approval_audit_tenant_id ON approval_audit (tenant_id)")
    op.execute("CREATE INDEX ix_approval_audit_created_at ON approval_audit (created_at)")

    op.execute("""
        CREATE TABLE session_transcripts (
            id UUID PRIMARY KEY,
            session_id UUID NOT NULL REFERENCES chat_session(id) ON DELETE CASCADE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP WITH TIME ZONE,
            total_llm_calls INTEGER NOT NULL DEFAULT 0,
            total_sql_queries INTEGER NOT NULL DEFAULT 0,
            total_operation_calls INTEGER NOT NULL DEFAULT 0,
            total_tool_calls INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            total_cost_usd FLOAT,
            total_duration_ms FLOAT NOT NULL DEFAULT 0,
            agent_type VARCHAR(50),
            connector_ids UUID[],
            user_query TEXT,
            status VARCHAR(20) NOT NULL DEFAULT 'running',
            deleted_at TIMESTAMP WITH TIME ZONE
        )
    """)
    op.execute("CREATE INDEX ix_session_transcripts_session_id ON session_transcripts (session_id)")
    op.execute("CREATE INDEX ix_session_transcripts_deleted_at ON session_transcripts (deleted_at)")
    op.execute(
        "CREATE INDEX ix_session_transcripts_completed_at ON session_transcripts (completed_at)"
    )

    op.execute("""
        CREATE TABLE transcript_events (
            id UUID PRIMARY KEY,
            transcript_id UUID NOT NULL REFERENCES session_transcripts(id) ON DELETE CASCADE,
            session_id UUID NOT NULL,
            timestamp TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            type VARCHAR(50) NOT NULL,
            summary TEXT NOT NULL,
            details JSONB NOT NULL DEFAULT '{}',
            parent_event_id UUID,
            step_number INTEGER,
            node_name VARCHAR(100),
            agent_name VARCHAR(50),
            duration_ms FLOAT
        )
    """)
    op.execute(
        "CREATE INDEX ix_transcript_events_transcript_id ON transcript_events (transcript_id)"
    )
    op.execute("CREATE INDEX ix_transcript_events_session_id ON transcript_events (session_id)")
    op.execute("CREATE INDEX ix_transcript_events_type ON transcript_events (type)")
    op.execute(
        "CREATE INDEX ix_transcript_events_session_timestamp "
        "ON transcript_events (session_id, timestamp)"
    )
    op.execute(
        "CREATE INDEX ix_transcript_events_session_type ON transcript_events (session_id, type)"
    )
    op.execute("""
        CREATE INDEX ix_transcript_events_details_gin
        ON transcript_events
        USING gin (details jsonb_path_ops)
    """)

    # ------------------------------------------------------------------------
    # ingestion -- webhook event templates (used by the ingestion pipeline)
    # ------------------------------------------------------------------------
    op.execute("""
        CREATE TABLE event_templates (
            id UUID PRIMARY KEY,
            connector_id VARCHAR(255) NOT NULL,
            event_type VARCHAR(255) NOT NULL,
            text_template TEXT NOT NULL,
            tag_rules JSON NOT NULL DEFAULT '[]',
            issue_detection_rule TEXT,
            tenant_id VARCHAR(255) NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    op.execute(
        "CREATE UNIQUE INDEX idx_event_templates_connector_event "
        "ON event_templates (connector_id, event_type)"
    )
    op.execute("CREATE INDEX idx_event_templates_tenant ON event_templates (tenant_id)")
    op.execute("CREATE INDEX idx_event_templates_connector ON event_templates (connector_id)")

    # ------------------------------------------------------------------------
    # scheduled_tasks -- cron-driven task definitions and run history
    # ------------------------------------------------------------------------
    op.execute("""
        CREATE TABLE scheduled_task (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id VARCHAR NOT NULL,
            name VARCHAR(255) NOT NULL,
            description TEXT,
            cron_expression VARCHAR(100) NOT NULL,
            timezone VARCHAR(50) NOT NULL,
            prompt TEXT NOT NULL,
            is_enabled BOOLEAN NOT NULL DEFAULT true,
            next_run_at TIMESTAMP WITH TIME ZONE,
            total_runs INTEGER NOT NULL DEFAULT 0,
            last_run_at TIMESTAMP WITH TIME ZONE,
            last_run_status VARCHAR(20),
            created_by_user_id VARCHAR(255),
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            allowed_connector_ids JSONB,
            delegate_credentials BOOLEAN NOT NULL DEFAULT false,
            delegation_active BOOLEAN NOT NULL DEFAULT true,
            notification_targets JSONB
        )
    """)
    op.execute("CREATE INDEX ix_scheduled_task_tenant ON scheduled_task (tenant_id)")
    op.execute("CREATE INDEX ix_scheduled_task_enabled ON scheduled_task (is_enabled)")
    op.execute("CREATE INDEX ix_scheduled_task_created_by ON scheduled_task (created_by_user_id)")

    op.execute("""
        CREATE TABLE scheduled_task_run (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            task_id UUID NOT NULL REFERENCES scheduled_task(id) ON DELETE CASCADE,
            tenant_id VARCHAR NOT NULL,
            status VARCHAR(20) NOT NULL,
            session_id UUID,
            error_message TEXT,
            prompt_snapshot TEXT NOT NULL,
            started_at TIMESTAMP WITH TIME ZONE NOT NULL,
            completed_at TIMESTAMP WITH TIME ZONE,
            duration_seconds INTEGER
        )
    """)
    op.execute("CREATE INDEX ix_sched_run_task ON scheduled_task_run (task_id)")
    op.execute("CREATE INDEX ix_sched_run_tenant ON scheduled_task_run (tenant_id)")
    op.execute("CREATE INDEX ix_sched_run_started ON scheduled_task_run (started_at)")

    # ------------------------------------------------------------------------
    # orchestrator_skills -- cross-system reasoning skills with LLM summaries
    # ------------------------------------------------------------------------
    op.execute("""
        CREATE TABLE orchestrator_skill (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id VARCHAR NOT NULL,
            name VARCHAR(255) NOT NULL,
            description TEXT,
            content TEXT NOT NULL,
            summary TEXT NOT NULL,
            is_active BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            is_customized BOOLEAN NOT NULL DEFAULT false,
            skill_type VARCHAR(50) NOT NULL DEFAULT 'orchestrator',
            connector_type VARCHAR(50)
        )
    """)
    op.execute("CREATE INDEX ix_orch_skill_tenant ON orchestrator_skill (tenant_id)")
    op.execute(
        "ALTER TABLE orchestrator_skill "
        "ADD CONSTRAINT uq_orch_skill_tenant_name_type "
        "UNIQUE (tenant_id, name, skill_type)"
    )
    op.execute(
        "CREATE INDEX ix_orch_skill_connector_type "
        "ON orchestrator_skill (tenant_id, skill_type, connector_type)"
    )

    # ------------------------------------------------------------------------
    # audit -- audit event log
    # ------------------------------------------------------------------------
    op.execute("""
        CREATE TABLE audit_event (
            id UUID PRIMARY KEY NOT NULL,
            tenant_id VARCHAR NOT NULL,
            user_id VARCHAR NOT NULL,
            user_email VARCHAR,
            event_type VARCHAR(50) NOT NULL,
            action VARCHAR(50) NOT NULL,
            resource_type VARCHAR(50) NOT NULL,
            resource_id VARCHAR,
            resource_name VARCHAR,
            details JSONB,
            result VARCHAR(20) NOT NULL DEFAULT 'success',
            ip_address VARCHAR(45),
            user_agent VARCHAR,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    op.execute("CREATE INDEX ix_audit_event_tenant_created ON audit_event (tenant_id, created_at)")
    op.execute("CREATE INDEX ix_audit_event_user_created ON audit_event (user_id, created_at)")
    op.execute("CREATE INDEX ix_audit_event_type ON audit_event (event_type)")


def downgrade() -> None:
    """Drop the full MEHO schema in reverse FK order."""
    op.execute("DROP TABLE IF EXISTS audit_event CASCADE")
    op.execute("DROP TABLE IF EXISTS orchestrator_skill CASCADE")
    op.execute("DROP TABLE IF EXISTS scheduled_task_run CASCADE")
    op.execute("DROP TABLE IF EXISTS scheduled_task CASCADE")
    op.execute("DROP TABLE IF EXISTS event_templates CASCADE")
    op.execute("DROP TABLE IF EXISTS transcript_events CASCADE")
    op.execute("DROP TABLE IF EXISTS session_transcripts CASCADE")
    op.execute("DROP TABLE IF EXISTS approval_audit CASCADE")
    op.execute("DROP TABLE IF EXISTS approval_request CASCADE")
    op.execute("DROP TABLE IF EXISTS tenant_agent_config_audit CASCADE")
    op.execute("DROP TABLE IF EXISTS tenant_agent_config CASCADE")
    op.execute("DROP TABLE IF EXISTS recipe_execution CASCADE")
    op.execute("DROP TABLE IF EXISTS recipe CASCADE")
    op.execute("DROP TABLE IF EXISTS chat_message CASCADE")
    op.execute("DROP TABLE IF EXISTS chat_session CASCADE")
    op.execute("DROP TABLE IF EXISTS connector_memory CASCADE")
    op.execute("DROP TABLE IF EXISTS ingestion_jobs CASCADE")
    op.execute("DROP TABLE IF EXISTS knowledge_chunk CASCADE")
    op.execute("DROP TABLE IF EXISTS webhook_event CASCADE")
    op.execute("DROP TABLE IF EXISTS webhook_registration CASCADE")
    op.execute("DROP TABLE IF EXISTS email_delivery_log CASCADE")
    op.execute("DROP TABLE IF EXISTS connector_type CASCADE")
    op.execute("DROP TABLE IF EXISTS connector_operation CASCADE")
    op.execute("DROP TABLE IF EXISTS soap_type_descriptor CASCADE")
    op.execute("DROP TABLE IF EXISTS soap_operation_descriptor CASCADE")
    op.execute("DROP TABLE IF EXISTS user_connector_credential CASCADE")
    op.execute("DROP TABLE IF EXISTS endpoint_descriptor CASCADE")
    op.execute("DROP TABLE IF EXISTS openapi_spec CASCADE")
    op.execute("DROP TABLE IF EXISTS connector CASCADE")
    op.execute("DROP TABLE IF EXISTS topology_same_as_suggestion CASCADE")
    op.execute("DROP TABLE IF EXISTS topology_same_as CASCADE")
    op.execute("DROP TABLE IF EXISTS topology_relationships CASCADE")
    op.execute("DROP TABLE IF EXISTS topology_embeddings CASCADE")
    op.execute("DROP TABLE IF EXISTS topology_entities CASCADE")
