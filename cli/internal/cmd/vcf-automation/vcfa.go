// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package vcfautomation hosts the cobra commands under `meho
// vcf-automation ...` for G3.6-T12 (#840) of Initiative #369. v0.5
// ships the operator-facing verb tree over the 11 enabled VCF
// Automation 9.0 read-only core ops (6 provider plane + 5 tenant
// plane), each pre-baking connector_id="vcfa-rest-9.0" so operators
// don't type the connector ID on every dispatch:
//
//   - `meho vcf-automation about --plane provider|tenant`
//   - `meho vcf-automation org list|get --plane provider`
//   - `meho vcf-automation region list|get --plane provider`
//   - `meho vcf-automation user list --plane provider`
//   - `meho vcf-automation project list --plane tenant`
//   - `meho vcf-automation deployment list|get --plane tenant`
//   - `meho vcf-automation blueprint list --plane tenant`
//   - `meho vcf-automation operation search|call` — meta-tool wrappers
//
// The dual-plane shape is the load-bearing departure from the other
// VCF management-plane CLIs: VCFA 9.x exposes two API planes on the
// same appliance (vCloud-Director-derived /cloudapi/* + Aria-IaaS-
// derived /iaas/api/*) with bespoke per-plane auth (HTTP Basic + JWT
// header vs JSON body + bearer). The `--plane` persistent flag picks
// the op namespace; the backend dispatcher routes each op to the
// correct auth plane via the spec_source tag the G0.7 dual-spec
// ingest (#836) wrote on every descriptor.
//
// Vhost routing is the second departure: VCFA enforces strict Host:
// header matching and returns 404 with empty body on every path when
// reached by IP without the correct vhost set. The `--fqdn` flag is a
// per-call override for the resolved target's fqdn column; the
// canonical home for the value is `fqdn:` in targets.yaml so the
// override only matters as a debugging escape hatch. Without an fqdn
// the connector raises VcfAutomationConfigurationError at session-
// establish time and surfaces a structured error_code on the wire
// rather than a confusing post-login 404 storm.
//
// Per CLAUDE.md postulate 5, these alias verbs are operator-only
// ergonomics -- they are not mirrored on the MCP surface. Agents
// continue to use search_operations / call_operation against the
// narrow-waist meta-tool contract.
package vcfautomation

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

// ConnectorID is the pre-baked connector_id every verb under `meho
// vcf-automation ...` dispatches against.
const ConnectorID = "vcfa-rest-9.0"

// Plane names recognised on the `--plane` persistent flag. Mirrors
// the backend's `Plane = Literal["provider", "tenant"]` literal type
// so the CLI rejects typos at the cobra layer rather than letting an
// unknown plane string slip through to a 404 from the dispatcher.
const (
	PlaneProvider = "provider"
	PlaneTenant   = "tenant"
)

// NewRootCmd returns the `meho vcf-automation` parent command.
//
// `--plane` is a persistent flag every verb in the tree inspects.
// Per-verb behaviour:
//
//   - Verbs unique to one plane (org/region/user on provider;
//     project/deployment/blueprint on tenant) treat `--plane` as a
//     consistency check: if the operator passed the wrong plane the
//     command refuses early with a clear message instead of dispatching
//     against the wrong op_id.
//   - `about` is dual-plane and reads `--plane` as required input.
//   - `operation call` / `operation search` ignore `--plane` (the raw
//     op_id already carries the plane via its path prefix).
//
// `--fqdn` is the second persistent flag; honoured by every verb that
// dispatches an op (it threads into the body's `target.fqdn` field
// before the POST). When unset the backend reads `target.fqdn` from
// the targets registry; when set, the value overrides for this one
// call only (the DB row is not modified). The canonical home for the
// value is `fqdn:` in targets.yaml.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "vcf-automation",
		Short: "Pre-scoped CLI verbs for the vcfa-rest-9.0 dual-plane connector",
		Long: "vcf-automation is the operator-facing verb tree for the\n" +
			"vcfa-rest-9.0 connector. Each verb dispatches through\n" +
			"POST /api/v1/operations/call with connector_id=\"vcfa-rest-9.0\"\n" +
			"pre-baked.\n\n" +
			"VCFA 9.x is **dual-plane** on a single appliance:\n" +
			"  - provider plane (/cloudapi/* + /api/*) — HTTP Basic →\n" +
			"    X-VMWARE-VCLOUD-ACCESS-TOKEN JWT\n" +
			"  - tenant plane (/iaas/api/*) — JSON POST → {\"token\": ...}\n" +
			"`--plane provider|tenant` picks the op namespace; the\n" +
			"backend dispatcher routes the call to the correct auth\n" +
			"plane via the descriptor's spec_source tag.\n\n" +
			"VCFA enforces strict vhost (Host:) routing. When the target\n" +
			"is reached by IP, set `--fqdn <vhost>` (or `fqdn:` in\n" +
			"targets.yaml) to the appliance's canonical FQDN — without\n" +
			"it every path returns 404 with empty body before the\n" +
			"application sees the request.\n\n" +
			"Per CLAUDE.md postulate 5, these alias verbs are operator-\n" +
			"only ergonomics; they are not mirrored on the MCP surface.\n" +
			"Agents continue to use search_operations / call_operation.",
		SilenceUsage: true,
	}
	// Persistent flags are read by every verb's RunE through cmd.Flags().
	cmd.PersistentFlags().String(
		"plane", "",
		"VCFA plane to target: 'provider' (cloudapi/*) or 'tenant' (iaas/api/*)",
	)
	cmd.PersistentFlags().String(
		"fqdn", "",
		"per-call vhost override (target.fqdn); honoured by the connector for vhost routing",
	)

	cmd.AddCommand(newAboutCmd())
	cmd.AddCommand(newOrgCmd())
	cmd.AddCommand(newRegionCmd())
	cmd.AddCommand(newUserCmd())
	cmd.AddCommand(newProjectCmd())
	cmd.AddCommand(newDeploymentCmd())
	cmd.AddCommand(newBlueprintCmd())
	cmd.AddCommand(newOperationCmd())
	return cmd
}

