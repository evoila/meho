# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Configuration utilities for MEHO agents.

Exports:
    ModelConfig: Model configuration with auto-detection
    InstructParams: Parameters for instruct/chat models
    ReasoningParams: Parameters for reasoning models (o1, o3)
    detect_model_type: Auto-detect model type from name
    AgentConfig: Agent configuration loaded from YAML
    load_yaml_config: Load and validate YAML configuration files
    load_tools_from_folder: Auto-discover tool classes
    load_nodes_from_folder: Auto-discover node classes
"""

from __future__ import annotations

from meho_app.modules.agents.config.loader import (
    AgentConfig,
    load_nodes_from_folder,
    load_prompt_file,
    load_tools_from_folder,
    load_yaml_config,
    render_prompt_template,
    resolve_file_reference,
)
from meho_app.modules.agents.config.models import (
    InstructParams,
    ModelConfig,
    ModelType,
    ReasoningParams,
    detect_model_type,
)

__all__ = [
    "AgentConfig",
    "InstructParams",
    "ModelConfig",
    "ModelType",
    "ReasoningParams",
    "detect_model_type",
    "load_nodes_from_folder",
    "load_prompt_file",
    "load_tools_from_folder",
    "load_yaml_config",
    "render_prompt_template",
    "resolve_file_reference",
]
