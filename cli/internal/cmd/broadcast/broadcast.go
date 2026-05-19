// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package broadcast hosts the cobra commands under `meho broadcast ...`
// for G6.3-T4 (#381) of Initiative #376. v0.2 ships three tenant-admin
// verbs wrapping the T4 REST surface (`/api/v1/broadcast/overrides`):
//
//   - `meho broadcast overrides list [--op-id-pattern P] [--json]
//     [--backplane URL]` — GET the operator's tenant's rules.
//   - `meho broadcast overrides set --op-id-pattern P [--scope-field F
//     --scope-value V] --detail D [--json] [--backplane URL]` — POST
//     a new rule.
//   - `meho broadcast overrides remove <id> [--json] [--backplane URL]` —
//     DELETE one rule by id.
//
// Each verb authenticates via the token `meho login` wrote, same
// shape as `meho audit` / `meho targets` / `meho retrieval`. RBAC at
// the backend rejects non-`tenant_admin` callers with HTTP 403; the
// verb renders this as `insufficient_role`.
package broadcast

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

// NewRootCmd returns the `meho broadcast` parent command. Mounted on
// the top-level meho tree by cmd/root.go alongside `meho audit`,
// `meho operation`, etc.
//
// The parent command takes no args and prints its own help; every
// behaviour lives in the per-subcommand RunE closures.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "broadcast",
		Short:        "Manage broadcast-detail overrides (overrides list / set / remove)",
		Long:         broadcastLongHelp,
		SilenceUsage: true,
	}
	cmd.AddCommand(newOverridesCmd())
	return cmd
}

const broadcastLongHelp = "Tenant-admin verbs for managing per-tenant broadcast-detail " +
	"override rules. The rules feed the publish-time resolver (G6.3-T2 #379) " +
	"that decides whether each broadcast event renders full-detail or " +
	"aggregate-only. Every verb is tenant_admin-only; non-admin callers " +
	"see 403 insufficient_role. Cross-tenant probes (DELETE on another " +
	"tenant's id) return 404 -- existence is not leaked across tenant " +
	"boundaries."

// newOverridesCmd returns the `meho broadcast overrides` parent.
func newOverridesCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "overrides",
		Short: "List, create, and delete broadcast-detail override rules",
		Long: "overrides wraps the three /api/v1/broadcast/overrides routes " +
			"shipped by G6.3-T4 (#381). Use `list` to inspect, `set` to " +
			"create a new rule, and `remove` to delete one by id.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newOverridesListCmd())
	cmd.AddCommand(newOverridesSetCmd())
	cmd.AddCommand(newOverridesRemoveCmd())
	return cmd
}

// Entry mirrors the backend `BroadcastOverrideRead` Pydantic model
// (`backend/src/meho_backplane/api/v1/broadcast_overrides.py`). Hand-
// written rather than aliased to a generated client type so the
// broadcast package stays decoupled from oapi-codegen churn -- same
// stance as the audit / targets / retrieval packages.
//
// `ScopeField` and `ScopeValue` are `*string` so the JSON round-trip
// preserves the explicit-null wire shape (an op-wide rule has both
// fields null).
type Entry struct {
	ID            string  `json:"id"`
	TenantID      string  `json:"tenant_id"`
	OpIDPattern   string  `json:"op_id_pattern"`
	ScopeField    *string `json:"scope_field"`
	ScopeValue    *string `json:"scope_value"`
	Detail        string  `json:"detail"`
	CreatedBySub  string  `json:"created_by_sub"`
	CreatedAt     string  `json:"created_at"`
	UpdatedAt     string  `json:"updated_at"`
}

// CreateRequest mirrors the backend `BroadcastOverrideCreate` Pydantic
// model. Optional fields use `*string` so the JSON round-trip omits
// them rather than sending an explicit null when the caller didn't
// supply a value.
type CreateRequest struct {
	OpIDPattern string  `json:"op_id_pattern"`
	ScopeField  *string `json:"scope_field,omitempty"`
	ScopeValue  *string `json:"scope_value,omitempty"`
	Detail      string  `json:"detail"`
}

// errNoBackplaneConfigured mirrors the cli/internal/cmd/audit/audit.go
// shape. Re-declared here to avoid the import cycle the cmd → cmd/audit
// → cmd path would create.
type errNoBackplaneConfigured struct{ inner error }

