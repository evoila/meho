// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newShowCmd returns the `meho approvals show` command.
//
//	meho approvals show <id> [--json] [--backplane <url>]
//
// Role: operator. Fetches a single approval request via
// GET /api/v1/approvals/{id} and renders it with proposed_effect and
// the elicitation_url so the operator knows where to POST a decision.
func newShowCmd() *cobra.Command {
	var (
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "show <id>",
		Short: "Inspect a pending approval request",
		Long: "show calls GET /api/v1/approvals/{id} and renders the " +
			"full approval request detail: the proposed operation " +
			"(connector, op, target, proposed_effect), its current " +
			"status, the principal that triggered it, and the " +
			"elicitation_url MCP clients can use to post a decision. " +
			"--json emits the raw JSON envelope.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runShow(cmd, args[0], jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw JSON instead of the human-readable view")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from `meho login`)")
	return cmd
}

func runShow(cmd *cobra.Command, idArg string, jsonOut bool, backplaneOverride string) error {
	requestID, err := parseRequestID(idArg)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(err.Error()),
			jsonOut,
		)
	}
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	client, cerr := newAuthedClient(cmd.Context(), cmd, backplaneURL, jsonOut)
	if cerr != nil {
		return cerr
	}
	detail, err := fetchDetail(cmd.Context(), client, requestID)
	if err != nil {
		return routeRequestError(cmd, backplaneURL, err, jsonOut)
	}
	if detail == nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("backplane returned 200 OK but no JSON body decoded against ApprovalRequestView"),
			jsonOut,
		)
	}
	if jsonOut {
		return output.PrintJSON(cmd.OutOrStdout(), detail)
	}
	printDetail(cmd, detail)
	return nil
}

// parseRequestID validates the operator's `<id>` arg as a UUID at
// the verb edge. The typed-client's path parameter is
// `openapi_types.UUID` (an alias for `uuid.UUID`); parsing here
// keeps the bad-input error a clean output.Unexpected instead of a
// `fmt.Errorf("invalid UUID: %s")` mid-request or a server-side 422
// after the round-trip. Returns a `uuid.UUID` (assignable to
// `openapi_types.UUID` since the alias resolves to the same type).
func parseRequestID(idArg string) (uuid.UUID, error) {
	id, err := uuid.Parse(idArg)
	if err != nil {
		return uuid.UUID{}, fmt.Errorf("approval-id is not a valid UUID: %s", idArg)
	}
	return id, nil
}

// fetchDetail drives the typed-client `GetApprovalRequestApiV1ApprovalsRequestIdGet`
// endpoint with the same one-shot 401-retry shape `fetchList` uses
// (see comments there for the rationale). Returns `*ApprovalRequestView`
// on success, `*httpResponseError` for a non-2xx response, the
// underlying error for transport-layer failures, and `(nil, nil)`
// if the backplane returned 200 with a body that didn't decode
// against `ApprovalRequestView` (the defensive-nil branch the
// caller surfaces as `output.Unexpected`).
func fetchDetail(
	ctx context.Context,
	client *api.AuthedClient,
	requestID uuid.UUID,
) (*api.ApprovalRequestView, error) {
	resp, err := client.GetApprovalRequestApiV1ApprovalsRequestIdGetWithResponse(ctx, requestID, nil)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode() == 401 {
		if rerr := client.Refresh(ctx); rerr != nil {
			return nil, rerr
		}
		resp, err = client.GetApprovalRequestApiV1ApprovalsRequestIdGetWithResponse(ctx, requestID, nil)
		if err != nil {
			return nil, err
		}
	}
	if resp.StatusCode() < 200 || resp.StatusCode() >= 300 {
		return nil, &httpResponseError{statusCode: resp.StatusCode(), body: resp.Body}
	}
	return resp.JSON200, nil
}

