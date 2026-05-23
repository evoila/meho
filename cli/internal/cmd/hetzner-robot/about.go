// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package hetznerrobot

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newAboutCmd returns `meho hetzner-robot about` → GET:/query.
//
// WARNING — 401 IP-BLOCK RISK: Hetzner Robot blocks the source IP for
// 10 minutes after 3 consecutive 401 responses. This command raises
// auth_failed on the FIRST 401 and never retries. Fix credentials
// before retrying.
func newAboutCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "about",
		Short: "Show Hetzner Robot API version and account summary",
		Long: "about dispatches GET:/query against connector_id=\"hetzner-rest-2026.04\"\n" +
			"and renders the API version and account-level summary.\n" +
			"--json emits the full OperationResult envelope.\n\n" +
			"WARNING — 401 IP-BLOCK RISK: Hetzner Robot blocks the egress IP\n" +
			"for 10 minutes after 3 consecutive 401 responses. MEHO shares one\n" +
			"egress IP across all operators — a misconfigured Webservice-user\n" +
			"credential will lock out ALL operators. The connector raises\n" +
			"auth_failed on the FIRST 401 and never retries. Check the\n" +
			"Webservice-user credentials at the target's Vault path before\n" +
			"retrying.\n\n" +
			"Exit codes mirror meho operation call:\n" +
			"  - 0   status == ok\n" +
			"  - 1   status == error / denied\n" +
			"  - 2   auth_expired\n" +
			"  - 3   unreachable\n" +
			"  - 4   unexpected response shape",
		Example: "  meho hetzner-robot about --target rdc-robot\n" +
			"  meho hetzner-robot about --target rdc-robot --json | jq .result",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runAbout(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "Hetzner Robot target slug")
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
	r, err := dispatchOp(cmd.Context(), backplaneURL, "GET:/query", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "GET:/query", r, jsonOut, printAbout)
}

func printAbout(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/query — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	if len(r.Result) == 0 || string(r.Result) == "null" {
		return
	}
	var info struct {
		APIVersion string `json:"api_version"`
		AccountID  string `json:"account_id"`
	}
	if err := jsonUnmarshalStrict(r.Result, &info); err != nil || info.APIVersion == "" {
		fallbackResultRender(w, r)
		return
	}
	if info.APIVersion != "" {
		fmt.Fprintf(w, "  api_version: %s\n", info.APIVersion)
	}
	if info.AccountID != "" {
		fmt.Fprintf(w, "  account_id:  %s\n", info.AccountID)
	}
}
