// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package k8s

import (
	"github.com/spf13/cobra"
)

// Workload op IDs registered by G3.2-T3 (#323). pod / deployment list
// take namespace XOR all_namespaces; info ops require name + namespace.
const (
	opPodList        = "k8s.pod.list"
	opPodInfo        = "k8s.pod.info"
	opDeploymentList = "k8s.deployment.list"
	opDeploymentInfo = "k8s.deployment.info"
)

// listFlags carries the per-list-verb flag set: namespace selector
// (--namespace XOR --all-namespaces) plus the optional filters.
// Backend schema enforces the XOR via JSON Schema oneOf; the CLI
// surfaces the constraint as a client-side error so the operator
// sees an argv-level message rather than a schema-validation
// round-trip.
type listFlags struct {
	namespace      string
	allNamespaces  bool
	labelSelector  string
	fieldSelector  string
	limit          int
	continueToken  string
	limitSet       bool
	allNamespacesS bool
}

func bindListFlags(cmd *cobra.Command, lf *listFlags) {
	cmd.Flags().StringVar(&lf.namespace, "namespace", "",
		"namespace to list within (mutually exclusive with --all-namespaces)")
	cmd.Flags().BoolVar(&lf.allNamespaces, "all-namespaces", false,
		"list across every namespace (mutually exclusive with --namespace)")
	cmd.Flags().StringVar(&lf.labelSelector, "label-selector", "",
		"k8s label selector forwarded server-side (e.g. app=argocd-server)")
	cmd.Flags().StringVar(&lf.fieldSelector, "field-selector", "",
		"k8s field selector forwarded server-side (e.g. status.phase=Running)")
	cmd.Flags().IntVar(&lf.limit, "limit", 0,
		"server-side ?limit= for paginated reads (1..1000)")
	cmd.Flags().StringVar(&lf.continueToken, "continue-token", "",
		"pagination cursor from a prior response's next_continue field")
}

// listParams folds the listFlags into the params map per the backend's
// K8S_POD_LIST / K8S_DEPLOYMENT_LIST schemas. Validates the namespace-
// XOR-all-namespaces constraint client-side so the operator gets the
// error before the round-trip.
func listParams(cmd *cobra.Command, lf *listFlags) (map[string]any, error) {
	lf.limitSet = cmd.Flags().Changed("limit")
	lf.allNamespacesS = cmd.Flags().Changed("all-namespaces")
	hasNamespace := lf.namespace != ""
	hasAll := lf.allNamespacesS && lf.allNamespaces
	if !hasNamespace && !hasAll {
		return nil, errMissingNamespaceSelector
	}
	if hasNamespace && hasAll {
		return nil, errBothNamespaceSelectors
	}
	params := map[string]any{}
	if hasNamespace {
		params["namespace"] = lf.namespace
	}
	if hasAll {
		params["all_namespaces"] = true
	}
	if lf.labelSelector != "" {
		params["label_selector"] = lf.labelSelector
	}
	if lf.fieldSelector != "" {
		params["field_selector"] = lf.fieldSelector
	}
	if lf.limitSet {
		params["limit"] = lf.limit
	}
	if lf.continueToken != "" {
		params["continue_token"] = lf.continueToken
	}
	return params, nil
}

// errMissingNamespaceSelector is returned when the operator omits both
// --namespace and --all-namespaces. Exported as a package var for the
// test suite; not wrapped in output.Unexpected so the renderer reads
// the same .Error() string that argv-level validation surfaces.
var (
	errMissingNamespaceSelector = newArgvError("either --namespace <ns> or --all-namespaces is required")
	errBothNamespaceSelectors   = newArgvError("--namespace and --all-namespaces are mutually exclusive")
)

type argvError struct{ msg string }

func (e *argvError) Error() string { return e.msg }

func newArgvError(msg string) *argvError { return &argvError{msg: msg} }

// newPodCmd returns the `meho k8s pod` parent + its list/info verbs.
func newPodCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "pod",
		Short:        "Pod verbs (list / info)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newPodListCmd())
	cmd.AddCommand(newPodInfoCmd())
	return cmd
}

func newPodListCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List pods (kubectl get pods)",
		Long: "list dispatches op_id=\"k8s.pod.list\" against connector_id=\n" +
			"\"k8s-1.x\". Exactly one of --namespace <ns> or --all-namespaces\n" +
			"is required (the backend schema enforces a JSON Schema oneOf).\n" +
			"--label-selector / --field-selector forward the standard k8s\n" +
			"selectors server-side. --limit pages the read at the API\n" +
			"server's ?limit= boundary; --continue-token resumes a previous\n" +
			"page. Result rows project to {name, namespace, status, ready,\n" +
			"restarts, age_seconds, node, ip}.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho k8s pod list --target rke2-meho --namespace argocd\n" +
			"  meho k8s pod list --target rke2-meho --all-namespaces --label-selector app=argocd-server\n" +
			"  meho k8s pod list --target rke2-meho --namespace kube-system --field-selector status.phase=Running --limit 50",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
	}
	f := bindK8sAddrFlags(cmd)
	lf := &listFlags{}
	bindListFlags(cmd, lf)
	cmd.RunE = func(cmd *cobra.Command, _ []string) error {
		params, err := listParams(cmd, lf)
		if err != nil {
			return err
		}
		return dispatchVerb(cmd, f, opPodList, params)
	}
	return cmd
}

func newPodInfoCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "info <name>",
		Short: "Full detail for one pod (kubectl describe pod)",
		Long: "info dispatches op_id=\"k8s.pod.info\" against connector_id=\n" +
			"\"k8s-1.x\". <name> is the pod name; exact match wins, otherwise\n" +
			"the handler treats it as a unique prefix within the namespace.\n" +
			"Ambiguous prefixes return a structured error listing the\n" +
			"candidate pod names. --namespace is required (the backend\n" +
			"schema enforces this).\n\n" +
			"Result carries spec/status sub-objects: container_statuses\n" +
			"(ready / restart_count / state), resource requests/limits,\n" +
			"node assignment, IPs, conditions, volumes (name + source kind).\n\n" +
			"Exit codes mirror meho operation call.",
		Example:       "  meho k8s pod info --target rke2-meho --namespace argocd argocd-server-7c4d8f6b6-abcde",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
	}
	f := bindK8sAddrFlags(cmd)
	var namespace string
	cmd.Flags().StringVar(&namespace, "namespace", "",
		"namespace the pod lives in (required)")
	_ = cmd.MarkFlagRequired("namespace")
	cmd.RunE = func(cmd *cobra.Command, args []string) error {
		params := map[string]any{
			"pod_name":  args[0],
			"namespace": namespace,
		}
		return dispatchVerb(cmd, f, opPodInfo, params)
	}
	return cmd
}

// newDeploymentCmd returns the `meho k8s deployment` parent + its
// list/info verbs.
func newDeploymentCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "deployment",
		Short:        "Deployment verbs (list / info)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newDeploymentListCmd())
	cmd.AddCommand(newDeploymentInfoCmd())
	return cmd
}

func newDeploymentListCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List deployments (kubectl get deployments)",
		Long: "list dispatches op_id=\"k8s.deployment.list\" against\n" +
			"connector_id=\"k8s-1.x\". Exactly one of --namespace <ns> or\n" +
			"--all-namespaces is required (backend schema oneOf). Selectors\n" +
			"and pagination flags mirror `pod list`. Result rows project to\n" +
			"{name, namespace, replicas, ready_replicas, updated_replicas,\n" +
			"available_replicas, age_seconds, labels}.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho k8s deployment list --target rke2-meho --namespace argocd\n" +
			"  meho k8s deployment list --target rke2-meho --all-namespaces",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
	}
	f := bindK8sAddrFlags(cmd)
	lf := &listFlags{}
	bindListFlags(cmd, lf)
	cmd.RunE = func(cmd *cobra.Command, _ []string) error {
		params, err := listParams(cmd, lf)
		if err != nil {
			return err
		}
		return dispatchVerb(cmd, f, opDeploymentList, params)
	}
	return cmd
}

func newDeploymentInfoCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "info <name>",
		Short: "Full detail for one deployment (kubectl describe deployment)",
		Long: "info dispatches op_id=\"k8s.deployment.info\" against\n" +
			"connector_id=\"k8s-1.x\". <name> is the deployment name; exact\n" +
			"match or unique prefix within the namespace. --namespace is\n" +
			"required. Result carries spec (replicas / strategy / selector /\n" +
			"template containers) and status (replicas / conditions).\n\n" +
			"Exit codes mirror meho operation call.",
		Example:       "  meho k8s deployment info --target rke2-meho --namespace argocd argocd-server",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
	}
	f := bindK8sAddrFlags(cmd)
	var namespace string
	cmd.Flags().StringVar(&namespace, "namespace", "",
		"namespace the deployment lives in (required)")
	_ = cmd.MarkFlagRequired("namespace")
	cmd.RunE = func(cmd *cobra.Command, args []string) error {
		params := map[string]any{
			"deployment_name": args[0],
			"namespace":       namespace,
		}
		return dispatchVerb(cmd, f, opDeploymentInfo, params)
	}
	return cmd
}
