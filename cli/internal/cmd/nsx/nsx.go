// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package nsx hosts the cobra commands under `meho nsx ...` for
// G3.5-T3 (#615) of Initiative #368. v0.2 ships the operator-facing
// alias verbs over the 9 enabled NSX read-only core ops, each
// pre-baking connector_id="nsx-rest-4.2" so operators don't type
// the connector ID on every dispatch:
//
//   - `meho nsx about [--target T]`                        — GET:/api/v1/node
//   - `meho nsx node list [--target T]`                    — GET:/api/v1/transport-nodes
//   - `meho nsx cluster status [--target T]`               — GET:/api/v1/cluster/status
//   - `meho nsx segment list [--target T]`                 — GET:/policy/api/v1/infra/segments
//   - `meho nsx transport-zone list [--target T]`          — GET:/policy/.../transport-zones
//   - `meho nsx tier0 list [--target T]`                   — GET:/policy/api/v1/infra/tier-0s
//   - `meho nsx tier1 list [--target T]`                   — GET:/policy/api/v1/infra/tier-1s
//   - `meho nsx firewall policy list [--scope D] [--target T]`
//   - `meho nsx firewall rule list <policy> [--scope D] [--target T]`
//   - `meho nsx operation search/call`                     — meta-tool wrappers
//
// Every verb is a thin Cobra command that POSTs to
// `/api/v1/operations/call` with connector_id="nsx-rest-4.2"
// pre-baked. No NSX logic in the CLI; pure Cobra-over-HTTP following
// the vmware precedent (CLAUDE.md postulate 5).
package nsx

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
	"github.com/evoila/meho/cli/internal/dispatch"
	"github.com/evoila/meho/cli/internal/output"
)

// ConnectorID is the pre-baked connector_id every verb under
// `meho nsx ...` dispatches against.
const ConnectorID = "nsx-rest-4.2"

// NewRootCmd returns the `meho nsx` parent command.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "nsx",
		Short: "Pre-scoped CLI verbs for the nsx-rest-4.2 connector",
		Long: "nsx is the operator-facing verb tree for the nsx-rest-4.2\n" +
			"connector. Each verb dispatches through POST /api/v1/operations/call\n" +
			"with connector_id=\"nsx-rest-4.2\" pre-baked so operators don't\n" +
			"type the connector ID on every command.\n\n" +
			"Per CLAUDE.md postulate 5, these alias verbs are operator-only\n" +
			"ergonomics — they are not mirrored on the MCP surface. Agents\n" +
			"continue to use search_operations / call_operation against the\n" +
			"narrow-waist meta-tool contract.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newAboutCmd())
	cmd.AddCommand(newNodeCmd())
	cmd.AddCommand(newClusterCmd())
	cmd.AddCommand(newSegmentCmd())
	cmd.AddCommand(newTransportZoneCmd())
	cmd.AddCommand(newTier0Cmd())
	cmd.AddCommand(newTier1Cmd())
	cmd.AddCommand(newFirewallCmd())
	cmd.AddCommand(newOperationCmd())
	return cmd
}

// nsxEntry is a single row in an NSX list response. NSX uses
// map[string]any because the fields differ per resource type.
type nsxEntry = map[string]any

// decodeNsxListResult unwraps NSX's `{"results": [...]}` envelope or
// falls back to a bare array.
func decodeNsxListResult(raw json.RawMessage) ([]nsxEntry, error) {
	if len(raw) == 0 || string(raw) == "null" {
		return nil, nil
	}
	var wrapped struct {
		Results []nsxEntry `json:"results"`
	}
	if err := json.Unmarshal(raw, &wrapped); err == nil && wrapped.Results != nil {
		return wrapped.Results, nil
	}
	var arr []nsxEntry
	if err := json.Unmarshal(raw, &arr); err != nil {
		return nil, fmt.Errorf("decode NSX list result: %w", err)
	}
	return arr, nil
}

// nsxStringField extracts a string value from an NSX entry map.
func nsxStringField(e nsxEntry, key string) string {
	v, ok := e[key]
	if !ok {
		return ""
	}
	if s, ok := v.(string); ok {
		return s
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
	const maxBody = int64(1 << 20) // 1 MiB
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

// nsxPathBasename returns the last path segment (the part after the final "/").
// NSX resource paths like /infra/sites/default/.../transport-zones/tz-1 encode
// the human-readable name as the last segment; displaying just the basename
// keeps list columns readable without truncating the identifier.
func nsxPathBasename(path string) string {
	if i := strings.LastIndex(path, "/"); i >= 0 && i < len(path)-1 {
		return path[i+1:]
	}
	return path
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
