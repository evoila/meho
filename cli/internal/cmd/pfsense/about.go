// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package pfsense

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newAboutCmd returns the `meho pfsense about` command.
//
// CLI shape:
//
//	meho pfsense about \
//	  [--target <slug>]     # pfSense target (required for dispatch)
//	  [--json]              # machine-readable output
//	  [--backplane <url>]   # override the backplane URL
//
// Maps to op_id `pfsense.about`. The renderer surfaces the vendor /
// product / version / build / kernel fields in the human path;
// --json emits the raw OperationResult envelope.
func newAboutCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "about",
		Short: "Show pfSense product / version / build for a target",
		Long: "about dispatches pfsense.about against the connector_id=\n" +
			"\"pfsense-ssh-2.7\" connector and renders the vendor / product /\n" +
			"version / build / kernel fields the handler returns (parsed from\n" +
			"/etc/version). The human render is a 5-line summary; --json emits\n" +
			"the full OperationResult envelope for scripting.\n\n" +
			"Exit codes mirror meho operation call:\n" +
			"  - 0   status == ok\n" +
			"  - 1   status == error / denied\n" +
			"  - 2   auth_expired (run `meho login`)\n" +
			"  - 3   unreachable (network / DNS / TLS)\n" +
			"  - 4   unexpected response shape",
		Example: "  meho pfsense about --target pfsense-hetzner-dc\n" +
			"  meho pfsense about --target pfsense-hetzner-dc --json | jq .result",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runAbout(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "",
		"target slug to dispatch against (required)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the full OperationResult envelope as JSON instead of the human render")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runAbout(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "pfsense.about", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "pfsense.about", r, jsonOut, printAbout)
}

// printAbout renders the pfsense.about result fields. The handler
// returns a flat dict shaped `{"vendor", "product", "version",
// "build", "kernel"}` — the renderer pulls those fields and falls back
// to the generic envelope renderer for unexpected shapes.
func printAbout(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s pfsense.about — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	about, err := decodeFlatResult(r.Result)
	if err != nil || about == nil {
		fallbackResultRender(w, r)
		return
	}
	for _, key := range []string{"vendor", "product", "version", "build", "kernel"} {
		v, ok := about[key]
		if !ok || v == nil {
			continue
		}
		fmt.Fprintf(w, "  %-10s %v\n", key+":", v)
	}
}
