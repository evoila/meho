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
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
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
	pretty, err := prettyJSON(r.Result)
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
		pretty, err := prettyJSON(r.Extras)
		if err == nil {
			fmt.Fprintln(w, pretty)
		} else {
			fmt.Fprintln(w, string(r.Extras))
		}
	}
}

type errNoBackplaneConfigured struct{ inner error }

func (e *errNoBackplaneConfigured) Error() string {
	return "no backplane URL configured; run `meho login <url>` first or pass --backplane <url>"
}
func (e *errNoBackplaneConfigured) Unwrap() error { return e.inner }

func resolveBackplane(override string) (string, error) {
	if override != "" {
		return normaliseURL(override)
	}
	cfg, err := auth.LoadConfig()
	if err != nil {
		if errors.Is(err, auth.ErrConfigNotFound) {
			return "", &errNoBackplaneConfigured{inner: err}
		}
		return "", err
	}
	return normaliseURL(cfg.BackplaneURL)
}

func classifyBackplaneError(err error) *output.StructuredError {
	if errors.Is(err, auth.ErrConfigNotFound) {
		return output.AuthExpired(err.Error())
	}
	return output.Unexpected(err.Error())
}

func normaliseURL(s string) (string, error) {
	trimmed := strings.TrimRight(strings.TrimSpace(s), "/")
	if trimmed == "" {
		return "", errors.New("backplane URL is empty")
	}
	u, err := url.ParseRequestURI(trimmed)
	if err != nil {
		return "", fmt.Errorf("invalid backplane URL %q: %w", s, err)
	}
	if u.Host == "" {
		return "", fmt.Errorf("backplane URL %q has no host", s)
	}
	u.Path = strings.TrimRight(u.Path, "/")
	return u.String(), nil
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
	var he *httpError
	if errors.As(err, &he) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("call %s: HTTP %d: %s",
				backplaneURL, he.StatusCode, he.Body)),
			jsonOut,
		)
	}
	return output.RenderError(cmd.ErrOrStderr(),
		output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, err)),
		jsonOut,
	)
}

type httpError struct {
	StatusCode int
	Body       string
}

func (e *httpError) Error() string {
	return fmt.Sprintf("HTTP %d: %s", e.StatusCode, e.Body)
}

func doAuthedRequest(
	ctx context.Context,
	backplaneURL, method, path string,
	body []byte,
) ([]byte, error) {
	authed, err := api.NewAuthedClient(ctx, backplaneURL, api.AuthedClientOptions{})
	if err != nil {
		return nil, err
	}
	httpClient := authed.HTTPClient()
	bearer := authed.AccessToken()
	if bearer == "" {
		return nil, errors.New("meho: stored token has no access_token")
	}
	resp, err := sendRequest(ctx, httpClient, backplaneURL, method, path, bearer, body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode == http.StatusUnauthorized {
		if rerr := authed.Refresh(ctx); rerr != nil {
			resp.Body.Close()
			return nil, rerr
		}
		resp.Body.Close()
		bearer = authed.AccessToken()
		resp, err = sendRequest(ctx, httpClient, backplaneURL, method, path, bearer, body)
		if err != nil {
			return nil, err
		}
	}
	defer resp.Body.Close()
	// 1 MiB cap matches the NSX sibling. vROps list payloads on a
	// large fleet can run into the hundreds of KiB; the cap leaves
	// headroom while still bounding pathological payloads.
	const maxBody = int64(1 << 20)
	raw, readErr := io.ReadAll(io.LimitReader(resp.Body, maxBody+1))
	if readErr != nil {
		return nil, fmt.Errorf("read response: %w", readErr)
	}
	if int64(len(raw)) > maxBody {
		return nil, fmt.Errorf("backplane response body exceeds %d bytes", maxBody)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, &httpError{StatusCode: resp.StatusCode, Body: strings.TrimSpace(string(raw))}
	}
	return raw, nil
}

func sendRequest(
	ctx context.Context,
	client *http.Client,
	backplaneURL, method, path, bearer string,
	body []byte,
) (*http.Response, error) {
	fullURL := backplaneURL + path
	var bodyReader io.Reader
	if body != nil {
		bodyReader = bytes.NewReader(body)
	}
	req, err := http.NewRequestWithContext(ctx, method, fullURL, bodyReader)
	if err != nil {
		return nil, fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+bearer)
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	return client.Do(req)
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
