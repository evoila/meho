// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package argocd

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newAppCmd returns the `meho argocd app` parent with four sub-verbs:
// `list` (argocd.app.list), `get` (argocd.app.get), `diff`
// (argocd.app.diff), and `resource-tree` (argocd.app.resource_tree).
func newAppCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use: "app",
		Short: "ArgoCD Application sub-verbs (list, get, diff, resource-tree; " +
			"sync, rollback, set, refresh, delete)",
		SilenceUsage: true,
	}
	// Read verbs (G3.12-T3 #1392).
	cmd.AddCommand(newAppListCmd())
	cmd.AddCommand(newAppGetCmd())
	cmd.AddCommand(newAppDiffCmd())
	cmd.AddCommand(newAppResourceTreeCmd())
	// Approval-gated write verbs (G3.12-T4 #1405).
	cmd.AddCommand(newAppSyncCmd())
	cmd.AddCommand(newAppRollbackCmd())
	cmd.AddCommand(newAppSetCmd())
	cmd.AddCommand(newAppRefreshCmd())
	cmd.AddCommand(newAppDeleteCmd())
	return cmd
}

// newAppListCmd returns the `meho argocd app list` command.
//
// Maps to op_id `argocd.app.list`. GETs /api/v1/applications and returns
// the Applications as {items, metadata}. Optional --project (repeatable)
// maps to the op's `projects` filter; --selector maps to a Kubernetes
// label selector.
func newAppListCmd() *cobra.Command {
	var (
		targetName        string
		projects          []string
		selector          string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List ArgoCD Applications with their sync and health status",
		Long: "list dispatches argocd.app.list and renders the Applications as a\n" +
			"table of name / project / sync status / health status. --project\n" +
			"(repeatable) filters to one or more AppProjects; --selector applies\n" +
			"a Kubernetes label selector. --json emits the full OperationResult\n" +
			"envelope.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example: "  meho argocd app list --target rdc-argocd\n" +
			"  meho argocd app list --target rdc-argocd --project platform --project apps\n" +
			"  meho argocd app list --target rdc-argocd --selector team=payments --json | jq '.result.items[].metadata.name'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runAppList(cmd, targetName, projects, selector, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "",
		"target slug to dispatch against (required)")
	cmd.Flags().StringArrayVar(&projects, "project", nil,
		"filter to one or more AppProjects (repeatable)")
	cmd.Flags().StringVar(&selector, "selector", "",
		"Kubernetes label selector (e.g. team=payments,env=prod)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from the most recent `meho login`)")
	return cmd
}

func runAppList(
	cmd *cobra.Command,
	targetName string,
	projects []string,
	selector string,
	jsonOut bool,
	backplaneOverride string,
) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	params := map[string]any{}
	if len(projects) > 0 {
		params["projects"] = projects
	}
	if selector != "" {
		params["selector"] = selector
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "argocd.app.list", targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "argocd.app.list", r, jsonOut, printAppList)
}

func printAppList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s argocd.app.list — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	items, err := decodeItemsResult(r.Result)
	if err != nil || items == nil {
		fallbackResultRender(w, r)
		return
	}
	fmt.Fprintf(w, "  %-32s %-16s %-10s %s\n", "NAME", "PROJECT", "SYNC", "HEALTH")
	for _, app := range items {
		name := truncate(appName(app), 32)
		project := truncate(nestedString(app, "spec", "project"), 16)
		sync := nestedString(app, "status", "sync", "status")
		health := nestedString(app, "status", "health", "status")
		fmt.Fprintf(w, "  %-32s %-16s %-10s %s\n", name, project, sync, health)
	}
	fmt.Fprintf(w, "  (%d applications)\n", len(items))
}

// appName pulls metadata.name from an Application object.
func appName(app map[string]any) string {
	meta, ok := app["metadata"].(map[string]any)
	if !ok {
		return ""
	}
	return stringField(meta, "name")
}

