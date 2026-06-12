// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package vcffleet hosts the cobra commands under `meho vcf-fleet ...`
// for G3.6-T9 (#839) of Initiative #369. v0.5 ships operator-facing
// alias verbs over the 8 enabled VCF Fleet (vRSLCM-derived) read-only
// core ops, each pre-baking connector_id="fleet-rest-9.0" so operators
// don't type the connector ID on every dispatch:
//
//   - `meho vcf-fleet about [--target T]`                                  — GET:/lcm/lcops/api/v2/about
//   - `meho vcf-fleet datacenter list [--target T]`                        — GET:/lcm/lcops/api/v2/datacenters
//   - `meho vcf-fleet vcenter list <datacenter-vmid> [--target T]`         — GET:/lcm/lcops/api/v2/datacenters/{dataCenterVmid}/vcenters
//   - `meho vcf-fleet environment list [--target T]`                       — GET:/lcm/lcops/api/v2/environments
//   - `meho vcf-fleet environment info <environment-id> [--target T]`      — GET:/lcm/lcops/api/v2/environments/{environmentId}
//   - `meho vcf-fleet product list <environment-id> [--target T]`          — GET:/lcm/lcops/api/v2/environments/{environmentId}/products
//   - `meho vcf-fleet request list [--target T]`                           — GET:/lcm/request/api/v2/requests
//   - `meho vcf-fleet request info <request-id> [--target T]`              — GET:/lcm/request/api/v2/requests/{requestId}
//   - `meho vcf-fleet operation search/call`                               — meta-tool wrappers
//
// Every verb is a thin Cobra command that POSTs to
// `/api/v1/operations/call` with connector_id="fleet-rest-9.0"
// pre-baked. No Fleet logic in the CLI; pure Cobra-over-HTTP following
// the vmware + nsx + sddc-manager precedent (CLAUDE.md postulate 5).
//
// Per CLAUDE.md postulate 5, these alias verbs are operator ergonomics
// only — they are NOT mirrored on the MCP surface. Agents continue to
// use search_operations / call_operation against the narrow-waist
// meta-tool contract.
package vcffleet

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
// `meho vcf-fleet ...` dispatches against. Matches the v2 registry
// triple (product="vcf-fleet", version="9.0", impl_id="fleet-rest")
// the backend's parse_connector_id round-trips: "fleet-rest-9.0".
const ConnectorID = "fleet-rest-9.0"

// NewRootCmd returns the `meho vcf-fleet` parent command.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "vcf-fleet",
		Short: "Pre-scoped CLI verbs for the fleet-rest-9.0 connector",
		Long: "vcf-fleet is the operator-facing verb tree for the fleet-rest-9.0\n" +
			"connector (VCF Fleet — vRSLCM-derived lifecycle manager). Each verb\n" +
			"dispatches through POST /api/v1/operations/call with\n" +
			"connector_id=\"fleet-rest-9.0\" pre-baked so operators don't type the\n" +
			"connector ID on every command.\n\n" +
			"Per CLAUDE.md postulate 5, these alias verbs are operator-only\n" +
			"ergonomics — they are not mirrored on the MCP surface. Agents\n" +
			"continue to use search_operations / call_operation against the\n" +
			"narrow-waist meta-tool contract.\n\n" +
			"Known issue (VCF 9.0): the /about endpoint returns HTTP 500. Use\n" +
			"`vcf-fleet datacenter list` as the reachability probe instead.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newAboutCmd())
	cmd.AddCommand(newDatacenterCmd())
	cmd.AddCommand(newVcenterCmd())
	cmd.AddCommand(newEnvironmentCmd())
	cmd.AddCommand(newProductCmd())
	cmd.AddCommand(newRequestCmd())
	cmd.AddCommand(newOperationCmd())
	return cmd
}

// fleetEntry is a single row in a Fleet list response. Fleet returns
// bare JSON arrays for list ops (no `elements` / `results` envelope —
// unlike SDDC Manager / NSX), so the decoder falls through to a
// straight `[]map[string]any` unmarshal.
type fleetEntry = map[string]any

// decodeListResult unwraps Fleet's bare-array list response, or falls
// back to a `{"data": [...]}` envelope if the appliance build wraps the
// array. Fleet's vRSLCM lineage REST surface returns bare arrays on the
// curated read ops verified against the wrapper.
func decodeListResult(raw json.RawMessage) ([]fleetEntry, error) {
	if len(raw) == 0 || string(raw) == "null" {
		return nil, nil
	}
	var arr []fleetEntry
	if err := json.Unmarshal(raw, &arr); err == nil {
		return arr, nil
	}
	// Some appliance builds wrap collections in `{"data": [...]}` — try
	// that shape before giving up. The fallback exists to keep the
	// pretty-printer robust to spec-ingest drift; the canary fixture
	// uses the bare-array shape.
	var wrapped struct {
		Data []fleetEntry `json:"data"`
	}
	if err := json.Unmarshal(raw, &wrapped); err == nil && wrapped.Data != nil {
		return wrapped.Data, nil
	}
	return nil, fmt.Errorf("decode Fleet list result: not a JSON array or {data:[...]} envelope")
}

// fleetStringField extracts a string value from a Fleet entry map.
func fleetStringField(e fleetEntry, key string) string {
	v, ok := e[key]
	if !ok {
		return ""
	}
	if s, ok := v.(string); ok {
		return s
	}
	return ""
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

func strDeref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
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
