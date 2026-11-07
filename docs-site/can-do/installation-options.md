# Ways to install MEHO

MEHO runs on your own systems. There is no hosted service. This page
lists the ways to install it and the state of each.

## What it does

Every way puts the same parts in your network: MEHO, PostgreSQL (the
database), Keycloak (the login server) and OpenBao (the password safe).
After the install, the steps to use MEHO are the same.

| Way | For whom | State |
|---|---|---|
| **Helm chart** on your Kubernetes or OpenShift | Teams that run their own Kubernetes | **Shipped.** You can use it today. |
| **Linux install with a built-in Kubernetes** | Hyper-V, cloud VMs, physical servers, sites with no internet | **Shipped as a test version.** Each release has one download and one install script. Use it for tests and pilots. Ask evoila before you use it in production. |
| **Appliance** (one OVA file for VMware) | VMware and VCF teams | **Planned.** Not released. The build script exists, but there is no download. Ask evoila. |

## Product versions

- **Helm chart:** a Kubernetes cluster that can run the chart. The
  [install trail](../install/index.md) lists what you need.
- **Linux install:** x86-64 with systemd. Ubuntu 24.04 is tested.
  Another Linux with systemd gets a "not tested" warning.
- **Appliance:** VMware vSphere. Details come with the release.

## State

- Helm chart: shipped.
- Linux install: shipped, in testing.
- Appliance: planned.

## Operator guide

- [Ways to install MEHO](../install/ways-to-install.md): the steps and
  the limits of each way.
- [The install trail](../install/index.md): the Helm chart, step by
  step.
- [Deployment shapes](../deployment-shapes.md): how the parts fit
  together.
