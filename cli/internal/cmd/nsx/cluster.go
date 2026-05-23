// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package nsx

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newClusterCmd returns the `meho nsx cluster` parent command (status sub-verb).
func newClusterCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "cluster",
		Short:        "NSX management cluster verbs (status)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newClusterStatusCmd())
	return cmd
}

// newClusterStatusCmd returns `meho nsx cluster status` → GET:/api/v1/cluster/status.
func newClusterStatusCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "status",
		Short: "Show NSX management cluster health",
		Long: "status dispatches GET:/api/v1/cluster/status against connector_id=\n" +
			"\"nsx-rest-4.2\". Renders the overall mgmt_cluster_status and\n" +
			"control_cluster_status fields; --json emits the full envelope.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho nsx cluster status --target rdc-nsx\n" +
			"  meho nsx cluster status --target rdc-nsx --json",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runClusterStatus(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "NSX target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runClusterStatus(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	r, err := conn.Call(cmd.Context(), backplaneURL, "GET:/api/v1/cluster/status", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, "GET:/api/v1/cluster/status", r, jsonOut, printClusterStatus)
}

func printClusterStatus(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/api/v1/cluster/status — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	if len(r.Result) == 0 || string(r.Result) == "null" {
		return
	}
	var cs struct {
		MgmtClusterStatus struct {
			Status string `json:"status"`
		} `json:"mgmt_cluster_status"`
		ControlClusterStatus struct {
			Status string `json:"status"`
		} `json:"control_cluster_status"`
	}
	if err := jsonUnmarshalStrict(r.Result, &cs); err != nil {
		fallbackResultRender(w, r)
		return
	}
	if s := cs.MgmtClusterStatus.Status; s != "" {
		fmt.Fprintf(w, "  mgmt_cluster:    %s\n", s)
	}
	if s := cs.ControlClusterStatus.Status; s != "" {
		fmt.Fprintf(w, "  control_cluster: %s\n", s)
	}
}
