// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package connector hosts the cobra commands under `meho connector ...`
// for the G0.7 spec-ingestion pipeline (Initiative #389). v0.2 ships
// the seven operator-facing verbs the ingestion workflow walks:
//
//   - `meho connector ingest`     — parse vendor specs and register
//     operations + groups into the endpoint_descriptor table. New
//     connector lands `review_status=staged` (operations not yet
//     dispatchable).
//   - `meho connector list`       — list ingested connectors filtered
//     by review status.
//   - `meho connector review`     — show the per-connector review
//     payload (groups + ops + flags) for operator approval.
//   - `meho connector edit-group` — patch a group's display name or
//     `when_to_use` hint.
//   - `meho connector edit-op`    — patch a per-op override (custom
//     description, safety level, requires_approval, is_enabled).
//   - `meho connector enable`     — transition a staged connector to
//     `review_status=enabled`; operations become dispatchable.
//   - `meho connector disable`    — flip back to disabled without
//     deleting rows (per-op overrides preserved for rollback).
//
// Each verb is a thin cobra command that POSTs / GETs / PATCHes a
// single backplane route under `/api/v1/connectors*` (T6 #406).
// Authentication piggybacks on the token meho login wrote — the
// shared doAuthedRequest helper handles the bearer injection +
// 401-refresh dance identically to the `operation` sibling package.
//
// Cross-task contract: this package consumes T6 #406's REST routes;
// the route shape (request/response Pydantic models) is documented
// in the issue body and stays load-bearing for both PRs. T7 #407's
// admin MCP tools wrap the same service layer T6 exposes, so the
// JSON envelope shapes here mirror what `meho.connector.*` MCP
// tools emit to MCP clients.
package connector

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
	"path/filepath"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/output"
)

// NewRootCmd returns the `meho connector` parent command. cmd/root.go
// grafts this onto the top-level command tree alongside the other
// built-in verbs. The parent itself takes no args and prints its
// own help; every piece of behaviour lives in the per-subcommand
// RunE closures.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "connector",
		Short:        "G0.7 spec-ingestion + review workflow (ingest / list / review / edit / enable / disable)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newIngestCmd())
	cmd.AddCommand(newListCmd())
	cmd.AddCommand(newReviewCmd())
	cmd.AddCommand(newEditGroupCmd())
	cmd.AddCommand(newEditOpCmd())
	cmd.AddCommand(newEnableCmd())
	cmd.AddCommand(newDisableCmd())
	return cmd
}

// errNoBackplaneConfigured wraps auth.ErrConfigNotFound so callers
// can distinguish "operator never logged in" (→ auth_expired exit
// code 2 — the right fix is `meho login`) from URL-parse failures
// (→ unexpected exit code 4 — the right fix is correcting argv).
// Same shape as the operation/retrieval helpers; kept independent
// because cmd/{operation,retrieval,connector} can't import each
// other without an import cycle (cmd/root.go grafts each onto the
// tree).
type errNoBackplaneConfigured struct{ inner error }

func (e *errNoBackplaneConfigured) Error() string {
	return "no backplane URL configured; run `meho login <url>` first or pass --backplane <url>"
}
func (e *errNoBackplaneConfigured) Unwrap() error { return e.inner }

// resolveBackplane mirrors the operation package's helper. The CLI
// resolves the backplane URL in priority order: --backplane override
// flag → meho config (written by `meho login`).
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
// output.StructuredError category. Identical contract to the
// operation sibling: missing-config → auth_expired; everything
// else (parse errors, fs errors) → unexpected.
func classifyBackplaneError(err error) *output.StructuredError {
	if errors.Is(err, auth.ErrConfigNotFound) {
		return output.AuthExpired(err.Error())
	}
	return output.Unexpected(err.Error())
}

// normaliseURL strips trailing slashes + parses the URL to fail fast
// on garbage input. Mirrors the operation sibling.
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

