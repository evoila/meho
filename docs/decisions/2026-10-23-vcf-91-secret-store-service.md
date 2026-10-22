# How MEHO works with the VCF 9.1 Secret Store service (decision)

**Status:** proposed. It waits for the program owner to agree in the pull request.
**Date:** 2026-10-23
**Issue:** [#4509](https://github.com/evoila/meho/issues/4509)
**Initiative:** [#4306](https://github.com/evoila/meho/issues/4306) — VCF bring-up, fleet lifecycle, identity and platform services through MEHO

## The decision in short

- The Secret Store service keeps secrets (for example passwords and tokens)
  for apps that run on a vSphere **Supervisor**. A Supervisor is the
  Kubernetes control plane that vSphere runs inside a cluster.
- The service has **no REST API of its own** in the vendor references. There
  is no file for it in `vmware/vcf-api-specs` (tags `9.1.0.0` and `9.1.1.0`)
  and no reference for it on the Broadcom developer portal.
- But MEHO can still do the usual work, through two APIs MEHO already calls:
  1. **vCenter REST, Supervisor services part.** It tells if the service is
     installed on a Supervisor, its version, and its state. These reads go
     on the vSphere connector (`vmware-rest`), as typed (hand-written)
     operations.
  2. **The Kubernetes API of the Supervisor.** A secret is a Kubernetes
     object of kind `KeyValueSecret` (API version
     `secretstore.vmware.com/v1alpha1`). These operations go on the
     Kubernetes connector (`k8s`). It can already log in to a Supervisor.
- So the service needs **no connector of its own**. No scope exception is
  needed.
- Inside, the service uses OpenBao (an open-source fork of HashiCorp
  Vault). MEHO does **not** call the OpenBao API. The vendor does not
  document it for operators.
- MEHO never sends a secret value back to the agent. Reads return names and
  key names only. Writes take the value from a secret MEHO already stores,
  like `k8s.secret.create` does today.
- Some facts are **not confirmed** and are marked below. The connector Task
  checks them on a lab Supervisor before the operations ship.

## What the service is

- VMware describes it as a service that stores "passwords, tokens, and
  certificates" for workloads in vSphere Namespaces
  ([9.0 VKS page](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-0/using-supervisor-services/using-secret-store-service-with-vsphere-supervisor/managing-secrets-in-vsphere-supervisor-workloads-with-secret-store-service/inject-secrets-into-pods-on-vks-clusters.html)).
- The 9.1 guide says it "leverages OpenBao" and "supports Kubernetes-native
  APIs". A secret belongs to one namespace. Only workloads in that
  namespace can use it
  ([9.1 overview](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-1/using-secret-store-service-with-vsphere-supervisor.html)).
- It is one of the services that ship with VCF 9.1
  ([VCF 9.1 blog](https://blogs.vmware.com/cloud-foundation/2026/05/05/accelerate-streamline-and-control-your-self-service-private-cloud-with-vcf-9-1/)).
- There are two ways to install it
  ([9.1 overview](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-1/using-secret-store-service-with-vsphere-supervisor.html)):
  - as a **VCF service**, from the Service Management page of the VCF
    Automation provider portal. Then one instance runs on one Supervisor per
    region, and its settings go to the other Supervisors of the region
    ([install with VCF Automation](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-1/using-secret-store-service-with-vsphere-supervisor/install-secret-store-service-using-vcf-automation.html));
  - as a **Supervisor service**, on one Supervisor, from the vSphere Client
    ([install as a Supervisor service](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-1/using-secret-store-service-with-vsphere-supervisor/install-and-configure-secret-store-as-a-supervisor-service.html)).
- After install, the service makes for each namespace a default policy
  (`secret/data/<namespace>`, read and list) and a default role. For each new
  VKS cluster (a Kubernetes cluster made by the Supervisor), it makes a login
  path, a policy and a role by itself
  ([install as a Supervisor service](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-1/using-secret-store-service-with-vsphere-supervisor/install-and-configure-secret-store-as-a-supervisor-service.html),
  [VKS page](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-0/using-supervisor-services/using-secret-store-service-with-vsphere-supervisor/managing-secrets-in-vsphere-supervisor-workloads-with-secret-store-service/inject-secrets-in-to-pods-on-vks-clusters(1).html)).
  MEHO does not need to manage these.

## Where the API reference is

| Source | What it has for the Secret Store |
|---|---|
| `vmware/vcf-api-specs`, tags `9.1.0.0` and `9.1.1.0` | No Secret Store file. A search of every OpenAPI file in both tags (VCF Operations and its parts, fleet lifecycle, SDDC Manager, SDDC lifecycle, VCF Installer, NSX, vSAN data protection, vCenter) finds no Secret Store path and no "secret store" text. |
| `vmware/vcf-api-specs`, `specifications/vsphere/openapi/automation/vcenter.yaml` (both tags) | The Supervisor services API under `/api/vcenter/namespace-management/...` (details below). It is the same API for every Supervisor service. It has no Secret Store text. |
| [Broadcom developer portal](https://developer.broadcom.com/xapis) | No API reference with "secret", "secret store", "vault" or "supervisor" in its name. |
| Broadcom TechDocs, VCF 9.1 service guide | How to install and use the service. The 9.1 pages name no REST path. The 9.0 [VCF CLI page](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-0/using-supervisor-services/using-secret-store-service-with-vsphere-supervisor/creating-and-managing-secrets/create-a-secret-for-a-secret-store-on-a-supervisor.html) gives the `KeyValueSecret` kind and the `vcf secret` commands. |
| [Secret Store package repository](https://packages.broadcom.com/artifactory/vsphere-distro/vsphere/iaas/secret-store/) | The service definition file for each version (`secret-store-service-definition.yaml`). It gives the service name `secret-store.vsphere.vmware.com` and the version string. |

## How to log in

- **vCenter REST** (install state and version): the vSphere connector logs in
  as it does today. Nothing new is needed. The reads need the `System.Read`
  privilege on the Supervisor (`vcenter.yaml`, operation
  `Vcenter.NamespaceManagement.Supervisors.SupervisorServices_list`).
- **Supervisor Kubernetes API** (secrets): the Kubernetes connector logs in
  to the Supervisor as it does today, with a vSphere SSO user name and
  password and `POST /wcp/login`. See `docs/codebase/connectors-kubernetes.md`,
  part "vSphere Supervisor (WCP) SSO auth mode". Nothing new is needed.
- **Not confirmed:** which vSphere role a user needs to read and write
  `KeyValueSecret` objects in a namespace. The vendor pages do not say. The
  connector Task checks it on a lab Supervisor.

## How to detect the version

- `GET /api/vcenter/namespace-management/supervisors/{supervisor}/supervisor-services/{supervisorService}`
  returns `current_version`, `desired_version`, `config_status`
  (`CONFIGURING`, `REMOVING`, `CONFIGURED` or `ERROR`) and `messages`. The
  call was added in vSphere API 9.0 (`vcenter.yaml`, 9.1 tags).
- `GET /api/vcenter/namespace-management/supervisors/{supervisor}/supervisor-services`
  lists every service on a Supervisor, with the same version and state
  fields. MEHO looks for the Secret Store in this list.
- The service name in the package files is `secret-store.vsphere.vmware.com`.
  The versions in the package repository are, for example, `9.1.0+25367485`,
  `9.1.0-25416073` and `9.1.1-25710217`.
- **Not confirmed:** that vCenter uses `secret-store.vsphere.vmware.com` as
  the `supervisorService` id. For other services in the Carvel package format
  (a package format for Kubernetes apps), the id is the package name. The
  connector Task checks it on a lab Supervisor.
- `GET /api/vcenter/namespace-management/supervisor-services/{supervisorService}/versions`
  lists the versions vCenter knows, each `ACTIVATED` or `DEACTIVATED`. This
  shows if a newer version is ready to install.
- The service has no version call of its own.

## What operators do with the service every week

The vendor pages list these tasks. That they happen weekly is our reading,
not a vendor statement.

- Check that the service runs on each Supervisor, and has no error.
- See which secrets each namespace has.
- Add a secret for a new app, or change one (for example a new database
  password).
- Delete a secret that no app uses any more.
- Check the version, and see if a newer version is ready.
- When an app says "permission denied", look at the service's pod
  (`secret-store-0`) and its log. A Broadcom article describes one such case
  ([KB 415914](https://knowledge.broadcom.com/external/article/415914)).
  MEHO's existing `k8s.pod.list` and `k8s.logs` already cover this.

## The usual work

Risk levels: a **read** is `safe` and runs at once. A **change** waits until a
second person approves. The ids are proposals. The connector Task picks the
final names.

| Operation (proposed id) | Connector | What it does | Vendor call | Risk |
|---|---|---|---|---|
| `vmware.composite.secret_store.status` | `vmware-rest` | Is the service installed on this Supervisor? Its current and desired version, state and messages. | `GET /api/vcenter/namespace-management/supervisors/{supervisor}/supervisor-services/{supervisorService}` (`vcenter.yaml`, 9.1 tags). Id **not confirmed** (see above). | read |
| `vmware.composite.secret_store.versions` | `vmware-rest` | The versions vCenter knows, and which are active. | `GET /api/vcenter/namespace-management/supervisor-services/{supervisorService}/versions` (`vcenter.yaml`, 9.1 tags). | read |
| `k8s.secret_store.secret.list` | `k8s` | The secrets in a namespace: name, key names, created and changed time. Never the values. | Kubernetes list of `KeyValueSecret`, group `secretstore.vmware.com`, version `v1alpha1`. The plural name (likely `keyvaluesecrets`) is **not confirmed**. | read |
| `k8s.secret_store.secret.info` | `k8s` | One secret: name and key names. Never the values. | Kubernetes get of one `KeyValueSecret` (same group and version). | read |
| `k8s.secret_store.secret.put` | `k8s` | Add a secret, or replace one. Each value comes from a secret MEHO already stores (a reference), not from the call. | Kubernetes create, or replace, of a `KeyValueSecret` with `spec.name` and `spec.data` (a list of `key` and `value`). Shape from the [VCF CLI page](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-0/using-supervisor-services/using-secret-store-service-with-vsphere-supervisor/creating-and-managing-secrets/create-a-secret-for-a-secret-store-on-a-supervisor.html). | change |
| `k8s.secret_store.secret.delete` | `k8s` | Delete a secret. | Kubernetes delete of one `KeyValueSecret`. | change |

**Notes**

1. **Replace means replace all.** The vendor says an update "completely
   overwrites the existing secret". The caller must send every key, not
   only the changed one
   ([VCF CLI page](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-0/using-supervisor-services/using-secret-store-service-with-vsphere-supervisor/creating-and-managing-secrets/create-a-secret-for-a-secret-store-on-a-supervisor.html)).
   The preview of `put` shows which keys stay, which are new and which go
   away.
2. **Not confirmed: that the VCF CLI uses the Kubernetes API.** The vendor
   gives the `KeyValueSecret` kind and API version and says the service
   "supports Kubernetes-native APIs". It does not show `kubectl` use. The
   connector Task checks on a lab Supervisor that the kind is served
   (`k8s.crd.list` shows it) and that list, get, create, replace and delete
   work.
3. **Not confirmed: whether a get answers with the values.** If the
   Supervisor sends `spec.data` values back on a read, the new reads drop
   them before the answer leaves the connector.
4. The `KeyValueSecret` shape is from the 9.0 VCF CLI page. The 9.1 guide has
   no CLI page. The connector Task checks the 9.1 shape in the lab.

## Why not something else

- **Its own connector.** There is no API of its own to call. A new connector
  would only wrap the two APIs above, which MEHO already logs in to.
- **The OpenBao API.** The service exposes a control-plane address
  (`virtualIP` in the service definition), but no vendor page tells
  operators to call it, how to log in to it, or which paths are stable.
  Using it would tie MEHO to an inside detail that can change in any
  release.
- **The VCF CLI (`vcf secret ...`).** It is a tool for people, not an API.
  Calling a CLI from MEHO would add a binary to the backplane and give no
  extra power over the Kubernetes API.
- **VCF Automation Service Management.** It installs the service. No public
  API reference for that page was found, and the install is a rare, one-time
  job. It stays out of the usual work.
- **VCF Operations.** The VCF Operations 9.1 files (including the
  `/suite-api/api/fleet-management/...` paths) have no Secret Store path.

## Out of scope

- Installing, upgrading or removing the service. These are rare. The
  vCenter calls exist (`POST` and `PUT` on
  `/api/vcenter/namespace-management/supervisors/{supervisor}/supervisor-services`
  and `.../{supervisorService}`). They can be added later if operators ask.
- The per-cluster login paths, policies and roles. The service makes them
  by itself.

## What happens next

1. The program owner agrees this record in the pull request.
2. A connector Task adds the six operations: two on `vmware-rest`, four on
   `k8s`. It first checks every **not confirmed** fact on a lab Supervisor
   with the service installed.
