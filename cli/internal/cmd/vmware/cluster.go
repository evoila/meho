// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vmware

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newClusterCmd returns the `meho vmware cluster` parent command
// and assembles its two verbs (list / patch).
func newClusterCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "cluster",
		Short:        "vSphere cluster verbs (list / patch)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newClusterListCmd())
	cmd.AddCommand(newClusterPatchCmd())
	return cmd
}

// newClusterListCmd returns `meho vmware cluster list`. Maps to
// GET:/vcenter/cluster.
func newClusterListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List vSphere clusters on a vCenter target",
		Long: "list dispatches GET:/vcenter/cluster against the connector_id=\n" +
			"\"vmware-rest-9.0\" connector. Renders moid / name / drs_enabled /\n" +
			"ha_enabled for human eyes; --json emits the full OperationResult\n" +
			"envelope.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho vmware cluster list --target rdc-vcenter\n" +
			"  meho vmware cluster list --target rdc-vcenter --json",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runClusterList(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "target slug to dispatch against")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runClusterList(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	r, err := conn.Call(cmd.Context(), backplaneURL, "GET:/vcenter/cluster", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, "GET:/vcenter/cluster", r, jsonOut, printClusterList)
}

func printClusterList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/vcenter/cluster — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	entries, err := decodeListResult(r.Result)
	if err != nil {
		fallbackResultRender(w, r)
		return
	}
	if len(entries) == 0 {
		fmt.Fprintln(w, "  (0 clusters)")
		return
	}
	fmt.Fprintf(w, "%-16s %-30s %-12s %-10s\n", "moid", "name", "drs", "ha")
	for _, e := range entries {
		fmt.Fprintf(w, "%-16s %-30s %-12v %-10v\n",
			truncate(stringField(e, "cluster"), 16),
			truncate(stringField(e, "name"), 30),
			e["drs_enabled"],
			e["ha_enabled"],
		)
	}
}

// newClusterPatchCmd returns `meho vmware cluster patch <name>`.
// Resolves the name to a cluster moid then dispatches the composite
// op_id vmware.composite.cluster.patch (ships in T6 #509).
func newClusterPatchCmd() *cobra.Command {
	var (
		targetName        string
		specFlag          string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "patch <name-or-id>",
		Short: "Patch a vSphere cluster (composite: lifecycle-managed)",
		Long: "patch dispatches op_id=\"vmware.composite.cluster.patch\" against\n" +
			"the connector_id=\"vmware-rest-9.0\" connector. The composite\n" +
			"orchestrates the vSphere Lifecycle Manager patch flow (precheck +\n" +
			"stage + apply + post-validation) that ships in G3.1-T6 (#509).\n\n" +
			"<name-or-id> accepts a cluster name (resolved client-side to a\n" +
			"`domain-c<N>` moid) or a moid directly.\n\n" +
			"--spec accepts inline JSON or @<file> describing the patch payload\n" +
			"(see the T6 task body for the field list).\n\n" +
			"Pre-merge of T6 (#509), the dispatcher returns \"operation not\n" +
			"found\" which surfaces in the standard error trailer.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho vmware cluster patch dc-prod-01 --target rdc-vcenter --spec @patch.json\n" +
			"  meho vmware cluster patch domain-c123 --target rdc-vcenter --json",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runClusterPatch(cmd, args[0], targetName, specFlag, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "target slug to dispatch against")
	cmd.Flags().StringVar(&specFlag, "spec", "", "patch spec as inline JSON or @<file>; optional")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runClusterPatch(cmd *cobra.Command, nameOrID, targetName, specFlag string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	moid, err := resolveName(cmd.Context(), backplaneURL, targetName, "cluster", nameOrID)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			&output.StructuredError{Code: "resolve_failed", Detail: err.Error(), Exit: 1},
			jsonOut)
	}
	params, err := loadParamsFlag(specFlag)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), jsonOut)
	}
	if params == nil {
		params = map[string]any{}
	}
	params["cluster"] = moid
	opID := "vmware.composite.cluster.patch"
	r, err := conn.Call(cmd.Context(), backplaneURL, opID, targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, opID, r, jsonOut, nil)
}
