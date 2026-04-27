# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for config loader functions.

These tests verify:
1. load_yaml_config loads and parses YAML correctly
2. resolve_file_reference resolves relative paths
3. load_tools_from_folder discovers tool classes
4. load_nodes_from_folder discovers node classes
"""

from __future__ import annotations

from pathlib import Path

import pytest

from meho_app.modules.agents.config import (
    AgentConfig,
    load_nodes_from_folder,
    load_prompt_file,
    load_tools_from_folder,
    load_yaml_config,
    render_prompt_template,
    resolve_file_reference,
)


class TestLoadYamlConfig:
    """Tests for load_yaml_config function."""

    def test_load_minimal_config(self, tmp_path: Path) -> None:
        """Should load config with just name."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("name: test_agent\n")

        config = load_yaml_config(config_file)

        assert config.name == "test_agent"
        assert config.description == ""
        assert config.max_steps == 100

    def test_load_full_config(self, tmp_path: Path) -> None:
        """Should load config with all fields."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
name: react_agent
description: A test agent
model:
  name: openai:gpt-4.1-mini
  temperature: 0.2
max_steps: 50
tools:
  list_connectors:
    enabled: true
  search_operations:
    enabled: true
    default_limit: 10
""")

        config = load_yaml_config(config_file)

        assert config.name == "react_agent"
        assert config.description == "A test agent"
        assert config.model.name == "openai:gpt-4.1-mini"
        assert config.model.temperature == pytest.approx(0.2)
        assert config.max_steps == 50
        assert config.tools["list_connectors"]["enabled"] is True
        assert config.tools["search_operations"]["default_limit"] == 10

    def test_load_config_with_system_prompt(self, tmp_path: Path) -> None:
        """Should load inline system prompt."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
name: test
system_prompt: |
  You are a helpful assistant.
  Always be concise.
""")

        config = load_yaml_config(config_file)

        assert "helpful assistant" in config.system_prompt

    def test_load_config_with_system_prompt_file(self, tmp_path: Path) -> None:
        """Should resolve system_prompt_file reference."""
        # Create prompt file
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        prompt_file = prompts_dir / "system.md"
        prompt_file.write_text("# System Prompt\n\nYou are MEHO.")

        # Create config referencing prompt file
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
name: test
system_prompt_file: prompts/system.md
""")

        config = load_yaml_config(config_file)

        assert "You are MEHO" in config.system_prompt

    def test_load_config_uses_filename_as_default_name(self, tmp_path: Path) -> None:
        """Should use filename as name if not specified."""
        config_file = tmp_path / "my_agent.yaml"
        config_file.write_text("description: Test\n")

        config = load_yaml_config(config_file)

        assert config.name == "my_agent"

    def test_load_config_not_found(self, tmp_path: Path) -> None:
        """Should raise FileNotFoundError for missing file."""
        with pytest.raises(FileNotFoundError):
            load_yaml_config(tmp_path / "nonexistent.yaml")

    def test_load_empty_config(self, tmp_path: Path) -> None:
        """Should handle empty config file."""
        config_file = tmp_path / "empty.yaml"
        config_file.write_text("")

        config = load_yaml_config(config_file)

        assert config.name == "empty"  # Uses filename


class TestResolveFileReference:
    """Tests for resolve_file_reference function."""

    def test_resolve_existing_file(self, tmp_path: Path) -> None:
        """Should resolve and read existing file."""
        file_path = tmp_path / "test.txt"
        file_path.write_text("Hello, World!")

        content = resolve_file_reference("test.txt", tmp_path)

        assert content == "Hello, World!"

    def test_resolve_nested_file(self, tmp_path: Path) -> None:
        """Should resolve nested paths."""
        nested_dir = tmp_path / "subdir" / "deeper"
        nested_dir.mkdir(parents=True)
        file_path = nested_dir / "file.txt"
        file_path.write_text("Nested content")

        content = resolve_file_reference("subdir/deeper/file.txt", tmp_path)

        assert content == "Nested content"

    def test_resolve_missing_file(self, tmp_path: Path) -> None:
        """Should raise FileNotFoundError for missing file."""
        with pytest.raises(FileNotFoundError):
            resolve_file_reference("nonexistent.txt", tmp_path)


class TestLoadToolsFromFolder:
    """Tests for load_tools_from_folder function."""

    def test_load_tools_from_empty_folder(self, tmp_path: Path) -> None:
        """Should return empty dict for empty folder."""
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()

        tools = load_tools_from_folder(tools_dir)

        assert tools == {}

    def test_load_tools_from_nonexistent_folder(self, tmp_path: Path) -> None:
        """Should return empty dict for nonexistent folder."""
        tools = load_tools_from_folder(tmp_path / "nonexistent")

        assert tools == {}

    def test_load_tools_discovers_tool_classes(self, tmp_path: Path) -> None:
        """Should discover classes with TOOL_NAME attribute."""
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()

        # Create a tool file
        tool_file = tools_dir / "my_tool.py"
        tool_file.write_text("""
from dataclasses import dataclass

@dataclass
class MyTool:
    TOOL_NAME = "my_tool"
    TOOL_DESCRIPTION = "A test tool"
