// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package hetznerrobot hosts the cobra commands under `meho hetzner-robot
// ...` for G3.7-T9 (#852) of Initiative #370. v0.2 ships the
// operator-facing alias verbs over the 10 Hetzner Robot read-only core
// ops, each pre-baking connector_id="hetzner-rest-2026.04" so operators
// don't type the connector ID on every dispatch:
//
//   - `meho hetzner-robot about [--target T]`               — GET:/query
//   - `meho hetzner-robot server list [--target T]`         — GET:/server
//   - `meho hetzner-robot server info <server-ip>`          — GET:/server/{server-ip}
//   - `meho hetzner-robot ip list [--target T]`             — GET:/ip
//   - `meho hetzner-robot subnet list [--target T]`         — GET:/subnet
//   - `meho hetzner-robot vswitch list [--target T]`        — GET:/vswitch
//   - `meho hetzner-robot vswitch info <id>`                — GET:/vswitch/{id}
//   - `meho hetzner-robot failover list [--target T]`       — GET:/failover
//   - `meho hetzner-robot rdns list [--target T]`           — GET:/rdns
//   - `meho hetzner-robot ssh-key list [--target T]`        — GET:/key
//   - `meho hetzner-robot operation search "<query>"`       — search pre-scoped
//   - `meho hetzner-robot operation call <op_id> ...`       — call pre-scoped
//
// Every verb POSTs to `/api/v1/operations/call` (or GETs
// `/api/v1/operations/search` for the search wrapper) with
// connector_id="hetzner-rest-2026.04" pre-baked. No Hetzner Robot logic
// in the CLI; pure Cobra-over-HTTP per CLAUDE.md postulate 5.
//
// WARNING — 401 IP-BLOCK RISK: Hetzner Robot blocks the source IP for
// 10 minutes after 3 consecutive 401 responses from the same IP. MEHO
// shares a single egress IP across all operators. A misconfigured target
// or wrong credentials will trip the block for ALL operators on the
// shared egress. The connector raises auth_failed on the FIRST 401 and
// never retries. If you see an auth_failed error, fix the Webservice-user
// credentials at the target's Vault path before retrying.
package hetznerrobot

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
// `meho hetzner-robot ...` dispatches against.
const ConnectorID = "hetzner-rest-2026.04"

// NewRootCmd returns the `meho hetzner-robot` parent command.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "hetzner-robot",
		Short: "Pre-scoped CLI verbs for the hetzner-rest-2026.04 connector",
		Long: "hetzner-robot is the operator-facing verb tree for the\n" +
			"hetzner-rest-2026.04 connector. Each verb dispatches through\n" +
			"POST /api/v1/operations/call with connector_id=\n" +
			"\"hetzner-rest-2026.04\" pre-baked.\n\n" +
			"WARNING — 401 IP-BLOCK RISK:\n" +
			"Hetzner Robot blocks the source IP for 10 minutes after 3\n" +
			"consecutive 401 responses from the same IP. MEHO runs on a\n" +
			"shared egress IP — a misconfigured target will lock out ALL\n" +
			"operators for 10 minutes. The connector raises auth_failed on\n" +
			"the FIRST 401 and never retries. Fix the Webservice-user\n" +
			"credentials at the target's Vault path before retrying.\n\n" +
			"The Webservice user is DISTINCT from the Robot portal login.\n" +
			"Create it in the Robot portal under Account > Settings >\n" +
			"Webservice and store the credentials in Vault under\n" +
			"target.secret_ref as {\"username\": ..., \"password\": ...}.\n\n" +
			"Per CLAUDE.md postulate 5, these alias verbs are operator-only\n" +
			"ergonomics — they are not mirrored on the MCP surface. Agents\n" +
			"continue to use search_operations / call_operation against the\n" +
			"narrow-waist meta-tool contract.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newAboutCmd())
	cmd.AddCommand(newServerCmd())
	cmd.AddCommand(newIPCmd())
	cmd.AddCommand(newSubnetCmd())
	cmd.AddCommand(newVswitchCmd())
	cmd.AddCommand(newFailoverCmd())
	cmd.AddCommand(newRdnsCmd())
	cmd.AddCommand(newSSHKeyCmd())
	cmd.AddCommand(newOperationCmd())
	return cmd
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

// strDeref dereferences an optional string pointer.
func strDeref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}