func (e *errNoBackplaneConfigured) Error() string {
	return "no backplane configured; run `meho login <url>` or pass --backplane"
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

// renderRequestError classifies a request error into the right
// StructuredError category. Maps the broadcast-overrides REST
// surface's status codes:
//
//   - 401 (refresh failed) → auth_expired.
//   - 403 → insufficient_role.
//   - 404 → unexpected with "broadcast override not found"
//     (cross-tenant probes land here per the backend's
//     no-existence-leak posture).
//   - 409 → unexpected with the duplicate-rule message.
//   - 422 → unexpected with the FastAPI validation envelope.
//   - Other 4xx/5xx → unexpected with the raw body.
//   - Pure transport errors → unreachable.
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
		return renderHTTPError(cmd, backplaneURL, he, jsonOut)
	}
	return output.RenderError(cmd.ErrOrStderr(),
		output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, err)),
		jsonOut,
	)
}

func renderHTTPError(
	cmd *cobra.Command,
	backplaneURL string,
	he *httpError,
	jsonOut bool,
) error {
	switch he.StatusCode {
	case http.StatusUnauthorized:
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"backplane rejected the stored token; run `meho login %s`",
				backplaneURL,
			)),
			jsonOut,
		)
	case http.StatusForbidden:
		return output.RenderError(cmd.ErrOrStderr(),
			output.InsufficientRole(decodeDetailString(he.Body)),
			jsonOut,
		)
	case http.StatusNotFound:
		// Surface the backend's own `detail` instead of hard-coding
		// "broadcast override not found": for `list` / `set` (which
		// don't carry an id in the path), a 404 means "route doesn't
		// exist on this backplane" -- typically because the operator
		// is talking to an older deploy that hasn't shipped T4 yet.
		// `remove`'s 404 carries `broadcast_override_not_found`
		// detail; both shapes round-trip cleanly through
		// `decodeDetailString`.
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(decodeDetailString(he.Body)),
			jsonOut,
		)
	case http.StatusConflict:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(decodeDetailString(he.Body)),
			jsonOut,
		)
	case http.StatusUnprocessableEntity:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("invalid request: %s", he.Body)),
			jsonOut,
		)
	default:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("call %s: HTTP %d: %s",
				backplaneURL, he.StatusCode, he.Body)),
			jsonOut,
		)
	}
}

type detailEnvelope struct {
	Detail json.RawMessage `json:"detail"`
}

func decodeDetailString(body string) string {
	var env detailEnvelope
	if err := json.Unmarshal([]byte(body), &env); err == nil {
		var s string
		if jerr := json.Unmarshal(env.Detail, &s); jerr == nil && s != "" {
			return s
		}
	}
	return strings.TrimSpace(body)
}

// doAuthedRequest issues a single HTTP request against the backplane
// with bearer injection and one-shot 401-refresh-retry. Returns the
// response body bytes (already drained) on 2xx, or an *httpError on
// non-2xx, or an error categorised by api.IsTokenNotFound /
// api.IsNoRefreshToken / generic transport.
//
// 204 No Content yields an empty body without error -- DELETE is the
// only verb that hits this path.
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

	raw, readErr := io.ReadAll(io.LimitReader(resp.Body, responseBodyCap+1))
	if readErr != nil {
		return nil, fmt.Errorf("read response: %w", readErr)
	}
	if int64(len(raw)) > responseBodyCap {
		return nil, fmt.Errorf(
			"response body exceeds %d-byte cap; refusing to decode possibly-truncated JSON",
			responseBodyCap,
		)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, &httpError{StatusCode: resp.StatusCode, Body: strings.TrimSpace(string(raw))}
	}
	return raw, nil
}

const responseBodyCap int64 = 1 << 20

type httpError struct {
	StatusCode int
	Body       string
}

func (e *httpError) Error() string {
	return fmt.Sprintf("HTTP %d: %s", e.StatusCode, e.Body)
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

func pathEscape(segment string) string {
	return url.PathEscape(segment)
}

func strDerefOrDash(s *string) string {
	if s == nil || *s == "" {
		return "-"
	}
	return *s
}
