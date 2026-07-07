# vcsim integration testing

Reference for the `vcsim_endpoint` test fixture (`backend/tests/acceptance/_vcsim.py`) and how future G3.x connectors should mirror its shape for their own integration-test surfaces.

## Why vcsim

`vcsim` is the [VMware-shipped govmomi-backed vCenter simulator](https://github.com/vmware/govmomi/tree/main/vcsim). It listens on `0.0.0.0:8989` by default, serves both `/api` (new REST surface) and `/rest` (legacy mount), and accepts any HTTP basic credential — the simulator never validates the username/password pair. That property lets the MEHO acceptance suite drive the production `VmwareRestConnector` (#498) through `POST /api/session` / `GET /api/about` / `GET /vcenter/*` without standing up a real vCenter.

Without vcsim, the G0.6 dispatcher's HTTP path is exercised only by unit-suite mocks (`tests/test_operations_dispatcher.py`); a regression in the real-HTTP-shape handling (session cookie threading, TLS context reuse, JSON deserialisation of vCenter's `{"value": [...]}` envelope) would slip through unit gates. vcsim closes that gap on every CI run.

## Where the fixture lives

The shared resolver + endpoint dataclass + container helper live in `backend/tests/acceptance/_vcsim.py`:

| Symbol | Purpose |
| --- | --- |
| `VcsimEndpoint` | Frozen dataclass — host / port / scheme / no-auth credential pair / pre-built base URL. |
| `VcsimTopology` | Frozen dataclass — seed counts (`vms`, `hosts`, `clusters`, `datastores`, `folders`). |
| `DEFAULT_VCSIM_TOPOLOGY` | `VcsimTopology(vms=50, hosts=3, clusters=1, datastores=2, folders=2)` — the canary's default. |
| `build_vcsim_command(topology, *, listen=...)` | Compose the vcsim CLI args (`-l ... -vm 50 -host 3 ...`). |
| `build_vcsim_http_client(endpoint)` | `httpx.AsyncClient` wired for vcsim's self-signed cert (`verify=False`). |
| `patch_vmware_connector_for_vcsim(connector, endpoint)` | Mutate a `VmwareRestConnector` instance in-place so its session loader returns vcsim's any-credentials pair + its httpx pool skips TLS verification. |
| `resolve_vcsim_endpoint(topology)` | Context manager — yields a `VcsimEndpoint` and tears the testcontainers boot down on exit. |

The conftest at `backend/tests/acceptance/conftest.py` wraps `resolve_vcsim_endpoint()` in a session-scoped `vcsim_endpoint` fixture; the same conftest exposes the `ingested_canary_vcsim` fixture (defined in `_canary_fixtures.py`) which seeds an `EndpointDescriptor` bundle + a Target row + a patched connector instance, ready for dispatch.

## Resolution priority

The helper consults sources in this order:

1. **`MEHO_VCSIM_URL`** — explicit base URL pointing at a running vcsim instance (e.g. `https://10.0.5.4:8989`). The helper parses the URL and returns an endpoint without booting a container. This is the CI path for the meho-runners-ci pool when a vcsim daemon is provisioned alongside the runner.
2. **testcontainers-managed `vmware/vcsim` boot** — when no env override resolves, the helper boots the public `vmware/vcsim` image with the seed flags described under "Seeding the topology" and yields an endpoint targeting the host-mapped port. The image name is overridable via `MEHO_TEST_VCSIM_IMAGE` for registry-mirror swaps.
3. **Skip** — when neither path is viable (no env var + no Docker socket), the fixture skips via `pytest.skip` with the `VCSIM_DOCKER_SKIP_REASON` message. The CI runner pool provisions Docker so the tests run there; agent sandboxes without Docker collect cleanly and skip.

## Seeding the topology

vcsim accepts CLI flags at startup to seed its in-memory inventory:

| Flag | Default in `DEFAULT_VCSIM_TOPOLOGY` | Purpose |
| --- | --- | --- |
| `-vm` | 50 | Simulated VMs. Matches the JSONFlux force-mode test's row-count assertion. |
| `-host` | 3 | ESXi hosts the cluster contains. |
| `-cluster` | 1 | Cluster the hosts roll up to. |
| `-ds` | 2 | Datastores. Matches the composite-dispatch test's assertion. |
| `-folder` | 2 | Folders, useful for resolver-ambiguity tests. |

The flags must be passed as separate `argv` entries (a list, not a single space-separated string). Passing a string lets Go's `flag.Parse()` halt at the first positional and silently fall back to `-l 127.0.0.1:8989` — the failure mode PR #518's first CI green attempt hit. `build_vcsim_command()` ships the right shape.

## Patching `VmwareRestConnector` for vcsim

Production code paths build the per-target `httpx.AsyncClient` with httpx's default `verify=True` and read credentials from Vault via `load_session_credentials_from_vault`. Neither works against vcsim — its cert isn't in the host CA bundle, and the test environment doesn't run Vault. `patch_vmware_connector_for_vcsim()` mutates one connector instance in place:

