// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package gcloud

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newAboutCmd returns the `meho gcloud about` command.
//
// CLI shape:
//
//	meho gcloud about \
//	  [--target <slug>]      # gcloud target (required for dispatch)
//	  [--json]               # machine-readable output
//	  [--backplane <url>]    # override the backplane URL
//
// Maps to op_id `gcloud.about`. The renderer surfaces GCP project
// identity fields (project_id, project_number, lifecycle_state,
// organization) in the human path; --json emits the raw
// OperationResult envelope.
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
		Short: "Show GCP project identity (project_id, lifecycle_state, organization)",
		Long: "about dispatches gcloud.about against connector_id=\n" +
			"\"gcloud-rest-1.0\" and renders the project identity fields\n" +
			"returned by Cloud Resource Manager v1 projects.get.\n\n" +
			"Use as the first call when connecting to a new gcloud target:\n" +
			"confirms the ADC + impersonation chain is working, returns the\n" +
			"project_number needed in some GCP API paths, and resolves the\n" +
			"parent organization ID.\n\n" +
			"--target names the gcloud target slug (e.g. rdc-gcp-dev).\n\n" +
			"Exit codes mirror meho operation call:\n" +
			"  - 0   status == ok\n" +
			"  - 1   status == error / denied\n" +
			"  - 2   auth_expired (run `meho login`)\n" +
			"  - 3   unreachable (network / DNS / TLS)\n" +
			"  - 4   unexpected response shape",
		Example: "  meho gcloud about --target rdc-gcp-dev\n" +
			"  meho gcloud about --target rdc-gcp-dev --json | jq .result",
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
	r, err := dispatchOp(cmd.Context(), backplaneURL, "gcloud.about", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "gcloud.about", r, jsonOut, printAbout)
}

// printAbout renders the gcloud.about result fields. The handler
// returns a flat dict shaped `{project_id, project_number,
// lifecycle_state, organization}` — the renderer pulls those fields
// and falls back to the generic envelope renderer for unexpected shapes.
func printAbout(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s gcloud.about — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	about, err := decodeFlatResult(r.Result)
	if err != nil || about == nil {
		fallbackResultRender(w, r)
		return
	}
	for _, key := range []string{"project_id", "project_number", "lifecycle_state", "organization"} {
		v, ok := about[key]
		if !ok || v == nil {
			continue
		}
		fmt.Fprintf(w, "  %-20s %v\n", key+":", v)
	}
}
