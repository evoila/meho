// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package gcloud hosts the cobra commands under `meho gcloud ...` for
// G3.7-T6 (#851) of Initiative #370. The verb tree is a thin Cobra
// layer over `POST /api/v1/operations/call`, pre-baking
// `connector_id="gcloud-rest-1.0"` so operators don't type the
// connector ID on every dispatch:
//
//   - `meho gcloud about [--target T]`                  — gcloud.about
//   - `meho gcloud project describe [--target T]`       — gcloud.project.describe
//   - `meho gcloud services list [--target T]`          — gcloud.services.list
//   - `meho gcloud iam sa list [--target T]`            — gcloud.iam.service_accounts.list
//   - `meho gcloud iam policy read [--target T]`        — gcloud.iam.policy.read
//   - `meho gcloud compute instances list [--target T]` — gcloud.compute.instances.list
//   - `meho gcloud compute networks list [--target T]`  — gcloud.compute.networks.list
//   - `meho gcloud compute subnets list [--target T]`   — gcloud.compute.subnetworks.list
//
// Every verb is a thin Cobra command that POSTs to
// `/api/v1/operations/call` with a pre-baked connector_id. No new
// backend code; no new HTTP routes — the CLI alias verbs are pure
// operator ergonomics over the existing dispatcher surface (per
// CLAUDE.md postulate 5: agent surface stays narrow-waist meta-tools;
// vendor-specific tooling lives only in the CLI).
//
// Auth model: GCP Application Default Credentials + Service Account
// Impersonation. The connector refuses SA JSON key material in
// `secret_ref` — org policy `constraints/iam.disableServiceAccountKeyCreation`
// is in force. The CLI is auth-agnostic (it POSTs to the backplane);
// the backend connector enforces the key refusal.
//
// The verb tree replaces `scripts/gcloud.sh` for the read-only
// workflows the operator runs daily (identity check, service audit,
// IAM review, VM inventory).
package gcloud

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/output"
)

// ConnectorID is the pre-baked connector_id every verb under
// `meho gcloud ...` dispatches against. Exported so the per-verb
// files and tests reference the same constant; a future re-versioning
// lands as a single-line edit here.
//
// The id encodes the registry-v2 natural key triple
// `(product="gcloud", version="1.0", impl_id="gcloud-rest")` per the
// connector_id parser convention in
// `backend/src/meho_backplane/operations/_lookup.py::parse_connector_id`.
const ConnectorID = "gcloud-rest-1.0"

// NewRootCmd returns the `meho gcloud` parent command. cmd/root.go
// grafts this onto the top-level command tree. The parent itself takes
// no args and prints its own help; every piece of behaviour lives in
// the per-subcommand RunE closures.
//
// Sub-tree layout follows the gcloud op groupings (Initiative #370):
//
//	gcloud about                             — identity / health check
//	gcloud project <describe>                — CRM project resource
//	gcloud services <list>                   — enabled APIs audit
//	gcloud iam <sa list | policy read>       — IAM inventory
//	gcloud compute <instances|networks|subnets> <list> — Compute inventory
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "gcloud",
		Short: "Pre-scoped CLI verbs for the gcloud-rest-1.0 connector",
		Long: "gcloud is the operator-facing verb tree for the gcloud-rest-1.0\n" +
			"connector (registry triple (product=\"gcloud\", version=\"1.0\",\n" +
			"impl_id=\"gcloud-rest\")). Each verb dispatches through\n" +
			"POST /api/v1/operations/call with connector_id=\"gcloud-rest-1.0\"\n" +
			"pre-baked so operators don't type the connector ID on every\n" +
			"command. Auth uses GCP Application Default Credentials + Service\n" +
			"Account Impersonation — SA JSON key material is refused by the\n" +
			"backend (org policy disableServiceAccountKeyCreation).\n\n" +
			"Per CLAUDE.md postulate 5, these alias verbs are operator-only\n" +
			"ergonomics — they are not mirrored on the MCP surface. Agents\n" +
			"continue to use search_operations / call_operation against the\n" +
			"narrow-waist meta-tool contract.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newAboutCmd())
	cmd.AddCommand(newProjectCmd())
	cmd.AddCommand(newServicesCmd())
	cmd.AddCommand(newIamCmd())
	cmd.AddCommand(newComputeCmd())
	return cmd
}

