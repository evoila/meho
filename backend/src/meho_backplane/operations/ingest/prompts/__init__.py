# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Jinja2 prompt templates for the T3 LLM operation-grouping pass.

The templates ship as package data (``.j2`` files alongside this
module) so :func:`jinja2.PackageLoader` resolves them via
``importlib.resources`` in both source-checkout and installed-wheel
layouts.

Prompt text lives in version control (per
``ai_engineering_best_practices.md`` -- "Prompts as code"); editing a
template is a reviewed code change with a corresponding eval / mock-
LLM run in the test suite. The two templates are intentionally
self-contained -- no shared partials, no per-call dynamic include --
so a snapshot test of the rendered output catches drift end-to-end.
"""
