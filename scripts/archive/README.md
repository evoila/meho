# Archived Scripts

These scripts were used during development but are not part of regular workflow.
They are kept for historical reference.

## Categories

### One-Time Migrations
- `migrate_openapi_chunks.py` - Migrated OpenAPI chunks to new schema
- `migrate_operations_to_knowledge_chunk.py` - Moved operations to knowledge chunks
- `migrate_to_agent_plan.py` - Migrated to new agent plan structure

### Database Schema Setup
- `create_planstatus_enum.py` - Created PlanStatus enum
- `create_workflow_tables.py` - Created workflow tables
- `update_agent_plan_enum.py` - Updated agent plan enum values

### Search Index Setup
- `build_bm25_indexes.py` - Built initial BM25 indexes
- `rebuild_bm25_indexes.py` - Rebuilt BM25 indexes
- `re_embed_knowledge_chunks.py` - Re-generated embeddings after model upgrade

### System-Specific Tools
- `split_vmware_connector.py` - Split VMware connector operations
- `generate_vmware_operations.py` - Generated VMware operation definitions
- `validate_vmware_operations.py` - Validated VMware operations
- `extract_k8s_connector_info.py` - Extracted K8s connector information

### Prompt Engineering
- `compare_search_algorithms.py` - Compared search algorithm performance
- `evaluate_prompts.py` - Evaluated prompt variations
- `swap_prompt.sh` - Swapped prompt variants
- `test_prompt_e2e.sh` - E2E tested prompts

### Debug/Audit Tools
- `audit_operation_descriptions.py` - Audited operation descriptions
- `debug_endpoint_search.py` - Debugged endpoint search
- `diagnose_vm_endpoint_search.py` - Diagnosed VM endpoint search issues

### One-Time Utilities
- `run-cleanup.py` - One-time cleanup script
- `test_intent_classifier.py` - Tested intent classifier
- `test_vcenter_soap.py` - Tested vCenter SOAP integration

