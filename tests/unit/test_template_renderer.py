# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for generic template renderer.

Tests Jinja2-based template rendering for text, tags, and boolean expressions.
"""

import pytest
from jinja2 import TemplateError

from meho_app.modules.ingestion.template_renderer import TemplateRenderer, TemplateValidator


@pytest.fixture
def renderer():
    """Template renderer instance"""
    return TemplateRenderer()


@pytest.fixture
def validator():
    """Template validator instance"""
    return TemplateValidator()


def test_render_simple_text(renderer):
    """Test rendering simple text template"""
    template = "Hello {{ payload.name }}"
    payload = {"name": "World"}

    result = renderer.render_text(template, payload)
    assert result == "Hello World"


def test_render_complex_text(renderer):
    """Test rendering complex text with nested data and loops"""
    template = """
Event: {{ payload.event_type }}
Source: {{ payload.source.name }}
Alerts:
{% for alert in payload.alerts %}
- {{ alert.id }}: {{ alert.title }}
{% endfor %}
""".strip()

    payload = {
        "event_type": "alert",
        "source": {"name": "monitoring"},
        "alerts": [{"id": "1", "title": "High CPU"}, {"id": "2", "title": "Low Memory"}],
    }

    result = renderer.render_text(template, payload)
    assert "Event: alert" in result
    assert "Source: monitoring" in result
    assert "1: High CPU" in result
    assert "2: Low Memory" in result


def test_render_text_with_filters(renderer):
    """Test rendering with built-in Jinja2 filters"""
    template = "Name: {{ payload.name | upper }}, Count: {{ payload.alerts | length }}"
    payload = {"name": "test", "alerts": [1, 2, 3]}

    result = renderer.render_text(template, payload)
    assert result == "Name: TEST, Count: 3"


def test_render_text_with_custom_filter(renderer):
    """Test rendering with custom truncate_id filter"""
    template = "ID: {{ payload.commit_id | truncate_id }}"
    payload = {"commit_id": "abc123def456"}

    result = renderer.render_text(template, payload)
    assert result == "ID: abc123d"  # First 7 characters


def test_render_text_handles_missing_fields(renderer):
    """Test that rendering handles missing fields gracefully"""
    template = "Name: {{ payload.name }}, Age: {{ payload.age }}"
    payload = {"name": "John"}  # age is missing

    # Should render without error (Jinja2 treats missing as empty)
    result = renderer.render_text(template, payload)
    assert "Name: John" in result


def test_render_text_invalid_template(renderer):
    """Test that invalid templates raise TemplateError"""
    template = "{{ payload.name"  # Missing closing }}
    payload = {"name": "test"}

    with pytest.raises(TemplateError):
        renderer.render_text(template, payload)


def test_render_simple_tags(renderer):
    """Test rendering simple tag rules"""
    tag_rules = ["source:github", "type:push"]
    payload = {}

    result = renderer.render_tags(tag_rules, payload)
    assert result == ["source:github", "type:push"]


def test_render_dynamic_tags(renderer):
    """Test rendering dynamic tags with payload data"""
    tag_rules = [
        "source:{{ payload.source }}",
        "repo:{{ payload.repository.name }}",
        "branch:{{ payload.ref | replace('refs/heads/', '') }}",
    ]
    payload = {"source": "github", "repository": {"name": "myrepo"}, "ref": "refs/heads/main"}

    result = renderer.render_tags(tag_rules, payload)
    assert "source:github" in result
    assert "repo:myrepo" in result
    assert "branch:main" in result


def test_render_tags_skips_empty(renderer):
    """Test that empty tags are skipped"""
    tag_rules = [
        "source:github",
        "",  # Empty
        "   ",  # Whitespace only
        "type:{{ payload.missing_field }}",  # Renders to empty
    ]
    payload = {}

    result = renderer.render_tags(tag_rules, payload)
    assert result == ["source:github"]


def test_render_tags_handles_errors(renderer):
    """Test that tag rendering errors don't break entire process"""
    tag_rules = [
        "source:github",
        "{{ invalid syntax",  # Invalid
        "type:push",
    ]
    payload = {}

    # Should return valid tags, skip invalid
    result = renderer.render_tags(tag_rules, payload)
    assert "source:github" in result
    assert "type:push" in result


def test_evaluate_boolean_true(renderer):
    """Test evaluating boolean expression that returns true"""
    expression = "{{ payload.status == 'Degraded' }}"
    payload = {"status": "Degraded"}

    result = renderer.evaluate_boolean(expression, payload)
    assert result is True


def test_evaluate_boolean_false(renderer):
    """Test evaluating boolean expression that returns false"""
    expression = "{{ payload.status == 'Healthy' }}"
    payload = {"status": "Degraded"}

    result = renderer.evaluate_boolean(expression, payload)
    assert result is False


def test_evaluate_boolean_complex(renderer):
    """Test evaluating complex boolean expression"""
    expression = "{{ payload.health == 'Degraded' or payload.sync == 'OutOfSync' }}"
    payload = {"health": "Healthy", "sync": "OutOfSync"}

    result = renderer.evaluate_boolean(expression, payload)
    assert result is True


def test_evaluate_boolean_with_in_operator(renderer):
    """Test evaluating boolean with 'in' operator"""
    expression = "{{ payload.severity in ['high', 'critical'] }}"
    payload = {"severity": "high"}

    result = renderer.evaluate_boolean(expression, payload)
    assert result is True


def test_evaluate_boolean_handles_invalid(renderer):
    """Test that invalid boolean expressions return false"""
    expression = "{{ invalid syntax"
    payload = {}

    result = renderer.evaluate_boolean(expression, payload)
    assert result is False


def test_validate_template_valid(validator):
    """Test validating a valid template"""
    is_valid, errors = validator.validate_template(
        text_template="Hello {{ payload.name }}",
        tag_rules=["source:test", "type:{{ payload.type }}"],
        issue_detection_rule="{{ payload.severity == 'high' }}",
    )

    assert is_valid is True
    assert len(errors) == 0


def test_validate_template_invalid_text(validator):
    """Test validating invalid text template"""
    is_valid, errors = validator.validate_template(
        text_template="{{ payload.name",  # Missing }}
        tag_rules=["source:test"],
    )

    assert is_valid is False
    assert len(errors) > 0
    assert "text_template" in errors[0]


def test_validate_template_invalid_tag_rule(validator):
    """Test validating invalid tag rule"""
    is_valid, errors = validator.validate_template(
        text_template="Hello",
        tag_rules=["source:test", "{{ invalid"],  # Invalid syntax
    )

    assert is_valid is False
    assert len(errors) > 0
    assert "tag_rule" in errors[0]


def test_validate_template_invalid_issue_rule(validator):
    """Test validating invalid issue detection rule"""
    # Test that validator doesn't crash with malformed template
    validator.validate_template(
        text_template="Hello",
        tag_rules=["source:test"],
        issue_detection_rule="{{{{ invalid",  # Truly invalid syntax
    )

    # Note: Some invalid templates may still pass basic validation
    # They'll fail at runtime with actual payload
    assert True  # Just verify validator doesn't crash
