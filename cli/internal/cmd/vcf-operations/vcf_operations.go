// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package vcfoperations hosts the cobra commands under
// `meho vcf-operations ...` for G3.6-T3 (#837) of Initiative #369.
// v0.5 ships the operator-facing alias verbs over the 8 enabled vROps
// read-only core ops (G3.6-T2 #833), each pre-baking
// connector_id="vrops-rest-9.0" so operators don't type the connector
// ID on every dispatch:
//
//   - `meho vcf-operations about [--target T]`                   — GET:/suite-api/api/versions/current
//   - `meho vcf-operations resource list [--target T] [--params J]` — GET:/suite-api/api/resources
//   - `meho vcf-operations resource get <id> [--target T]`        — GET:/suite-api/api/resources/{id}
//   - `meho vcf-operations alert list [--target T] [--params J]`  — GET:/suite-api/api/alerts
//   - `meho vcf-operations alertdefinition list [--target T] [--params J]` — GET:/suite-api/api/alertdefinitions
//   - `meho vcf-operations symptom list [--target T] [--params J]` — GET:/suite-api/api/symptoms
//   - `meho vcf-operations recommendation list [--target T] [--params J]` — GET:/suite-api/api/recommendations
//   - `meho vcf-operations supermetric list [--target T] [--params J]` — GET:/suite-api/api/supermetrics
//   - `meho vcf-operations operation search/call`                 — meta-tool wrappers
//
// Every verb is a thin Cobra command that POSTs to
// `/api/v1/operations/call` with connector_id="vrops-rest-9.0"
// pre-baked. No vROps logic in the CLI; pure Cobra-over-HTTP
// following the NSX/SDDC Manager precedents (CLAUDE.md postulate 5 —
// agent surface stays narrow-waist meta-tools; vendor-specific
// tooling lives only in the CLI).
//
// The verb-tree label is `vcf-operations` (the operator-facing
// product label that matches `Target.product`), while the dispatched
// connector_id is `vrops-rest-9.0` (the parse_connector_id slug the
// dispatcher routes by — see `backend/src/meho_backplane/connectors/
// vcf_operations/core_ops.py` for the product/version/impl_id split
// rationale).
package vcfoperations

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/dispatch"
	"github.com/evoila/meho/cli/internal/output"
)

// ConnectorID is the pre-baked connector_id every verb under
// `meho vcf-operations ...` dispatches against. Matches
// `VROPS_CONNECTOR_ID` in
// `backend/src/meho_backplane/connectors/vcf_operations/core_ops.py`
// (the parse_connector_id slug derived from product="vcf-operations"
// + version="9.0" + impl_id="vrops-rest").
const ConnectorID = "vrops-rest-9.0"

// NewRootCmd returns the `meho vcf-operations` parent command.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "vcf-operations",
		Short: "Pre-scoped CLI verbs for the vrops-rest-9.0 connector",
		Long: "vcf-operations is the operator-facing verb tree for the vrops-rest-9.0\n" +
			"connector (VMware Aria Operations / vROps 9.0). Each verb dispatches\n" +
			"through POST /api/v1/operations/call with connector_id=\"vrops-rest-9.0\"\n" +
			"pre-baked so operators don't type the connector ID on every command.\n\n" +
			"Per CLAUDE.md postulate 5, these alias verbs are operator-only\n" +
			"ergonomics — they are not mirrored on the MCP surface. Agents\n" +
			"continue to use search_operations / call_operation against the\n" +
			"narrow-waist meta-tool contract.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newAboutCmd())
	cmd.AddCommand(newResourceCmd())
	cmd.AddCommand(newAlertCmd())
	cmd.AddCommand(newAlertDefinitionCmd())
	cmd.AddCommand(newSymptomCmd())
	cmd.AddCommand(newRecommendationCmd())
	cmd.AddCommand(newSupermetricCmd())
	cmd.AddCommand(newOperationCmd())
	return cmd
}

// vropsEntry is a single row in a vROps list response. vROps uses
// map[string]any because the fields differ per resource kind.
type vropsEntry = map[string]any

// vropsListKeysByOp maps each list op's wrapper key (vROps wraps lists
// under noun-specific keys: “resourceList“, “alerts“,
// “alertDefinitions“, “symptoms“, “recommendations“,
// “superMetrics“). The renderers consult this map to find the rows
// regardless of which list op they are handling.
var vropsListKeysByOp = map[string]string{
	"GET:/suite-api/api/resources":        "resourceList",
	"GET:/suite-api/api/alerts":           "alerts",
	"GET:/suite-api/api/alertdefinitions": "alertDefinitions",
	"GET:/suite-api/api/symptoms":         "symptoms",
	"GET:/suite-api/api/recommendations":  "recommendations",
	"GET:/suite-api/api/supermetrics":     "superMetrics",
}

