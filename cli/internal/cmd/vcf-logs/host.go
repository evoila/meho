// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vcflogs

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newHostCmd returns the `meho vcf-logs host` parent command (list sub-verb).
func newHostCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "host",
		Short:        "vRLI host-inventory verbs (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newHostListCmd())
	return cmd
}

// newHostListCmd returns `meho vcf-logs host list` → GET:/api/v2/hosts.
func newHostListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List hosts currently reporting log events to this vRLI cluster",
		Long: "list dispatches GET:/api/v2/hosts against connector_id=\"vrli-rest-9.0\".\n" +
			"Renders hostname / sourceType / lastReceivedTimestamp for human eyes;\n" +
			"--json emits the full OperationResult envelope.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired, 3=unreachable, 4=unexpected.",
		Example:       "  meho vcf-logs host list --target rdc-vrli\n",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runHostList(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "vRLI target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runHostList(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	r, err := conn.Call(cmd.Context(), backplaneURL, "GET:/api/v2/hosts", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, "GET:/api/v2/hosts", r, jsonOut, printHostList)
}

func printHostList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/api/v2/hosts — status=%s (%.0fms)\n",
		ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	entries, err := decodeArrayField(r.Result, "hosts")
	if err != nil {
		fallbackResultRender(w, r)
		return
	}
	if len(entries) == 0 {
		fmt.Fprintln(w, "  (0 hosts)")
		return
	}
	fmt.Fprintf(w, "%-40s %-16s %s\n", "hostname", "source_type", "last_received")
	for _, e := range entries {
		fmt.Fprintf(w, "%-40s %-16s %s\n",
			truncate(vrliStringField(e, "hostname"), 40),
			truncate(vrliStringField(e, "sourceType"), 16),
			truncate(vrliStringField(e, "lastReceivedTimestamp"), 30),
		)
	}
}