// readPlane returns the value of the persistent --plane flag, walking
// up to the parent command (the persistent flag is registered on the
// `vcf-automation` root; sub-sub-commands like `org list` need the
// walk to find it).
func readPlane(cmd *cobra.Command) string {
	v, _ := cmd.Flags().GetString("plane")
	return strings.TrimSpace(v)
}

// readFqdn returns the persistent --fqdn override (may be empty).
func readFqdn(cmd *cobra.Command) string {
	v, _ := cmd.Flags().GetString("fqdn")
	return strings.TrimSpace(v)
}

// validatePlane returns an error when the caller passed --plane=X but
// the verb expects --plane=expected. Empty --plane is accepted on
// single-plane verbs (the verb's own plane is the implicit default).
func validatePlane(got, expected string) *output.StructuredError {
	if got == "" || got == expected {
		return nil
	}
	return output.Unexpected(fmt.Sprintf(
		"--plane %q is invalid for this verb; expected --plane %q (or omit the flag)",
		got, expected,
	))
}

// requirePlane returns an error when --plane is missing or unknown on
// a verb that is dual-plane (the `about` verb). Both planes share the
// resource name so we cannot derive the plane from the verb itself.
func requirePlane(got string) *output.StructuredError {
	switch got {
	case PlaneProvider, PlaneTenant:
		return nil
	case "":
		return output.Unexpected(
			"--plane is required for this verb; pass --plane provider or --plane tenant",
		)
	default:
		return output.Unexpected(fmt.Sprintf(
			"--plane %q is unknown; pass --plane provider or --plane tenant", got,
		))
	}
}

// vcfaEntry is a single row in a VCFA list response. VCFA returns
// either a `values` array (provider plane, vCD lineage) or a `content`
// array (tenant plane, Aria IaaS lineage); the per-verb renderers
// know which.
type vcfaEntry = map[string]any

// decodeProviderListResult unwraps the provider plane's
// {"values": [...], "resultTotal": N} envelope.
func decodeProviderListResult(raw json.RawMessage) ([]vcfaEntry, error) {
	if len(raw) == 0 || string(raw) == "null" {
		return nil, nil
	}
	var wrapped struct {
		Values []vcfaEntry `json:"values"`
	}
	if err := json.Unmarshal(raw, &wrapped); err == nil && wrapped.Values != nil {
		return wrapped.Values, nil
	}
	var arr []vcfaEntry
	if err := json.Unmarshal(raw, &arr); err != nil {
		return nil, fmt.Errorf("decode provider list result: %w", err)
	}
	return arr, nil
}

// decodeTenantListResult unwraps the tenant plane's
// {"content": [...], "totalElements": N} envelope.
func decodeTenantListResult(raw json.RawMessage) ([]vcfaEntry, error) {
	if len(raw) == 0 || string(raw) == "null" {
		return nil, nil
	}
	var wrapped struct {
		Content []vcfaEntry `json:"content"`
	}
	if err := json.Unmarshal(raw, &wrapped); err == nil && wrapped.Content != nil {
		return wrapped.Content, nil
	}
	var arr []vcfaEntry
	if err := json.Unmarshal(raw, &arr); err != nil {
		return nil, fmt.Errorf("decode tenant list result: %w", err)
	}
	return arr, nil
}

// vcfaStringField extracts a string value from a VCFA entry map.
func vcfaStringField(e vcfaEntry, key string) string {
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
