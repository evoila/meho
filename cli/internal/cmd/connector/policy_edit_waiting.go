// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package connector

import (
	"encoding/json"
	"fmt"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// decodePolicyEditWaiting reads the 202 body of a risk level / approval
// edit or of an override removal. With CUSTOM_POLICY on, the backplane
// answers 202 and the change waits until an admin approves it in the web
// console. Returns nil when the body is not that answer.
func decodePolicyEditWaiting(body []byte) *api.PolicyEditRequestResponse {
	var waiting api.PolicyEditRequestResponse
	if err := json.Unmarshal(body, &waiting); err != nil || waiting.Status == "" {
		return nil
	}
	return &waiting
}

// printPolicyEditWaiting prints the request that waits: the backplane's
// sentence that says who approves it, and the approval request id.
func printPolicyEditWaiting(
	cmd *cobra.Command, connectorID, opID string, waiting *api.PolicyEditRequestResponse, jsonOut bool,
) error {
	if jsonOut {
		return output.PrintJSON(cmd.OutOrStdout(), waiting)
	}
	w := cmd.OutOrStdout()
	fmt.Fprintf(w, "%s/%s — not changed yet\n", connectorID, opID)
	fmt.Fprintf(w, "  %s\n", waiting.Message)
	if waiting.ApprovalRequestId != nil {
		fmt.Fprintf(w, "  approval request: %s\n", *waiting.ApprovalRequestId)
	}
	return nil
}
