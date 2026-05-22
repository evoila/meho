// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vcffleet

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/dispatch"
	"github.com/evoila/meho/cli/internal/output"
)

// aboutOpID is the curated Fleet `about` op_id. The endpoint returns
// HTTP 500 in VCF 9.0 builds (a known regression — see
// connectors/vcf_fleet/core_ops.py). The verb is kept for parity with
// the spec + future fix; the docstring + the verb's long help warn
// operators to use `vcf-fleet datacenter list` as the reachability
// probe in 9.0.
const aboutOpID = "GET:/lcm/lcops/api/v2/about"

// newAboutCmd returns `meho vcf-fleet about` → GET:/lcm/lcops/api/v2/about.
func newAboutCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "about",
		Short: "Show vRSLCM appliance identity (apiVersion + productVersion + build)",
		Long: "about dispatches GET:/lcm/lcops/api/v2/about against\n" +
			"connector_id=\"fleet-rest-9.0\" and renders the appliance identity.\n" +
			"--json emits the full OperationResult envelope.\n\n" +
			"KNOWN ISSUE (VCF 9.0): the /about endpoint returns HTTP 500. The\n" +
			"verb is kept for spec parity; in 9.0 builds, use\n" +
			"`vcf-fleet datacenter list` as the reachability probe instead\n" +
			"(it doubles as the wrapper-verified probe and works on 9.0).\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired, 3=unreachable, 4=unexpected.",
		Example: "  meho vcf-fleet about --target rdc-fleet\n" +
			"  meho vcf-fleet about --target rdc-fleet --json | jq .result",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runAbout(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "VCF Fleet target slug")
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
	r, err := conn.Call(cmd.Context(), backplaneURL, aboutOpID, targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, aboutOpID, r, jsonOut, printAbout)
}

func printAbout(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s %s — status=%s (%.0fms)\n", ConnectorID, aboutOpID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	if len(r.Result) == 0 || string(r.Result) == "null" {
		return
	}
	var payload struct {
		APIVersion     string `json:"apiVersion"`
		ProductVersion string `json:"productVersion"`
		BuildNumber    string `json:"buildNumber"`
		ReleaseDate    string `json:"releaseDate"`
	}
	if err := jsonUnmarshalStrict(r.Result, &payload); err != nil || payload.APIVersion == "" {
		if len(r.Result) > 0 && string(r.Result) != "null" {
			pretty, perr := dispatch.PrettyJSON(r.Result)
			if perr == nil {
				fmt.Fprintln(w, pretty)
				return
			}
			fmt.Fprintln(w, string(r.Result))
		}
		return
	}
	if payload.APIVersion != "" {
		fmt.Fprintf(w, "  api_version:     %s\n", payload.APIVersion)
	}
	if payload.ProductVersion != "" {
		fmt.Fprintf(w, "  product_version: %s\n", payload.ProductVersion)
	}
	if payload.BuildNumber != "" {
		fmt.Fprintf(w, "  build:           %s\n", payload.BuildNumber)
	}
	if payload.ReleaseDate != "" {
		fmt.Fprintf(w, "  release_date:    %s\n", payload.ReleaseDate)
	}
}
