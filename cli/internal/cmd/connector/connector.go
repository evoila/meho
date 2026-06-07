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
// Authentication piggybacks on the token meho login wrote — every
// verb drives the generated `api.ClientWithResponses` surface via
// `api.AuthedClient`, which wires the bearer + lazy 401-refresh
// editor onto the embedded typed client.
//
// G0.12-T7 #1265 migrated this package off the sibling-verb pattern
// of hand-rolled HTTP + hand-typed copies of backend pydantic models.
// `api.CatalogListResponse` / `api.ConnectorSpecEntry`,
// `api.IngestRequest` / `api.IngestResponse` /
// `api.IngestionResultModel` / `api.GroupingResultModel` /
// `api.SpecSource`, `api.ConnectorReviewPayload` /
// `api.ConnectorReviewGroup` / `api.ConnectorReviewOp`,
// `api.EditGroupBody`, and `api.EditOpBody` are the single source
// of truth on the CLI side, kept in lock-step with the FastAPI
// Pydantic models by the `cli-api-snapshot-freshness` CI gate. The
// list endpoint deliberately returns `dict[str, list[dict]]` on the
// backend (per-row UUID-serialisation reason; see
// `backend/src/meho_backplane/api/v1/connectors_ingest.py:list_endpoint`),
// so the list-response shape stays a package-private decode against
// the raw `*Response.Body` bytes — the typed client is still the
// transport.
//
// Cross-task contract: this package consumes T6 #406's REST routes;
// the route shape (request/response Pydantic models) is documented
// in the issue body and stays load-bearing for both PRs. T7 #407's
// admin MCP tools wrap the same service layer T6 exposes, so the
// JSON envelope shapes here mirror what `meho.connector.*` MCP
// tools emit to MCP clients.
package connector

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path"
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
		Short:        "G0.7 spec-ingestion + review workflow (ingest / list / catalog / review / edit / enable / disable)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newIngestCmd())
	cmd.AddCommand(newListCmd())
	cmd.AddCommand(newCatalogCmd())
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
	if u.Scheme != "http" && u.Scheme != "https" {
		// Fail fast on schemes we can't dial (ftp://, ssh://, plain
		// host names without a scheme parsed as opaque, etc.). Without
		// this check the verb only fails much later inside the HTTP
		// client with a less actionable error.
		return "", fmt.Errorf("backplane URL %q must use http or https", s)
	}
	if u.Host == "" {
		return "", fmt.Errorf("backplane URL %q has no host", s)
	}
	u.Path = strings.TrimRight(u.Path, "/")
	return u.String(), nil
}

// httpResponseError carries a non-2xx status from a typed-client
// `*WithResponse` call up to the verb's renderer. The typed-client
// surface returns non-2xx responses in-band on the `(*Response,
// nil)` tuple (transport-layer failures come back on `(nil, err)`);
// we lift the HTTP-failure case to an error type so the call sites
// can use a single `if err != nil` branch and `errors.As` routes
// the right way (HTTP status → renderHTTPStatus, everything else →
// renderRequestError).
type httpResponseError struct {
	statusCode int
	body       []byte
}

func (e *httpResponseError) Error() string {
	return fmt.Sprintf("HTTP %d: %s", e.statusCode, strings.TrimSpace(string(e.body)))
}

// errMissingAccessToken is the sentinel newAuthedClient returns when
// the stored token row exists but its access_token is empty — a
// credential-state failure renderRequestError maps to auth_expired
// with a `meho login` hint. Same shape as the sibling-package
// agent-principal / agent packages.
var errMissingAccessToken = errors.New("meho: stored token has no access_token")

// newAuthedClient builds an api.AuthedClient for the supplied
// backplane URL and verifies a non-empty bearer is loaded. Centralised
// so every verb's typed-call path goes through the same
// "stored-token-loaded + non-empty bearer" gate; the caller forwards
// any returned error to renderRequestError for category mapping.
func newAuthedClient(ctx context.Context, backplaneURL string) (*api.AuthedClient, error) {
	authed, err := api.NewAuthedClient(ctx, backplaneURL, api.AuthedClientOptions{})
	if err != nil {
		return nil, err
	}
	if authed.AccessToken() == "" {
		return nil, errMissingAccessToken
	}
	return authed, nil
}

