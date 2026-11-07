# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``check_poll_url`` accepts only the shape of an Azure operation status URL.

A poll URL must end in ``<kind>/<id>``, where ``<kind>`` is ``operations``,
``operationStatuses``, ``operationResults`` or ``asyncOperations`` (any
case), under the subscription, a provider namespace (with an optional
location), or a resource group. Every id and name here is made up.
"""

from __future__ import annotations

import pytest

from meho_backplane.connectors.azure._errors import AzureUrlRefusedError
from meho_backplane.connectors.azure._polling import check_poll_url
from tests._azure_fixtures import ARM_BASE, SUBSCRIPTION_A, AzureTargetStub

_SUB = f"{ARM_BASE}/subscriptions/{SUBSCRIPTION_A}"
_OP_ID = "22222222-0000-0000-0000-00000000f001"
_API = "?api-version=2024-07-01"


@pytest.mark.parametrize(
    "url",
    [
        f"{_SUB}/providers/Microsoft.Compute/locations/westeurope/operations/{_OP_ID}{_API}",
        f"{_SUB}/providers/Microsoft.HybridCompute/locations/westeurope/operationResults/{_OP_ID}",
        f"{_SUB}/providers/Microsoft.Storage/operations/{_OP_ID}?monitor=true&api-version=2023-05-01",
        f"{_SUB}/operationresults/eyJqb2JJZCI6IlJFU09VUkNFR1JPVVBERUxFVElPTkpPQiJ9{_API}",
        f"{_SUB}/providers/Microsoft.Network/locations/westeurope/operationStatuses/{_OP_ID}",
        f"{_SUB}/resourceGroups/rg-app/providers/Microsoft.Web/asyncOperations/{_OP_ID}",
        f"{_SUB}/resourcegroups/rg-app/providers/Microsoft.Sql/locations/westeurope/OPERATIONS/{_OP_ID}",
        f"{_SUB}/resourceGroups/rg-app/operationResults/{_OP_ID}",
    ],
)
def test_a_status_url_is_accepted(url: str) -> None:
    check_poll_url(AzureTargetStub(), url)


@pytest.mark.parametrize(
    "url",
    [
        # A VM read, also with its user data.
        f"{_SUB}/resourceGroups/rg-app/providers/Microsoft.Compute/virtualMachines/vm-01{_API}",
        f"{_SUB}/resourceGroups/rg-app/providers/Microsoft.Compute/virtualMachines/vm-01"
        f"?$expand=userData&api-version=2024-07-01",
        # A Log Analytics query on a workspace.
        f"{_SUB}/resourceGroups/rg-app/providers/Microsoft.OperationalInsights/workspaces/"
        f"ws-01/query?query=Heartbeat&api-version=2020-08-01",
        # A path part with "operation" in it that is not a status URL.
        f"{_SUB}/resourceGroups/rg-app/providers/Microsoft.OperationalInsights/workspaces/"
        f"operations-ws/tables{_API}",
        f"{_SUB}/resourceGroups/rg-app/providers/Microsoft.ApiManagement/service/apim-01/"
        f"apis/api-01/operations/{_OP_ID}{_API}",
        f"{_SUB}/providers/Microsoft.Compute/locations/westeurope/operations{_API}",
        f"{_SUB}/providers/Microsoft.Compute/locations/westeurope/operations/{_API}",
        f"{_SUB}/providers/Microsoft.Compute/locations/westeurope/operations/{_OP_ID}/extra",
        f"{_SUB}/providers/Microsoft.Compute//operations/{_OP_ID}",
        f"{_SUB}/providers/Compute/operations/{_OP_ID}",
        f"{_SUB}/providers/Microsoft.Compute/regions/westeurope/operations/{_OP_ID}",
        f"{_SUB}/resourceGroups/operations/{_OP_ID}",
        f"{_SUB}/operations{_API}",
        f"{_SUB}{_API}",
        f"{_SUB}/resourceGroups/rg-app{_API}",
    ],
)
def test_a_url_that_is_no_status_url_is_refused(url: str) -> None:
    with pytest.raises(AzureUrlRefusedError):
        check_poll_url(AzureTargetStub(), url)
