// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package dispatch is the shared operation-call core for the per-vendor
// CLI command packages (cmd/vault, cmd/harbor, cmd/nsx, ...). Before it,
// every vendor package carried a byte-identical dispatch.go — CallResult,
// dispatchOp, renderCallResult, printGenericResult, prettyJSON —
// duplicated only because sibling cmd/* packages can't import one another
// without an import cycle (cmd/root.go grafts each onto the tree). This
// leaf package breaks that cycle: each vendor package binds one
// Connector{ID, Request} and calls its methods.
//
// The authed transport is *injected* (Request) rather than imported here,
// so the per-package doAuthedRequest helper stays where it is for now;
// deduplicating that transport cluster is a separate change.
package dispatch

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// CallResult mirrors the backend OperationResult Pydantic model. Result
// and Extras stay json.RawMessage because the backend types Result as a
// oneOf(dict, list) union — pretty-printing the raw bytes is the cleanest
// renderer. Set-shaped responses arrive already reduced to a JSONFlux
// handle envelope + sample by the dispatcher, so they render verbatim
// here with the handle hint intact.
type CallResult struct {
	Status     string          `json:"status"`
	OpID       string          `json:"op_id"`
	Result     json.RawMessage `json:"result"`
	Error      *string         `json:"error"`
	Extras     json.RawMessage `json:"extras,omitempty"`
	DurationMs float64         `json:"duration_ms"`
}

// CallRequestBody mirrors the backend CallOperationBody Pydantic model.
// Target is a map so the empty case serialises as null rather than an
// empty struct; the route layer's resolver short-circuits the missing-
// name case for ops that need a target.
type CallRequestBody struct {
	ConnectorID string         `json:"connector_id"`
	OpID        string         `json:"op_id"`
	Target      map[string]any `json:"target"`
	Params      map[string]any `json:"params,omitempty"`
}

// ErrOpError is the sentinel returned when the dispatcher reported a
// structured failure (status == "error" or "denied"). main translates a
// non-nil RunE error into a non-zero exit; SilenceErrors=true on each
// command keeps cobra from double-printing the error string.
var ErrOpError = errors.New("operation status not ok")

// RequestFunc issues a single authed HTTP request against the backplane
// and returns the response body. It matches each vendor package's
// doAuthedRequest verbatim and is injected into a Connector so this
// package stays transport-agnostic.
type RequestFunc func(ctx context.Context, backplaneURL, method, path string, body []byte) ([]byte, error)

// Connector binds a pre-baked connector_id and an authed transport so
// every alias verb in a vendor package shares one dispatch + render
// implementation.
type Connector struct {
	ID      string
	Request RequestFunc
}

// Call POSTs an OperationCall to the backplane and returns the decoded
// CallResult. An empty targetSlug omits the target field on the wire; a
// nil params omits params.
func (c Connector) Call(
	ctx context.Context,
	backplaneURL, opID, targetSlug string,
	params map[string]any,
) (*CallResult, error) {
	var target map[string]any
	if targetSlug != "" {
		target = map[string]any{"name": targetSlug}
	}
	return c.CallWithTarget(ctx, backplaneURL, opID, target, params)
}

// CallWithTarget is Call for connectors that need extra target keys
// beyond "name" (e.g. cmd/vcf-automation threads a per-call "fqdn"
// vhost override). target is encoded verbatim — pass nil to omit the
// target field (JSON null) for ops that don't act on a target.
func (c Connector) CallWithTarget(
	ctx context.Context,
	backplaneURL, opID string,
	target map[string]any,
	params map[string]any,
) (*CallResult, error) {
	body := CallRequestBody{ConnectorID: c.ID, OpID: opID, Target: target}
	if params != nil {
		body.Params = params
	}
	raw, err := json.Marshal(body)
	if err != nil {
		return nil, fmt.Errorf("marshal call request: %w", err)
	}
	respBody, err := c.Request(ctx, backplaneURL, "POST", "/api/v1/operations/call", raw)
	if err != nil {
		return nil, err
	}
	var out CallResult
	if err := json.Unmarshal(respBody, &out); err != nil {
		return nil, fmt.Errorf("decode call response: %w", err)
	}
	return &out, nil
}

// Render runs the unified post-dispatch path every verb uses: validate
// the status enum, render the envelope (JSON or human), then map
// "error" / "denied" to ErrOpError. When prettyPrinter is nil the generic
// envelope is used.
func (c Connector) Render(
	cmd *cobra.Command,
	opID string,
	r *CallResult,
	jsonOut bool,
	prettyPrinter func(w io.Writer, r *CallResult),
) error {
	switch r.Status {
	case "ok", "error", "denied":
		// fall through.
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
		c.PrintGeneric(cmd.OutOrStdout(), opID, r)
	}
	if r.Status == "ok" {
		return nil
	}
	return ErrOpError
}

// PrintGeneric renders a CallResult in the generic envelope shape: a
// status line, then the pretty-printed Result on success or the connector
// error + extras on failure. Used as the default pretty-printer when a
// verb passes none.
func (c Connector) PrintGeneric(w io.Writer, opID string, r *CallResult) {
	fmt.Fprintf(w, "%s %s — status=%s (%.0fms)\n", c.ID, opID, r.Status, r.DurationMs)
	if r.Status == "ok" {
		if len(r.Result) > 0 && string(r.Result) != "null" {
			pretty, err := PrettyJSON(r.Result)
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
		pretty, err := PrettyJSON(r.Extras)
		if err == nil {
			fmt.Fprintln(w, pretty)
		} else {
			fmt.Fprintln(w, string(r.Extras))
		}
	}
}

// PrettyJSON pretty-prints a json.RawMessage with 2-space indent.
func PrettyJSON(raw json.RawMessage) (string, error) {
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