// errNoBackplaneConfigured wraps auth.ErrConfigNotFound so callers can
// distinguish "operator never logged in" from URL-parse failures.
// Same shape as the bind9 / k8s siblings; kept independent because
// cmd packages can't import each other without an import cycle
// (cmd/root.go grafts each onto the tree).
type errNoBackplaneConfigured struct{ inner error }

func (e *errNoBackplaneConfigured) Error() string {
	return "no backplane URL configured; run `meho login <url>` first or pass --backplane <url>"
}
func (e *errNoBackplaneConfigured) Unwrap() error { return e.inner }

// resolveBackplane mirrors the bind9 / k8s sibling helpers:
// --backplane override flag wins; otherwise read the URL the most
// recent `meho login` wrote to config.json.
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

// classifyBackplaneError maps a resolveBackplane error to the right
// output.StructuredError category. Mirrors the bind9 / k8s siblings.
func classifyBackplaneError(err error) *output.StructuredError {
	if errors.Is(err, auth.ErrConfigNotFound) {
		return output.AuthExpired(err.Error())
	}
	return output.Unexpected(err.Error())
}

// normaliseURL strips trailing slashes + parses the URL to fail fast
// on garbage input. Mirrors the bind9 / k8s siblings.
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

// renderRequestError translates a doAuthedRequest error into the right
// output.RenderError category. Same classification ladder as the
// bind9 / k8s siblings.
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

// httpError carries a non-2xx response. Same shape as the bind9 / k8s
// siblings.
type httpError struct {
	StatusCode int
	Body       string
}

func (e *httpError) Error() string {
	return fmt.Sprintf("HTTP %d: %s", e.StatusCode, e.Body)
}

// doAuthedRequest issues a single HTTP request against the backplane
// with bearer injection + one-shot 401-refresh-retry. Mirrors the
// bind9 / k8s siblings verbatim (duplicated to avoid an import cycle).
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

	// 1 MiB cap — gcloud aggregated instance/subnet lists can be
	// large on projects with many zones/regions; the cap bounds
	// pathological payloads.
	raw, readErr := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if readErr != nil {
		return nil, fmt.Errorf("read response: %w", readErr)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, &httpError{StatusCode: resp.StatusCode, Body: strings.TrimSpace(string(raw))}
	}
	return raw, nil
}

// sendRequest builds + fires the HTTP request. Mirrors the bind9 / k8s
// siblings; split out so the 401-refresh-retry path can reuse the
// same body bytes without re-marshalling.
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

// truncate cuts s to maxLen runes, appending an ellipsis when
// truncation happened. Operates on runes (not bytes) so multi-byte
// UTF-8 in GCP resource names survives. Same implementation as the
// bind9 / k8s siblings.
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

// CallResult mirrors the backend OperationResult Pydantic model. Same
// shape as the bind9 / k8s siblings; duplicated to avoid an import
// cycle.
type CallResult struct {
	Status     string          `json:"status"`
	OpID       string          `json:"op_id"`
	Result     json.RawMessage `json:"result"`
	Error      *string         `json:"error"`
	Extras     json.RawMessage `json:"extras,omitempty"`
	DurationMs float64         `json:"duration_ms"`
}

// callRequestBody mirrors the backend CallOperationBody Pydantic
// model. Target uses a map[string]any so the empty case serialises
// as `null`; the route layer's resolver short-circuits on missing-name
// for ops that need a target.
type callRequestBody struct {
	ConnectorID string         `json:"connector_id"`
	OpID        string         `json:"op_id"`
	Target      map[string]any `json:"target"`
	Params      map[string]any `json:"params,omitempty"`
}

// errOpError is the sentinel returned when the dispatcher reported a
// structured-failure result (status == "error" or status == "denied").
var errOpError = errors.New("operation status not ok")

