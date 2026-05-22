// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package topology hosts the cobra commands under `meho topology ...`
// for G9.1-T6 (#454) of Initiative #363 (read/traversal verbs) and
// G9.2-T6 (#599) of Initiative #364 (curated-edge write + listing
// verbs). v0.2 ships seven operator-facing verbs over the T5 REST
// surface (#453, #597) shipped by
// `backend/src/meho_backplane/api/v1/topology.py`:
//
//   - `meho topology refresh <target> [--json]` —
//     POST /api/v1/topology/refresh/{target}. Rediscovers one
//     target's topology and reconciles it into the graph; renders
//     the per-target added/removed/updated node + edge counts. Role:
//     operator.
//   - `meho topology dependents <name|alias> [--depth N]
//     [--kind <edge_kind>] [--json]` — GET
//     /api/v1/topology/dependents/{name}. Reverse closure ("what
//     depends on me"). Role: operator.
//   - `meho topology dependencies <name|alias> [--depth N]
//     [--kind <edge_kind>] [--json]` — GET
//     /api/v1/topology/dependencies/{name}. Forward closure ("what I
//     depend on"). Role: operator.
//   - `meho topology path <from> <to> [--max-hops N] [--json]` —
//     GET /api/v1/topology/path?from=A&to=B. Shortest unweighted
//     path between two named nodes, or the no-path line when
//     unreachable. Role: operator.
//   - `meho topology annotate <from> <kind> <to> [--note "..."]
//     [--evidence-url URL] [--json]` — POST /api/v1/topology/edges.
//     Operator-curated cross-system edge assertion. Idempotent.
//     Role: tenant_admin.
//   - `meho topology unannotate <edge-id> | <from> <kind> <to>` —
//     DELETE /api/v1/topology/edges/{edge_id}. The tuple form is
//     client-side: a GET resolves the unique curated edge id, then
//     DELETE removes it. Auto rows refuse with 409 + remediation
//     hint. Role: tenant_admin.
//   - `meho topology list-edges [--kind K] [--source curated|auto]
//     [--from N] [--to N] [--conflicts] [--json]` — GET
//     /api/v1/topology/edges. Flat filterable listing of the
//     tenant's edges. Role: operator.
//   - `meho topology bulk-import <file> [--dry-run] [--json]` —
//     POST /api/v1/topology/edges/bulk. Reads a YAML/JSON file
//     shaped as `{edges: [...]}` and posts the whole batch in one
//     transaction. The server runs validation first; a single bad
//     row aborts the entire batch (no partial apply). Re-running
//     the same file is a per-row no-op. Role: tenant_admin.
//
// The fifth G9.1-T6 verb, `meho targets discover <product>`, lives
// under `cli/internal/cmd/targets/discover.go` because it sits under
// the canonical `/api/v1/targets` prefix next to the other target-
// scoped verbs (mirroring where the backend registers
// GET /api/v1/targets/discover on the targets router).
//
// Each verb wraps one backplane route and renders the response in
// either a human-readable form (count summary / node table / path
// chain) or `--json` mode. Authentication piggybacks on the token
// `meho login` wrote — same pattern as `meho audit`, `meho targets`,
// and `meho kb`.
//
// The implementation deliberately follows the in-package HTTP helper
// pattern the sibling verb trees use (one resolveBackplane /
// doAuthedRequest / renderRequestError trio per package) rather than
// a shared `cli/internal/api_client` package. The reason is the
// import-cycle one the `kb` and `targets` packages already document:
// each verb tree is registered onto the root command, so a shared
// helper imported from cmd/* and from any per-tree package would
// close the cycle. Duplicating the small, stable helpers is the
// convention every sibling package follows. (Initiative #363 names a
// `cli/internal/api_client/topology.go`; the codebase convention
// supersedes that path — the intent, "a Go client for the T5
// routes," is satisfied in-package.)
//
// Per [CLAUDE.md](../../CLAUDE.md) postulate 5 these verbs are
// operator-facing ergonomics; the agent surface for the same data is
// the single `query_topology` MCP meta-tool (T7), not per-verb tools.
package topology

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

