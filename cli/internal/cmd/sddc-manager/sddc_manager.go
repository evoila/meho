// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package sddcmanager hosts the cobra commands under `meho sddc-manager ...`
// for G3.5-T6 (#618) of Initiative #368. v0.2 ships the operator-facing
// alias verbs over the 9 enabled SDDC Manager read-only core ops, each
// pre-baking connector_id="sddc-rest-9.0" so operators don't type the
// connector ID on every dispatch:
//
//   - `meho sddc-manager about [--target T]`                — GET:/v1/releases/system
//   - `meho sddc-manager manager list [--target T]`         — GET:/v1/sddc-managers
//   - `meho sddc-manager domain list [--target T]`          — GET:/v1/domains
//   - `meho sddc-manager domain info <id> [--target T]`     — GET:/v1/domains/{id}
//   - `meho sddc-manager cluster list [--domain D] [--target T]`   — GET:/v1/clusters
//   - `meho sddc-manager host list [--domain D] [--cluster C] [--target T]` — GET:/v1/hosts
//   - `meho sddc-manager network-pool list [--target T]`    — GET:/v1/network-pools
//   - `meho sddc-manager bundle list [--target T]`          — GET:/v1/bundles
//   - `meho sddc-manager workflow list [--status S] [--target T]`  — GET:/v1/tasks
//   - `meho sddc-manager operation search/call`             — meta-tool wrappers
//
// Every verb is a thin Cobra command that POSTs to
// `/api/v1/operations/call` with connector_id="sddc-rest-9.0" pre-baked.
// No SDDC Manager logic in the CLI; pure Cobra-over-HTTP following the
// vmware + nsx precedent (CLAUDE.md postulate 5).
package sddcmanager

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
// `meho sddc-manager ...` dispatches against.
const ConnectorID = "sddc-rest-9.0"

// NewRootCmd returns the `meho sddc-manager` parent command.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "sddc-manager",
		Short: "Pre-scoped CLI verbs for the sddc-rest-9.0 connector",
		Long: "sddc-manager is the operator-facing verb tree for the sddc-rest-9.0\n" +
			"connector. Each verb dispatches through POST /api/v1/operations/call\n" +
			"with connector_id=\"sddc-rest-9.0\" pre-baked so operators don't\n" +
			"type the connector ID on every command.\n\n" +
			"Per CLAUDE.md postulate 5, these alias verbs are operator-only\n" +
			"ergonomics — they are not mirrored on the MCP surface. Agents\n" +
			"continue to use search_operations / call_operation against the\n" +
			"narrow-waist meta-tool contract.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newAboutCmd())
	cmd.AddCommand(newManagerCmd())
	cmd.AddCommand(newDomainCmd())
	cmd.AddCommand(newClusterCmd())
	cmd.AddCommand(newHostCmd())
	cmd.AddCommand(newNetworkPoolCmd())
	cmd.AddCommand(newBundleCmd())
	cmd.AddCommand(newWorkflowCmd())
	cmd.AddCommand(newOperationCmd())
	return cmd
}

// sddcEntry is a single row in an SDDC Manager list response.
type sddcEntry = map[string]any

// decodeElementsResult unwraps SDDC Manager's paginated `{"elements": [...]}`
// envelope or falls back to a bare array.
func decodeElementsResult(raw json.RawMessage) ([]sddcEntry, error) {
	if len(raw) == 0 || string(raw) == "null" {
		return nil, nil
	}
	var wrapped struct {
		Elements []sddcEntry `json:"elements"`
	}
	if err := json.Unmarshal(raw, &wrapped); err == nil && wrapped.Elements != nil {
		return wrapped.Elements, nil
	}
	var arr []sddcEntry
	if err := json.Unmarshal(raw, &arr); err != nil {
		return nil, fmt.Errorf("decode SDDC Manager list result: %w", err)
	}
	return arr, nil
}

// sddcStringField extracts a string value from an SDDC Manager entry map.
func sddcStringField(e sddcEntry, key string) string {
	v, ok := e[key]
	if !ok {
		return ""
	}
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}

// sddcNestedField extracts a nested map from an entry.
func sddcNestedField(e sddcEntry, key string) sddcEntry {
	v, ok := e[key]
	if !ok {
		return nil
	}
	if m, ok := v.(map[string]any); ok {
		return m
	}
	return nil
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
