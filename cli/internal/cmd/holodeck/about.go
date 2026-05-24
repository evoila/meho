// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package holodeck

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newAboutCmd returns the `meho holodeck about` command.
//
// CLI shape:
//
//	meho holodeck about \
//	  [--target <slug>]     # HoloRouter target (required for dispatch)
//	  [--json]              # machine-readable output
//	  [--backplane <url>]   # override the backplane URL
//
// Maps to op_id `holodeck.about`. The renderer surfaces the vendor /
// product / version / build / photon_version / pod_id fields the
// handler returns; --json emits the raw OperationResult envelope.
func newAboutCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "about",
		Short: "Show Holodeck product / version / Photon OS for a target",
		Long: "about dispatches holodeck.about against the connector_id=\n" +
			"\"holodeck-ssh-9.0\" connector and renders the vendor / product /\n" +
			"version / build / photon_version / pod_id fields the handler\n" +
			"returns (parsed from /etc/photon-release + Get-HoloDeckConfig\n" +
			"via pwsh-over-SSH). The human render is a short summary;\n" +
			"--json emits the full OperationResult envelope for scripting.\n\n" +
			"Exit codes mirror meho operation call:\n" +
			"  - 0   status == ok\n" +
			"  - 1   status == error / denied\n" +
			"  - 2   auth_expired (run `meho login`)\n" +
			"  - 3   unreachable (network / DNS / TLS)\n" +
			"  - 4   unexpected response shape",
		Example: "  meho holodeck about --target holorouter-hetzner-dc\n" +
			"  meho holodeck about --target holorouter-hetzner-dc --json | jq .result",
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
	r, err := dispatchOp(cmd.Context(), backplaneURL, "holodeck.about", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "holodeck.about", r, jsonOut, printAbout)
}

// printAbout renders the holodeck.about result fields. The handler
// returns a flat dict shaped `{"vendor", "product", "version", "build",
// "photon_version", "pod_id"}` — the renderer pulls those fields and
// falls back to the generic envelope renderer for unexpected shapes.
func printAbout(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s holodeck.about — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	about, err := decodeFlatResult(r.Result)
	if err != nil || about == nil {
		fallbackResultRender(w, r)
		return
	}
	for _, key := range []string{"vendor", "product", "version", "build", "photon_version", "pod_id"} {
		v, ok := about[key]
		if !ok || v == nil {
			continue
		}
		fmt.Fprintf(w, "  %-16s %v\n", key+":", v)
	}
}
