# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Configuration management for MEHO.

Loads configuration from environment variables using Pydantic Settings.
"""

import json
from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, PrivateAttr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MODEL_CLAUDE_OPUS = "anthropic:claude-opus-4-6"
MODEL_CLAUDE_SONNET = "anthropic:claude-sonnet-4-6"

DEFAULT_CORS_ORIGINS = ["http://localhost:5173"]


class Config(BaseSettings):
    """MEHO configuration loaded from environment variables"""

    # Environment
    env: Literal["dev", "test", "prod"] = Field(default="dev")
    log_level: str = Field(default="INFO")

    # Licensing (Phase 80: Open-Core Foundation)
    license_key: str | None = Field(
        default=None,
        description="Ed25519-signed license key for enterprise edition. Omit for community.",
    )

    # Database
    database_url: str = Field(..., description="PostgreSQL connection URL")

    # Object Storage (S3-compatible)
    object_storage_endpoint: str | None = Field(
        default=None, description="MinIO/S3 endpoint (required for document upload)"
    )
    object_storage_bucket: str = Field(default="meho-knowledge", description="Bucket name")
    object_storage_access_key: str | None = Field(
        default=None, description="Access key (required for document upload)"
    )
    object_storage_secret_key: str | None = Field(
        default=None, description="Secret key (required for document upload)"
    )
    object_storage_use_ssl: bool = Field(default=False)

    # Knowledge Ingestion Safety (Phase 90.2)
    ingestion_memory_limit_mb: int = Field(
        default=8192,
        description="Memory limit in MB for document conversion subprocess (Linux only, ignored on macOS)",
    )
    ingestion_max_file_size_mb: int = Field(
        default=200,
        description="Maximum upload file size in MB. Files exceeding this are rejected before processing.",
    )
    ingestion_page_batch_size: int = Field(
        default=50,
        description="Number of PDF pages to process per Docling batch. Limits peak memory for large documents.",
    )
    ingestion_ocr_enabled: bool = Field(
        default=False,
        description="Enable OCR for scanned PDFs. Disabled by default to save ~500MB memory. "
        "Text-native PDFs extract text without OCR.",
    )

    # Ephemeral Ingestion Worker (Phase 97.1)
    ingestion_backend: Literal["local", "kubernetes", "cloudrun", "docker"] = Field(
        default="local",
        alias="meho_ingestion_backend",
        description="Ingestion worker backend: local (subprocess), kubernetes (K8s Jobs), "
        "cloudrun (Cloud Run Jobs), docker (SSH + docker run)",
    )
    ingestion_offload_threshold_pages: int = Field(
        default=50,
        alias="meho_ingestion_offload_threshold",
        description="Documents with more pages than this threshold use the configured backend "
        "instead of in-process conversion. Set to 0 to always offload.",
    )
    worker_image: str = Field(
        default="",
        alias="meho_worker_image",
        description="Docker image for the ingestion worker (K8s, CloudRun, Docker backends). "
        "Same image as the MEHO API with different CMD.",
    )

    # Kubernetes backend config
    k8s_ingestion_namespace: str = Field(
        default="default",
        alias="meho_k8s_namespace",
        description="K8s namespace for ingestion Jobs",
    )
    k8s_ingestion_server: str = Field(
        default="",
        alias="meho_k8s_server",
        description="K8s API server URL (empty = in-cluster config)",
    )
    k8s_ingestion_token: str | None = Field(
        default=None,
        alias="meho_k8s_token",
        description="K8s bearer token (None = in-cluster auth)",
    )
    k8s_ingestion_ca_cert: str | None = Field(
        default=None,
        alias="meho_k8s_ca_cert",
        description="K8s CA certificate path (None = skip TLS verify or in-cluster)",
    )
    k8s_ingestion_service_account: str | None = Field(
        default=None,
        alias="meho_k8s_service_account",
        description="K8s service account for ingestion Jobs pod spec",
    )

    # Cloud Run backend config
    cloudrun_project: str = Field(
        default="",
        alias="meho_cloudrun_project",
        description="GCP project ID for Cloud Run Jobs",
    )
    cloudrun_region: str = Field(
        default="us-central1",
        alias="meho_cloudrun_region",
        description="GCP region for Cloud Run Jobs",
    )
    cloudrun_job_name: str = Field(
        default="meho-ingestion-worker",
        alias="meho_cloudrun_job_name",
        description="Cloud Run Job name (must be pre-created in GCP)",
    )

    # Docker backend config
    docker_ingestion_host: str = Field(
        default="",
        alias="meho_docker_host",
        description="Docker host URL for remote execution (e.g., ssh://user@gpu-vm)",
    )

    # Cache
    redis_url: str = Field(..., description="Redis connection URL")

    # AI/LLM Configuration
    llm_provider: Literal["anthropic", "openai", "ollama"] = Field(
        default="anthropic",
        alias="meho_llm_provider",
        description="LLM provider: anthropic (default), openai, or ollama",
    )
    anthropic_api_key: str | None = Field(
        default=None,
        description="Anthropic API key (required for LLM features, optional for startup)",
    )
    voyage_api_key: str | None = Field(
        default=None,
        description="Voyage AI API key for embeddings (optional -- uses local TEI when unset)",
    )

    # Embedding Model
    # Using Voyage AI voyage-4-large (1024D) -- fits pgvector HNSW 2000D limit comfortably
    embedding_model: str = Field(
        default="voyage-4-large",
        description="Voyage AI embedding model for knowledge base and topology",
    )

    # LLM Models for Different Use Cases
    # Reasoning tasks use Opus 4.6, utility tasks use Sonnet 4.6
    # Default/fallback model for general agent operations
    llm_model: str = Field(
        default=MODEL_CLAUDE_OPUS, description="Default LLM model for agents (fallback)"
    )

    # Workflow Builder Agent (conversational interpretation)
    workflow_builder_model: str = Field(
        default=MODEL_CLAUDE_SONNET,
        description="Model for workflow builder conversational agent (TASK-82)",
    )

    # Transform Expression Generation
    transform_generation_model: str = Field(
        default=MODEL_CLAUDE_SONNET,
        description="Model for generating Jinja2 transform expressions",
    )

    # Streaming Chat Agent
    streaming_agent_model: str = Field(
        default=MODEL_CLAUDE_OPUS, description="Model for streaming chat responses"
    )

    # Planning Agent
    planner_model: str = Field(
        default=MODEL_CLAUDE_OPUS, description="Model for creating execution plans"
    )

    # Execution Agent
    executor_model: str = Field(
        default=MODEL_CLAUDE_OPUS, description="Model for executing plan steps"
    )

    # Connector Classification
    classifier_model: str = Field(
        default=MODEL_CLAUDE_SONNET,
        description="Model for classifying which connector/system to use",
    )

    # Result Interpretation
    interpreter_model: str = Field(
        default=MODEL_CLAUDE_OPUS,
        description="Model for interpreting search results and API responses",
    )

    # Data Extraction/Summarization
    data_extractor_model: str = Field(
        default=MODEL_CLAUDE_SONNET,
        description="Model for extracting and summarizing data from responses",
    )

    # Skill Generation (Phase 6)
    skill_generation_model: str = Field(
        default=MODEL_CLAUDE_SONNET,
        description="Model for generating connector skills from operations",
    )

    # LLM Report Generation in Workflows
    workflow_llm_report_model: str = Field(
        default=MODEL_CLAUDE_SONNET,
        description="Model for generating reports in workflow execution",
    )

    # Memory Extraction (Phase 10)
    memory_extraction_model: str = Field(
        default=MODEL_CLAUDE_SONNET,
        description="Model for extracting memories from conversation findings",
    )
    enable_memory_extraction: bool = Field(
        default=True,
        description="Enable automatic memory extraction after conversations. Set ENABLE_MEMORY_EXTRACTION=false to disable.",
    )

    # LLM Inference Settings
    llm_inference_timeout: float = Field(
        default=120.0,
        description="Default timeout in seconds for one-shot LLM inference calls (infer, infer_structured)",
    )

    # TASK-126: Endpoint Search Algorithm
    # Controls whether endpoint/operation search uses BM25-only or hybrid (BM25 + semantic)
    # - "bm25_only": Original BM25 search (faster, keyword-focused)
    # - "bm25_hybrid": Hybrid search combining BM25 + semantic embeddings (better quality)
    endpoint_search_algorithm: Literal["bm25_only", "bm25_hybrid"] = Field(
        default="bm25_only",  # Safe default - change to "bm25_hybrid" after validation
        description="Search algorithm for endpoints/operations (bm25_only or bm25_hybrid)",
    )

    # TASK-181: Orchestrator Agent Settings
    # These override the orchestrator's config.yaml settings when set via environment
    orchestrator_max_iterations: int | None = Field(
        default=None, description="Override orchestrator max iterations (default: 3)"
    )
    orchestrator_agent_timeout: float | None = Field(
        default=None, description="Override per-agent timeout in seconds (default: 30.0)"
    )
    orchestrator_total_timeout: float | None = Field(
        default=None, description="Override total iteration timeout in seconds (default: 120.0)"
    )

    # TASK-143: Topology Auto-Discovery
    # Automatically extracts entities from connector operation results
    topology_auto_discovery_enabled: bool = Field(
        default=True,
        description="Enable automatic topology entity extraction from connector operations",
    )
    topology_discovery_batch_size: int = Field(
        default=100, description="Maximum discovery messages to process per batch"
    )
    topology_discovery_interval_seconds: int = Field(
        default=5, description="Interval between background processing cycles"
    )
    topology_discovery_queue_key: str = Field(
        default="topology:discovery:queue", description="Redis key for the discovery queue"
    )

    # TASK-144 Phase 3: LLM-Assisted Verification for SAME_AS Suggestions
    # Controls how suggestions are handled based on confidence scores
    suggestion_auto_approve_threshold: float = Field(
        default=0.90, description="Auto-approve suggestions with confidence >= this value"
    )
    suggestion_llm_verify_threshold: float = Field(
        default=0.70,
        description="Trigger LLM verification for suggestions with confidence >= this (and < auto_approve)",
    )
    suggestion_llm_approve_confidence: float = Field(
        default=0.80, description="LLM must be at least this confident to auto-approve/reject"
    )

    # Rate Limiting (TASK-186)
    # Controls rate limiting for API endpoints to prevent abuse
    # Note: These settings are defined in Phase 1 (Infrastructure & Utilities) but
    # will be applied to observability endpoints in Phase 5 (Deep Observability UI).
    # The rate_limit_* decorators will use these config values via get_config().
    enable_rate_limiting: bool = Field(
        default=True,
        description="Enable rate limiting for API endpoints. Set ENABLE_RATE_LIMITING=false to disable.",
    )
    rate_limit_transcript: str = Field(
        default="60/minute", description="Rate limit for transcript endpoints"
    )
    rate_limit_search: str = Field(
        default="30/minute", description="Rate limit for search endpoints"
    )
    rate_limit_export: str = Field(
        default="5/minute", description="Rate limit for export endpoints"
    )
    rate_limit_cleanup: str = Field(
        default="1/hour", description="Rate limit for cleanup endpoints"
    )

    # Transcript Persistence (TASK-186)
    # Controls whether agent execution transcripts are persisted to the database.
    # When enabled, detailed events (LLM calls, tool calls, HTTP requests) are recorded
    # for debugging and observability. Disable in high-throughput environments if needed.
    enable_transcript_persistence: bool = Field(
        default=True,
        description="Enable transcript persistence for observability. Set ENABLE_TRANSCRIPT_PERSISTENCE=false to disable.",
    )

    # Transcript Retention (TASK-186)
    # Controls how long transcripts are kept before cleanup
    transcript_retention_days: int = Field(
        default=30, description="Days to retain transcripts before soft-delete"
    )
    transcript_grace_days: int = Field(
        default=7, description="Days after soft-delete before permanent deletion"
    )

    # Detailed Event Emission (TASK-186 Phase 4)
    # Controls whether detailed events (with full context) are emitted for deep observability.
    # When enabled, EventEmitter's *_detailed() methods will persist full LLM prompts,
    # HTTP payloads, tool inputs/outputs to transcripts. Disable if storage is a concern.
    enable_detailed_events: bool = Field(
        default=True,
        description="Enable detailed event emission for deep observability. Set ENABLE_DETAILED_EVENTS=false to disable.",
    )

    # Observability API (TASK-186 Phase 5)
    # Controls whether the observability API endpoints are available.
    # When enabled, provides endpoints for session listing, transcript retrieval,
    # event filtering, search, explanation, retention management, and export.
    enable_observability_api: bool = Field(
        default=True,
        description="Enable observability API endpoints. Set ENABLE_OBSERVABILITY_API=false to disable.",
    )

    # Approval Expiry (Phase 5: Graduated Trust Model)
    approval_expiry_minutes: int = Field(
        default=60,
        description="Minutes before a pending approval request expires. Set APPROVAL_EXPIRY_MINUTES to override.",
    )

    # Application
    api_host: str = Field(default="0.0.0.0")  # noqa: S104 -- intentional bind to all interfaces
    api_port: int = Field(default=8000)
    knowledge_service_port: int = Field(default=8001)
    openapi_service_port: int = Field(default=8002)
    agent_service_port: int = Field(default=8003)
    ingestion_service_port: int = Field(default=8004)

    # Security
    credential_encryption_key: str = Field(
        ..., description="Fernet encryption key for user credentials"
    )

    # CORS Settings (Phase 58: Security Hardening)
    # Accepts JSON array, comma-separated string, or single origin.
    # No wildcard "*" is ever returned -- explicit origins only.
    cors_origins_input: Any = Field(
        default=None,
        alias="cors_origins",
        description="Raw CORS origins from CORS_ORIGINS env var",
    )
    _cors_origins: list[str] = PrivateAttr(default_factory=list)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    @model_validator(mode="after")
    def _finalize_cors(self) -> "Config":
        self._cors_origins = self._normalize_cors(self.cors_origins_input)
        return self

    @model_validator(mode="after")
    def _validate_llm_keys(self) -> "Config":
        """Warn (not crash) when LLM keys are missing -- allows degraded startup."""
        import logging

        log = logging.getLogger("meho.config")
        if self.llm_provider == "anthropic" and not self.anthropic_api_key:
            log.warning(
                "MEHO_LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set. "
                "LLM features will be unavailable until an API key is configured."
            )
        return self

    @staticmethod
    def _normalize_cors(value: Any) -> list[str]:  # NOSONAR (cognitive complexity)
        """
        Normalize CORS origins from various input formats.

        Accepts:
          - JSON array: '["http://a","http://b"]'
          - Comma-separated: 'http://a,http://b'
          - Bracketed without quotes: '[http://a,http://b]'
          - Single origin string: 'http://a'
          - None: returns DEFAULT_CORS_ORIGINS

        Never returns ["*"] -- explicit origins only.
        """
        if value is None:
            return DEFAULT_CORS_ORIGINS.copy()

        if isinstance(value, list):
            origins = [
                o.strip() for o in value if isinstance(o, str) and o.strip() and o.strip() != "*"
            ]
            return origins or DEFAULT_CORS_ORIGINS.copy()

        if isinstance(value, str):
            raw = value.strip()
            if not raw or raw == "*":
                return DEFAULT_CORS_ORIGINS.copy()

            if raw.startswith("[") and raw.endswith("]"):
                raw = raw[1:-1]

            # If quote characters, try JSON parse
            if '"' in raw or "'" in raw:
                try:
                    parsed = json.loads(f"[{raw}]")
                    if isinstance(parsed, list):
                        return [
                            o.strip()
                            for o in parsed
                            if isinstance(o, str) and o.strip() and o.strip() != "*"
                        ] or DEFAULT_CORS_ORIGINS.copy()
                except json.JSONDecodeError:
                    pass

            items: list[str] = []
            for origin in raw.split(","):
                cleaned = origin.strip().strip("\"'")
                if cleaned and cleaned != "*":
                    items.append(cleaned)
            return items or DEFAULT_CORS_ORIGINS.copy()

        return DEFAULT_CORS_ORIGINS.copy()

    @property
    def cors_origins(self) -> list[str]:
        """Return normalized CORS origins (never wildcard)."""
        return self._cors_origins or DEFAULT_CORS_ORIGINS.copy()

    def model_post_init(self, __context: Any) -> None:
        """Validate configuration after initialization"""
        # Ensure encryption key is valid Fernet key (44 bytes base64)
        if len(self.credential_encryption_key) < 32:
            raise ValueError("credential_encryption_key must be at least 32 characters")


# Singleton instance
_config: Config | None = None


@lru_cache
def get_config() -> Config:
    """
    Get configuration singleton.

    Returns:
        Config: Application configuration

    Raises:
        ValidationError: If required environment variables are missing
    """
    global _config
    if _config is None:
        _config = Config()
    return _config


def reset_config() -> None:
    """
    Reset configuration singleton (for testing).

    This should only be used in tests to reset config between test runs.
    """
    global _config
    _config = None
    get_config.cache_clear()
