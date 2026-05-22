// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package vcflogs hosts the cobra commands under `meho vcf-logs ...`
// for G3.6-T6 (#838) of Initiative #369. v0.5 ships the operator-facing
// alias verbs over the 7 enabled vRLI read-only core ops, each
// pre-baking connector_id="vrli-rest-9.0" so operators don't type the
// connector ID on every dispatch:
//
//   - `meho vcf-logs about [--target T]`                        — GET:/api/v2/version
//   - `meho vcf-logs query <constraints> [--time-range D] [--limit N] [--target T]`
//     — GET:/api/v2/events/{constraints}
//   - `meho vcf-logs aggregated <constraints> [--time-range D] [--target T]`
//     — GET:/api/v2/aggregated-events/{constraints}
//   - `meho vcf-logs field list [--target T]`                   — GET:/api/v2/fields
//   - `meho vcf-logs host list [--target T]`                    — GET:/api/v2/hosts
//   - `meho vcf-logs content-pack list [--target T]`            — GET:/api/v2/content/contentpack/list
//   - `meho vcf-logs alert list [--target T]`                   — GET:/api/v2/alerts
//   - `meho vcf-logs operation search/call`                     — meta-tool wrappers
//
// Every verb is a thin Cobra command that POSTs to
// `/api/v1/operations/call` with connector_id="vrli-rest-9.0" pre-baked.
// No vRLI logic in the CLI; pure Cobra-over-HTTP following the nsx /
// sddc-manager precedent (CLAUDE.md postulate 5).
//
// Per CLAUDE.md postulate 5, these alias verbs are operator-only
// ergonomics — they are not mirrored on the MCP surface. Agents
// continue to use search_operations / call_operation against the
// narrow-waist meta-tool contract.
package vcflogs

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
// `meho vcf-logs ...` dispatches against. The dispatcher's
// `parse_connector_id` splits this on the first hyphen-segment into
// (product="vrli", version="9.0", impl_id="vrli-rest") — see
// backend/src/meho_backplane/connectors/vcf_logs/core_ops.py
// (VRLI_CONNECTOR_ID).
const ConnectorID = "vrli-rest-9.0"

// NewRootCmd returns the `meho vcf-logs` parent command.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "vcf-logs",
		Short: "Pre-scoped CLI verbs for the vrli-rest-9.0 connector (VCF Operations for Logs)",
		Long: "vcf-logs is the operator-facing verb tree for the vrli-rest-9.0\n" +
			"connector (VMware Aria Operations for Logs, formerly vRLI). Each verb\n" +
			"dispatches through POST /api/v1/operations/call with connector_id=\n" +
			"\"vrli-rest-9.0\" pre-baked so operators don't type the connector ID\n" +
			"on every command.\n\n" +
			"Per CLAUDE.md postulate 5, these alias verbs are operator-only\n" +
			"ergonomics — they are not mirrored on the MCP surface. Agents\n" +
			"continue to use search_operations / call_operation against the\n" +
			"narrow-waist meta-tool contract.\n\n" +
			"Replaces the consumer's `./scripts/vcf-logs.sh` wrapper.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newAboutCmd())
	cmd.AddCommand(newQueryCmd())
	cmd.AddCommand(newAggregatedCmd())
	cmd.AddCommand(newFieldCmd())
	cmd.AddCommand(newHostCmd())
	cmd.AddCommand(newContentPackCmd())
	cmd.AddCommand(newAlertCmd())
	cmd.AddCommand(newOperationCmd())
	return cmd
}

// vrliEntry is a single row in a vRLI list response. The vRLI APIs
// return divergent shapes per endpoint (events have one envelope,
// fields/hosts have another, content-packs another); the list verbs
// each inspect the result map themselves.
type vrliEntry = map[string]any

// decodeArrayField extracts a top-level array field by key (e.g.
// "events", "fields", "hosts", "contentPackMetadataList", "alerts")
// from a JSON object envelope. Falls back to a bare array if the raw
// payload is a JSON array. Returns nil for empty/null payloads.
//
// vRLI's response envelopes are not uniform: events come back under
// `events`, fields under `fields`, hosts under `hosts`, content packs
// under `contentPackMetadataList`, alerts under `alerts`. The list
// verbs each pass their own key; this helper centralises the
// envelope-unwrap logic.
func decodeArrayField(raw json.RawMessage, key string) ([]vrliEntry, error) {
	if len(raw) == 0 || string(raw) == "null" {
		return nil, nil
	}
	var asMap map[string]json.RawMessage
	if err := json.Unmarshal(raw, &asMap); err == nil {
		if inner, ok := asMap[key]; ok {
			var arr []vrliEntry
			if uerr := json.Unmarshal(inner, &arr); uerr != nil {
				return nil, fmt.Errorf("decode %s array under %q: %w", "vRLI", key, uerr)
			}
			return arr, nil
		}
	}
	var arr []vrliEntry
	if err := json.Unmarshal(raw, &arr); err != nil {
		return nil, fmt.Errorf("decode vRLI list result: %w", err)
	}
	return arr, nil
}

// vrliStringField extracts a string value from a vRLI entry map.
func vrliStringField(e vrliEntry, key string) string {
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