// renderRequestError translates an error from doAuthedRequest into
// the right output.RenderError category. Adds one branch over the
// operation sibling: HTTP 403 lands as InsufficientRole (exit 5) so
// the tenant_admin-gated verbs produce the right hint when an
// operator-role token tries to ingest. HTTP 401-after-refresh,
// non-recoverable refresh failures, and missing tokens all map to
// auth_expired (exit 2) as in the sibling.
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
		// 403 carries a distinct exit code so operators see "ask
		// the tenant admin for a role grant" rather than "your
		// network is down". The connector verb tree's mutating
		// routes (ingest / edit-* / enable / disable) all require
		// tenant_admin; a plain operator-role token gets the
		// right hint here.
		if he.StatusCode == http.StatusForbidden {
			return output.RenderError(cmd.ErrOrStderr(),
				output.InsufficientRole(fmt.Sprintf(
					"call %s: HTTP 403: %s (this verb requires tenant_admin role)",
					backplaneURL, he.Body,
				)),
				jsonOut,
			)
		}
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

// doAuthedRequest issues a single HTTP request against the backplane
// with bearer injection + one-shot 401-refresh-retry. Mirrors the
// operation sibling's implementation verbatim (the underlying auth
// dance is shared; cmd/connector can't import cmd/operation without
// an import cycle since cmd/root.go grafts both onto the tree).
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

	// 4 MiB cap. Higher than the operation sibling's 1 MiB because
	// an ingest of vcenter.yaml (961 paths) returns an
	// IngestionResult + GroupingResult envelope that can run into
	// the hundreds of KiB; review payload for the same connector is
	// similarly fat (per-op flags + descriptions). 4 MiB is
	// generous enough for v0.2's largest known spec (vi-json.yaml,
	// 2195 paths) while still capping pathological responses.
	raw, readErr := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if readErr != nil {
		return nil, fmt.Errorf("read response: %w", readErr)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, &httpError{StatusCode: resp.StatusCode, Body: strings.TrimSpace(string(raw))}
	}
	return raw, nil
}

// httpError carries a non-2xx response so renderRequestError can
// pick the right StructuredError category (403 → insufficient_role;
// other 4xx/5xx → unexpected_response). Same shape as the operation
// sibling.
type httpError struct {
	StatusCode int
	Body       string
}

func (e *httpError) Error() string {
	return fmt.Sprintf("HTTP %d: %s", e.StatusCode, e.Body)
}

// sendRequest builds + fires the HTTP request. Split out so the
// 401-refresh-retry path can reuse the same body bytes without
// re-marshalling.
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

// loadTextFlag reads a flag value that supports both inline text and
// the `@<path>` file-reference form. Returns (value, false, nil)
// when no value is set so callers can omit the field from the PATCH
// body; returns (value, true, nil) when explicitly set (including
// empty-string, e.g. operator wants to clear an override). Mirrors
// `loadParamsFlag` in the operation sibling but for plain text
// rather than JSON.
//
// The third return value distinguishes "operator did not pass the
// flag" from "operator passed --when-to-use ”" — important for the
// PATCH semantics where field-omitted means "leave unchanged" and
// field-present-empty means "reset to default".
func loadTextFlag(cmd *cobra.Command, name string) (value string, present bool, err error) {
	if !cmd.Flags().Changed(name) {
		return "", false, nil
	}
	raw, ferr := cmd.Flags().GetString(name)
	if ferr != nil {
		return "", false, fmt.Errorf("read --%s: %w", name, ferr)
	}
	if strings.HasPrefix(raw, "@") {
		path := strings.TrimPrefix(raw, "@")
		blob, rerr := os.ReadFile(path)
		if rerr != nil {
			return "", false, fmt.Errorf("read --%s file %q: %w", name, path, rerr)
		}
		// Trim the trailing newline editors add by reflex. Embedded
		// newlines inside the text are preserved; only the final \n
		// is stripped so a 1-line file passed via @ doesn't carry a
		// gratuitous trailing newline through the JSON body.
		return strings.TrimRight(string(blob), "\n"), true, nil
	}
	return raw, true, nil
}

// truncate cuts s to maxLen runes, appending an ellipsis when
// truncation happened. Operates on runes (not bytes) so multi-byte
// UTF-8 in group names / when_to_use strings survives without
// producing an invalid UTF-8 cut. Same implementation as the
// operation sibling — duplicated to avoid an import cycle.
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

