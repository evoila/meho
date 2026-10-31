// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vcffleet

import (
	"encoding/json"
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/dispatch"
	"github.com/evoila/meho/cli/internal/output"
)

const (
	requestListOpID = "fleet.request.list"
	requestGetOpID  = "fleet.request.get"
)

// newRequestCmd returns the `meho vcf-fleet request` sub-tree.
func newRequestCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "request",
		Short:        "VCF Fleet lifecycle request operations (list / info)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newRequestListCmd())
	cmd.AddCommand(newRequestInfoCmd())
	return cmd
}

func newRequestListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List Fleet lifecycle requests (deploy / patch / upgrade workflows)",
		Long: "list dispatches fleet.request.list against\n" +
			"connector_id=\"fleet-rest-9.0\". Returns the most recent requests\n" +
			"first, each with its state and a plain outcome; operators on busy\n" +
			"appliances may see a JSONFlux result handle — page it with\n" +
			"result_query(handle_id, offset, limit).\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired, 3=unreachable, 4=unexpected.",
		Example: "  meho vcf-fleet request list --target rdc-fleet\n" +
			"  meho vcf-fleet request list --target rdc-fleet --json | jq '.result.requests[] | select(.outcome==\"running\")'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runRequestList(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "VCF Fleet target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runRequestList(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	r, err := conn.Call(cmd.Context(), backplaneURL, requestListOpID, targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, requestListOpID, r, jsonOut, printRequestList)
}

func printRequestList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s %s — status=%s (%.0fms)\n", ConnectorID, requestListOpID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	var page struct {
		Requests []fleetEntry `json:"requests"`
	}
	if err := json.Unmarshal(r.Result, &page); err != nil || page.Requests == nil {
		conn.PrintGeneric(w, requestListOpID, r)
		return
	}
	if len(page.Requests) == 0 {
		fmt.Fprintln(w, "  (0 requests)")
		return
	}
	fmt.Fprintf(w, "%-38s %-24s %-14s %s\n", "request_id", "type", "state", "name")
	for _, e := range page.Requests {
		fmt.Fprintf(w, "%-38s %-24s %-14s %s\n",
			truncate(fleetStringField(e, "request_id"), 38),
			truncate(fleetStringField(e, "type"), 24),
			truncate(fleetStringField(e, "state"), 14),
			truncate(fleetStringField(e, "name"), 40),
		)
	}
}

func newRequestInfoCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "info <request-id>",
		Short: "Show the full detail of one Fleet lifecycle request",
		Long: "info dispatches fleet.request.get against\n" +
			"connector_id=\"fleet-rest-9.0\". Returns the state, a plain outcome\n" +
			"and, for a failed request, its error causes (the first one is the\n" +
			"failed step). Requires a request id from `vcf-fleet request list`.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired, 3=unreachable, 4=unexpected.",
		Example:       "  meho vcf-fleet request info req-vmid-001 --target rdc-fleet",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runRequestInfo(cmd, args[0], targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "VCF Fleet target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runRequestInfo(cmd *cobra.Command, requestID, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	params := map[string]any{"request_id": requestID}
	r, err := conn.Call(cmd.Context(), backplaneURL, requestGetOpID, targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, requestGetOpID, r, jsonOut, printRequestInfo)
}

func printRequestInfo(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s %s — status=%s (%.0fms)\n", ConnectorID, requestGetOpID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	if len(r.Result) == 0 || string(r.Result) == "null" {
		return
	}
	var req struct {
		RequestID       string      `json:"request_id"`
		RequestName     string      `json:"name"`
		RequestType     string      `json:"type"`
		State           string      `json:"state"`
		Outcome         string      `json:"outcome"`
		ExecutionStatus string      `json:"execution_status"`
		ErrorCause      string      `json:"error_cause"`
		CreatedBy       string      `json:"created_by"`
		LastUpdatedOn   json.Number `json:"last_updated_on"`
		FailedStep      *struct {
			Message string `json:"message"`
		} `json:"failed_step"`
	}
	if err := jsonUnmarshalStrict(r.Result, &req); err != nil || req.RequestID == "" {
		if pretty, perr := dispatch.PrettyJSON(r.Result); perr == nil {
			fmt.Fprintln(w, pretty)
		} else {
			fmt.Fprintln(w, string(r.Result))
		}
		return
	}
	fmt.Fprintf(w, "request:       %s (%s) — state=%s outcome=%s\n", req.RequestName, req.RequestID, req.State, req.Outcome)
	if req.RequestType != "" {
		fmt.Fprintf(w, "type:          %s\n", req.RequestType)
	}
	if req.ExecutionStatus != "" {
		fmt.Fprintf(w, "execution:     %s\n", req.ExecutionStatus)
	}
	if req.CreatedBy != "" {
		fmt.Fprintf(w, "created_by:    %s\n", req.CreatedBy)
	}
	if req.LastUpdatedOn != "" {
		fmt.Fprintf(w, "updated_on:    %s\n", req.LastUpdatedOn)
	}
	if req.ErrorCause != "" {
		fmt.Fprintf(w, "error_cause:   %s\n", req.ErrorCause)
	}
	if req.FailedStep != nil && req.FailedStep.Message != "" {
		fmt.Fprintf(w, "failed_step:   %s\n", req.FailedStep.Message)
	}
}
