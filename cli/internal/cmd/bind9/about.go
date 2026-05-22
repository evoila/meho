// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package bind9

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newAboutCmd returns the `meho bind9 about` command.
//
// CLI shape:
//
//	meho bind9 about \
//	  [--target <slug>]                        # bind9 target (required for dispatch)
//	  [--json]                                 # machine-readable output
//	  [--backplane <url>]                      # override the backplane URL
//
// Maps to op_id `bind9.about`. The renderer surfaces the bind9
// vendor / product / version / OS in the human path; --json emits
// the raw OperationResult envelope.
//
// Exit codes follow the meho operation call convention:
//   - 0   operation invoked + status == "ok"
//   - 1   operation invoked but status == "error" / "denied"
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected response shape
func newAboutCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "about",
		Short: "Show bind9 vendor / product / version / OS for a target",
		Long: "about dispatches bind9.about against the connector_id=\n" +
			"\"bind9-ssh-9.x\" connector and renders the vendor / product /\n" +
			"version / OS fields the handler returns (named -v + os-release\n" +
			"parsed). The human render is a 4-line summary; --json emits\n" +
			"the full OperationResult envelope for scripting.\n\n" +
			"--target names the bind9 target slug; required if no operator\n" +
			"default target is configured. The dispatch path is the same\n" +
			"/api/v1/operations/call route the agent surface uses — auth,\n" +
			"audit, broadcast, and policy gates all run as documented in\n" +
			"CLAUDE.md §6.\n\n" +
			"Exit codes mirror meho operation call:\n" +
			"  - 0   status == ok\n" +
			"  - 1   status == error / denied\n" +
			"  - 2   auth_expired (run `meho login`)\n" +
			"  - 3   unreachable (network / DNS / TLS)\n" +
			"  - 4   unexpected response shape",
		Example: "  meho bind9 about --target vcf-router-bind9\n" +
			"  meho bind9 about --target vcf-router-bind9 --json | jq .result",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runAbout(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "",
		"target slug to dispatch against (required for ops that read a target)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the full OperationResult envelope as JSON instead of the human render")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runAbout(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	r, err := conn.Call(cmd.Context(), backplaneURL, "bind9.about", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, "bind9.about", r, jsonOut, printAbout)
}

// printAbout renders the bind9.about result fields. The handler
// returns a flat dict shaped `{"vendor", "product", "version",
// "build", "os", "named_conf_path"}` — the renderer pulls those
// fields and falls back to the generic envelope renderer for
// unexpected shapes.
//
// Why the per-field unpack: operators read about output to confirm
// which bind9 instance they're talking to (Debian 9.18 vs Alpine
// 9.20 vs a future 9.x derivative). The generic JSON dump is too
// noisy for that decision; a 6-line summary fits in a glance.
func printAbout(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s bind9.about — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	about, err := decodeFlatResult(r.Result)
	if err != nil || about == nil {
		fallbackResultRender(w, r)
		return
	}
	for _, key := range []string{"vendor", "product", "version", "build", "os", "named_conf_path"} {
		v, ok := about[key]
		if !ok || v == nil {
			continue
		}
		fmt.Fprintf(w, "  %-17s %v\n", key+":", v)
	}
}
