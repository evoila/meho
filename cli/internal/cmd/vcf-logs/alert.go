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

// newAlertCmd returns the `meho vcf-logs alert` parent command (list sub-verb).
func newAlertCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "alert",
		Short:        "vRLI alert-definition verbs (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newAlertListCmd())
	return cmd
}

// newAlertListCmd returns `meho vcf-logs alert list` → GET:/api/v2/alerts.
func newAlertListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List vRLI alert definitions",
		Long: "list dispatches GET:/api/v2/alerts against connector_id=\"vrli-rest-9.0\".\n" +
			"Renders name / enabled / hitCount for human eyes; --json emits the full\n" +
			"OperationResult envelope. Read-only in v0.5 — alert create / update / delete\n" +
			"are deliberately excluded from the curated core.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired, 3=unreachable, 4=unexpected.",
		Example:       "  meho vcf-logs alert list --target rdc-vrli\n",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runAlertList(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "vRLI target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runAlertList(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	r, err := conn.Call(cmd.Context(), backplaneURL, "GET:/api/v2/alerts", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, "GET:/api/v2/alerts", r, jsonOut, printAlertList)
}

func printAlertList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/api/v2/alerts — status=%s (%.0fms)\n",
		ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	entries, err := decodeArrayField(r.Result, "alerts")
	if err != nil {
		fallbackResultRender(w, r)
		return
	}
	if len(entries) == 0 {
		fmt.Fprintln(w, "  (0 alerts)")
		return
	}
	fmt.Fprintf(w, "%-36s %-8s %s\n", "name", "enabled", "hit_count")
	for _, e := range entries {
		enabled := "?"
		if v, ok := e["enabled"].(bool); ok {
			if v {
				enabled = "true"
			} else {
				enabled = "false"
			}
		}
		var hit any = "?"
		if v, ok := e["hitCount"]; ok {
			hit = v
		}
		fmt.Fprintf(w, "%-36s %-8s %v\n",
			truncate(vrliStringField(e, "name"), 36),
			enabled,
			hit,
		)
	}
}
