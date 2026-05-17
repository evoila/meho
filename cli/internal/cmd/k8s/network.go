// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package k8s

import (
	"github.com/spf13/cobra"
)

// Network / config / observability op IDs registered by G3.2-T4
// (#324). All read-only; all require --namespace per the backend
// schema.
const (
	opServiceList   = "k8s.service.list"
	opIngressList   = "k8s.ingress.list"
	opConfigmapList = "k8s.configmap.list"
	opConfigmapInfo = "k8s.configmap.info"
	opEventList     = "k8s.event.list"
)

// namespaceVerbBuilder factors the shared "namespace-required list
// verb" shape used by service / ingress / configmap. Each verb
// differs only in op_id + help text.
func namespaceVerbBuilder(
	use, opID, short, long, example string,
) *cobra.Command {
	cmd := &cobra.Command{
		Use:           use,
		Short:         short,
		Long:          long + "\n\nExit codes mirror meho operation call.",
		Example:       example,
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
	}
	f := bindK8sAddrFlags(cmd)
	var namespace string
	cmd.Flags().StringVar(&namespace, "namespace", "",
		"namespace to list within (required)")
	_ = cmd.MarkFlagRequired("namespace")
	cmd.RunE = func(cmd *cobra.Command, _ []string) error {
		return dispatchVerb(cmd, f, opID, map[string]any{"namespace": namespace})
	}
	return cmd
}

// newServiceCmd returns the `meho k8s service` parent + `list` verb.
func newServiceCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "service",
		Short:        "Service verbs (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(namespaceVerbBuilder(
		"list", opServiceList,
		"List Services in a namespace (kubectl get svc)",
		"list dispatches op_id=\"k8s.service.list\" against connector_id=\n"+
			"\"k8s-1.x\". --namespace is required. Result rows project to\n"+
			"{name, namespace, type, cluster_ip, external_ips, ports:\n"+
			"[{name, port, target_port, protocol}], selector}.",
		"  meho k8s service list --target rke2-meho --namespace argocd",
	))
	return cmd
}

// newIngressCmd returns the `meho k8s ingress` parent + `list` verb.
func newIngressCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "ingress",
		Short:        "Ingress verbs (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(namespaceVerbBuilder(
		"list", opIngressList,
		"List Ingresses in a namespace (kubectl get ingress)",
		"list dispatches op_id=\"k8s.ingress.list\" against connector_id=\n"+
			"\"k8s-1.x\". --namespace is required. Result rows project to\n"+
			"{name, namespace, class, hosts, tls_hosts, rules: [{host,\n"+
			"paths: [{path, path_type, service, port}]}]}. hosts /\n"+
			"tls_hosts are deduplicated sorted unions across the rule set.",
		"  meho k8s ingress list --target rke2-meho --namespace argocd",
	))
	return cmd
}

// newConfigmapCmd returns the `meho k8s configmap` parent + list/info
// verbs. configmap.list returns key NAMES only (no values); configmap.
// info returns full data and audits as a separate per-cm read.
func newConfigmapCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "configmap",
		Short:        "ConfigMap verbs (list keys-only / info full data)",
		SilenceUsage: true,
	}
	cmd.AddCommand(namespaceVerbBuilder(
		"list", opConfigmapList,
		"List ConfigMaps in a namespace - KEY NAMES ONLY, no values",
		"list dispatches op_id=\"k8s.configmap.list\" against connector_id=\n"+
			"\"k8s-1.x\". --namespace is required. Result rows project to\n"+
			"{name, namespace, keys, age_seconds} where `keys` is the\n"+
			"sorted union of `data` + `binary_data` key names. Values are\n"+
			"NEVER included in this op - the keys-only shape protects\n"+
			"against bulk-broadcasting config data. Use `k8s configmap info\n"+
			"<name> --namespace <ns>` to fetch values for one ConfigMap;\n"+
			"that op records a per-configmap audit row.",
		"  meho k8s configmap list --target rke2-meho --namespace argocd",
	))
	cmd.AddCommand(newConfigmapInfoCmd())
	return cmd
}

func newConfigmapInfoCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "info <name>",
		Short: "Fetch one ConfigMap including all key=value data",
		Long: "info dispatches op_id=\"k8s.configmap.info\" against\n" +
			"connector_id=\"k8s-1.x\". <name> is the configmap name (exact\n" +
			"match; no prefix resolution). --namespace is required. Result\n" +
			"carries {name, namespace, data (text key->value), binary_data\n" +
			"(key->base64 string), metadata: {labels, annotations,\n" +
			"age_seconds}}. Audited as op_class=read; G6.3 may upgrade\n" +
			"sensitively-named configmaps to op_class=sensitive-read.\n\n" +
			"Exit codes mirror meho operation call.",
		Example:       "  meho k8s configmap info --target rke2-meho --namespace argocd argocd-cmd-params-cm",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
	}
	f := bindK8sAddrFlags(cmd)
	var namespace string
	cmd.Flags().StringVar(&namespace, "namespace", "",
		"namespace the configmap lives in (required)")
	_ = cmd.MarkFlagRequired("namespace")
	cmd.RunE = func(cmd *cobra.Command, args []string) error {
		params := map[string]any{
			"name":      args[0],
			"namespace": namespace,
		}
		return dispatchVerb(cmd, f, opConfigmapInfo, params)
	}
	return cmd
}

// newEventCmd returns the `meho k8s event` parent + `list` verb.
// Event list supports --field-selector and --limit on top of the
// required --namespace.
func newEventCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "event",
		Short:        "Event verbs (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newEventListCmd())
	return cmd
}

func newEventListCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List recent events in a namespace (kubectl get events)",
		Long: "list dispatches op_id=\"k8s.event.list\" against connector_id=\n" +
			"\"k8s-1.x\". --namespace is required. --field-selector accepts\n" +
			"the standard k8s field selectors (e.g. type=Warning,\n" +
			"involvedObject.kind=Pod). --limit caps the row count (default\n" +
			"100, capped at 500). Rows are sorted most-recent-first and\n" +
			"projected to {name, namespace, type, reason, message,\n" +
			"involved_object: {kind, name, namespace}, source, count,\n" +
			"first_seen_seconds, last_seen_seconds}.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho k8s event list --target rke2-meho --namespace argocd --field-selector type=Warning\n" +
			"  meho k8s event list --target rke2-meho --namespace kube-system --limit 25",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
	}
	f := bindK8sAddrFlags(cmd)
	var (
		namespace     string
		fieldSelector string
		limit         int
	)
	cmd.Flags().StringVar(&namespace, "namespace", "",
		"namespace to list within (required)")
	cmd.Flags().StringVar(&fieldSelector, "field-selector", "",
		"k8s field selector forwarded server-side (e.g. type=Warning)")
	cmd.Flags().IntVar(&limit, "limit", 0,
		"maximum rows to return (server default 100, capped at 500)")
	_ = cmd.MarkFlagRequired("namespace")
	cmd.RunE = func(cmd *cobra.Command, _ []string) error {
		params := map[string]any{"namespace": namespace}
		if fieldSelector != "" {
			params["field_selector"] = fieldSelector
		}
		if cmd.Flags().Changed("limit") {
			params["limit"] = limit
		}
		return dispatchVerb(cmd, f, opEventList, params)
	}
	return cmd
}
