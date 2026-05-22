// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sddcmanager

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/dispatch"
	"github.com/evoila/meho/cli/internal/output"
)

// newAboutCmd returns `meho sddc-manager about` → GET:/v1/releases/system.
func newAboutCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "about",
		Short: "Show SDDC Manager VCF release version, build date, and component BOM",
		Long: "about dispatches GET:/v1/releases/system against connector_id=\"sddc-rest-9.0\"\n" +
			"and renders the VCF version, release date, and component BOM.\n" +
			"--json emits the full OperationResult envelope.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired, 3=unreachable, 4=unexpected.",
		Example: "  meho sddc-manager about --target rdc-sddc-manager\n" +
			"  meho sddc-manager about --target rdc-sddc-manager --json | jq .result",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runAbout(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "SDDC Manager target slug")
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
	r, err := conn.Call(cmd.Context(), backplaneURL, "GET:/v1/releases/system", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, "GET:/v1/releases/system", r, jsonOut, printAbout)
}

func printAbout(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/v1/releases/system — status=%s (%.0fms)\n",
		ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	var payload struct {
		Version     string `json:"version"`
		ReleaseDate string `json:"releaseDate"`
		Description string `json:"description"`
		BOM         []struct {
			ComponentType    string `json:"componentType"`
			ComponentVersion string `json:"componentVersion"`
		} `json:"bom"`
	}
	if err := jsonUnmarshalStrict(r.Result, &payload); err == nil && payload.Version != "" {
		fmt.Fprintf(w, "version:      %s\n", payload.Version)
		if payload.ReleaseDate != "" {
			fmt.Fprintf(w, "release_date: %s\n", payload.ReleaseDate)
		}
		if payload.Description != "" {
			fmt.Fprintf(w, "description:  %s\n", payload.Description)
		}
		if len(payload.BOM) > 0 {
			fmt.Fprintln(w, "components:")
			for _, c := range payload.BOM {
				fmt.Fprintf(w, "  %-24s %s\n", c.ComponentType, c.ComponentVersion)
			}
		}
		return
	}
	if len(r.Result) > 0 && string(r.Result) != "null" {
		pretty, err := dispatch.PrettyJSON(r.Result)
		if err == nil {
			fmt.Fprintln(w, pretty)
		} else {
			fmt.Fprintln(w, string(r.Result))
		}
	}
}
