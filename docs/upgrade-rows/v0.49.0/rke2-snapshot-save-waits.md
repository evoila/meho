## Note
**Action needed.** `rke2.etcd-snapshot.save` is risk level `caution` and waits for a person to approve each call: from the web console, a person's token, an agent login and a hosted agent run. The snapshot runs as root and writes a file with all cluster data, Secrets too, on the server node. A standing grant of a service login still covers it. A run approval does not cover it.

## Action
Tell the teams whose agents or runbooks save RKE2 snapshots that each call now waits for a person. Plan for a person to approve each call in the console or the CLI. A scheduled snapshot job of a service login needs a standing grant for `rke2.etcd-snapshot.save` (`meho service-principals grants list`). No schema action.
