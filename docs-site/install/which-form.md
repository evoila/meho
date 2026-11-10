# Which install form is right for you?

MEHO has three install forms. An install form is how MEHO gets onto a
machine. Every form puts the same four parts in your network: MEHO,
PostgreSQL (the database), Keycloak (the login server) and OpenBao (the
password safe). After the install, the guide goes on with the same
steps for every form.

## Answer three questions

Start at the first question and stop at the first "yes".

1. **Do you run VMware vSphere or VMware Cloud Foundation (VCF)?**
   Yes: the [VMware appliance](form-1-vmware-appliance.md) is not
   released yet. Use form 3 ([your Kubernetes](form-3-your-kubernetes.md))
   if you have one. This includes your own VKS cluster (vSphere
   Kubernetes Service): see
   [VKS](form-3-your-kubernetes.md#vks-vsphere-kubernetes-service).
   Otherwise use form 2, the [Linux server](form-2-linux-server.md),
   which can be a VM in vSphere. Ask evoila about the appliance.
2. **Do you run your own Kubernetes or OpenShift cluster, and may you
   install a Helm chart on it?**
   Yes: use form 3, [your Kubernetes](form-3-your-kubernetes.md).
3. **Anything else** (Hyper-V, a cloud VM, a physical server, a site
   with no internet): use form 2, the
   [Linux server](form-2-linux-server.md).

## The three forms

| Form | You bring | State |
|---|---|---|
| [1. VMware appliance](form-1-vmware-appliance.md) | A vCenter, a name, an address, DNS and a time server | In progress, not released. |
| [2. Linux server](form-2-linux-server.md) | One Linux server with a DNS name | Available, in testing. |
| [3. Your Kubernetes](form-3-your-kubernetes.md) | A Kubernetes cluster, an ingress controller, a storage class and a DNS name | Available. |

## Tested platforms

| Form | Tested | Should work, not tested |
|---|---|---|
| 1. VMware appliance | In progress, not released. | VMware vSphere, as the release notes say when it ships. |
| 2. Linux server | Ubuntu 24.04 on x86-64. | Another Linux with systemd. The install shows a "not tested" warning and goes on. |
| 3. Your Kubernetes | Kubernetes 1.28 or newer on kind, and on RKE2 (the Kubernetes inside form 2). VKS (vSphere Kubernetes Service) on VMware Cloud Foundation 9.1, with Kubernetes 1.35. | Other Kubernetes that follow the standard, and OpenShift. |

Not on the list? Ask evoila before you use it in production.

## What is the same for every form

After the install chapter, follow these pages in this order:

1. [The setup page and the settings file](setup-page.md)
2. [The first-start checks](first-start-checks.md)
3. [Your first day](first-day.md)
4. [Set up login](login-setup.md)
5. [Tools for your laptop](laptop-tools.md)
6. [Back up, restore and update](backup-restore-update.md)
7. [Common problems](common-problems.md)

An install form is not a deployment shape. The form is how MEHO gets
onto a machine. The [deployment shape](../deployment-shapes.md) is how
your network and your password store look.
