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

// newAboutCmd returns `meho nsx about` → nsx.node.status.
//
// Renders the NSX Manager's node_version / hostname / node_uuid /
// kernel_version identity fields; --json emits the raw envelope.
func newAboutCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "about",
		Short: "Show NSX Manager version, hostname, and node UUID",
		Long: "about dispatches nsx.node.status against connector_id=\"nsx-rest-4.2\"\n" +
			"and renders the manager's node_version / hostname / node_uuid fields.\n" +
			"--json emits the full OperationResult envelope.\n\n" +
			"Exit codes mirror meho operation call:\n" +
			"  - 0   status == ok\n" +
			"  - 1   status == error / denied\n" +
			"  - 2   auth_expired\n" +
			"  - 3   unreachable\n" +
			"  - 4   unexpected response shape",
		Example: "  meho nsx about --target rdc-nsx\n" +
			"  meho nsx about --target rdc-nsx --json | jq .result",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runAbout(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "NSX target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runAbout(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	r, err := conn.Call(cmd.Context(), backplaneURL, "nsx.node.status", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, "nsx.node.status", r, jsonOut, printAbout)
}

func printAbout(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s nsx.node.status — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	if len(r.Result) == 0 || string(r.Result) == "null" {
		return
	}
	var node struct {
		NodeVersion   string `json:"node_version"`
		KernelVersion string `json:"kernel_version"`
		NodeUUID      string `json:"node_uuid"`
		Hostname      string `json:"hostname"`
	}
	if err := jsonUnmarshalStrict(r.Result, &node); err != nil || node.NodeVersion == "" {
		fallbackResultRender(w, r)
		return
	}
	if node.NodeVersion != "" {
		fmt.Fprintf(w, "  version:   %s\n", node.NodeVersion)
	}
	if node.KernelVersion != "" {
		fmt.Fprintf(w, "  build:     %s\n", node.KernelVersion)
	}
	if node.Hostname != "" {
		fmt.Fprintf(w, "  hostname:  %s\n", node.Hostname)
	}
	if node.NodeUUID != "" {
		fmt.Fprintf(w, "  node_uuid: %s\n", node.NodeUUID)
	}
}