// resolveSpecURI normalises a --spec flag value into the canonical
// URI the backplane's IngestRequest accepts. Three input shapes are
// supported:
//
//   - `file://<absolute path>`    — passes through after path validation.
//   - `https://<url>` / `http://` — passes through.
//   - `docs:<product-version>/<spec>` — shorthand resolving against
//     the consumer's checked-in docs/ directory. The base directory
//     comes from the CLAUDE_RDC_DOCS env var (operator workstation
//     convention — points at a checkout of
//     `claude-rdc-hetzner-dc/docs/meho-coordination/`). The resolved
//     form is `file://<absolute path>` so the backplane sees a uniform
//     scheme. When CLAUDE_RDC_DOCS is unset, the shorthand resolves
//     to a bare `docs:<...>` URI and the backplane handles resolution
//     server-side against its own configured docs root.
//
// Anything else returns an error — the operator gets a clear "use
// one of file:// / https:// / docs:<...>" hint rather than a 422
// from the backplane.
func resolveSpecURI(raw string) (string, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return "", errors.New("--spec value is empty")
	}
	if strings.HasPrefix(raw, "file://") {
		return raw, nil
	}
	if strings.HasPrefix(raw, "https://") || strings.HasPrefix(raw, "http://") {
		return raw, nil
	}
	if strings.HasPrefix(raw, "docs:") {
		shorthand := strings.TrimPrefix(raw, "docs:")
		if shorthand == "" {
			return "", errors.New("--spec docs: shorthand has no path")
		}
		root := os.Getenv("CLAUDE_RDC_DOCS")
		if root == "" {
			// No env override: pass the shorthand through verbatim
			// for the backplane to resolve against its own checked-in
			// docs root. Documented behaviour — the v0.2 backplane
			// understands the `docs:` scheme natively.
			return raw, nil
		}
		abs := filepath.Join(root, shorthand)
		if !filepath.IsAbs(abs) {
			// filepath.Join keeps relative roots relative; reject
			// rather than ship a half-resolved URI.
			return "", fmt.Errorf("CLAUDE_RDC_DOCS=%q produced non-absolute spec path %q", root, abs)
		}
		return "file://" + abs, nil
	}
	return "", fmt.Errorf(
		"--spec %q: unknown URI scheme; expected file:// / https:// / docs:<product-version>/<spec>",
		raw,
	)
}

// confirm prompts on stdin/stdout with the given message and returns
// true only when the operator types y/yes/Y. EOF (closed stdin) is
// treated as a no — scripted use must pass --confirm. Honours
// cmd.InOrStdin() so tests can wire a bytes.Buffer.
func confirm(cmd *cobra.Command, prompt string) bool {
	fmt.Fprintf(cmd.OutOrStdout(), "%s [y/N]: ", prompt)
	var answer string
	if _, err := fmt.Fscanln(cmd.InOrStdin(), &answer); err != nil {
		// Most common error: io.EOF from a piped empty stdin.
		// Treat as "no" so scripts that pipe /dev/null don't
		// accidentally enable a connector.
		return false
	}
	answer = strings.ToLower(strings.TrimSpace(answer))
	return answer == "y" || answer == "yes"
}

// pathEscapeOpID escapes the op_id segment for use in a URL path.
// op_id values contain `:` (method separator) and `/` (path
// separator) — e.g. `GET:/api/vcenter/cluster`. url.PathEscape on
// the raw value produces a single segment the FastAPI router can
// match via `{op_id:path}` (or equivalent). connector_id and
// group_key never contain reserved chars in v0.2 but pass through
// the same escape for defence in depth.
func pathEscapeOpID(s string) string {
	return url.PathEscape(s)
}

// decodeJSON unmarshals raw into out and wraps any error with a
// caller-friendly context string. Pattern matches the operation
// sibling.
func decodeJSON(raw []byte, what string, out any) error {
	if err := json.Unmarshal(raw, out); err != nil {
		return fmt.Errorf("decode %s response: %w", what, err)
	}
	return nil
}