// newAppGetCmd returns the `meho argocd app get` command.
//
// Maps to op_id `argocd.app.get`. GETs /api/v1/applications/{name} and
// returns the full Application object {metadata, spec, status}. --name is
// required; --project optionally scopes the lookup.
func newAppGetCmd() *cobra.Command {
	var (
		targetName        string
		appNameFlag       string
		project           string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "get",
		Short: "Read one ArgoCD Application's full spec and status by name",
		Long: "get dispatches argocd.app.get for the Application whose\n" +
			"metadata.name is --name (from `meho argocd app list`). Renders the\n" +
			"spec source/destination plus the sync/health summary; --json emits\n" +
			"the full OperationResult envelope. --project optionally scopes the\n" +
			"lookup to one AppProject.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example: "  meho argocd app get --target rdc-argocd --name platform-bootstrap\n" +
			"  meho argocd app get --target rdc-argocd --name platform-bootstrap --json | jq '.result.status'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runAppGet(cmd, targetName, appNameFlag, project, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "",
		"target slug to dispatch against (required)")
	cmd.Flags().StringVar(&appNameFlag, "name", "",
		"the Application's metadata.name (required)")
	cmd.Flags().StringVar(&project, "project", "",
		"optional AppProject to scope the lookup")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from the most recent `meho login`)")
	if err := cmd.MarkFlagRequired("name"); err != nil {
		panic(err) // programmer error: the flag is defined directly above
	}
	return cmd
}

func runAppGet(
	cmd *cobra.Command,
	targetName, appNameFlag, project string,
	jsonOut bool,
	backplaneOverride string,
) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	params := map[string]any{"name": appNameFlag}
	if project != "" {
		params["project"] = project
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "argocd.app.get", targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "argocd.app.get", r, jsonOut, printAppGet)
}

func printAppGet(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s argocd.app.get — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	app, err := decodeObject(r.Result)
	if err != nil || app == nil {
		fallbackResultRender(w, r)
		return
	}
	fmt.Fprintf(w, "  name:        %s\n", appName(app))
	fmt.Fprintf(w, "  project:     %s\n", nestedString(app, "spec", "project"))
	fmt.Fprintf(w, "  repoURL:     %s\n", nestedString(app, "spec", "source", "repoURL"))
	fmt.Fprintf(w, "  path:        %s\n", nestedString(app, "spec", "source", "path"))
	fmt.Fprintf(w, "  destination: %s/%s\n",
		nestedString(app, "spec", "destination", "server"),
		nestedString(app, "spec", "destination", "namespace"))
	fmt.Fprintf(w, "  sync:        %s\n", nestedString(app, "status", "sync", "status"))
	fmt.Fprintf(w, "  health:      %s\n", nestedString(app, "status", "health", "status"))
}

// newAppDiffCmd returns the `meho argocd app diff` command.
//
// Maps to op_id `argocd.app.diff`. GETs
// /api/v1/applications/{name}/managed-resources and returns the
// per-resource desired-vs-live drift as {items}. The read-only
// counterpart of the `argocd app diff` CLI.
func newAppDiffCmd() *cobra.Command {
	var (
		targetName        string
		appNameFlag       string
		project           string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "diff",
		Short: "Show the desired-vs-live drift for an ArgoCD Application",
		Long: "diff dispatches argocd.app.diff and renders the managed-resources\n" +
			"delta for the Application named --name — the read-only API-level\n" +
			"equivalent of `argocd app diff <app>`. Each row is a managed\n" +
			"resource with its group/kind/namespace/name and a modified flag.\n" +
			"--json emits the full OperationResult envelope.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example: "  meho argocd app diff --target rdc-argocd --name platform-bootstrap\n" +
			"  meho argocd app diff --target rdc-argocd --name platform-bootstrap --json | jq '.result.items'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runAppDiff(cmd, targetName, appNameFlag, project, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "",
		"target slug to dispatch against (required)")
	cmd.Flags().StringVar(&appNameFlag, "name", "",
		"the Application's metadata.name (required)")
	cmd.Flags().StringVar(&project, "project", "",
		"optional AppProject to scope the lookup")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from the most recent `meho login`)")
	if err := cmd.MarkFlagRequired("name"); err != nil {
		panic(err) // programmer error: the flag is defined directly above
	}
	return cmd
}

func runAppDiff(
	cmd *cobra.Command,
	targetName, appNameFlag, project string,
	jsonOut bool,
	backplaneOverride string,
) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	params := map[string]any{"name": appNameFlag}
	if project != "" {
		params["project"] = project
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "argocd.app.diff", targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "argocd.app.diff", r, jsonOut, printAppDiff)
}

