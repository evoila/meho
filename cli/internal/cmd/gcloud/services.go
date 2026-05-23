// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package gcloud

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newServicesCmd returns the `meho gcloud services` parent command and
// assembles its list verb.
func newServicesCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "services",
		Short:        "GCP services (APIs) verbs (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newServicesListCmd())
	return cmd
}

// newServicesListCmd returns `meho gcloud services list`. Maps to
// op_id `gcloud.services.list`. Output is the canonical `{rows, total}`
// envelope; rows carry `{name, title, state}`.
func newServicesListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
		allServices       bool
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List GCP services (APIs) enabled on the project",
		Long: "list dispatches gcloud.services.list against\n" +
			"connector_id=\"gcloud-rest-1.0\". Returns one row per API with\n" +
			"its service name, display title, and state (ENABLED/DISABLED).\n" +
			"By default only ENABLED services are returned; use --all to\n" +
			"include disabled services as well.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho gcloud services list --target rdc-gcp-dev\n" +
			"  meho gcloud services list --target rdc-gcp-dev --all\n" +
			"  meho gcloud services list --target rdc-gcp-dev --json | jq '.result.rows[].name'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runServicesList(cmd, targetName, jsonOut, backplaneOverride, allServices)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "target slug to dispatch against")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	cmd.Flags().BoolVar(&allServices, "all", false,
		"include disabled services in addition to enabled ones")
	return cmd
}

func runServicesList(
	cmd *cobra.Command,
	targetName string,
	jsonOut bool,
	backplaneOverride string,
	allServices bool,
) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	params := map[string]any{"enabled_only": !allServices}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "gcloud.services.list", targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "gcloud.services.list", r, jsonOut, printServicesList)
}

// printServicesList renders the services list. Each row carries
// `{name, title, state}` per the op's response schema.
func printServicesList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s gcloud.services.list — status=%s (%.0fms)\n",
		ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	rows, err := decodeRowsResult(r.Result)
	if err != nil {
		fallbackResultRender(w, r)
		return
	}
	if len(rows) == 0 {
		fmt.Fprintln(w, "  (0 services)")
		return
	}
	fmt.Fprintf(w, "%-55s %-10s %s\n", "service", "state", "title")
	for _, row := range rows {
		name := stringField(row, "name")
		state := stringField(row, "state")
		title := stringField(row, "title")
		fmt.Fprintf(w, "%-55s %-10s %s\n",
			truncate(name, 55),
			truncate(state, 10),
			truncate(title, 50),
		)
	}
}
