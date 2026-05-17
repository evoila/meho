// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package vault hosts the cobra commands under `meho vault ...` for
// G3.3-T6 (#550) of Initiative #366. v0.2 ships the operator-facing
// alias verbs the Initiative #366 work-item table names, each pre-
// baking the `connector_id="vault-1.x"` argument so operators don't
// type the connector ID on every dispatch:
//
//   - `meho vault kv read <mount> <path>`        — vault.kv.read
//   - `meho vault kv list <mount> <path>`        — vault.kv.list
//   - `meho vault kv put <mount> <path> --data`  — vault.kv.put
//   - `meho vault kv versions <mount> <path>`    — vault.kv.versions
//   - `meho vault kv delete <mount> <path>`      — vault.kv.delete
//   - `meho vault sys health`                    — vault.sys.health
//   - `meho vault sys seal-status`               — vault.sys.seal_status
//   - `meho vault sys mounts-list`               — vault.sys.mounts.list
//   - `meho vault sys auth-list`                 — vault.sys.auth.list
//   - `meho vault auth userpass-list`            — vault.auth.userpass.list
//   - `meho vault auth userpass-read <user>`     — vault.auth.userpass.read
//   - `meho vault auth approle-list`             — vault.auth.approle.list
//   - `meho vault auth approle-read <role>`      — vault.auth.approle.read
//
// Every verb is a thin Cobra command that POSTs to
// `/api/v1/operations/call` with a pre-baked connector_id. No new
// backend code; no new HTTP routes — CLI alias verbs are pure operator
// ergonomics over the existing dispatcher surface (per CLAUDE.md
// postulate 5: agent surface stays narrow-waist meta-tools; vendor-
// specific tooling lives only in the CLI). The underlying typed ops
// register via G3.3-T1/T2/T3 (#545/#546/#547); this verb tree is the
// operator front-end over the same auth/policy/audit/JSONFlux path the
// agent surface uses.
//
// `meho vault kv read --target rdc-vault secret <path>` replaces the
// consumer's `_secret-read.sh secret/<mount>/<path>` wrapper.
package vault

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
// `meho vault ...` dispatches against. Exported so the per-verb files
// and tests reference the same constant; a future re-versioning
// (vault-2.x) lands as a single line edit here. The string form is the
// dispatcher's natural-key encoding (product="vault", version="1.x",
// impl_id="vault" → "vault-1.x"), pinned by the backend's
// connector-id-parse contract test.
const ConnectorID = "vault-1.x"

// NewRootCmd returns the `meho vault` parent command. cmd/root.go
// grafts this onto the top-level command tree alongside the other
// built-in verb trees (operation / connector / targets / kb /
// retrieval / audit / vmware). The parent itself takes no args and
// prints its own help; every piece of behaviour lives in the per-
// subcommand RunE closures.
//
// Sub-tree layout follows Initiative #366's work-item grouping:
//   - `vault kv <read|list|put|versions|delete>`  — KV-v2 sub-tree
//   - `vault sys <health|seal-status|mounts-list|auth-list>` — sys sub-tree
//   - `vault auth <userpass-list|userpass-read|approle-list|approle-read>`
//
// Sub-tree roots delegate to their own factories in this package so
// each noun's verbs live next to their tests.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "vault",
		Short: "Pre-scoped CLI verbs for the vault-1.x connector",
		Long: "vault is the operator-facing verb tree for the vault-1.x\n" +
			"connector. Each verb dispatches through POST /api/v1/operations/call\n" +
			"with connector_id=\"vault-1.x\" pre-baked so operators don't type\n" +
			"the connector ID on every command. The KV-v2 verbs address secrets\n" +
			"as <mount> <path> (mirroring the consumer's _secret-read.sh /\n" +
			"vault.sh wrappers); sys and auth verbs are read-only diagnostics.\n\n" +
			"Per CLAUDE.md postulate 5, these alias verbs are operator-only\n" +
			"ergonomics — they are not mirrored on the MCP surface. Agents\n" +
			"continue to use search_operations / call_operation against the\n" +
			"narrow-waist meta-tool contract.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newKVCmd())
	cmd.AddCommand(newSysCmd())
	cmd.AddCommand(newAuthCmd())
	return cmd
}

