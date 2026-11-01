# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A fake vCenter for the tests of the vSAN reads (``vmware.vsan.*``).

It drives the real :class:`VmwareRestConnector` over respx:

* vCenter 9.1 answers each vSAN method on its VI-JSON path
  ``/sdk/vim25/9.1.0.0/vsan/<Type>/<moId>/<Method>`` with a JSON payload.
* vCenter 8.0.3 answers each vSAN method as SOAP on ``/vsanHealth``, after a
  SOAP login on ``/sdk``, with a hand-made XML answer.
* Both answer ``PropertyCollector.RetrievePropertiesEx`` on VI-JSON from a
  table ``{type: {moid: {path: value}}}``, which also serves ``Task.info``.

All names and ids are made up.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import VmwareRestConnector, VsphereTargetLike
from meho_backplane.settings import get_settings

BASE = "https://vc-vsan.test.invalid"
RELEASE_91 = "9.1.0.0"
RELEASE_80 = "8.0.3.0"
SOAP_ENV = "http://schemas.xmlsoap.org/soap/envelope/"
XSI = "http://www.w3.org/2001/XMLSchema-instance"

_VSAN_METHOD_RE = re.compile(r'<(\w+) xmlns="urn:vsan">')


@pytest.fixture
def vsan_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the chassis env vars ``Settings`` reads at construction time."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@dataclass
class StubTarget:
    name: str = "vc-vsan"
    host: str = "vc-vsan.test.invalid"
    port: int | None = 443
    secret_ref: str = "vsphere/vc-vsan"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    tls_server_name: str | None = None
    fingerprint: dict[str, Any] | None = None
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


