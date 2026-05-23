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

// newFieldCmd returns the `meho vcf-logs field` parent command (list sub-verb).
func newFieldCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "field",
		Short:        "vRLI indexer-field catalog verbs (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newFieldListCmd())
	return cmd
}

// newFieldListCmd returns `meho vcf-logs field list` → GET:/api/v2/fields.
func newFieldListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List vRLI indexer fields (static + extracted)",
		Long: "list dispatches GET:/api/v2/fields against connector_id=\"vrli-rest-9.0\".\n" +
			"Renders name / type / source for human eyes; --json emits the full\n" +
			"OperationResult envelope.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired, 3=unreachable, 4=unexpected.",
		Example:       "  meho vcf-logs field list --target rdc-vrli\n",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runFieldList(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "vRLI target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runFieldList(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	r, err := conn.Call(cmd.Context(), backplaneURL, "GET:/api/v2/fields", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, "GET:/api/v2/fields", r, jsonOut, printFieldList)
}

func printFieldList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/api/v2/fields — status=%s (%.0fms)\n",
		ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	entries, err := decodeArrayField(r.Result, "fields")
	if err != nil {
		fallbackResultRender(w, r)
		return
	}
	if len(entries) == 0 {
		fmt.Fprintln(w, "  (0 fields)")
		return
	}
	fmt.Fprintf(w, "%-32s %-12s %s\n", "name", "type", "source")
	for _, e := range entries {
		fmt.Fprintf(w, "%-32s %-12s %s\n",
			truncate(vrliStringField(e, "name"), 32),
			truncate(vrliStringField(e, "type"), 12),
			truncate(vrliStringField(e, "source"), 40),
		)
	}
}