// retryOn401 invokes call once, and if the typed response carries a
// 401, runs a one-shot bearer refresh and re-issues call. Mirrors
// the behaviour `api.AuthedClient.GetHealth` implements for the
// /api/v1/health endpoint, generalised so every connector verb runs
// the same transparent-retry contract.
//
// statusOf reads the StatusCode off the typed response envelope
// (the generated *Response types expose StatusCode() through their
// embedded *http.Response). A nil response counts as "no retry" —
// the transport already failed and the caller surfaces err directly.
func retryOn401[R any](
	ctx context.Context,
	authed *api.AuthedClient,
	call func(ctx context.Context) (*R, error),
	statusOf func(*R) int,
) (*R, error) {
	resp, err := call(ctx)
	if err != nil {
		return nil, err
	}
	if resp == nil || statusOf(resp) != http.StatusUnauthorized {
		return resp, nil
	}
	if rerr := authed.Refresh(ctx); rerr != nil {
		return resp, rerr
	}
	return call(ctx)
}

// renderRequestError translates a transport-layer or
// credential-state error (returned on `(nil, err)` from the typed-
// client surface, or surfaced by newAuthedClient before any HTTP
// round-trip) into the right output.RenderError category. Non-2xx
// statuses carried in a typed response envelope are classified by
// renderHTTPStatus instead.
//
// One branch differs from the operation sibling: HTTP 403 lands as
// InsufficientRole (exit 5) so the tenant_admin-gated verbs produce
// the right hint when an operator-role token tries to ingest. That
// branch lives in renderHTTPStatus now; the pre-migration
// `*httpError` 403 path moved with the rest of the status-code
// switch.
func renderRequestError(
	cmd *cobra.Command,
	backplaneURL string,
	err error,
	jsonOut bool,
) error {
	if errors.Is(err, errMissingAccessToken) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"stored credentials for %s are incomplete; run `meho login %s`",
				backplaneURL, backplaneURL,
			)),
			jsonOut,
		)
	}
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
	// Distinguish response-shape failures (decode / contract drift)
	// from genuine network failures. The former are *Unexpected*
	// — the request reached the backplane, the backplane returned
	// 200, but the body didn't match the agreed wire contract. The
	// latter (default branch) remain *Unreachable*: connection
	// reset, DNS failure, TLS handshake, etc. Without this split,
	// a contract drift between T5 and T6 (different field names,
	// changed status enum) presents to the operator as "your
	// network is down", which is misleading.
	var syntaxErr *json.SyntaxError
	var unmarshalErr *json.UnmarshalTypeError
	if errors.As(err, &syntaxErr) ||
		errors.As(err, &unmarshalErr) ||
		errors.Is(err, io.ErrUnexpectedEOF) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("call %s: invalid JSON response: %v",
				backplaneURL, err)),
			jsonOut,
		)
	}
	return output.RenderError(cmd.ErrOrStderr(),
		output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, err)),
		jsonOut,
	)
}

// renderHTTPStatus classifies a non-2xx response (or 401 after a
// failed refresh) carried in the typed envelope into the right
// StructuredError category. Mirrors the pre-migration `httpError`
// switch but acts on the (statusCode, body) pair lifted off the
// generated `*Response.HTTPResponse` + `Body` fields rather than a
// sentinel value. The mapping preserved across the migration:
//
//   - 401 → auth_expired (refresh impossible / token rejected).
//   - 403 → insufficient_role; the mutating verbs (ingest /
//     edit-* / enable / disable) all require tenant_admin, so the
//     hint names the role.
//   - Other non-2xx → unexpected with the raw body.
func renderHTTPStatus(
	cmd *cobra.Command,
	backplaneURL string,
	statusCode int,
	body []byte,
	jsonOut bool,
) error {
	bodyStr := strings.TrimSpace(string(body))
	switch statusCode {
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
			output.InsufficientRole(fmt.Sprintf(
				"call %s: HTTP 403: %s (this verb requires tenant_admin role)",
				backplaneURL, bodyStr,
			)),
			jsonOut,
		)
	default:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("call %s: HTTP %d: %s",
				backplaneURL, statusCode, bodyStr)),
			jsonOut,
		)
	}
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
		// newlines inside the text are preserved; only the final
		// CRLF / LF is stripped so a 1-line file passed via @ doesn't
		// carry a gratuitous trailing newline through the JSON body.
		// `\r\n` covers CRLF (Windows) and LF (Unix) line endings;
		// trimming only `\n` leaves a stray `\r` on CRLF files which
		// leaks into persisted field values.
		return strings.TrimRight(string(blob), "\r\n"), true, nil
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

