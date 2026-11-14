// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package dispatch

import (
	"encoding/json"
	"fmt"
	"io"

	"github.com/evoila/meho/cli/internal/output"
)

// RESTParked is the answer of a REST write that waits for approval
// (HTTP 202). The target and topology writes give it for a call made
// with a token; only a person in the web interface writes at once.
type RESTParked struct {
	Status            string `json:"status"`
	OpID              string `json:"op_id"`
	ApprovalRequestID string `json:"approval_request_id"`
	Message           string `json:"message"`
}

// ParseRESTParked returns the parked answer in body, or false when body
// is not one (any other answer, or a body that does not decode).
func ParseRESTParked(body []byte) (RESTParked, bool) {
	var p RESTParked
	if err := json.Unmarshal(body, &p); err != nil {
		return RESTParked{}, false
	}
	if p.Status != StatusAwaitingApproval || p.ApprovalRequestID == "" {
		return RESTParked{}, false
	}
	return p, true
}

// RenderRESTParked prints a parked REST write and reports true, or
// prints nothing and reports false when body is not a parked answer.
// With --json it prints the answer as it came. Otherwise it prints the
// operation, its status and the same approval hint `meho operation
// call` prints. A parked write is not a failure, so the caller exits 0.
func RenderRESTParked(w io.Writer, body []byte, jsonOut bool) (bool, error) {
	p, ok := ParseRESTParked(body)
	if !ok {
		return false, nil
	}
	if jsonOut {
		return true, output.PrintJSON(w, p)
	}
	fmt.Fprintf(w, "%s — status=%s\n", p.OpID, p.Status)
	WriteParkedHint(w, body)
	return true, nil
}

// RESTWriteApprovalNote is the help text paragraph of each CLI command
// that writes targets or topology through REST.
const RESTWriteApprovalNote = "The change waits until a person approves it, because the CLI " +
	"calls with a token. The command prints the approval id and exits 0; " +
	"approving the request makes the change. A person in the web console " +
	"makes the same change at once.\n\n"
