"""
Agent Configuration System for MEHO.

Provides multi-layer configuration loading:
1. Config files (lowest priority, defaults)
2. Environment variables
3. Database (tenant-specific context)
4. Runtime injection (highest priority, for testing/evals)

Session 81: TASK-77 - Externalize Prompts & Models
"""
from pathlib import Path
from pydantic import BaseModel, Field, field_validator
from typing import Optional, Dict, Any
import os
import logging
import yaml

logger = logging.getLogger(__name__)

# Default paths relative to project root
DEFAULT_CONFIG_PATH = "config/agent.yaml"
DEFAULT_BASE_PROMPT_PATH = "config/prompts/base_system_prompt.md"


class ModelConfig(BaseModel):
    """LLM Model configuration."""
    name: str = Field(description="Model identifier (e.g., 'openai:gpt-4.1-mini')")
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, gt=0)


class PromptSources(BaseModel):
    """Paths to prompt files."""
    base: str = Field(default=DEFAULT_BASE_PROMPT_PATH)
    tools: Optional[str] = None
    safety: Optional[str] = None


class DataReductionConfig(BaseModel):
    """Configuration for UnifiedExecutor data reduction."""
    auto_reduce_threshold: int = Field(default=50, description="Records threshold")
    auto_reduce_size_kb: int = Field(default=50, description="Size threshold in KB")


class AgentConfig(BaseModel):
    """
    Complete configuration for StreamingMEHOAgent.
    
    Supports multi-layer configuration:
    - Layer 1: Config file defaults (config/agent.yaml)
    - Layer 2: Environment variable overrides
    - Layer 3: Database tenant context
    - Layer 4: Runtime injection (for testing)
    """
    
    model: ModelConfig
    prompt_sources: PromptSources = Field(default_factory=PromptSources)
    tenant_context: Optional[str] = Field(
        default=None,
        description="Admin-defined installation context from database"
    )
    runtime_prompt: Optional[str] = Field(
        default=None,
        description="Additional prompt for testing/evals"
    )
    data_reduction: DataReductionConfig = Field(default_factory=DataReductionConfig)
    retries: int = Field(default=2, ge=0)
    instrument: bool = Field(default=True)
    logfire_enabled: bool = Field(default=True)
    
    @classmethod
    async def load(
        cls,
        tenant_id: Optional[str] = None,
        config_path: str = DEFAULT_CONFIG_PATH,
        runtime_overrides: Optional[Dict[str, Any]] = None,
        session_maker: Optional[Any] = None,
    ) -> "AgentConfig":
        """
        Load configuration from all layers.
        
        Args:
            tenant_id: Tenant ID for loading context from database
            config_path: Path to agent.yaml config file
            runtime_overrides: Optional dict with runtime overrides (for testing)
            session_maker: SQLAlchemy session maker (for DB access)
            
        Returns:
            AgentConfig with merged configuration from all layers
        """
        # Layer 1: Load from config file
        file_config = cls._load_config_file(config_path)
        
        agent_config = file_config.get("agent", {})
        
        # Layer 2: Environment variables override
        model_name = os.getenv(
            agent_config.get("model", {}).get("env_var", "STREAMING_AGENT_MODEL"),
            agent_config.get("model", {}).get("default", "openai:gpt-4.1-mini")
        )
        
        temperature = float(os.getenv(
            agent_config.get("temperature", {}).get("env_var", "MEHO_LLM_TEMPERATURE"),
            agent_config.get("temperature", {}).get("default", 0.7)
        ))
        
        max_tokens = int(os.getenv(
            agent_config.get("max_tokens", {}).get("env_var", "MEHO_LLM_MAX_TOKENS"),
            agent_config.get("max_tokens", {}).get("default", 4096)
        ))
        
        # Prompt sources from config
        prompts_config = agent_config.get("prompts", {})
        prompt_sources = PromptSources(
            base=prompts_config.get("base", DEFAULT_BASE_PROMPT_PATH),
            tools=prompts_config.get("tools"),
            safety=prompts_config.get("safety"),
        )
        
        # Data reduction config
        dr_config = agent_config.get("data_reduction", {})
        data_reduction = DataReductionConfig(
            auto_reduce_threshold=dr_config.get("auto_reduce_threshold", 50),
            auto_reduce_size_kb=dr_config.get("auto_reduce_size_kb", 50),
        )
        
        # Layer 3: Tenant context from database
        tenant_context = None
        if tenant_id and session_maker:
            tenant_context = await cls._load_tenant_context(tenant_id, session_maker)
        
        # Layer 4: Runtime overrides
        if runtime_overrides:
            model_name = runtime_overrides.get("model", model_name)
            temperature = runtime_overrides.get("temperature", temperature)
            max_tokens = runtime_overrides.get("max_tokens", max_tokens)
        
        runtime_prompt = runtime_overrides.get("runtime_prompt") if runtime_overrides else None
        
        logger.info(f"📋 AgentConfig loaded from external config: model={model_name}, temp={temperature}")
        
        return cls(
            model=ModelConfig(
                name=model_name,
                temperature=temperature,
                max_tokens=max_tokens,
            ),
            prompt_sources=prompt_sources,
            tenant_context=tenant_context,
            runtime_prompt=runtime_prompt,
            data_reduction=data_reduction,
            retries=agent_config.get("retries", 2),
            instrument=agent_config.get("instrument", True),
            logfire_enabled=agent_config.get("logfire_enabled", True),
        )
    
    @staticmethod
    def _load_config_file(config_path: str) -> Dict[str, Any]:
        """Load configuration from YAML file."""
        config_file = Path(config_path)
        
        if not config_file.exists():
            # Try relative to current working directory
            config_file = Path.cwd() / config_path
            
        if not config_file.exists():
            logger.warning(f"Config file not found: {config_path}, using defaults")
            return {}
        
        try:
            with open(config_file) as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            logger.error(f"Error loading config file {config_path}: {e}")
            return {}
    
    @staticmethod
    async def _load_tenant_context(tenant_id: str, session_maker: Any) -> Optional[str]:
        """
        Load tenant-specific context from database.
        
        Args:
            tenant_id: Tenant identifier
            session_maker: SQLAlchemy async session maker
            
        Returns:
            Tenant installation context or None
        """
        try:
            from meho_agent.models import TenantAgentConfig
            from sqlalchemy import select
            
            async with session_maker() as session:
                result = await session.execute(
                    select(TenantAgentConfig).where(
                        TenantAgentConfig.tenant_id == tenant_id
                    )
                )
                config = result.scalar_one_or_none()
                
                if config and config.installation_context:
                    logger.info(f"Loaded tenant context for {tenant_id}")
                    return str(config.installation_context)
                    
        except ImportError:
            logger.debug("TenantAgentConfig model not available, skipping tenant context")
        except Exception as e:
            logger.warning(f"Could not load tenant context: {e}")
        
        return None


