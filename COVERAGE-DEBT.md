# Coverage Debt Tracker

Deleted Phase 84 skipped tests (2026-04-01). These tested APIs that no longer exist.
Rewriting requires building new tests against current APIs.

## Priority 1: Auth & Security (4 files, ~80 functions)
- test_auth_enforcement.py -- route-level auth middleware
- test_auth_tenant_override.py -- tenant context override
- test_permissions.py -- permission checks
- test_agent_dependencies_security.py -- agent dependency security

## Priority 2: Knowledge & Ingestion (5 files, ~90 functions)
- test_knowledge_job_repository.py -- knowledge job CRUD
- test_knowledge_repository_crud.py -- knowledge document CRUD
- test_knowledge_store.py -- knowledge store operations
- test_ingestion_repository_crud.py -- ingestion repository CRUD
- test_openapi_knowledge_ingestion.py -- OpenAPI spec ingestion

## Priority 3: Agent & Orchestration (5 files, ~85 functions)
- test_agent_dependencies.py -- agent dependency injection
- test_meho_agent_dependencies.py -- MEHO-specific dependencies
- test_conversation_context.py -- conversation context management
- test_response_summarization.py -- response summarization
- test_old_agent_detailed_events.py -- agent detailed events

## Priority 4: Connectors (4 files, ~70 functions)
- test_vmware_connector.py -- VMware connector operations
- test_vmware_sync.py -- VMware sync logic
- test_proxmox_connector.py -- Proxmox connector operations
- test_connector_repository_update.py -- connector CRUD updates

## Priority 5: Infrastructure & Config (7 files, ~100 functions)
- test_core_config.py -- core configuration
- test_core_config_provider.py -- config provider
- test_core_logging.py -- logging setup
- test_otel_logging.py -- OpenTelemetry logging
- test_meho_api_config.py -- API configuration
- test_unified_cache.py -- caching infrastructure
- test_state_store.py -- state store operations

## Priority 6: Events & Realtime (6 files, ~90 functions)
- test_sse_broadcaster.py -- SSE broadcasting
- test_war_room_broadcast.py -- war room broadcasting
- test_detailed_events.py -- detailed events
- test_event_ghost_session_fix.py -- ghost session fix
- test_user_created_events.py -- user creation events
- test_tenant_discovery.py -- tenant discovery

## Priority 7: Misc (8 files, ~141 functions)
- test_topology_schema.py -- topology schema validation
- test_connector_topology_registration.py -- topology registration
- test_hybrid_search.py -- hybrid search
- test_call_operation_caching.py -- operation call caching
- test_sql_data_query.py -- SQL data query adapter
- test_spec_parser.py -- spec parser
- test_optional_body_parameters.py -- optional body params
- test_generic_processor.py -- generic event processor