// errNoBackplaneConfigured wraps auth.ErrConfigNotFound so callers can
// distinguish "operator never logged in" (→ auth_expired exit code 2 —
// the right fix is `meho login`) from URL-parse failures (→ unexpected
// exit code 4 — the right fix is correcting argv). Same shape as the
// operation / connector / vmware siblings; kept independent because
// cmd/{operation,connector,kb,vmware,vault} can't import each other
// without an import cycle (cmd/root.go grafts each onto the tree).
type errNoBackplaneConfigured struct{ inner error }

func (e *errNoBackplaneConfigured) Error() string {
	return "no backplane URL configured; run `meho login <url>` first or pass --backplane <url>"
}
func (e *errNoBackplaneConfigured) Unwrap() error { return e.inner }

// resolveBackplane mirrors the vmware sibling helper: --backplane
// override flag wins; otherwise read the URL the most recent
// `meho login` wrote to config.json. Missing config surfaces as
// errNoBackplaneConfigured so classifyBackplaneError can route it to
// auth_expired.
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
// output.StructuredError category. Identical routing to the vmware
// sibling: missing-config → auth_expired; everything else (parse
// errors, fs errors) → unexpected.
func classifyBackplaneError(err error) *output.StructuredError {
	if errors.Is(err, auth.ErrConfigNotFound) {
		return output.AuthExpired(err.Error())
	}
	return output.Unexpected(err.Error())
}

// normaliseURL strips trailing slashes + parses the URL to fail fast
// on garbage input. Mirrors the vmware sibling.
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

// renderRequestError translates an error from doAuthedRequest into the
// right output.RenderError category. Same classification ladder as the
// vmware sibling: token-not-found / no-refresh-token → auth_expired
// with `meho login` hints; HTTP-error → unexpected; everything else
// (transport) → unreachable.
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

// httpError carries a non-2xx response so renderRequestError can pick
// the right StructuredError category. Same shape as the vmware sibling.
type httpError struct {
	StatusCode int
	Body       string
}

func (e *httpError) Error() string {
	return fmt.Sprintf("HTTP %d: %s", e.StatusCode, e.Body)
}

// doAuthedRequest issues a single HTTP request against the backplane
// with bearer injection + one-shot 401-refresh-retry. Mirrors the
// vmware sibling verbatim (duplicated to avoid an import cycle —
// cmd/root.go grafts each onto the tree). Centralised per-package so
// the per-verb runners stay small.
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

	// 1 MiB cap matches the vmware sibling. Vault secret payloads are
	// small; the cap leaves headroom while still bounding pathological
	// payloads.
	raw, readErr := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if readErr != nil {
		return nil, fmt.Errorf("read response: %w", readErr)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, &httpError{StatusCode: resp.StatusCode, Body: strings.TrimSpace(string(raw))}
	}
	return raw, nil
}

// sendRequest builds + fires the HTTP request. Mirrors the vmware
// sibling; split out so the 401-refresh-retry path can reuse the same
// body bytes without re-marshalling.
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

// loadJSONFlag parses a flag value that is either inline JSON or an
// `@<file>` reference. Returns nil for an empty value so the caller
// can omit the key. Same shape as the vmware sibling's
// loadParamsFlag, kept local to avoid the cross-package import cycle.
func loadJSONFlag(val string) (map[string]any, error) {
	if val == "" {
		return nil, nil
	}
	var raw []byte
	if strings.HasPrefix(val, "@") {
		path := strings.TrimPrefix(val, "@")
		var err error
		raw, err = os.ReadFile(path)
		if err != nil {
			return nil, fmt.Errorf("read JSON file %q: %w", path, err)
		}
	} else {
		raw = []byte(val)
	}
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		return nil, fmt.Errorf("parse JSON: %w", err)
	}
	return m, nil
}

// truncate cuts s to maxLen runes, appending an ellipsis when
// truncation happened. Operates on runes (not bytes) so multi-byte
// UTF-8 in Vault-side names survives without producing an invalid
// UTF-8 cut. Same implementation as the vmware sibling — duplicated to
// avoid the import cycle.
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