func printAppDiff(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s argocd.app.diff — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	items, err := decodeItemsResult(r.Result)
	if err != nil || items == nil {
		fallbackResultRender(w, r)
		return
	}
	fmt.Fprintf(w, "  %-10s %-22s %-20s %-8s %s\n", "KIND", "NAME", "NAMESPACE", "MODIFIED", "GROUP")
	modifiedCount := 0
	for _, res := range items {
		kind := truncate(stringField(res, "kind"), 10)
		name := truncate(stringField(res, "name"), 22)
		namespace := truncate(stringField(res, "namespace"), 20)
		modified := boolField(res, "modified")
		if modified {
			modifiedCount++
		}
		group := stringField(res, "group")
		fmt.Fprintf(w, "  %-10s %-22s %-20s %-8t %s\n", kind, name, namespace, modified, group)
	}
	fmt.Fprintf(w, "  (%d managed resources, %d modified)\n", len(items), modifiedCount)
}

// boolField pulls a bool field from a row entry, defaulting to false
// when the field is missing or wrong type.
func boolField(e map[string]any, key string) bool {
	if v, ok := e[key].(bool); ok {
		return v
	}
	return false
}

// newAppResourceTreeCmd returns the `meho argocd app resource-tree`
// command.
//
// Maps to op_id `argocd.app.resource_tree`. GETs
// /api/v1/applications/{name}/resource-tree and returns the reconciled
// resource tree {nodes, orphanedNodes, hosts, shardsCount}.
func newAppResourceTreeCmd() *cobra.Command {
	var (
		targetName        string
		appNameFlag       string
		project           string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "resource-tree",
		Short: "Show an ArgoCD Application's reconciled resource tree",
		Long: "resource-tree dispatches argocd.app.resource_tree and renders the\n" +
			"reconciled resource tree for the Application named --name — each\n" +
			"node's kind/name/namespace plus per-node health and sync status,\n" +
			"and any orphaned nodes. --json emits the full OperationResult\n" +
			"envelope.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example: "  meho argocd app resource-tree --target rdc-argocd --name platform-bootstrap\n" +
			"  meho argocd app resource-tree --target rdc-argocd --name platform-bootstrap --json | jq '.result.nodes'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runAppResourceTree(cmd, targetName, appNameFlag, project, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "",
		"target slug to dispatch against (required)")
	cmd.Flags().StringVar(&appNameFlag, "name", "",
		"the Application's metadata.name (required)")
	cmd.Flags().StringVar(&project, "project", "",
		"optional AppProject to scope the lookup")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from the most recent `meho login`)")
	if err := cmd.MarkFlagRequired("name"); err != nil {
		panic(err) // programmer error: the flag is defined directly above
	}
	return cmd
}

func runAppResourceTree(
	cmd *cobra.Command,
	targetName, appNameFlag, project string,
	jsonOut bool,
	backplaneOverride string,
) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	params := map[string]any{"name": appNameFlag}
	if project != "" {
		params["project"] = project
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "argocd.app.resource_tree", targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "argocd.app.resource_tree", r, jsonOut, printAppResourceTree)
}

func printAppResourceTree(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s argocd.app.resource_tree — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	tree, err := decodeObject(r.Result)
	if err != nil || tree == nil {
		fallbackResultRender(w, r)
		return
	}
	nodes, _ := tree["nodes"].([]any)
	orphaned, _ := tree["orphanedNodes"].([]any)
	fmt.Fprintf(w, "  %-10s %-26s %-20s %s\n", "KIND", "NAME", "NAMESPACE", "HEALTH")
	for _, n := range nodes {
		node, ok := n.(map[string]any)
		if !ok {
			continue
		}
		kind := truncate(stringField(node, "kind"), 10)
		name := truncate(stringField(node, "name"), 26)
		namespace := truncate(stringField(node, "namespace"), 20)
		health := nestedString(node, "health", "status")
		fmt.Fprintf(w, "  %-10s %-26s %-20s %s\n", kind, name, namespace, health)
	}
	fmt.Fprintf(w, "  (%d nodes, %d orphaned)\n", len(nodes), len(orphaned))
}