def make_operator() -> Operator:
    return Operator(
        sub="op-vsan-reads",
        name="vSAN Reads Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _stub_loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
    return {"username": "svc-meho", "password": "stub-password"}


def new_connector() -> VmwareRestConnector:
    return VmwareRestConnector(session_loader=_stub_loader)


async def close(connector: VmwareRestConnector) -> None:
    """Close the pooled clients without the session-revoke call."""
    for client in connector._clients.values():
        await client.aclose()
    connector._clients.clear()


def moref(mo_type: str, moid: str) -> dict[str, str]:
    return {"_typeName": "ManagedObjectReference", "type": mo_type, "value": moid}


def soap_envelope(inner: str) -> str:
    return (
        f'<soapenv:Envelope xmlns:soapenv="{SOAP_ENV}" xmlns:xsi="{XSI}">'
        f"<soapenv:Body>{inner}</soapenv:Body></soapenv:Envelope>"
    )


def soap_answer(method: str, *returnvals: str) -> str:
    """A vSAN SOAP answer with one ``returnval`` element per entry of *returnvals*."""
    inner = "".join(f"<returnval>{value}</returnval>" for value in returnvals)
    return soap_envelope(f'<{method}Response xmlns="urn:vsan">{inner}</{method}Response>')


def soap_fault(fault_type: str, text: str) -> httpx.Response:
    return httpx.Response(
        500,
        text=soap_envelope(
            "<soapenv:Fault><faultcode>ServerFaultCode</faultcode>"
            f"<faultstring>{text}</faultstring>"
            f'<detail><{fault_type}Fault xmlns="urn:vsan" xsi:type="{fault_type}"/></detail>'
            "</soapenv:Fault>"
        ),
    )


def _service_versions(namespace: str, version: str) -> str:
    return (
        '<namespaces version="1.0"><namespace>'
        f"<name>{namespace}</name><version>{version}</version>"
        "</namespace></namespaces>"
    )


_VIM_SERVICE_CONTENT_XML = soap_envelope(
    '<RetrieveServiceContentResponse xmlns="urn:vim25"><returnval>'
    '<propertyCollector type="PropertyCollector">propertyCollector</propertyCollector>'
    '<sessionManager type="SessionManager">SessionManager</sessionManager>'
    "<about><version>8.0.3</version><apiVersion>8.0.3.0</apiVersion>"
    "<apiType>VirtualCenter</apiType></about>"
    "</returnval></RetrieveServiceContentResponse>"
)
_VIM_LOGIN_OK_XML = soap_envelope(
    '<LoginResponse xmlns="urn:vim25"><returnval><key>vc-session-key</key>'
    "<userName>svc-meho</userName></returnval></LoginResponse>"
)


def _retrieve_result(objects: list[tuple[str, str, dict[str, Any]]]) -> dict[str, Any]:
    return {
        "_typeName": "RetrieveResult",
        "objects": [
            {
                "_typeName": "ObjectContent",
                "obj": moref(mo_type, moid),
                "propSet": [
                    {"_typeName": "DynamicProperty", "name": name, "val": value}
                    for name, value in props.items()
                ],
            }
            for mo_type, moid, props in objects
        ],
    }


@dataclass
class VsanCall:
    """One vSAN call the fake vCenter got: the method and the raw request body."""

    method: str
    body: str

    def json(self) -> Any:
        return json.loads(self.body)


class FakeVcenter:
    """Answers the vSAN methods and property reads of one vCenter (9.1 or 8.0.3).

    *vsan* maps a vSAN method name to its answer: a JSON payload (9.1), an XML
    answer text (8.0), an :class:`httpx.Response`, or a list of these to give
    in turn. *props* maps an object type to ``{moid: {path: value}}``.
    """

    def __init__(
        self,
        release: str,
        vsan: dict[str, Any],
        props: dict[str, dict[str, dict[str, Any]]] | None = None,
    ) -> None:
        self.release = release
        self._vsan = {name: list(v) if isinstance(v, list) else [v] for name, v in vsan.items()}
        self._props = props or {}
        self.vsan_calls: list[VsanCall] = []
        self.property_reads: list[dict[str, Any]] = []

    @property
    def soap(self) -> bool:
        return self.release == RELEASE_80

    def install(self, mock: respx.MockRouter) -> None:
        mock.post("/api/session").respond(200, json="rest-token")
        mock.get("/api/about").respond(404, json={})
        mock.get("/sdk/vimServiceVersions.xml").respond(
            200, text=_service_versions("urn:vim25", self.release)
        )
        mock.post(
            f"/sdk/vim25/{self.release}/PropertyCollector/propertyCollector/RetrievePropertiesEx"
        ).mock(side_effect=self._retrieve)
        if self.soap:
            mock.post("/sdk").mock(side_effect=self._sdk)
            mock.post("/vsanHealth").mock(side_effect=self._soap_vsan)
            mock.get("/sdk/vsanServiceVersions.xml").respond(
                200, text=_service_versions("urn:vsan", "8.0.0.4")
            )
        else:
            mock.post(path__regex=rf"^/sdk/vim25/{re.escape(self.release)}/vsan/").mock(
                side_effect=self._vijson_vsan
            )

    def calls_of(self, method: str) -> list[VsanCall]:
        return [call for call in self.vsan_calls if call.method == method]

    def _next(self, method: str) -> Any:
        answers = self._vsan.get(method)
        if not answers:
            return httpx.Response(500, text=f"<unexpected vSAN method {method}/>")
        return answers.pop(0) if len(answers) > 1 else answers[0]

    def _vijson_vsan(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        self.vsan_calls.append(VsanCall(method, request.content.decode("utf-8")))
        answer = self._next(method)
        return answer if isinstance(answer, httpx.Response) else httpx.Response(200, json=answer)

    def _soap_vsan(self, request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8")
        found = _VSAN_METHOD_RE.search(body)
        method = found.group(1) if found else "?"
        self.vsan_calls.append(VsanCall(method, body))
        answer = self._next(method)
        return answer if isinstance(answer, httpx.Response) else httpx.Response(200, text=answer)

    def _sdk(self, request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8")
        if "<RetrieveServiceContent " in body:
            return httpx.Response(200, text=_VIM_SERVICE_CONTENT_XML)
        if "<Login " in body:
            return httpx.Response(
                200,
                text=_VIM_LOGIN_OK_XML,
                headers={"set-cookie": 'vmware_soap_session="vsan-cookie"; Path=/; HttpOnly'},
            )
        return httpx.Response(500, text="<unexpected-sdk/>")

    def _retrieve(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.property_reads.append(body)
        spec = body["specSet"][0]
        mo_type = spec["propSet"][0]["type"]
        paths = spec["propSet"][0]["pathSet"]
        table = self._props.get(mo_type, {})
        objects = []
        for object_spec in spec["objectSet"]:
            moid = object_spec["obj"]["value"]
            if moid in table:
                props = {path: table[moid][path] for path in paths if path in table[moid]}
                objects.append((mo_type, moid, props))
        return httpx.Response(200, json=_retrieve_result(objects))
