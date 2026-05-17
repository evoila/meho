// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package k8s

import (
	"github.com/spf13/cobra"
)

// k8s.logs op id registered by G3.2-T5 (#325). Single-shot fetch
// (no streaming in v0.2 - see docs/codebase/kubernetes-connector.md
// "Known issues").
const opLogs = "k8s.logs"

// newLogsCmd returns `meho k8s logs <pod>`. Mirrors `kubectl logs`
// flag shape: --tail / --since / --previous / --container.
func newLogsCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "logs <pod>",
		Short: "Fetch a chunk of pod logs (kubectl logs - non-streaming)",
		Long: "logs dispatches op_id=\"k8s.logs\" against connector_id=\n" +
			"\"k8s-1.x\". <pod> is the pod name; exact match wins, otherwise\n" +
			"treated as a unique prefix within the namespace. --namespace is\n" +
			"required. --container is required for multi-container pods\n" +
			"(auto-selected when the pod has only one container). --tail\n" +
			"defaults to 100 and is capped at 5000 (use --since for time-\n" +
			"bounded slices instead of larger tails). --since accepts a\n" +
			"duration string ('5m', '1h', '24h', '7d'). --previous fetches\n" +
			"logs from the previous container instance (after a restart).\n\n" +
			"Non-streaming: the response body is capped at 1 MiB serialised;\n" +
			"oversize payloads truncate line-boundary from the FRONT (most-\n" +
			"recent lines kept) and the result carries truncated=true with\n" +
			"truncated_byte_count. For live tailing, operators continue\n" +
			"using kubectl-vcf.sh -f until v0.2.next ships the streaming\n" +
			"transport.\n\n" +
			"Audit row records the request params only; log contents are\n" +
			"never written to the audit_log payload.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho k8s logs --target rke2-meho --namespace argocd argocd-server-7c4d8f6b6-abcde\n" +
			"  meho k8s logs --target rke2-meho --namespace argocd --container argocd-server --tail 500 argocd-server\n" +
			"  meho k8s logs --target rke2-meho --namespace argocd --since 15m --previous argocd-server",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
	}
	f := bindK8sAddrFlags(cmd)
	var (
		namespace string
		container string
		tail      int
		since     string
		previous  bool
	)
	cmd.Flags().StringVar(&namespace, "namespace", "",
		"namespace the pod lives in (required)")
	cmd.Flags().StringVar(&container, "container", "",
		"container name within the pod (required for multi-container pods)")
	cmd.Flags().IntVar(&tail, "tail", 0,
		"lines from the end of the log (default 100, capped at 5000)")
	cmd.Flags().StringVar(&since, "since", "",
		"duration string for time-bounded fetch (e.g. 5m, 1h, 24h, 7d)")
	cmd.Flags().BoolVar(&previous, "previous", false,
		"fetch logs from the previous container instance (after a restart)")
	_ = cmd.MarkFlagRequired("namespace")
	cmd.RunE = func(cmd *cobra.Command, args []string) error {
		params := map[string]any{
			"pod_name":  args[0],
			"namespace": namespace,
		}
		if container != "" {
			params["container"] = container
		}
		if cmd.Flags().Changed("tail") {
			params["tail"] = tail
		}
		if since != "" {
			params["since"] = since
		}
		if previous {
			params["previous"] = true
		}
		return dispatchVerb(cmd, f, opLogs, params)
	}
	return cmd
}
