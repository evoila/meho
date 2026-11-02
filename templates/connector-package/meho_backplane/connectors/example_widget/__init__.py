# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The example widget connector: a made-up connector package.

MEHO imports this folder when it starts. The import:

* registers the connector class under its product, version and
  implementation id (connector id ``example-widget-1.x``);
* queues the function that registers the two operations;
* registers the preview of ``example.widget.rename``.

Every file in this folder imports MEHO only through the connector kit,
``meho_connector_kit``. ``tests/test_kit_imports.py`` checks that.
"""

from meho_connector_kit import (
    register_connector_v2,
    register_preview_builder,
    register_typed_op_registrar,
)

from .connector import CONNECTOR_ID, IMPL_ID, PRODUCT, VERSION, ExampleWidgetConnector
from .operations import RENAME_OP_ID, register_example_widget_operations, rename_preview

register_connector_v2(product=PRODUCT, version=VERSION, impl_id=IMPL_ID, cls=ExampleWidgetConnector)
register_typed_op_registrar(register_example_widget_operations)
register_preview_builder(RENAME_OP_ID, rename_preview)

__all__ = ["CONNECTOR_ID", "ExampleWidgetConnector", "register_example_widget_operations"]
