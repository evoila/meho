// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vcfoperations

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newAboutCmd returns `meho vcf-operations about` →
// GET:/suite-api/api/versions/current.
//
// Renders the vROps appliance's releaseName / buildNumber /
// humanlyReadableReleaseName identity fields; --json emits the raw
// envelope.
func newAboutCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "about",
		Short: "Show vROps appliance release name and build number",
		Long: "about dispatches GET:/suite-api/api/versions/current against\n" +
			"connector_id=\"vrops-rest-9.0\" and renders the appliance's\n" +
			"releaseName / buildNumber / humanlyReadableReleaseName fields.\n" +
			"--json emits the full OperationResult envelope.\n\n" +
			"Exit codes mirror meho operation call:\n" +
			"  - 0   status == ok\n" +
			"  - 1   status == error / denied\n" +
			"  - 2   auth_expired\n" +
			"  - 3   unreachable\n" +
			"  - 4   unexpected response shape",
		Example: "  meho vcf-operations about --target rdc-vrops\n" +
			"  meho vcf-operations about --target rdc-vrops --json | jq .result",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runAbout(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "vROps target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runAbout(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	const opID = "GET:/suite-api/api/versions/current"
	r, err := conn.Call(cmd.Context(), backplaneURL, opID, targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, opID, r, jsonOut, printAbout)
}

func printAbout(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/suite-api/api/versions/current — status=%s (%.0fms)\n",
		ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	if len(r.Result) == 0 || string(r.Result) == "null" {
		return
	}
	var version struct {
		ReleaseName                string `json:"releaseName"`
		BuildNumber                int64  `json:"buildNumber"`
		HumanlyReadableReleaseName string `json:"humanlyReadableReleaseName"`
	}
	if err := jsonUnmarshalStrict(r.Result, &version); err != nil || version.ReleaseName == "" {
		fallbackResultRender(w, r)
		return
	}
	fmt.Fprintf(w, "  release:  %s\n", version.ReleaseName)
	if version.BuildNumber != 0 {
		fmt.Fprintf(w, "  build:    %d\n", version.BuildNumber)
	}
	if version.HumanlyReadableReleaseName != "" {
		fmt.Fprintf(w, "  human:    %s\n", version.HumanlyReadableReleaseName)
	}
}