// resolveSpecURI normalises a --spec flag value into the (uri, content)
// pair the backplane's IngestRequest accepts. Three input shapes are
// supported:
//
//   - `https://<url>` -- passed through as the uri; the backplane fetches
//     it under the https-only SSRF / local-file guard (#95). content is
//     empty. (`http://` is passed through too, but the https-only guard
//     rejects it backplane-side.)
//   - `file://<absolute path>` -- the CLI reads the file and uploads its
//     bytes as content; uri is kept as the audit label. No local path
//     reaches the backplane.
//   - `docs:<product-version>/<spec>` -- resolved CLI-side against the
//     consumer's checked-in docs/ directory ($CLAUDE_RDC_DOCS, e.g. a
//     checkout of `claude-rdc-hetzner-dc/docs/meho-coordination/`), then
//     read + uploaded as content with the `docs:` label kept as uri.
//     When CLAUDE_RDC_DOCS is unset the shorthand is rejected here with a
//     hint naming the env var (#1535).
//
// Reading docs:/file:// CLI-side and uploading the bytes is what keeps
// the #95 https-only backend from breaking the local-spec on-ramp: the
// backplane never sees a local path or a non-https scheme (#102).
//
// Anything else returns an error -- the operator gets a clear "use one of
// file:// / https:// / docs:<...>" hint rather than a 422 from the
// backplane.
func resolveSpecURI(raw string) (uri string, content string, err error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return "", "", errors.New("--spec value is empty")
	}
	if strings.HasPrefix(raw, "file://") {
		// Validate locally so operators see a fast CLI error rather than a
		// backplane 4xx. The contract is `file://<absolute path>`.
		u, perr := url.Parse(raw)
		if perr != nil || u.Scheme != "file" {
			return "", "", fmt.Errorf("--spec %q: invalid file URI", raw)
		}
		// Per RFC 8089, file URIs have either an empty authority or
		// `localhost`. Reject any other host so a `file://relative/path`
		// typo surfaces here instead of as a confused on-disk lookup.
		if u.Host != "" && u.Host != "localhost" {
			return "", "", fmt.Errorf("--spec %q: file URI host must be empty or \"localhost\"", raw)
		}
		// Root-only (`file:///`) is rejected too -- no spec file to read.
		if len(u.Path) <= 1 || !path.IsAbs(u.Path) {
			return "", "", fmt.Errorf("--spec %q: file URI must be an absolute path to a spec", raw)
		}
		b, rerr := os.ReadFile(u.Path) // #nosec G304 -- operator-supplied spec path, operator-only CLI
		if rerr != nil {
			return "", "", fmt.Errorf("--spec %q: %w", raw, rerr)
		}
		return raw, string(b), nil
	}
	if strings.HasPrefix(raw, "https://") || strings.HasPrefix(raw, "http://") {
		return raw, "", nil
	}
	if strings.HasPrefix(raw, "docs:") {
		shorthand := strings.TrimPrefix(raw, "docs:")
		if shorthand == "" {
			return "", "", errors.New("--spec docs: shorthand has no path")
		}
		root := os.Getenv("CLAUDE_RDC_DOCS")
		if root == "" {
			// The `docs:` shorthand is resolved CLI-side only. The backplane
			// has no docs root and is https-only, so it cannot resolve a bare
			// `docs:` URI. Fail here with a clear hint naming the env var
			// the operator must set (#1535).
			return "", "", errors.New(
				"--spec docs: shorthand requires $CLAUDE_RDC_DOCS to be set " +
					"(the backplane does not resolve docs: URIs); set it to a " +
					"docs checkout, or pass a file:// / https:// spec URI",
			)
		}
		abs := filepath.Join(root, shorthand)
		if !filepath.IsAbs(abs) {
			// filepath.Join keeps relative roots relative; reject rather
			// than read from a half-resolved path.
			return "", "", fmt.Errorf("CLAUDE_RDC_DOCS=%q produced non-absolute spec path %q", root, abs)
		}
		b, rerr := os.ReadFile(abs) // #nosec G304 -- operator-supplied docs path, operator-only CLI
		if rerr != nil {
			return "", "", fmt.Errorf("--spec %q (resolved to %q): %w", raw, abs, rerr)
		}
		return raw, string(b), nil
	}
	return "", "", fmt.Errorf(
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