// dispatchOp POSTs an OperationCall to the backplane and returns the
// decoded CallResult. The pre-baked connector_id ("gcloud-rest-1.0")
// is baked in; callers pass op_id, an optional target slug (empty
// string → no target field on the wire), and an optional params map.
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

// renderCallResult handles the unified post-dispatch path every verb
// uses: validate status enum, render the envelope (JSON or human),
// then translate "error" / "denied" into errOpError.
func renderCallResult(
	cmd *cobra.Command,
	opID string,
	r *CallResult,
	jsonOut bool,
	prettyPrinter func(w io.Writer, r *CallResult),
) error {
	switch r.Status {
	case "ok", "error", "denied":
		// expected statuses — fall through
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

// printGenericResult renders a CallResult in the generic envelope
// shape. Used as the fallback pretty-printer for verbs that don't
// define their own table layout.
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
	printErrorTrailer(w, r)
}

// printErrorTrailer surfaces the dispatcher error / extras envelope.
// Mirrors the bind9 / k8s siblings.
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

// prettyJSON pretty-prints a json.RawMessage with 2-space indent.
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

// errRowsKeyAbsent is returned by decodeRowsResult when the result
// envelope is a well-formed JSON object that carries no `rows` key at
// all. This is distinct from an empty list (`{"rows": []}`): an absent
// key signals a malformed or unexpected envelope, so callers route it
// to fallbackResultRender (dump the raw shape) rather than rendering a
// misleading "(0 rows)" line. A sentinel so callers can branch on it
// via errors.Is if they ever need to.
var errRowsKeyAbsent = errors.New("rows key absent from result envelope")

// decodeRowsResult decodes the canonical `{"rows": [...], "total": N}`
// envelope that every set-shaped gcloud read op returns. Returns the
// row list, or an error when the shape doesn't match.
//
// An absent `rows` key is treated as a malformed envelope and reported
// as errRowsKeyAbsent — distinct from a legitimately-empty list
// (`{"rows": []}`), which returns an empty slice and a nil error. A
// JSON null result (or no result at all) is the operation's "nothing to
// render" case and returns (nil, nil), unchanged from before.
func decodeRowsResult(raw json.RawMessage) ([]map[string]any, error) {
	if len(raw) == 0 || string(raw) == "null" {
		return nil, nil
	}
	var envelope map[string]json.RawMessage
	if err := json.Unmarshal(raw, &envelope); err != nil {
		return nil, fmt.Errorf("decode rows envelope: %w", err)
	}
	rowsRaw, ok := envelope["rows"]
	if !ok {
		return nil, errRowsKeyAbsent
	}
	var rows []map[string]any
	if err := json.Unmarshal(rowsRaw, &rows); err != nil {
		return nil, fmt.Errorf("decode rows: %w", err)
	}
	return rows, nil
}

// decodeFlatResult decodes a flat-dict result (gcloud.about,
// gcloud.project.describe, gcloud.iam.policy.read). Returns the
// decoded map or an error.
func decodeFlatResult(raw json.RawMessage) (map[string]any, error) {
	if len(raw) == 0 || string(raw) == "null" {
		return nil, nil
	}
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		return nil, fmt.Errorf("decode flat: %w", err)
	}
	return m, nil
}

// stringField pulls a string field from a result entry, returning empty
// string when the field is missing or wrong type. Mirrors the bind9 /
// k8s siblings.
func stringField(e map[string]any, key string) string {
	v, ok := e[key]
	if !ok {
		return ""
	}
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}

// boolField pulls a boolean field from a result entry, returning false
// when the field is missing or wrong type.
func boolField(e map[string]any, key string) bool {
	v, ok := e[key]
	if !ok {
		return false
	}
	if b, ok := v.(bool); ok {
		return b
	}
	return false
}

// fallbackResultRender dumps the result envelope verbatim when the
// typed per-verb decode fails. Used by every verb's pretty-printer
// so contract drift surfaces with the same affordance.
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
