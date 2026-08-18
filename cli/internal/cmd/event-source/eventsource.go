// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package eventsource hosts the cobra commands under `meho event-source
// ...` for the event_source registry (Initiative #2877, Task #2880). It
// wraps the tenant-scoped admin CRUD surface at /api/v1/event-sources:
//
//   - `meho event-source add <slug>` (alias `create`) — POST, register a
//     source. The auth secret is never a flag: it is read from the
//     MEHO_EVENT_SOURCE_SECRET env var, or from stdin with --secret-stdin,
//     so it never lands in shell history or `ps` output.
//   - `meho event-source list [--status S]` — GET, keyset-paginated.
//   - `meho event-source describe <slug>` — GET one source.
//   - `meho event-source update <slug> [flags]` — PATCH; --secret-stdin /
//     the env var rotates the Vault secret.
//   - `meho event-source delete <slug> [--confirm]` — DELETE (soft).
//
// There is no generated typed client for event-source yet, so every verb
// uses the package-local untyped `doAuthedRequest` plumbing (bearer
// inject + one-shot 401-refresh), mirroring `meho targets add`. A copy of
// the render helpers lives here rather than being shared to avoid the
// cmd -> cmd/<x> -> cmd import cycle.
package eventsource

import (
	"bufio"
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
	"github.com/evoila/meho/cli/internal/output"
)

// SecretEnvVar is the environment variable an operator sets to supply an
// event source's auth secret without it entering argv.
const SecretEnvVar = "MEHO_EVENT_SOURCE_SECRET"

// NewRootCmd returns the `meho event-source` parent command.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "event-source",
		Aliases:      []string{"event-sources"},
		Short:        "Operate the MEHO event_source registry (add / list / describe / update / delete)",
		Long:         "Register, list, describe, update, and delete inbound event sources in the operator's tenant.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newAddCmd())
	cmd.AddCommand(newListCmd())
	cmd.AddCommand(newDescribeCmd())
	cmd.AddCommand(newUpdateCmd())
	cmd.AddCommand(newDeleteCmd())
	return cmd
}

// --------------------------------------------------------------------- wire types

// EventSource mirrors the backend EventSource read model.
type EventSource struct {
	ID           string         `json:"id"`
	TenantID     string         `json:"tenant_id"`
	Name         string         `json:"name"`
	Slug         string         `json:"slug"`
	Kind         string         `json:"kind"`
	AuthStrategy string         `json:"auth_strategy"`
	SecretRef    *string        `json:"secret_ref"`
	Status       string         `json:"status"`
	Extras       map[string]any `json:"extras"`
	CreatedBySub string         `json:"created_by_sub"`
	CreatedAt    string         `json:"created_at"`
	UpdatedAt    string         `json:"updated_at"`
	DeletedAt    *string        `json:"deleted_at"`
}

// Summary mirrors the backend EventSourceSummary list row.
type Summary struct {
	Name         string  `json:"name"`
	Slug         string  `json:"slug"`
	Kind         string  `json:"kind"`
	AuthStrategy string  `json:"auth_strategy"`
	Status       string  `json:"status"`
	SecretRef    *string `json:"secret_ref"`
}

// ListResponse mirrors the {items, next_cursor} envelope.
type ListResponse struct {
	Items      []Summary `json:"items"`
	NextCursor *string   `json:"next_cursor"`
}

// --------------------------------------------------------------------- secret input

// resolveSecret returns the auth secret and whether one was supplied. It
// never accepts the value as a flag: the env var wins, else stdin is read
// when --secret-stdin is set. Mirrors the admin/keycloak no-leak pattern.
func resolveSecret(cmd *cobra.Command, useStdin bool) (string, bool, error) {
	if v := os.Getenv(SecretEnvVar); v != "" {
		return v, true, nil
	}
	if !useStdin {
		return "", false, nil
	}
	if _, err := fmt.Fprint(cmd.ErrOrStderr(), "Enter event-source secret: "); err != nil {
		return "", false, err
	}
	line, err := bufio.NewReader(cmd.InOrStdin()).ReadString('\n')
	if err != nil && !errors.Is(err, io.EOF) {
		return "", false, err
	}
	line = strings.TrimRight(line, "\r\n")
	if line == "" {
		return "", false, errors.New("empty secret on stdin")
	}
	return line, true, nil
}

// --------------------------------------------------------------------- HTTP plumbing

var errMissingAccessToken = errors.New("meho: stored token has no access_token")

type httpError struct {
	StatusCode int
	Body       string
}

func (e *httpError) Error() string {
	return fmt.Sprintf("HTTP %d: %s", e.StatusCode, e.Body)
}