""")

        tools = load_tools_from_folder(tools_dir)

        assert "my_tool" in tools
        assert tools["my_tool"].TOOL_NAME == "my_tool"

    def test_load_tools_ignores_private_files(self, tmp_path: Path) -> None:
        """Should ignore files starting with underscore."""
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()

        # Create private file
        private_file = tools_dir / "_private.py"
        private_file.write_text("""
class PrivateTool:
    TOOL_NAME = "private"
""")

        tools = load_tools_from_folder(tools_dir)

        assert "private" not in tools

    def test_load_multiple_tools(self, tmp_path: Path) -> None:
        """Should load multiple tools from different files."""
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()

        # Create first tool
        (tools_dir / "tool_a.py").write_text("""
class ToolA:
    TOOL_NAME = "tool_a"
""")

        # Create second tool
        (tools_dir / "tool_b.py").write_text("""
class ToolB:
    TOOL_NAME = "tool_b"
""")

        tools = load_tools_from_folder(tools_dir)

        assert "tool_a" in tools
        assert "tool_b" in tools


class TestLoadNodesFromFolder:
    """Tests for load_nodes_from_folder function."""

    def test_load_nodes_from_empty_folder(self, tmp_path: Path) -> None:
        """Should return empty dict for empty folder."""
        nodes_dir = tmp_path / "nodes"
        nodes_dir.mkdir()

        nodes = load_nodes_from_folder(nodes_dir)

        assert nodes == {}

    def test_load_nodes_from_nonexistent_folder(self, tmp_path: Path) -> None:
        """Should return empty dict for nonexistent folder."""
        nodes = load_nodes_from_folder(tmp_path / "nonexistent")

        assert nodes == {}

    def test_load_nodes_discovers_node_classes(self, tmp_path: Path) -> None:
        """Should discover classes with NODE_NAME attribute."""
        nodes_dir = tmp_path / "nodes"
        nodes_dir.mkdir()

        # Create a node file
        node_file = nodes_dir / "my_node.py"
        node_file.write_text("""
from dataclasses import dataclass

@dataclass
class MyNode:
    NODE_NAME = "my_node"
""")

        nodes = load_nodes_from_folder(nodes_dir)

        assert "my_node" in nodes
        assert nodes["my_node"].NODE_NAME == "my_node"

    def test_load_nodes_ignores_private_files(self, tmp_path: Path) -> None:
        """Should ignore files starting with underscore."""
        nodes_dir = tmp_path / "nodes"
        nodes_dir.mkdir()

        # Create private file
        (nodes_dir / "_init.py").write_text("""
class PrivateNode:
    NODE_NAME = "private"
""")

        nodes = load_nodes_from_folder(nodes_dir)

        assert "private" not in nodes


class TestLoadPromptFile:
    """Tests for load_prompt_file function."""

    def test_load_existing_prompt(self, tmp_path: Path) -> None:
        """Should load prompt from file."""
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("# System Prompt\n\nYou are an assistant.")

        content = load_prompt_file(prompt_file)

        assert "System Prompt" in content
        assert "assistant" in content

    def test_load_missing_prompt(self, tmp_path: Path) -> None:
        """Should raise FileNotFoundError for missing file."""
        with pytest.raises(FileNotFoundError):
            load_prompt_file(tmp_path / "missing.md")


class TestRenderPromptTemplate:
    """Tests for render_prompt_template function."""

    def test_render_with_variables(self) -> None:
        """Should substitute variables in template."""
        template = "You are {role}. Your goal is {goal}."
        result = render_prompt_template(
            template,
            {"role": "MEHO", "goal": "help users"},
        )

        assert result == "You are MEHO. Your goal is help users."

    def test_render_with_missing_variable(self) -> None:
        """Should handle missing variables gracefully."""
        template = "Hello {name}, your role is {role}."
        # Only providing 'name', not 'role'
        result = render_prompt_template(template, {"name": "User"})

        # Should return original template (with partial substitution fail)
        assert "{role}" in result or "Hello" in result

    def test_render_no_variables(self) -> None:
        """Should return template unchanged if no variables."""
        template = "This is a static prompt."
        result = render_prompt_template(template, {})

        assert result == "This is a static prompt."


class TestAgentConfigDataclass:
    """Tests for AgentConfig dataclass."""

    def test_create_agent_config(self) -> None:
        """Should create AgentConfig with all fields."""
        config = AgentConfig(
            name="test",
            description="A test agent",
            max_steps=50,
        )

        assert config.name == "test"
        assert config.description == "A test agent"
        assert config.max_steps == 50
        assert config.tools == {}

    def test_default_values(self) -> None:
        """Should have correct defaults."""
        config = AgentConfig(name="minimal")

        assert config.description == ""
        assert config.system_prompt == ""
        assert config.max_steps == 100
        assert config.tools == {}


class TestConfigImports:
    """Tests for module imports."""

    def test_importable_from_config(self) -> None:
        """Functions should be importable from config module."""
        from meho_app.modules.agents.config import (
            AgentConfig,
            load_nodes_from_folder,
            load_tools_from_folder,
            load_yaml_config,
        )

        assert load_yaml_config is not None
        assert load_tools_from_folder is not None
        assert load_nodes_from_folder is not None
        assert AgentConfig is not None
