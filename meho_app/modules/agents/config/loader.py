# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Configuration loading utilities for MEHO agents.

This module provides functions for loading YAML configs and auto-discovering
tools and nodes from folder structures.

Example:
    >>> config = load_yaml_config("path/to/config.yaml")
    >>> tools = load_tools_from_folder(Path("path/to/tools"))
    >>> nodes = load_nodes_from_folder(Path("path/to/nodes"))
"""

from __future__ import annotations

import importlib.util
import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

import yaml

from meho_app.core.otel import get_logger
from meho_app.modules.agents.config.models import ModelConfig

logger = get_logger(__name__)

# Type variables
T = TypeVar("T")


def _get_default_model_name() -> str:
    """Get the default model name from environment config.

    Returns the LLM_MODEL env var value (via core/config.py Pydantic Settings),
    ensuring .env is the single source of truth for model names.
    """
    from meho_app.core.config import get_config

    return get_config().llm_model


@dataclass
class AgentConfig:
    """Agent configuration loaded from YAML.

    Attributes:
        name: Agent name/identifier.
        description: Human-readable description.
        model: Model configuration.
        system_prompt: System prompt content (resolved from file if needed).
        max_steps: Maximum ReAct steps.
        tools: Tool configuration overrides.
        raw: Raw YAML data for additional fields.
    """

    name: str
    description: str = ""
    model: ModelConfig = field(default_factory=lambda: ModelConfig(name=_get_default_model_name()))
    system_prompt: str = ""
    max_steps: int = 100
    tools: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


def load_yaml_config(path: str | Path) -> AgentConfig:
    """Load and validate a YAML configuration file.

    Args:
        path: Path to the YAML configuration file.

    Returns:
        Parsed AgentConfig object.

    Raises:
        FileNotFoundError: If the config file doesn't exist.
        yaml.YAMLError: If the YAML is invalid.
        ValueError: If required fields are missing.
    """
    path = Path(path)

    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path) as f:
        data = yaml.safe_load(f)

    if not data:
        data = {}

    # Validate required fields
    if "name" not in data:
        # Use filename without extension as default name
        data["name"] = path.stem

    # Parse model config - resolve name from env if YAML omits it
    default_model_name = _get_default_model_name()
    model_data = data.get("model", {"name": default_model_name})
    if isinstance(model_data, dict) and "name" not in model_data:
        model_data["name"] = default_model_name
    model = ModelConfig.from_dict(model_data)

    # Resolve system prompt from file if specified
    system_prompt = data.get("system_prompt", "")
    system_prompt_file = data.get("system_prompt_file")
    if system_prompt_file:
        system_prompt = resolve_file_reference(system_prompt_file, path.parent)

    return AgentConfig(
        name=data.get("name", path.stem),
        description=data.get("description", ""),
        model=model,
        system_prompt=system_prompt,
        max_steps=data.get("max_steps", 100),
        tools=data.get("tools", {}),
        raw=data,
    )


def resolve_file_reference(file_path: str, base_folder: Path) -> str:
    """Resolve a file reference relative to a base folder.

    Args:
        file_path: Relative path to resolve.
        base_folder: Base folder for resolution.

    Returns:
        Contents of the resolved file.

    Raises:
        FileNotFoundError: If the referenced file doesn't exist.
    """
    resolved_path = base_folder / file_path

    if not resolved_path.exists():
        raise FileNotFoundError(f"Referenced file not found: {resolved_path}")

    with open(resolved_path) as f:
        return f.read()


def load_tools_from_folder(folder: Path) -> dict[str, type]:
    """Auto-discover and load tool classes from a folder.

    Scans all Python files in the folder for classes that have a TOOL_NAME
    class attribute (indicating they're BaseTool subclasses).

    Args:
        folder: Path to the tools folder.

    Returns:
        Dictionary mapping tool names to tool classes.

    Example:
        >>> tools = load_tools_from_folder(Path("react_agent/tools"))
        >>> tools
        {'list_connectors': ListConnectorsTool, 'search_operations': SearchOperationsTool}
    """
    if not folder.exists():
        logger.debug(f"Tools folder does not exist: {folder}")
        return {}

    tools: dict[str, type] = {}

    for py_file in folder.glob("*.py"):
        if py_file.name.startswith("_"):
            continue

        try:
            # Load the module
            module = _load_module_from_file(py_file)

            # Find all classes with TOOL_NAME attribute
            for _name, obj in inspect.getmembers(module, inspect.isclass):
                if hasattr(obj, "TOOL_NAME") and obj.__module__ == module.__name__:
                    tool_name = obj.TOOL_NAME
                    tools[tool_name] = obj
                    logger.debug(f"Discovered tool: {tool_name} from {py_file.name}")

        except Exception as e:
            logger.warning(f"Failed to load tools from {py_file}: {e}")

    return tools


def load_nodes_from_folder(folder: Path) -> dict[str, type]:
    """Auto-discover and load node classes from a folder.

    Scans all Python files in the folder for classes that have a NODE_NAME
    class attribute (indicating they're BaseNode subclasses).

    Args:
        folder: Path to the nodes folder.

    Returns:
        Dictionary mapping node names to node classes.

    Example:
        >>> nodes = load_nodes_from_folder(Path("react_agent/nodes"))
        >>> nodes
        {'reason': ReasonNode, 'tool_dispatch': ToolDispatchNode}
    """
    if not folder.exists():
        logger.debug(f"Nodes folder does not exist: {folder}")
        return {}

    nodes: dict[str, type] = {}

    for py_file in folder.glob("*.py"):
        if py_file.name.startswith("_"):
            continue

        try:
            # Load the module
            module = _load_module_from_file(py_file)

            # Find all classes with NODE_NAME attribute
            for _name, obj in inspect.getmembers(module, inspect.isclass):
                if hasattr(obj, "NODE_NAME") and obj.__module__ == module.__name__:
                    node_name = obj.NODE_NAME
                    nodes[node_name] = obj
                    logger.debug(f"Discovered node: {node_name} from {py_file.name}")

        except Exception as e:
            logger.debug(f"Skipped {py_file.name} during node discovery: {e}")

    return nodes


def _load_module_from_file(file_path: Path) -> Any:
    """Load a Python module from a file path.

    Args:
        file_path: Path to the Python file.

    Returns:
        Loaded module object.

    Raises:
        ImportError: If the module cannot be loaded.
    """
    module_name = file_path.stem
    spec = importlib.util.spec_from_file_location(module_name, file_path)

    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module spec from {file_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    return module


def load_prompt_file(file_path: str | Path) -> str:
    """Load a prompt from a file.

    Args:
        file_path: Path to the prompt file (markdown or text).

    Returns:
        Contents of the prompt file.

    Raises:
        FileNotFoundError: If the file doesn't exist.
    """
    path = Path(file_path)

    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")

    with open(path) as f:
        return f.read()


def render_prompt_template(
    template: str,
    variables: dict[str, Any],
) -> str:
    """Render a prompt template with variables.

    Uses simple string formatting with {variable} placeholders.
    For complex templating, consider using Jinja2.

    Args:
        template: The prompt template string.
        variables: Dictionary of variables to substitute.

    Returns:
        Rendered prompt string.

    Example:
        >>> template = "You are {role}. Your goal is {goal}."
        >>> render_prompt_template(template, {"role": "MEHO", "goal": "help users"})
        'You are MEHO. Your goal is help users.'
    """
    try:
        return template.format(**variables)
    except KeyError as e:
        logger.warning(f"Missing template variable: {e}")
        # Return template with missing variables as-is
        return template
