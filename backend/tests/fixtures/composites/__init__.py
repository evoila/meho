# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Composite-handler test fixtures.

Module-level composite handler implementations used by
:mod:`tests.test_operations_composite_register`. Kept here so the
test module's dotted-path round-trip (the dispatcher's
``import_handler`` walk over ``handler_ref``) has a stable module
location that doesn't shift if the test file is renamed -- and so
the bound-method cross-rejection tests (which look at a class
defined here) don't pollute the test module's top-level namespace.

Avoid module-level side effects -- the existing fixture pattern is
plain ``async def foo(operator, target, params, dispatch_child):
...`` with no global state.
"""
