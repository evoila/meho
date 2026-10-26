// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vcfautomation

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newAboutCmd returns `meho vcf-automation about --plane provider|tenant`.
//
// Dual-plane verb: both VCFA planes expose an "about" surface but they
// answer different unauthenticated endpoints with different payload
// shapes, so `--plane` is required (no implicit default — the wrong
// plane would silently dispatch a different op_id).
//
// Provider plane → `vcfa.provider.health` — structured health: an
// authenticated GET:/cloudapi/1.0.0/orgs (pageSize=1) plus the
// unauthenticated GET:/iaas/api/about API versions (evoila/meho#3865;
// the former GET:/cloudapi/1.0.0/site answers 405 on VCFA 9.1).
// Tenant plane → `vcfa.tenant.about` (GET:/iaas/api/about) — supported
// API versions + latestApiVersion.
func newAboutCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "about",
		Short: "Show VCFA appliance identity (plane-specific)",
		Long: "about dispatches the per-plane VCFA self-describe endpoint\n" +
			"against connector_id=\"vcfa-rest-9.0\":\n\n" +
			"  --plane provider → vcfa.provider.health\n" +
			"  --plane tenant   → vcfa.tenant.about\n\n" +
			"Both endpoints authenticate per their plane (provider Basic\n" +
			"→ JWT; tenant JSON-body login → bearer); the connector picks\n" +
			"the right token based on the op_id's path family. --json\n" +
			"emits the full OperationResult envelope.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho vcf-automation about --plane provider --target rdc-vcfa\n" +
			"  meho vcf-automation about --plane tenant --target rdc-vcfa --json",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runAbout(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "VCFA target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runAbout(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	plane := readPlane(cmd)
	if se := requirePlane(plane); se != nil {
		return output.RenderError(cmd.ErrOrStderr(), se, jsonOut)
	}
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	opID := aboutOpForPlane(plane)
	r, err := dispatchOp(cmd.Context(), backplaneURL, opID, targetName, readFqdn(cmd), nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	printer := func(w io.Writer, res *CallResult) { printAbout(w, plane, res) }
	return conn.Render(cmd, opID, r, jsonOut, printer)
}

// aboutOpForPlane returns the op_id the `about` verb dispatches for
// each plane. Centralised so the per-plane mapping has one obvious
// place to read off.
func aboutOpForPlane(plane string) string {
	if plane == PlaneTenant {
		return "vcfa.tenant.about"
	}
	return "vcfa.provider.health"
}

func printAbout(w io.Writer, plane string, r *CallResult) {
	fmt.Fprintf(w, "%s about (--plane %s) — status=%s (%.0fms)\n",
		ConnectorID, plane, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	if len(r.Result) == 0 || string(r.Result) == "null" {
		return
	}
	if plane == PlaneTenant {
		printAboutTenant(w, r)
		return
	}
	printAboutProvider(w, r)
}

func printAboutProvider(w io.Writer, r *CallResult) {
	var health struct {
		ProviderPlane *struct {
			Reachable     bool   `json:"reachable"`
			Authenticated bool   `json:"authenticated"`
			Check         string `json:"check"`
			OrgCount      *int   `json:"org_count"`
		} `json:"provider_plane"`
		API *struct {
			Reachable            bool     `json:"reachable"`
			LatestAPIVersion     string   `json:"latestApiVersion"`
			SupportedAPIVersions []string `json:"supportedApiVersions"`
			Error                string   `json:"error"`
		} `json:"api"`
	}
	if err := jsonUnmarshalStrict(r.Result, &health); err != nil || health.ProviderPlane == nil {
		fallbackResultRender(w, r)
		return
	}
	pp := health.ProviderPlane
	fmt.Fprintf(w, "  provider_plane:  reachable=%t authenticated=%t\n", pp.Reachable, pp.Authenticated)
	if pp.Check != "" {
		fmt.Fprintf(w, "  check:           %s\n", pp.Check)
	}
	if pp.OrgCount != nil {
		fmt.Fprintf(w, "  org_count:       %d\n", *pp.OrgCount)
	}
	if health.API == nil {
		return
	}
	if !health.API.Reachable {
		fmt.Fprintf(w, "  api:             unreachable (%s)\n", health.API.Error)
		return
	}
	if health.API.LatestAPIVersion != "" {
		fmt.Fprintf(w, "  latest_api_version: %s\n", health.API.LatestAPIVersion)
	}
	if len(health.API.SupportedAPIVersions) > 0 {
		fmt.Fprintf(w, "  supported_apis:\n")
		for _, v := range health.API.SupportedAPIVersions {
			fmt.Fprintf(w, "    - %s\n", v)
		}
	}
}

func printAboutTenant(w io.Writer, r *CallResult) {
	var about struct {
		LatestAPIVersion string `json:"latestApiVersion"`
		SupportedAPIs    []struct {
			APIVersion    string `json:"apiVersion"`
			Documentation string `json:"documentation"`
		} `json:"supportedApis"`
	}
	if err := jsonUnmarshalStrict(r.Result, &about); err != nil || about.LatestAPIVersion == "" {
		fallbackResultRender(w, r)
		return
	}
	fmt.Fprintf(w, "  latest_api_version: %s\n", about.LatestAPIVersion)
	if len(about.SupportedAPIs) > 0 {
		fmt.Fprintf(w, "  supported_apis:\n")
		for _, a := range about.SupportedAPIs {
			fmt.Fprintf(w, "    - %s\n", a.APIVersion)
		}
	}
}
