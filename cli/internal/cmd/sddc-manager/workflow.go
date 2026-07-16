// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sddcmanager

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newWorkflowCmd returns the `meho sddc-manager workflow` sub-tree.
func newWorkflowCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "workflow",
		Short:        "VCF workflow task operations",
		SilenceUsage: true,
	}
	cmd.AddCommand(newWorkflowListCmd())
	return cmd
}

func newWorkflowListCmd() *cobra.Command {
	var (
		targetName        string
		statusFilter      string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List in-flight or recent VCF workflow tasks",
		Long: "list dispatches sddc.task.list against connector_id=\"sddc-rest-9.0\".\n" +
			"Pass --status to filter (Successful, Failed, In_Progress, Pending, Cancelled).\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired, 3=unreachable, 4=unexpected.",
		Example: "  meho sddc-manager workflow list --target rdc-sddc-manager\n" +
			"  meho sddc-manager workflow list --status In_Progress --target rdc-sddc-manager",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runWorkflowList(cmd, targetName, statusFilter, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "SDDC Manager target slug")
	cmd.Flags().StringVar(&statusFilter, "status", "",
		"filter by task status: Successful, Failed, In_Progress, Pending, Cancelled")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runWorkflowList(cmd *cobra.Command, targetName, statusFilter string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	var params map[string]any
	if statusFilter != "" {
		params = map[string]any{"status": statusFilter}
	}
	r, err := conn.Call(cmd.Context(), backplaneURL, "sddc.task.list", targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, "sddc.task.list", r, jsonOut, printWorkflowList)
}

func printWorkflowList(w io.Writer, r *CallResult) {
	entries, err := decodeElementsResult(r.Result)
	if err != nil || r.Status != "ok" {
		conn.PrintGeneric(w, "sddc.task.list", r)
		return
	}
	fmt.Fprintf(w, "VCF workflow tasks (%d)\n", len(entries))
	if len(entries) == 0 {
		fmt.Fprintln(w, "(0 tasks)")
		return
	}
	fmt.Fprintf(w, "%-36s  %-16s  %-32s  %s\n", "id", "status", "name", "type")
	for _, e := range entries {
		fmt.Fprintf(w, "%-36s  %-16s  %-32s  %s\n",
			truncate(sddcStringField(e, "id"), 36),
			sddcStringField(e, "status"),
			truncate(sddcStringField(e, "name"), 32),
			truncate(sddcStringField(e, "type"), 40),
		)
	}
}
