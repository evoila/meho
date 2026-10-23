# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Collect the vSAN methods the vCenter connector calls, for the reconcile lanes (#4441).

Every vSAN operation declares the method it calls as a module-level
:class:`~meho_backplane.connectors.vmware_rest._mount.VsanMethod` constant.
This walks the ``vmware_rest`` package and returns those constants, so the
lanes check the live set and never a copied list that can drift.
"""

from __future__ import annotations

import functools
import importlib
import pkgutil

import meho_backplane.connectors.vmware_rest as vmware_rest_package
from meho_backplane.connectors.vmware_rest._mount import VsanMethod, vsan_method_path


@functools.cache
def vsan_methods() -> frozenset[VsanMethod]:
    """Every module-level ``VsanMethod`` constant in the ``vmware_rest`` package."""
    found: set[VsanMethod] = set()
    prefix = f"{vmware_rest_package.__name__}."
    for info in pkgutil.walk_packages(vmware_rest_package.__path__, prefix=prefix):
        module = importlib.import_module(info.name)
        found.update(value for value in vars(module).values() if isinstance(value, VsanMethod))
    return frozenset(found)


def vsan_vi_json_op_id(method: VsanMethod) -> str:
    """The method as the vendor VI/JSON file names it: ``POST:/vsan/<Type>/{moId}/<Method>``."""
    return f"POST:{vsan_method_path(method.mo_type, '{moId}', method.method)}"
