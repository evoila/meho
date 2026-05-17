// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package k8s

import (
	"github.com/spf13/cobra"
)

// Inventory op IDs registered by G3.2-T1 / T2 (#321 / #322). All
// read-only. about / namespace-list / node-list take no params; ls
// takes an optional path.
const (
	opAbout         = "k8s.about"
	opLs            = "k8s.ls"
	opNamespaceList = "k8s.namespace.list"
	opNodeList      = "k8s.node.list"
)

// newAboutCmd returns `meho k8s about`. Top-level convenience verb
// over `k8s.about` (the canary op registered by G3.2-T1).
func newAboutCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "about",
		Short: "Identify the cluster (product / version / platform)",
		Long: "about dispatches op_id=\"k8s.about\" against connector_id=\n" +
			"\"k8s-1.x\". The result envelope carries the cluster's product\n" +
			"slug (rke2 / k3s / eks / gke / aks / vanilla), full git_version,\n" +
			"build date, major/minor, platform, and go_version — derived from\n" +
			"a single `GET /version` against the API server. No params.\n\n" +
			"Pair with k8s.namespace.list / k8s.node.list for the operator-\n" +
			"facing \"what is this cluster?\" question.\n\n" +
			"Exit codes mirror meho operation call.",
		Example:       "  meho k8s about --target rke2-meho\n  meho k8s about --target rke2-meho --json | jq .result.git_version",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
	}
	f := bindK8sAddrFlags(cmd)
	cmd.RunE = func(cmd *cobra.Command, _ []string) error {
		return dispatchVerb(cmd, f, opAbout, nil)
	}
	return cmd
}

// newLsCmd returns `meho k8s ls [path]`. Optional positional path
// argument (defaults to "/" server-side when omitted).
func newLsCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "ls [path]",
		Short: "Inventory walker (cluster root / namespace summary / kind list)",
		Long: "ls dispatches op_id=\"k8s.ls\" against connector_id=\n" +
			"\"k8s-1.x\". [path] is one of:\n" +
			"  - \"/\" (or omitted): cluster root - namespace names +\n" +
			"    fixed cluster-scoped kind list.\n" +
			"  - \"/<namespace>\": kind->count summary for the namespace\n" +
			"    (one probed kind per round-trip via limit=1 + the API\n" +
			"    server's remaining_item_count field).\n" +
			"  - \"/<namespace>/<kind>\": forwards through the dispatcher\n" +
			"    to k8s.<kind>.list (the kind-specific list op). Kinds\n" +
			"    whose list op has not shipped surface as the dispatcher's\n" +
			"    structured unknown_op envelope.\n\n" +
			"Mirrors govc ls /; use as the entry point when the operator's\n" +
			"question is exploratory.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho k8s ls --target rke2-meho\n" +
			"  meho k8s ls --target rke2-meho /argocd\n" +
			"  meho k8s ls --target rke2-meho /argocd/pods",
		Args:          cobra.MaximumNArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
	}
	f := bindK8sAddrFlags(cmd)
	cmd.RunE = func(cmd *cobra.Command, args []string) error {
		if len(args) == 0 {
			// Omit `path` entirely so the dispatcher applies the
			// schema's `default: "/"` rather than sending an empty
			// string the handler would parse as the root anyway.
			return dispatchVerb(cmd, f, opLs, nil)
		}
		return dispatchVerb(cmd, f, opLs, map[string]any{"path": args[0]})
	}
	return cmd
}

// newNamespaceCmd returns the `meho k8s namespace` parent + its `list`
// verb. Single-verb sub-tree, kept under the noun group so the help
// surface stays predictable as future namespace verbs land.
func newNamespaceCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "namespace",
		Short:        "Namespace verbs (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newNamespaceListCmd())
	return cmd
}

func newNamespaceListCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List Kubernetes namespaces (name / status / age / labels)",
		Long: "list dispatches op_id=\"k8s.namespace.list\" against\n" +
			"connector_id=\"k8s-1.x\". Read-only; result carries the\n" +
			"namespace roster with phase, age, and labels per row. No\n" +
			"params.\n\n" +
			"Exit codes mirror meho operation call.",
		Example:       "  meho k8s namespace list --target rke2-meho",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
	}
	f := bindK8sAddrFlags(cmd)
	cmd.RunE = func(cmd *cobra.Command, _ []string) error {
		return dispatchVerb(cmd, f, opNamespaceList, nil)
	}
	return cmd
}

// newNodeCmd returns the `meho k8s node` parent + its `list` verb.
func newNodeCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "node",
		Short:        "Node verbs (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newNodeListCmd())
	return cmd
}

func newNodeListCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List cluster nodes (status / roles / version / taints)",
		Long: "list dispatches op_id=\"k8s.node.list\" against connector_id=\n" +
			"\"k8s-1.x\". Read-only; result carries per-node Ready status,\n" +
			"roles (derived from node-role.kubernetes.io/<role> labels),\n" +
			"kubelet version, kernel/OS, internal IP, taints, and labels.\n" +
			"No params.\n\n" +
			"Exit codes mirror meho operation call.",
		Example:       "  meho k8s node list --target rke2-meho",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
	}
	f := bindK8sAddrFlags(cmd)
	cmd.RunE = func(cmd *cobra.Command, _ []string) error {
		return dispatchVerb(cmd, f, opNodeList, nil)
	}
	return cmd
}