// NewRootCmd returns the `meho topology` parent command. The command
// is grafted onto the top-level meho command tree by cmd/root.go
// alongside `meho audit`, `meho targets`, `meho kb`, and the rest.
// The parent itself takes no args and prints its own help; every
// piece of behaviour lives in the per-subcommand RunE closures.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "topology",
		Short: "Query and refresh the MEHO topology graph (refresh / dependents / dependencies / path / timeline / diff / annotate / unannotate / list-edges)",
		Long: "Operate the tenant-scoped topology graph wired by G9.1 + " +
			"G9.2 + G9.3. Refresh a target's discovered topology, walk " +
			"what depends on a node (dependents), walk what a node " +
			"depends on (dependencies), find the shortest path between " +
			"two nodes, walk the tenant's chronological change feed " +
			"from the diff-on-write history substrate (timeline), " +
			"assert a curated cross-system edge (annotate), delete a " +
			"curated edge (unannotate), or list the tenant's edges " +
			"(list-edges). Read verbs are operator-level; annotate / " +
			"unannotate require tenant_admin. Tenant scoping is " +
			"enforced server-side via the JWT — no surface accepts a " +
			"tenant id, and cross-tenant queries return the same " +
			"empty/404 shape as a node that does not exist.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newRefreshCmd())
	cmd.AddCommand(newDependentsCmd())
	cmd.AddCommand(newDependenciesCmd())
	cmd.AddCommand(newPathCmd())
	cmd.AddCommand(newAnnotateCmd())
	cmd.AddCommand(newUnannotateCmd())
	cmd.AddCommand(newListEdgesCmd())
	cmd.AddCommand(newBulkImportCmd())
	cmd.AddCommand(newTimelineCmd())
	cmd.AddCommand(newDiffCmd())
	return cmd
}

// errNoBackplaneConfigured wraps auth.ErrConfigNotFound so callers
// can distinguish "operator never logged in" (→ auth_expired exit
// code 2 — the right fix is `meho login`) from URL-parse failures
// (→ unexpected exit code 4 — the right fix is correcting argv).
// Same shape as the helper in cli/internal/cmd/targets/targets.go;
// kept independent because the cmd/targets package can't be imported
// here without an import cycle.
type errNoBackplaneConfigured struct{ inner error }

func (e *errNoBackplaneConfigured) Error() string {
	return "no backplane URL configured; run `meho login <url>` first or pass --backplane <url>"
}
func (e *errNoBackplaneConfigured) Unwrap() error { return e.inner }

// resolveBackplane re-implements the host-trimming + parsing rules
// the cmd package's resolveBackplaneURL applies. We can't import cmd
// from a subpackage without an import cycle (cmd/root.go grafts this
// package onto the tree), so the resolution shape is mirrored here —
// same shape as targets/targets.go's helper.
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
// targets/targets.go sibling.
func classifyBackplaneError(err error) *output.StructuredError {
	if errors.Is(err, auth.ErrConfigNotFound) {
		return output.AuthExpired(err.Error())
	}
	return output.Unexpected(err.Error())
}

// normaliseURL strips trailing slashes + parses the URL to fail fast
// on garbage input. Mirrors normaliseURL in targets/targets.go.
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

// renderRequestError translates an error from one of the per-verb
// request helpers into the right output.StructuredError category.
// Same classification ladder as targets/targets.go but with the
// topology-specific 4xx envelopes:
//
//   - 401 → auth_expired with a `meho login <url>` hint.
//   - 403 (RBAC denial) → insufficient_role; the backend's 403 detail
//     string names the required role (operator).
//   - 404 → unexpected_response. `refresh` resolves the target via
//     resolve_target, so its 404 carries the structured
//     `{"error":"no_target","query":...,"matches":[...]}` envelope
//     (near-misses surfaced); the read verbs never 404 (a missing
//     node is an empty list / null path, 200), so a 404 there is a
//     routing surprise surfaced verbatim.
//   - 409 with detail.error == "ambiguous_node" → unexpected, listing
//     the colliding kinds so the operator can re-issue with
//     --node-kind (the anchor `kind` pin; --kind is the edge filter
//     and does not clear this 409).
//   - Any other 4xx/5xx → unexpected with the raw body.
//   - Pure transport errors (timeouts, DNS, connection refused) →
//     unreachable.
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

// renderHTTPError classifies a non-2xx response into the right
// StructuredError category.
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
		// 403 detail is FastAPI/Starlette's HTTPException shape
		// ({"detail": "<string>"}). require_role writes the required
		// role into the detail string; pass it through verbatim.
		return output.RenderError(cmd.ErrOrStderr(),
			output.InsufficientRole(decodeDetailString(he.Body)),
			jsonOut,
		)
	case http.StatusNotFound:
		// Only `refresh` can 404 — resolve_target's structured
		// `{"error":"no_target","query":...,"matches":[...]}`
		// envelope. Render near-miss suggestions when present so the
		// operator sees "did you mean rdc-vcenter?". This is also the
		// cross-tenant boundary surface for refresh: a target in
		// another tenant resolves to no_target, identical to a typo.
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(formatNotFound(he.Body)),
			jsonOut,
		)
	case http.StatusConflict:
		// 409 from the query layer's AmbiguousNodeError: a bare name
		// resolved to more than one graph_node.kind. Surface the
		// colliding kinds so the operator re-issues with --node-kind
		// (the anchor `kind` pin — not --kind, which is the edge
		// filter `kind_filter` and cannot clear the ambiguity).
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(formatAmbiguousNode(he.Body)),
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

// detailEnvelope models FastAPI's HTTPException JSON shape, with the
// detail field accepting either a string (FastAPI's default) or a
// nested object (resolver's no_target / query's ambiguous_node).
type detailEnvelope struct {
	Detail json.RawMessage `json:"detail"`
}

