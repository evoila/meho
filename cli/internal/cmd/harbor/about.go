// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package harbor

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newAboutCmd returns `meho harbor about` → GET:/api/v2.0/systeminfo.
func newAboutCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "about",
		Short: "Show Harbor version, auth mode, and registry URL",
		Long: "about dispatches GET:/api/v2.0/systeminfo against connector_id=\"harbor-rest-2.x\"\n" +
			"and renders the appliance's harbor_version / auth_mode / registry_url fields.\n" +
			"--json emits the full OperationResult envelope.\n\n" +
			"Exit codes mirror meho operation call:\n" +
			"  - 0   status == ok\n" +
			"  - 1   status == error / denied\n" +
			"  - 2   auth_expired\n" +
			"  - 3   unreachable\n" +
			"  - 4   unexpected response shape",
		Example: "  meho harbor about --target prod-harbor\n" +
			"  meho harbor about --target prod-harbor --json | jq .result",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runAbout(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "Harbor target slug")
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
	r, err := dispatchOp(cmd.Context(), backplaneURL, "GET:/api/v2.0/systeminfo", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "GET:/api/v2.0/systeminfo", r, jsonOut, printAbout)
}

func printAbout(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/api/v2.0/systeminfo — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	if len(r.Result) == 0 || string(r.Result) == "null" {
		return
	}
	var info struct {
		HarborVersion              string `json:"harbor_version"`
		AuthMode                   string `json:"auth_mode"`
		RegistryURL                string `json:"registry_url"`
		ExternalURL                string `json:"external_url"`
		ProjectCreationRestriction string `json:"project_creation_restriction"`
		ReadOnly                   bool   `json:"read_only"`
	}
	if err := jsonUnmarshalStrict(r.Result, &info); err != nil || info.HarborVersion == "" {
		fallbackResultRender(w, r)
		return
	}
	if info.HarborVersion != "" {
		fmt.Fprintf(w, "  version:      %s\n", info.HarborVersion)
	}
	if info.AuthMode != "" {
		fmt.Fprintf(w, "  auth_mode:    %s\n", info.AuthMode)
	}
	if info.RegistryURL != "" {
		fmt.Fprintf(w, "  registry_url: %s\n", info.RegistryURL)
	}
	if info.ExternalURL != "" {
		fmt.Fprintf(w, "  external_url: %s\n", info.ExternalURL)
	}
	if info.ReadOnly {
		fmt.Fprintf(w, "  read_only:    true\n")
	}
}

// newHealthCmd returns `meho harbor health` → GET:/api/v2.0/health.
func newHealthCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "health",
		Short: "Show Harbor composite health across all subsystems",
		Long: "health dispatches GET:/api/v2.0/health against connector_id=\"harbor-rest-2.x\"\n" +
			"and renders the overall status plus per-component health.\n" +
			"--json emits the full OperationResult envelope.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho harbor health --target prod-harbor\n" +
			"  meho harbor health --target prod-harbor --json | jq .result.components",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runHealth(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "Harbor target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runHealth(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "GET:/api/v2.0/health", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "GET:/api/v2.0/health", r, jsonOut, printHealth)
}

func printHealth(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/api/v2.0/health — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	if len(r.Result) == 0 || string(r.Result) == "null" {
		return
	}
	var health struct {
		Status     string `json:"status"`
		Components []struct {
			Name   string `json:"name"`
			Status string `json:"status"`
			Error  string `json:"error,omitempty"`
		} `json:"components"`
	}
	if err := jsonUnmarshalStrict(r.Result, &health); err != nil || health.Status == "" {
		fallbackResultRender(w, r)
		return
	}
	fmt.Fprintf(w, "  overall: %s\n", health.Status)
	for _, c := range health.Components {
		line := fmt.Sprintf("  %-20s %s", c.Name, c.Status)
		if c.Error != "" {
			line += "  (" + c.Error + ")"
		}
		fmt.Fprintln(w, line)
	}
}