func printDetail(cmd *cobra.Command, d *api.ApprovalRequestView) {
	w := cmd.OutOrStdout()
	rc := d.ReviewerContext
	// The redacted "what will happen" sentence (#3353) leads the detail so
	// a human reads what the request DOES before scanning the ids below.
	if rc != nil && rc.Summary != nil && *rc.Summary != "" {
		fmt.Fprintf(w, "Summary:      %s\n", *rc.Summary)
	}
	fmt.Fprintf(w, "ID:           %s\n", d.Id.String())
	fmt.Fprintf(w, "Status:       %s\n", string(d.Status))
	fmt.Fprintf(w, "Connector:    %s\n", d.ConnectorId)
	fmt.Fprintf(w, "Operation:    %s\n", d.OpId)
	// Resolved subject of a path-variable op (#3353): the concrete entity
	// (name + moid) recovered from identity params, so the reviewer reads
	// `web-01 (vm-1042)` instead of the unresolved `{vm}` in the op id.
	if rc != nil && rc.Subject != nil && *rc.Subject != "" {
		fmt.Fprintf(w, "Subject:      %s\n", *rc.Subject)
	}
	if d.TargetId != nil {
		fmt.Fprintf(w, "Target:       %s\n", targetLabel(d.TargetId.String(), rc))
	}
	// Parent composite op for a composite-child park (#3353, #3348 lineage).
	if rc != nil && rc.ParentCompositeOpId != nil && *rc.ParentCompositeOpId != "" {
		fmt.Fprintf(w, "Part of:      %s\n", *rc.ParentCompositeOpId)
	}
	fmt.Fprintf(w, "Principal:    %s\n", principalLabel(d.PrincipalSub, d.PrincipalName))
	if d.PrincipalAct != nil {
		fmt.Fprintf(w, "Acting as:    %s\n", *d.PrincipalAct)
	}
	// Run context (#3353): the change-ticket ref and originating run id, so
	// the reviewer can trace the request to the work that issued it.
	if d.WorkRef != nil && *d.WorkRef != "" {
		fmt.Fprintf(w, "Change ref:   %s\n", *d.WorkRef)
	}
	if d.RunId != nil {
		fmt.Fprintf(w, "Agent run:    %s\n", d.RunId.String())
	}
	fmt.Fprintf(w, "Params hash:  %s\n", d.ParamsHash)
	fmt.Fprintf(w, "Created:      %s\n", d.CreatedAt)
	if d.ExpiresAt != nil {
		fmt.Fprintf(w, "Expires:      %s\n", *d.ExpiresAt)
	}
	if d.ReviewedBy != nil {
		fmt.Fprintf(w, "Reviewed by:  %s\n", principalLabel(*d.ReviewedBy, d.ReviewedByName))
	}
	if d.DecidedAt != nil {
		fmt.Fprintf(w, "Decided at:   %s\n", *d.DecidedAt)
	}
	if d.ProposedEffect != nil {
		b, _ := json.MarshalIndent(d.ProposedEffect, "  ", "  ")
		fmt.Fprintf(w, "Effect:\n  %s\n", string(b))
	}
}

// targetLabel renders a target for the detail view: the resolved name +
// product/version alongside the bare GUID (#3353), or the bare GUID alone
// when nothing resolved (fail-open). The GUID always rides along so the
// stable id stays directly reachable.
func targetLabel(id string, rc *api.ReviewerContextView) string {
	if rc == nil || rc.TargetName == nil || *rc.TargetName == "" {
		return id
	}
	label := *rc.TargetName
	if rc.TargetProduct != nil && *rc.TargetProduct != "" {
		if rc.TargetVersion != nil && *rc.TargetVersion != "" {
			label += fmt.Sprintf(" (%s %s)", *rc.TargetProduct, *rc.TargetVersion)
		} else {
			label += fmt.Sprintf(" (%s)", *rc.TargetProduct)
		}
	}
	return fmt.Sprintf("%s [%s]", label, id)
}