class PromptBuilder:
    """
    Builds final system prompt from multiple sources.
    
    Composition order:
    1. Base prompt (from file)
    2. Tool descriptions (if separate file)
    3. Safety guidelines (if separate file)
    4. Tenant context (from database)
    5. Runtime additions (for testing)
    """
    
    def __init__(self, config: AgentConfig):
        self.config = config
    
    async def build(self) -> str:
        """
        Compose final system prompt from all sources.
        
        Returns:
            Complete system prompt string
        """
        parts = []
        
        # 1. Base prompt from file
        base_prompt = self._load_prompt_file(self.config.prompt_sources.base)
        logger.info(f"📄 Loaded base prompt from: {self.config.prompt_sources.base} ({len(base_prompt)} chars)")
        parts.append(base_prompt)
        
        # 2. Tool descriptions (if separate file)
        if self.config.prompt_sources.tools:
            tools_prompt = self._load_prompt_file(self.config.prompt_sources.tools)
            parts.append(f"\n\n## Available Tools\n\n{tools_prompt}")
        
        # 3. Safety guidelines (if separate file)
        if self.config.prompt_sources.safety:
            safety_prompt = self._load_prompt_file(self.config.prompt_sources.safety)
            parts.append(f"\n\n## Safety Guidelines\n\n{safety_prompt}")
        
        # 4. Tenant-specific context (from database)
        if self.config.tenant_context:
            parts.append(f"\n\n## Your Environment\n\n{self.config.tenant_context}")
        
        # 5. Runtime additions (for testing/evals)
        if self.config.runtime_prompt:
            parts.append(f"\n\n{self.config.runtime_prompt}")
        
        final_prompt = "\n".join(parts)
        logger.debug(f"Built system prompt: {len(final_prompt)} characters")
        
        return final_prompt
    
    def _load_prompt_file(self, path: str) -> str:
        """
        Load prompt from file.
        
        Args:
            path: Path to prompt file
            
        Returns:
            Prompt content
            
        Raises:
            FileNotFoundError: If prompt file not found
        """
        prompt_path = Path(path)
        
        # Try relative to cwd
        if not prompt_path.exists():
            prompt_path = Path.cwd() / path
        
        # Try relative to this file's directory
        if not prompt_path.exists():
            prompt_path = Path(__file__).parent.parent / path
        
        if not prompt_path.exists():
            raise FileNotFoundError(
                f"Prompt file not found: {path}. "
                f"Looked in: {path}, {Path.cwd() / path}"
            )
        
        return prompt_path.read_text()


# Convenience functions for backward compatibility

async def get_agent_config(
    tenant_id: Optional[str] = None,
    runtime_overrides: Optional[Dict[str, Any]] = None,
) -> AgentConfig:
    """
    Get agent configuration with all layers applied.
    
    This is the main entry point for getting agent configuration.
    
    Args:
        tenant_id: Optional tenant ID for context
        runtime_overrides: Optional overrides for testing
        
    Returns:
        AgentConfig instance
    """
    return await AgentConfig.load(
        tenant_id=tenant_id,
        runtime_overrides=runtime_overrides,
    )


async def build_system_prompt(
    tenant_id: Optional[str] = None,
    runtime_prompt: Optional[str] = None,
) -> str:
    """
    Build complete system prompt.
    
    Convenience function for getting the final composed prompt.
    
    Args:
        tenant_id: Optional tenant ID for context
        runtime_prompt: Optional additional prompt for testing
        
    Returns:
        Complete system prompt string
    """
    config = await AgentConfig.load(
        tenant_id=tenant_id,
        runtime_overrides={"runtime_prompt": runtime_prompt} if runtime_prompt else None,
    )
    builder = PromptBuilder(config)
    return await builder.build()

