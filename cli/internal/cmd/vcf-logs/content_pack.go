// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vcflogs

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newContentPackCmd returns the `meho vcf-logs content-pack` parent command (list sub-verb).
func newContentPackCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "content-pack",
		Short:        "vRLI content-pack inventory verbs (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newContentPackListCmd())
	return cmd
}

// newContentPackListCmd returns `meho vcf-logs content-pack list` →
// GET:/api/v2/content/contentpack/list.
func newContentPackListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List installed vRLI content packs",
		Long: "list dispatches GET:/api/v2/content/contentpack/list against connector_id=\n" +
			"\"vrli-rest-9.0\". Renders namespace / name / version for human eyes;\n" +
			"--json emits the full OperationResult envelope.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired, 3=unreachable, 4=unexpected.",
		Example:       "  meho vcf-logs content-pack list --target rdc-vrli\n",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runContentPackList(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "vRLI target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runContentPackList(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	const opID = "GET:/api/v2/content/contentpack/list"
	r, err := dispatchOp(cmd.Context(), backplaneURL, opID, targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, opID, r, jsonOut, printContentPackList)
}

func printContentPackList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/api/v2/content/contentpack/list — status=%s (%.0fms)\n",
		ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	entries, err := decodeArrayField(r.Result, "contentPackMetadataList")
	if err != nil {
		fallbackResultRender(w, r)
		return
	}
	if len(entries) == 0 {
		fmt.Fprintln(w, "  (0 content packs)")
		return
	}
	fmt.Fprintf(w, "%-28s %-32s %s\n", "namespace", "name", "version")
	for _, e := range entries {
		fmt.Fprintf(w, "%-28s %-32s %s\n",
			truncate(vrliStringField(e, "namespace"), 28),
			truncate(vrliStringField(e, "name"), 32),
			truncate(vrliStringField(e, "contentPackVersion"), 16),
		)
	}
}
