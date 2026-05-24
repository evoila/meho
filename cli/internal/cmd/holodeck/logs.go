// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package holodeck

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newLogsCmd returns the `meho holodeck logs` parent with one
// sub-verb: `tail <component>` (holodeck.logs.tail).
func newLogsCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "logs",
		Short:        "Holodeck runtime log sub-verbs (tail)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newLogsTailCmd())
	return cmd
}

// newLogsTailCmd returns the `meho holodeck logs tail <component>`
// command.
//
// Maps to op_id `holodeck.logs.tail`. Runs `tail -n <lines>
// /holodeck-runtime/logs/<component>*.log` over plain SSH (no pwsh
// indirection) and returns the parsed tail envelope.
//
// The component slug is restricted on the backend to
// `[A-Za-z0-9._-]+` so the resulting `tail` cmdline cannot smuggle
// shell metacharacters or escape the `/holodeck-runtime/logs/` prefix
// via a directory traversal. The CLI forwards the value verbatim and
// the backend refuses misshapen slugs.
//
// The `--lines` flag is clamped on the backend to [1, 5000]; defaults
// to 200.
func newLogsTailCmd() *cobra.Command {
	var (
		targetName        string
		lines             int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "tail <component>",
		Short: "Tail Holodeck runtime log files for a given component",
		Long: "tail dispatches holodeck.logs.tail and returns the last\n" +
			"<lines> lines per matching log file under\n" +
			"/holodeck-runtime/logs/<component>*.log on the appliance.\n\n" +
			"Component slugs map to bundled services: `dhcp`, `dns`,\n" +
			"`frr`, `webtop`, `k8s`. Allowed chars: [A-Za-z0-9._-]+.\n\n" +
			"--lines defaults to 200; backend clamps to [1, 5000].\n\n" +
			"The human render surfaces each matching file separately\n" +
			"(GNU `tail` `==> path <==` headers). --json emits the full\n" +
			"OperationResult envelope including the raw stdout.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example: "  meho holodeck logs tail dhcp --target holorouter-hetzner-dc\n" +
			"  meho holodeck logs tail frr --lines 500 --target holorouter-hetzner-dc\n" +
			"  meho holodeck logs tail dns --target holorouter-hetzner-dc --json | jq .result",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runLogsTail(cmd, args[0], lines, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "",
		"target slug to dispatch against (required)")
	cmd.Flags().IntVar(&lines, "lines", 200,
		"number of trailing lines to return per file (backend clamps to [1, 5000])")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from the most recent `meho login`)")
	return cmd
}

func runLogsTail(
	cmd *cobra.Command,
	component string,
	lines int,
	targetName string,
	jsonOut bool,
	backplaneOverride string,
) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	params := map[string]any{
		"component": component,
		"lines":     lines,
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "holodeck.logs.tail", targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "holodeck.logs.tail", r, jsonOut, printLogsTail)
}

func printLogsTail(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s holodeck.logs.tail — status=%s (%.0fms)\n",
		ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	flat, err := decodeFlatResult(r.Result)
	if err != nil || flat == nil {
		fallbackResultRender(w, r)
		return
	}
	if errStr, ok := flat["error"].(string); ok && errStr != "" {
		fmt.Fprintf(w, "  error: %s\n", errStr)
		return
	}
	reqLines, _ := flat["lines_requested"].(float64)
	fmt.Fprintf(w, "  lines_requested: %d\n", int(reqLines))
	files, _ := flat["files"].([]any)
	if len(files) == 0 {
		fmt.Fprintln(w, "  (no matching log files)")
		return
	}
	for _, fileEntry := range files {
		entry, ok := fileEntry.(map[string]any)
		if !ok {
			continue
		}
		path := ""
		if p, ok := entry["path"].(string); ok {
			path = p
		}
		linesText, _ := entry["lines"].(string)
		if path != "" {
			fmt.Fprintf(w, "  ==> %s <==\n", path)
		} else {
			fmt.Fprintln(w, "  ==> (single-file tail) <==")
		}
		for _, line := range splitLines(linesText) {
			fmt.Fprintln(w, "    "+line)
		}
	}
}