// decodeVropsListResult unwraps a vROps suite-api list payload from
// the documented per-noun wrapper key. “key“ is the expected
// wrapper (one of “resourceList“ / “alerts“ / etc.). The fallback
// path accepts a bare array so future spec drift to a flat shape
// doesn't break the renderer; the fallback is best-effort, not part
// of the documented contract.
func decodeVropsListResult(raw json.RawMessage, key string) ([]vropsEntry, error) {
	if len(raw) == 0 || string(raw) == "null" {
		return nil, nil
	}
	// Wrapper-keyed shape — the canonical case.
	var wrapped map[string]json.RawMessage
	if err := json.Unmarshal(raw, &wrapped); err == nil {
		if inner, ok := wrapped[key]; ok && len(inner) > 0 && string(inner) != "null" {
			var arr []vropsEntry
			if uerr := json.Unmarshal(inner, &arr); uerr == nil {
				return arr, nil
			}
		}
	}
	// Bare-array fallback. vROps' v0.5 surface always wraps, but a
	// future re-shape (or a vendor doc drift) shouldn't poison the
	// renderer; falling through to the raw-JSON dump in the verb's
	// printer is friendlier than panicking on shape drift.
	var arr []vropsEntry
	if err := json.Unmarshal(raw, &arr); err != nil {
		return nil, fmt.Errorf("decode vROps list result: %w", err)
	}
	return arr, nil
}

// vropsStringField extracts a string value from a vROps entry map.
// Mirrors nsxStringField in the NSX sibling.
func vropsStringField(e vropsEntry, key string) string {
	v, ok := e[key]
	if !ok {
		return ""
	}
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}

// vropsResourceName extracts the “resourceKey.name“ from a vROps
// resource entry. The list payload nests the human-readable name
// inside the “resourceKey“ object alongside the adapterKindKey and
// resourceKindKey; surfacing it flat in the column makes the table
// readable without the operator running --json | jq.
func vropsResourceName(e vropsEntry) string {
	rk, ok := e["resourceKey"].(map[string]any)
	if !ok {
		return ""
	}
	if name, ok := rk["name"].(string); ok {
		return name
	}
	return ""
}

// vropsResourceKindKey extracts “resourceKey.resourceKindKey“ from a
// vROps resource entry. Same nesting rationale as
// :func:`vropsResourceName`.
func vropsResourceKindKey(e vropsEntry) string {
	rk, ok := e["resourceKey"].(map[string]any)
	if !ok {
		return ""
	}
	if kind, ok := rk["resourceKindKey"].(string); ok {
		return kind
	}
	return ""
}

// fallbackResultRender dumps raw result JSON when the typed decoder
// doesn't match the actual response shape.
func fallbackResultRender(w io.Writer, r *CallResult) {
	if len(r.Result) == 0 || string(r.Result) == "null" {
		return
	}
	pretty, err := dispatch.PrettyJSON(r.Result)
	if err == nil {
		fmt.Fprintln(w, pretty)
		return
	}
	fmt.Fprintln(w, string(r.Result))
}

// printErrorTrailer surfaces the dispatcher error + extras envelope.
func printErrorTrailer(w io.Writer, r *CallResult) {
	if r.Error != nil && *r.Error != "" {
		fmt.Fprintf(w, "meho: connector error: %s\n", *r.Error)
	} else {
		fmt.Fprintf(w, "meho: connector status=%s\n", r.Status)
	}
	if len(r.Extras) > 0 && string(r.Extras) != "null" {
		fmt.Fprintln(w, "extras:")
		pretty, err := dispatch.PrettyJSON(r.Extras)
		if err == nil {
			fmt.Fprintln(w, pretty)
		} else {
			fmt.Fprintln(w, string(r.Extras))
		}
	}
}

func renderRequestError(
	cmd *cobra.Command,
	backplaneURL string,
	err error,
	jsonOut bool,
) error {
	if api.IsTokenNotFound(err) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"no stored credentials for %s; run `meho login %s`",
				backplaneURL, backplaneURL,
			)),
			jsonOut,
		)
	}
	if api.IsNoRefreshToken(err) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"stored token rejected and no refresh_token present; run `meho login %s`",
				backplaneURL,
			)),
			jsonOut,
		)
	}
	var apiErr *dispatch.APIResponseError
	if errors.As(err, &apiErr) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("call %s: HTTP %d: %s",
				backplaneURL, apiErr.StatusCode, apiErr.Body)),
			jsonOut,
		)
	}
	return output.RenderError(cmd.ErrOrStderr(),
		output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, err)),
		jsonOut,
	)
}

// loadParamsFlag parses the --params flag value. Prefixing with '@'
// loads the named file as JSON; otherwise the value itself is parsed
// as inline JSON. Returns nil for an empty value so the caller can
// omit the "params" key from the JSON body.
func loadParamsFlag(val string) (map[string]any, error) {
	if val == "" {
		return nil, nil
	}
	var raw []byte
	if strings.HasPrefix(val, "@") {
		path := strings.TrimPrefix(val, "@")
		var err error
		raw, err = os.ReadFile(path)
		if err != nil {
			return nil, fmt.Errorf("read params file %q: %w", path, err)
		}
	} else {
		raw = []byte(val)
	}
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		return nil, fmt.Errorf("parse params JSON: %w", err)
	}
	return m, nil
}

func jsonUnmarshalStrict(raw []byte, out any) error {
	return json.Unmarshal(raw, out)
}

func truncate(s string, maxLen int) string {
	if maxLen < 1 {
		return ""
	}
	runes := []rune(s)
	if len(runes) <= maxLen {
		return s
	}
	return string(runes[:maxLen-1]) + "…"
}
