// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package operation

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// ExactOpResponse is the JSON envelope GET /api/v1/operations/search
// returns when op_id is set (#3668): one call contract, no ranked hits.
type ExactOpResponse struct {
	Operation OperationContract `json:"operation"`
}

// OperationContract mirrors the backend OperationContract model
// (backend/src/meho_backplane/operations/_contract.py). The parameter
// schema and pagination hint stay raw JSON so --json prints them
// exactly as the backend stored them.
type OperationContract struct {
	ConnectorID      string            `json:"connector_id"`
	OpID             string            `json:"op_id"`
	Summary          *string           `json:"summary"`
	Description      *string           `json:"description"`
	GroupKey         *string           `json:"group_key"`
	SafetyLevel      string            `json:"safety_level"`
	RequiresApproval bool              `json:"requires_approval"`
	ParameterSchema  json.RawMessage   `json:"parameter_schema"`
	Examples         []ContractExample `json:"examples"`
	Target           ContractTarget    `json:"target"`
	ResultShape      ContractResult    `json:"result_shape"`
	PaginationHint   json.RawMessage   `json:"pagination_hint"`
	Recovery         []string          `json:"recovery"`
}

// ContractExample is one small example value from the contract.
type ContractExample struct {
	Source string          `json:"source"`
	Param  *string         `json:"param"`
	Value  json.RawMessage `json:"value"`
}

// ContractTarget says whether the operation needs a target and how to
// name it (always by name, never by id).
type ContractTarget struct {
	Required   bool   `json:"required"`
	ResolvesBy string `json:"resolves_by"`
	Guidance   string `json:"guidance"`
}

// ContractResult describes the call_operation result shape.
type ContractResult struct {
	EnvelopeFields []string `json:"envelope_fields"`
	Guidance       string   `json:"guidance"`
}

func runExactOp(cmd *cobra.Command, client operationsAPI, backplaneURL string, opts searchOptions) error {
	result, err := getExactOp(cmd.Context(), client, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	printContract(cmd.OutOrStdout(), &result.Operation)
	return nil
}

// getExactOp sends the search GET with op_id set and decodes the
// single contract. q / group / limit are not sent: the backend
// ignores them in this mode.
func getExactOp(ctx context.Context, client operationsAPI, opts searchOptions) (*ExactOpResponse, error) {
	opID := opts.OpID
	params := &api.GetSearchApiV1OperationsSearchGetParams{
		ConnectorId: opts.ConnectorID,
		OpId:        &opID,
	}
	body, err := fetchSearch(ctx, client, params)
	if err != nil {
		return nil, err
	}
	var out ExactOpResponse
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, fmt.Errorf("decode operation contract: %w", err)
	}
	return &out, nil
}

func printContract(w io.Writer, c *OperationContract) {
	fmt.Fprintf(w, "%s  (connector %s)\n", c.OpID, c.ConnectorID)
	if s := strDeref(c.Summary); s != "" {
		fmt.Fprintf(w, "summary:  %s\n", s)
	}
	approval := "no"
	if c.RequiresApproval {
		approval = "yes"
	}
	fmt.Fprintf(w, "safety:   %s   needs approval: %s\n", c.SafetyLevel, approval)
	fmt.Fprintf(w, "target:   %s\n", c.Target.Guidance)
	fmt.Fprintln(w, "\nparameter_schema:")
	fmt.Fprintln(w, indentJSON(c.ParameterSchema))
	if len(c.Examples) > 0 {
		fmt.Fprintln(w, "\nexamples:")
		for _, ex := range c.Examples {
			name := strDeref(ex.Param)
			if name == "" {
				name = "(whole call)"
			}
			fmt.Fprintf(w, "  %s: %s\n", name, string(ex.Value))
		}
	}
	if len(c.PaginationHint) > 0 && string(c.PaginationHint) != "null" {
		fmt.Fprintf(w, "\npagination hint: %s\n", string(c.PaginationHint))
	}
	fmt.Fprintf(w, "\nresult: %s\n", c.ResultShape.Guidance)
	if len(c.Recovery) > 0 {
		fmt.Fprintln(w, "\nif the call fails:")
		for _, rule := range c.Recovery {
			fmt.Fprintf(w, "  - %s\n", rule)
		}
	}
}

// indentJSON pretty-prints raw JSON for the human view; it falls back
// to the raw text when the bytes are not valid JSON.
func indentJSON(raw json.RawMessage) string {
	var buf bytes.Buffer
	if err := json.Indent(&buf, raw, "  ", "  "); err != nil {
		return "  " + string(raw)
	}
	return "  " + buf.String()
}