// doAuthedRequest issues one request with bearer inject + one-shot
// 401-refresh-retry, returning the 2xx body or an *httpError on non-2xx.
// Copied from `meho targets` import.go for the import-cycle reason.
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
		return nil, errMissingAccessToken
	}
	resp, err := sendRequest(ctx, httpClient, backplaneURL, method, path, bearer, body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode == http.StatusUnauthorized {
		if rerr := authed.Refresh(ctx); rerr != nil {
			resp.Body.Close() //nolint:errcheck
			return nil, rerr
		}
		resp.Body.Close() //nolint:errcheck
		bearer = authed.AccessToken()
		resp, err = sendRequest(ctx, httpClient, backplaneURL, method, path, bearer, body)
		if err != nil {
			return nil, err
		}
	}
	defer resp.Body.Close()                                      //nolint:errcheck
	raw, readErr := io.ReadAll(io.LimitReader(resp.Body, 1<<20)) // 1 MiB cap
	if readErr != nil {
		return nil, fmt.Errorf("read response: %w", readErr)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
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
	var bodyReader io.Reader
	if body != nil {
		bodyReader = bytes.NewReader(body)
	}
	req, err := http.NewRequestWithContext(ctx, method, backplaneURL+path, bodyReader)
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

// renderRequestErr maps an error from doAuthedRequest to a StructuredError:
// an *httpError routes through renderHTTPStatus; transport / auth-state
// errors are categorised by the shared ladder.
func renderRequestErr(cmd *cobra.Command, backplaneURL string, err error, jsonOut bool) error {
	var he *httpError
	if errors.As(err, &he) {
		return renderHTTPStatus(cmd, backplaneURL, he.StatusCode, he.Body, jsonOut)
	}
	if errors.Is(err, errMissingAccessToken) || api.IsTokenNotFound(err) || api.IsNoRefreshToken(err) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf("run `meho login %s`", backplaneURL)), jsonOut)
	}
	return output.RenderError(cmd.ErrOrStderr(),
		output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, err)), jsonOut)
}

// renderHTTPStatus classifies a non-2xx (statusCode, body) into a
// StructuredError. 404 conflates absent/cross-tenant per the backend's
// uniform no-existence-oracle discipline.
func renderHTTPStatus(cmd *cobra.Command, backplaneURL string, statusCode int, body string, jsonOut bool) error {
	detail := decodeDetail(body)
	switch statusCode {
	case http.StatusUnauthorized:
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf("backplane rejected the stored token; run `meho login %s`", backplaneURL)),
			jsonOut)
	case http.StatusForbidden:
		return output.RenderError(cmd.ErrOrStderr(), output.InsufficientRole(detail), jsonOut)
	case http.StatusNotFound:
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected("event source not found"), jsonOut)
	default:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("call %s: HTTP %d: %s", backplaneURL, statusCode, detail)), jsonOut)
	}
}

// decodeDetail pulls a human string out of FastAPI's {"detail": ...}
// envelope, whether detail is a plain string or a structured object
// ({"error", "message", ...}); falls back to the raw body.
func decodeDetail(body string) string {
	var env struct {
		Detail json.RawMessage `json:"detail"`
	}
	if err := json.Unmarshal([]byte(body), &env); err != nil || len(env.Detail) == 0 {
		return strings.TrimSpace(body)
	}
	var s string
	if json.Unmarshal(env.Detail, &s) == nil && s != "" {
		return s
	}
	var obj struct {
		Message string `json:"message"`
		Error   string `json:"error"`
	}
	if json.Unmarshal(env.Detail, &obj) == nil {
		if obj.Message != "" {
			return obj.Message
		}
		if obj.Error != "" {
			return obj.Error
		}
	}
	return strings.TrimSpace(body)
}

// pathEscape escapes a single path segment for a backend URL.
func pathEscape(segment string) string {
	return url.PathEscape(segment)
}

// strDeref returns *s or "" when s is nil.
func strDeref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

// truncate cuts s to maxLen runes, appending an ellipsis on truncation.
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

// confirmPrompt prints prompt to stderr and reads a y/N line from stdin;
// a closed/EOF stdin reads as "no".
func confirmPrompt(cmd *cobra.Command, prompt string) bool {
	fmt.Fprintf(cmd.ErrOrStderr(), "%s [y/N]: ", prompt)
	line, err := bufio.NewReader(cmd.InOrStdin()).ReadString('\n')
	if err != nil && !errors.Is(err, io.EOF) {
		return false
	}
	line = strings.TrimSpace(line)
	return line == "y" || line == "Y" || line == "yes" || line == "Yes"
}