```python
from tests.acceptance._vcsim import patch_vmware_connector_for_vcsim

instance = get_or_create_connector_instance(VmwareRestConnector)
patch_vmware_connector_for_vcsim(instance, vcsim_endpoint)
```

Two mutations:

* `_session_loader` → returns `{"username": "user", "password": "pass"}` (vcsim accepts any pair).
* `_http_client` → builds the per-target `AsyncClient` with `verify=ssl_ctx` where `ssl_ctx.verify_mode = ssl.CERT_NONE`.

The mutations are scoped to one instance; tests using the helper MUST call `reset_dispatcher_caches()` in teardown so the next test sees a fresh, unpatched instance. The `ingested_canary_vcsim` fixture handles that automatically.

## Acceptance tests built on the fixture

Four modules under `backend/tests/acceptance/` consume the fixture:

* `test_vmware_rest_dispatch_smoke.py` — parametrised across 5 read ops (`GET:/api/about`, `GET:/vcenter/cluster`, `GET:/vcenter/host`, `GET:/vcenter/datastore`, `GET:/vcenter/network`); asserts each returns `status='ok'`.
* `test_vmware_rest_jsonflux_force_handle.py` — installs a test-only `ForceHandleReducer` that wraps every payload in a `ResultHandle`; asserts the handle's `total_rows=50` matches vcsim's seed.
* `test_vmware_rest_agent_flow_e2e.py` — exercises the four-step agent chain (`search_connectors → list_operation_groups → search_operations → call_operation`) end-to-end.
* `test_vmware_rest_composite_dispatch.py` — soft-dependent on G3.1-T5 (#508 read composites); skips cleanly when the composite isn't registered yet, dispatches it against vcsim once T5 lands.

The canary test `test_g07_vsphere_canary.py` also has a `MEHO_VCSIM_TARGET`-gated test (`test_g07_canary_vcsim_dispatch`, shipped in #519) that uses the env-override path against a pre-provisioned vcsim daemon rather than the testcontainers boot — both paths converge on the same `VcsimEndpoint` shape via `resolve_vcsim_endpoint()`.

## Mirroring the pattern for future G3.x connectors

Future connectors with vendor simulators should mirror the same shape:

| Connector | Simulator / fake | Image / package | Helper file |
| --- | --- | --- | --- |
| `vmware-rest-9.0` (G3.1) | `vmware/vcsim` | `docker pull vmware/vcsim:v0.55.1` (CI pins a release tag, never `:latest`) | `_vcsim.py` |
| `vault-kv-1` (G3.x) | HashiCorp Vault dev mode | `testcontainers-python` ships `VaultContainer` | future `_vault.py` |
| `k8s-kubeconfig-1` (G3.x) | k3s/k3d | `rancher/k3s:v1.32.x-k3s1` | `tests/integration/test_connectors_k8s_k3d.py` (already in place) |
| `bind9` (G3.x) | bind9 dev container | Vendored bind9 image | future `_bind9.py` |

The repeating shape:

1. A frozen `<Service>Endpoint` dataclass carrying host / port / scheme / credentials.
2. A context-manager `resolve_<service>_endpoint()` that consults env override + testcontainers boot in priority order.
3. A session-scoped `<service>_endpoint` fixture in `tests/acceptance/conftest.py`.
4. A `patch_<connector>_for_<service>()` helper that mutates a connector instance in place to accept the simulator's no-auth surface + self-signed cert.

Doc PRs adding a new helper should cite this module as the prior art and call out any place the shape diverges (e.g. Vault's `VAULT_TOKEN` env-var contract differs from vcsim's any-credentials path).

## CI integration

The vcsim-backed tests run on the `meho-runners-ci` pool (gha-runner-scale-set, ARC v2) per the existing test workflow. The Docker Hub login step at [`.github/workflows/ci.yml:109-110`](../../.github/workflows/ci.yml) authenticates pulls of the pinned `vmware/vcsim` release tag (`v0.55.1` via the Harbor dockerhub-proxy — pinned, like the sibling pgvector/valkey/Vault images, so an upstream `:latest` push can't change CI behavior without a commit); no static service definition is needed because the testcontainers fixture boots vcsim per-test-session.

`MEHO_VCSIM_URL` is **not** set in CI by default — the testcontainers path runs on every PR. Operators can override locally with `MEHO_VCSIM_URL=http://localhost:8989` when running `vcsim -l :8989` outside the pytest session.

## References

* `backend/tests/acceptance/_vcsim.py` — the resolver + endpoint helpers (source of truth).
* `backend/tests/acceptance/_canary_fixtures.py` — minimal descriptor-row seeding + connector-patch fixture.
* `backend/tests/integration/test_connectors_vmware_rest_vcsim.py` — pre-T8 integration tests (live-fingerprint / live-probe / session cache) shipped with #498.
* `backend/tests/acceptance/test_g07_vsphere_canary.py` — `MEHO_VCSIM_TARGET`-gated dispatch + audit/broadcast extension shipped with #519.
* [vcsim upstream](https://github.com/vmware/govmomi/tree/main/vcsim) — CLI flags, supported API surface, image at `docker pull vmware/vcsim:latest`.
