// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vmware

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/dispatch"
	"github.com/evoila/meho/cli/internal/output"
)

// newAboutCmd returns the `meho vmware about` command.
//
// CLI shape:
//
//	meho vmware about \
//	  [--target <slug>]                        # vCenter target (required for dispatch)
//	  [--json]                                 # machine-readable output
//	  [--backplane <url>]                      # override the backplane URL
//
// Maps to op_id `GET:/api/about` (vSphere REST 9.0's product info
// endpoint). The renderer surfaces vSphere product / version / build
// in the human path; --json emits the raw OperationResult envelope.
//
// Exit codes follow the meho operation call convention (see #511
// references):
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
		Short: "Show vSphere product, version, and build for a target",
		Long: "about dispatches GET:/api/about against the connector_id=\"vmware-rest-9.0\"\n" +
			"connector and renders the vSphere product / version / build / api_type\n" +
			"fields the endpoint returns. The human render is a 4-line summary; --json\n" +
			"emits the full OperationResult envelope for scripting.\n\n" +
			"--target names the vCenter target slug; required if no operator default\n" +
			"target is configured. The dispatch path is the same /api/v1/operations/call\n" +
			"route the agent surface uses — auth, audit, broadcast, and policy gates\n" +
			"all run as documented in CLAUDE.md §6.\n\n" +
			"Exit codes mirror meho operation call:\n" +
			"  - 0   status == ok\n" +
			"  - 1   status == error / denied\n" +
			"  - 2   auth_expired (run `meho login`)\n" +
			"  - 3   unreachable (network / DNS / TLS)\n" +
			"  - 4   unexpected response shape",
		Example: "  meho vmware about --target rdc-vcenter\n" +
			"  meho vmware about --target rdc-vcenter --json | jq .result",
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
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := conn.Call(cmd.Context(), backplaneURL, "GET:/api/about", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, "GET:/api/about", r, jsonOut, printAbout)
}

// printAbout renders the about endpoint's result fields. The vSphere
// REST /api/about endpoint returns
// `{"product", "version", "build", "api_type", ...}` — the renderer
// pulls those four fields and falls back to the generic envelope
// renderer for unexpected shapes.
//
// Why the per-field unpack: operators read about output to confirm
// which vCenter they're talking to (product line varies between
// VMware Cloud Foundation and standalone vSphere). The generic
// JSON dump is too noisy for that decision; a 4-line summary fits
// in a glance.
func printAbout(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/api/about — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	if len(r.Result) == 0 || string(r.Result) == "null" {
		return
	}
	about, ok := decodeAboutPayload(r.Result)
	if !ok {
		// Fallback to raw JSON dump when the shape doesn't match the
		// documented /api/about contract. Contract drift surfaces at
		// the inspection layer rather than as a panic.
		pretty, perr := dispatch.PrettyJSON(r.Result)
		if perr == nil {
			fmt.Fprintln(w, pretty)
		} else {
			fmt.Fprintln(w, string(r.Result))
		}
		return
	}
	if about.Product != "" {
		fmt.Fprintf(w, "  product:  %s\n", about.Product)
	}
	if about.Version != "" {
		fmt.Fprintf(w, "  version:  %s\n", about.Version)
	}
	if about.Build != "" {
		fmt.Fprintf(w, "  build:    %s\n", about.Build)
	}
	if about.APIType != "" {
		fmt.Fprintf(w, "  api_type: %s\n", about.APIType)
	}
}

// aboutPayload mirrors the documented vSphere REST /api/about shape.
// Extra fields land in the raw JSON dump fallback (the typed struct
// only covers the four fields operators read; the rest are scripted
// consumers' concern via --json).
type aboutPayload struct {
	Product string `json:"product"`
	Version string `json:"version"`
	Build   string `json:"build"`
	APIType string `json:"api_type"`
}

// decodeAboutPayload unpacks the result envelope into the typed
// aboutPayload. Two response shapes accepted:
//   - the canonical vSphere 9.0 shape: bare `{"product":..., ...}`.
//   - the legacy 6.x/7.x wrapper: `{"value":{"product":..., ...}}` —
//     some 9.0 endpoints still emit the wrapper when the request
//     carries a legacy `Accept` header. Both shapes ship via the
//     same `endpoint_descriptor`; the unwrap stays best-effort.
func decodeAboutPayload(raw []byte) (aboutPayload, bool) {
	var bare aboutPayload
	if err := jsonUnmarshalStrict(raw, &bare); err == nil && bare.Product != "" {
		return bare, true
	}
	var wrapped struct {
		Value aboutPayload `json:"value"`
	}
	if err := jsonUnmarshalStrict(raw, &wrapped); err == nil && wrapped.Value.Product != "" {
		return wrapped.Value, true
	}
	return aboutPayload{}, false
}

// printErrorTrailer surfaces the dispatcher error / extras envelope.
// Used by every per-verb pretty-printer's error branch so the
// "status=error" output is consistent across verbs.
func printErrorTrailer(w io.Writer, r *CallResult) {
	if r.Error != nil && *r.Error != "" {
		fmt.Fprintf(w, "meho: connector error: %s\n", *r.Error)
	} else {
		fmt.Fprintf(w, "meho: connector status=%s\n", r.Status)
	}
	if len(r.Extras) > 0 && string(r.Extras) != "null" {
		fmt.Fprintln(w, "extras:")
		pretty, err := dispatch.PrettyJSON(r.Extras)
		if err == nil {
			fmt.Fprintln(w, pretty)
		} else {
			fmt.Fprintln(w, string(r.Extras))
		}
	}
}
