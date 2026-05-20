// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sddcmanager

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// CallResult mirrors the backend OperationResult Pydantic model.
type CallResult struct {
	Status     string          `json:"status"`
	OpID       string          `json:"op_id"`
	Result     json.RawMessage `json:"result"`
	Error      *string         `json:"error"`
	Extras     json.RawMessage `json:"extras,omitempty"`
	DurationMs float64         `json:"duration_ms"`
}

// callRequestBody mirrors the backend CallOperationBody Pydantic model.
type callRequestBody struct {
	ConnectorID string         `json:"connector_id"`
	OpID        string         `json:"op_id"`
	Target      map[string]any `json:"target"`
	Params      map[string]any `json:"params,omitempty"`
}

var errOpError = errors.New("operation status not ok")

// dispatchOp POSTs an OperationCall to the backplane with connector_id="sddc-rest-9.0" pre-baked.
func dispatchOp(
	ctx context.Context,
	backplaneURL, opID, targetSlug string,
	params map[string]any,
) (*CallResult, error) {
	body := callRequestBody{
		ConnectorID: ConnectorID,
		OpID:        opID,
		Target:      nil,
	}
	if targetSlug != "" {
		body.Target = map[string]any{"name": targetSlug}
	}
	if params != nil {
		body.Params = params
	}
	raw, err := json.Marshal(body)
	if err != nil {
		return nil, fmt.Errorf("marshal call request: %w", err)
	}
	respBody, err := doAuthedRequest(ctx, backplaneURL, "POST", "/api/v1/operations/call", raw)
	if err != nil {
		return nil, err
	}
	var out CallResult
	if err := json.Unmarshal(respBody, &out); err != nil {
		return nil, fmt.Errorf("decode call response: %w", err)
	}
	return &out, nil
}

// renderCallResult handles the unified post-dispatch rendering path.
func renderCallResult(
	cmd *cobra.Command,
	opID string,
	r *CallResult,
	jsonOut bool,
	prettyPrinter func(w io.Writer, r *CallResult),
) error {
	switch r.Status {
	case "ok", "error", "denied":
	default:
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"backplane returned invalid OperationResult.status %q (expected one of: ok / error / denied)",
				r.Status,
			)),
			jsonOut,
		)
	}
	if jsonOut {
		if err := output.PrintJSON(cmd.OutOrStdout(), r); err != nil {
			return err
		}
	} else if prettyPrinter != nil {
		prettyPrinter(cmd.OutOrStdout(), r)
	} else {
		printGenericResult(cmd.OutOrStdout(), opID, r)
	}
	if r.Status == "ok" {
		return nil
	}
	return errOpError
}

func printGenericResult(w io.Writer, opID string, r *CallResult) {
	fmt.Fprintf(w, "%s %s — status=%s (%.0fms)\n", ConnectorID, opID, r.Status, r.DurationMs)
	if r.Status == "ok" {
		if len(r.Result) > 0 && string(r.Result) != "null" {
			pretty, err := prettyJSON(r.Result)
			if err == nil {
				fmt.Fprintln(w, pretty)
				return
			}
			fmt.Fprintln(w, string(r.Result))
		}
		return
	}
	if r.Error != nil && *r.Error != "" {
		fmt.Fprintf(w, "meho: connector error: %s\n", *r.Error)
	} else {
		fmt.Fprintf(w, "meho: connector status=%s\n", r.Status)
	}
	if len(r.Extras) > 0 && string(r.Extras) != "null" {
		fmt.Fprintln(w, "extras:")
		pretty, err := prettyJSON(r.Extras)
		if err == nil {
			fmt.Fprintln(w, pretty)
		} else {
			fmt.Fprintln(w, string(r.Extras))
		}
	}
}

func prettyJSON(raw json.RawMessage) (string, error) {
	var v any
	if err := json.Unmarshal(raw, &v); err != nil {
		return "", err
	}
	out, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return "", err
	}
	return string(out), nil
}
