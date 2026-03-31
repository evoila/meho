# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Generic template renderer for webhook events.

Uses Jinja2 to render templates with webhook payloads.
"""

from typing import Any

from jinja2 import BaseLoader, Environment, TemplateError, select_autoescape

from meho_app.core.otel import get_logger

logger = get_logger(__name__)


class TemplateRenderer:
    """
    Generic template renderer using Jinja2.

    Renders text templates and tag rules with webhook payloads.
    """

    def __init__(self) -> None:
        """Initialize Jinja2 environment with safe defaults"""
        # nosemgrep: direct-use-of-jinja2 -- server-controlled templates with autoescape enabled
        self.env = Environment(
            loader=BaseLoader(),
            autoescape=select_autoescape(["html", "xml"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )

        # Add custom filters if needed
        self.env.filters["truncate_id"] = self._truncate_id

    def render_text(self, template_str: str, payload: dict[str, Any]) -> str:
        """
        Render text template with payload.

        Args:
            template_str: Jinja2 template string
            payload: Webhook payload

        Returns:
            Rendered text

        Raises:
            TemplateError: If template rendering fails
        """
        try:
            template = self.env.from_string(template_str)
            return template.render(payload=payload)
        except TemplateError as e:
            logger.error(f"Failed to render text template: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error rendering text template: {e}")
            raise TemplateError(f"Failed to render template: {e!s}") from e

    def render_tags(self, tag_rules: list[str], payload: dict[str, Any]) -> list[str]:
        """
        Render tag rules with payload.

        Each rule is a Jinja2 expression that should evaluate to a string.

        Args:
            tag_rules: List of Jinja2 expressions
            payload: Webhook payload

        Returns:
            List of rendered tags

        Example:
            tag_rules = [
                "source:github",
                "repo:{{ payload.repository.full_name }}",
                "branch:{{ payload.ref | replace('refs/heads/', '') }}"
            ]

            Returns: ["source:github", "repo:myorg/myrepo", "branch:main"]
        """
        rendered_tags = []

        for rule in tag_rules:
            try:
                template = self.env.from_string(rule)
                rendered = template.render(payload=payload).strip()

                # Skip empty tags (including tags like "type:" with no value)
                if rendered and not rendered.endswith(":"):
                    rendered_tags.append(rendered)

            except TemplateError as e:
                logger.warning(f"Failed to render tag rule '{rule}': {e}")
                # Continue with other rules
            except Exception as e:
                logger.warning(f"Unexpected error rendering tag rule '{rule}': {e}")

        return rendered_tags

    def evaluate_boolean(self, expression: str, payload: dict[str, Any]) -> bool:
        """
        Evaluate boolean expression with payload.

        Args:
            expression: Jinja2 boolean expression
            payload: Webhook payload

        Returns:
            Boolean result

        Example:
            expression = "{{ payload.health_status == 'Degraded' }}"
            Returns: True or False
        """
        try:
            template = self.env.from_string(expression)
            result = template.render(payload=payload).strip().lower()

            # Convert string result to boolean
            if result in ("true", "1", "yes"):
                return True
            elif result in ("false", "0", "no", ""):
                return False
            else:
                logger.warning(f"Boolean expression returned non-boolean: {result}")
                return False

        except TemplateError as e:
            logger.error(f"Failed to evaluate boolean expression: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error evaluating boolean: {e}")
            return False

    def _truncate_id(self, value: str, length: int = 7) -> str:
        """
        Custom Jinja2 filter to truncate IDs.

        Example: {{ commit.id | truncate_id }} → "abc1234"
        """
        return value[:length] if value else ""


class TemplateValidator:
    """
    Validates event templates for correctness.

    Ensures templates are valid Jinja2 and won't cause runtime errors.
    """

    def __init__(self) -> None:
        self.renderer = TemplateRenderer()

    def validate_template(
        self, text_template: str, tag_rules: list[str], issue_detection_rule: str | None = None
    ) -> tuple[bool, list[str]]:
        """
        Validate an event template.

        Args:
            text_template: Jinja2 text template
            tag_rules: List of tag rules
            issue_detection_rule: Optional boolean expression

        Returns:
            Tuple of (is_valid, errors)

        Example:
            is_valid, errors = validator.validate_template(
                text_template="Hello {{ payload.name }}",
                tag_rules=["source:test"],
                issue_detection_rule="{{ payload.severity == 'high' }}"
            )
        """
        errors = []

        # Test payload for validation
        test_payload = {"test_field": "test_value", "nested": {"field": "value"}, "list": [1, 2, 3]}

        # Validate text template
        try:
            self.renderer.render_text(text_template, test_payload)
        except Exception as e:
            errors.append(f"Invalid text_template: {e!s}")

        # Validate tag rules
        for i, rule in enumerate(tag_rules):
            try:
                self.renderer.env.from_string(rule)
            except Exception as e:
                errors.append(f"Invalid tag_rule[{i}] '{rule}': {e!s}")

        # Validate issue detection rule
        if issue_detection_rule:
            try:
                self.renderer.evaluate_boolean(issue_detection_rule, test_payload)
            except Exception as e:
                errors.append(f"Invalid issue_detection_rule: {e!s}")

        return (len(errors) == 0, errors)
