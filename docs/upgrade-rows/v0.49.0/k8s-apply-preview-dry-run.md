## Note
A new switch `K8S_APPLY_PREVIEW_DRY_RUN`, off by default. With it on, `preview_operation` for `k8s.apply` asks the cluster what the apply would do: it sends a server-side apply with `dryRun=All` for every document of the manifest, with the target's own cluster account. Nothing is stored on the cluster. The preview then shows for each document whether the apply creates it, changes it or is refused. Only a caller with the role Operator or higher, whom the policy does not refuse the apply, gets this dry run. The apply itself still waits for approval as before. With the switch off, nothing changes.

## Action
None. To turn it on, set the switch with Helm: `extraEnv: [{name: K8S_APPLY_PREVIEW_DRY_RUN, value: "true"}]`. The cluster account of each Kubernetes target already needs the rights of the real apply; the dry run needs the same rights.