// resolverDetail mirrors the structured detail TargetNotFoundError
// raises (resolver.py): {"error","query","matches":[...]}.
type resolverDetail struct {
	Error   string                   `json:"error"`
	Query   string                   `json:"query"`
	Matches []resolverMatchOnTheWire `json:"matches"`
}

// resolverMatchOnTheWire mirrors the human-readable fields of the
// resolver's near-miss matches (TargetSummary.model_dump).
type resolverMatchOnTheWire struct {
	Name    string   `json:"name"`
	Aliases []string `json:"aliases"`
}

// ambiguousNodeDetail mirrors the 409 body topology.py's
// _ambiguous_node_http emits: {"error":"ambiguous_node",
// "name":"<n>","kinds":["host","vm"]}.
type ambiguousNodeDetail struct {
	Error string   `json:"error"`
	Name  string   `json:"name"`
	Kinds []string `json:"kinds"`
}

// decodeDetailString pulls the `detail` field out of a FastAPI error
// body when it's a plain string. Falls back to the raw body when the
// JSON shape doesn't match — better to surface the raw error than to
// swallow it.
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

// formatNotFound renders a refresh 404 envelope into one operator-
// readable line. Structured form (resolver) → "Target not found: <q>;
// did you mean: <a, b, c>"; plain string fallback → raw detail.
func formatNotFound(body string) string {
	var env detailEnvelope
	if err := json.Unmarshal([]byte(body), &env); err == nil {
		var detail resolverDetail
		if jerr := json.Unmarshal(env.Detail, &detail); jerr == nil && detail.Error != "" {
			if len(detail.Matches) == 0 {
				return fmt.Sprintf("Target not found: %q (no near-misses)", detail.Query)
			}
			names := make([]string, 0, len(detail.Matches))
			for _, m := range detail.Matches {
				names = append(names, m.Name)
			}
			return fmt.Sprintf("Target not found: %q; did you mean: %s",
				detail.Query, strings.Join(names, ", "))
		}
	}
	return "not found: " + decodeDetailString(body)
}

// formatAmbiguousNode renders a 409 ambiguous_node envelope into one
// line that names the colliding kinds and the --node-kind remedy.
// The anchor pin is --node-kind (the route's `kind` param); --kind is
// the edge filter (`kind_filter`) and does not resolve this 409.
func formatAmbiguousNode(body string) string {
	var env detailEnvelope
	if err := json.Unmarshal([]byte(body), &env); err == nil {
		var detail ambiguousNodeDetail
		if jerr := json.Unmarshal(env.Detail, &detail); jerr == nil && detail.Error == "ambiguous_node" {
			return fmt.Sprintf(
				"node %q is ambiguous across kinds: %s — re-run with --node-kind <one of them>",
				detail.Name, strings.Join(detail.Kinds, ", "))
		}
	}
	return "ambiguous node: " + decodeDetailString(body)
}

// doAuthedRequest issues a single HTTP request against the backplane
// with bearer injection + one-shot 401-refresh-retry. Returns the
// response body bytes (already drained) on a 2xx outcome, or an
// *httpError when the backplane returned a non-2xx, or an error
// categorised by api.IsTokenNotFound / api.IsNoRefreshToken / generic
// transport so renderRequestError can pick the right category.
//
// Mirrors cli/internal/cmd/targets/targets.go::doAuthedRequest. Kept
// independent of the targets package for the import-cycle reason
// called out on resolveBackplane.
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
		// One-shot refresh + retry, mirroring the targets sibling.
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

	raw, readErr := io.ReadAll(io.LimitReader(resp.Body, 1<<20)) // 1 MiB cap
	if readErr != nil {
		return nil, fmt.Errorf("read response: %w", readErr)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, &httpError{StatusCode: resp.StatusCode, Body: strings.TrimSpace(string(raw))}
	}
	return raw, nil
}

// httpError carries a non-2xx response so per-verb runners can render
// the right category. Not an output.StructuredError directly —
// renderHTTPError decides exit-code class based on status.
type httpError struct {
	StatusCode int
	Body       string
}

func (e *httpError) Error() string {
	return fmt.Sprintf("HTTP %d: %s", e.StatusCode, e.Body)
}

// sendRequest is the bottom of the stack: build the http.Request,
// stamp bearer + content headers, fire it. Split out so the
// 401-refresh-retry path can reuse the same body bytes.
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

// pathEscape escapes a single path segment for use inside a backend
// URL. url.PathEscape escapes path-segment-unsafe characters without
// touching the unreserved set.
func pathEscape(segment string) string {
	return url.PathEscape(segment)
}

// truncate cuts s to maxLen runes, appending an ellipsis when
// truncation happened. Rune-aware so multi-byte UTF-8 survives the
// cut. Same shape as the targets-package helper; kept here to avoid
// the import-cycle the cmd → cmd/topology → cmd path would create.
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
