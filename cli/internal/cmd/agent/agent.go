// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package agent hosts the cobra commands under `meho agent ...` for
// G11.1-T2 (#809) of Initiative #802 (the P1 agent runtime). v0.2 ships
// five operator-facing verbs that wrap the T2 REST surface
// (`backend/src/meho_backplane/api/v1/agents.py`):
//
//   - `meho agent list [--limit N] [--offset N] [--json]` — paginated
//     definition listing via GET /api/v1/agents. Role: operator.
//   - `meho agent show <name> [--json]` — single-definition fetch via
//     GET /api/v1/agents/{name}. Role: operator.
//   - `meho agent create <name> --identity-ref R --model-tier T
//     --system-prompt P --turn-budget N [--toolset @file] [--output-schema @file]
//     [--disabled] [--json]` — create via POST /api/v1/agents.
//     Role: tenant_admin.
//   - `meho agent edit <name> [--identity-ref R] [--model-tier T]
//     [--system-prompt P] [--turn-budget N] [--toolset @file]
//     [--output-schema @file] [--enabled|--disabled] [--json]` —
//     partial update via PATCH /api/v1/agents/{name}. Role: tenant_admin.
//   - `meho agent delete <name> [--confirm] [--json]` — delete via
//     DELETE /api/v1/agents/{name}. Role: tenant_admin.
//
// G11.1-T4 (#811) adds three operator-facing invocation verbs that wrap
// the T4 REST surface (`backend/src/meho_backplane/api/v1/agent_runs.py`):
//
//   - `meho agent run <name> --input TEXT [--async] [--json]` — run an
//     agent via POST /api/v1/agents/{name}/run. Sync (default) blocks for
//     the result; --async (or a sync run past the server-side timeout)
//     returns a handle. Role: operator.
//   - `meho agent run-status <handle> [--json]` — poll a run's durable
//     status via GET /api/v1/agents/runs/{handle}. Role: operator.
//   - `meho agent run-events <name> --input TEXT [--json]` — stream a fresh
//     run's events over SSE from POST /api/v1/agents/{name}/run/events.
//     Role: operator.
//
// Authentication piggybacks on the token meho login wrote — same
// pattern as `meho kb`, `meho broadcast`, `meho audit`. RBAC at the
// backend rejects non-tenant_admin write callers with HTTP 403; the
// verbs render this as insufficient_role.
//
// The HTTP surface is the generated oapi-codegen typed client at
// `cli/internal/api/client.gen.go`, wrapped by `api.AuthedClient` for
// the bearer + one-shot 401-refresh retry. The agent package no longer
// owns request/response struct definitions: `api.AgentDefinitionRead`,
// `api.AgentDefinitionListResponse`, `api.AgentDefinitionCreate`,
// `api.AgentDefinitionUpdate`, `api.AgentRunRequest`,
// `api.AgentRunStatusResponse`, `api.AgentGrantRead`,
// `api.AgentGrantListResponse`, `api.AgentGrantCreate`, and
// `api.AgentElevationCreate` are the single source of truth — kept in
// lock-step with the FastAPI Pydantic models by the
// `cli-api-snapshot-freshness` CI gate (G0.12 Initiative #1118).
package agent

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// NewRootCmd returns the `meho agent` parent command. Grafted onto the
// top-level meho tree by cmd/root.go alongside `meho kb`,
// `meho broadcast`, etc. The parent takes no args and prints its own
// help; every behaviour lives in the per-subcommand RunE closures.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "agent",
		Short: "Manage agent definitions (list / show / create / edit / delete)",
		Long: "Manage tenant-scoped agent definitions wired by G11.1. " +
			"An agent definition holds the identity reference, logical " +
			"model tier, system prompt, toolset spec, turn budget, and " +
			"optional output schema MEHO's agent runtime loads to run an " +
			"agent. Write verbs (create / edit / delete) require " +
			"tenant_admin; read verbs (list / show) are operator-level. " +
			"Tenant scoping is enforced server-side via the JWT — no " +
			"surface accepts a tenant id, and cross-tenant probes return " +
			"404 so existence is not leaked across tenant boundaries.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newListCmd())
	cmd.AddCommand(newShowCmd())
	cmd.AddCommand(newCreateCmd())
	cmd.AddCommand(newEditCmd())
	cmd.AddCommand(newDeleteCmd())
	// G11.1-T4 (#811): the invocation verbs — run (sync/async), run-status
	// (poll), run-events (SSE stream of a fresh run).
	cmd.AddCommand(newRunCmd())
	cmd.AddCommand(newRunStatusCmd())
	cmd.AddCommand(newRunEventsCmd())
	// G11.2-T6 (#819): permission grant management sub-tree — grant list /
	// show / create / elevate / revoke. All verbs require tenant_admin.
	cmd.AddCommand(NewGrantRootCmd())
	return cmd
}

// validModelTiers mirrors the backend AgentModelTier enum. CLI-side
// validation gives the operator an immediate rejection rather than a
// remote 422.
var validModelTiers = map[string]bool{"standard": true, "fast": true, "deep": true}

// errMissingAccessToken is the sentinel newAuthedClient returns when
// the stored token row exists but its access_token is empty — a
// credential-state failure that renderRequestError maps to
// auth_expired with a `meho login` hint.
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
// 401, runs a one-shot bearer refresh and re-issues call. Mirrors the
// behaviour `api.AuthedClient.GetHealth` implements for the
// /api/v1/health endpoint, generalised so every agent verb runs the
// same transparent-retry contract.
//
// statusOf reads the StatusCode off the typed response envelope (the
// generated *Response types expose StatusCode() through their embedded
// *http.Response). A nil response counts as "no retry" — the transport
// already failed and the caller surfaces err directly.
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

// renderRequestError translates a request error or the status code on
// a typed response into the right output.StructuredError category.
// Maps the agents REST surface's status codes:
//
//   - empty stored bearer → auth_expired.
//   - 401 (refresh failed) → auth_expired with a `meho login` hint.
//   - 403 → insufficient_role.
//   - 404 → unexpected with the backend's detail (`agent_not_found`;
//     cross-tenant probes land here per the no-existence-leak posture).
//   - 409 → unexpected with the duplicate detail (`agent_already_exists`).
//   - 422 → unexpected with the FastAPI validation envelope.
//   - Other 4xx/5xx → unexpected with the raw body.
//   - Pure transport errors → unreachable.
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
	return output.RenderError(cmd.ErrOrStderr(),
		output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, err)),
		jsonOut,
	)
}

// renderHTTPStatus classifies a non-2xx response (or 401 after a
// failed refresh) carried in the typed envelope into the right
// StructuredError category. resp must be non-nil and statusCode must
// be the HTTP status from resp; body is the raw response body bytes
// the envelope already buffered.
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
			output.InsufficientRole(decodeDetailString(bodyStr)),
			jsonOut,
		)
	case http.StatusNotFound:
		// `agent show / edit / delete` on an absent name surface here;
		// `agent_not_found` covers both genuine absence and cross-tenant
		// probes (the conflation prevents enumerating other tenants via
		// status-code differential). For `list` / `create` (no name in
		// the path) a 404 means the route doesn't exist on this
		// backplane — typically an older deploy without T2.
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(decodeDetailString(bodyStr)),
			jsonOut,
		)
	case http.StatusConflict:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(decodeDetailString(bodyStr)),
			jsonOut,
		)
	case http.StatusUnprocessableEntity:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("invalid request: %s", bodyStr)),
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

// detailEnvelope models FastAPI's HTTPException JSON shape.
type detailEnvelope struct {
	Detail json.RawMessage `json:"detail"`
}

// decodeDetailString pulls the `detail` field out of a FastAPI error
// body when it's a plain string. Falls back to the raw body when the
// JSON shape doesn't match.
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

// printDefinitionSummary renders a definition as a key-value summary.
// The system prompt is reported by length (not dumped) so the summary
// stays scannable; operators wanting the full prompt use `--json`.
func printDefinitionSummary(w io.Writer, e *api.AgentDefinitionRead) {
	if e == nil {
		return
	}
	fmt.Fprintf(w, "%-16s %s\n", "name:", e.Name)
	fmt.Fprintf(w, "%-16s %s\n", "id:", e.Id.String())
	fmt.Fprintf(w, "%-16s %s\n", "tenant_id:", e.TenantId.String())
	fmt.Fprintf(w, "%-16s %s\n", "identity_ref:", e.IdentityRef)
	fmt.Fprintf(w, "%-16s %s\n", "model_tier:", e.ModelTier)
	fmt.Fprintf(w, "%-16s %d\n", "turn_budget:", e.TurnBudget)
	fmt.Fprintf(w, "%-16s %t\n", "enabled:", e.Enabled)
	fmt.Fprintf(w, "%-16s %d bytes\n", "system_prompt:", len(e.SystemPrompt))
	fmt.Fprintf(w, "%-16s %t\n", "output_schema:", e.OutputSchema != nil)
	fmt.Fprintf(w, "%-16s %s\n", "created_by:", e.CreatedBySub)
	fmt.Fprintf(w, "%-16s %s\n", "created_at:", e.CreatedAt.UTC().Format("2006-01-02T15:04:05Z"))
	fmt.Fprintf(w, "%-16s %s\n", "updated_at:", e.UpdatedAt.UTC().Format("2006-01-02T15:04:05Z"))
}

// confirmPrompt prompts on stdin/stdout and returns true only when the
// operator types y/yes. EOF (closed stdin) is treated as a no — scripted
// use must pass --confirm. Mirrors the kb package's confirm helper.
func confirmPrompt(cmd *cobra.Command, prompt string) bool {
	fmt.Fprintf(cmd.OutOrStdout(), "%s [y/N]: ", prompt)
	var answer string
	if _, err := fmt.Fscanln(cmd.InOrStdin(), &answer); err != nil {
		return false
	}
	answer = strings.ToLower(strings.TrimSpace(answer))
	return answer == "y" || answer == "yes"
}

// jsonObjectCap bounds a --toolset / --output-schema @<path> or @- read
// so an adversarial / malformed file or pipe can't pin a create / edit
// verb in unbounded ReadAll. 256 KiB is generous for any realistic
// toolset spec or JSON Schema.
const jsonObjectCap int64 = 256 << 10

// readJSONFile is the file-read seam — a var so the unit tests can stub
// it deterministically without touching the filesystem. The
// implementation enforces jsonObjectCap so a multi-GiB JSON file
// passed via `@<path>` cannot OOM the CLI (review M4 on PR #1128 --
// the scheduler helper carried the same shape and is fixed in
// lock-step here).
var readJSONFile = func(path string) ([]byte, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	read, err := io.ReadAll(io.LimitReader(f, jsonObjectCap+1))
	if err != nil {
		return nil, err
	}
	if int64(len(read)) > jsonObjectCap {
		return nil, fmt.Errorf("file %q exceeds %d-byte cap", path, jsonObjectCap)
	}
	return read, nil
}

// loadJSONObjectFlag reads a flag value that carries a JSON object,
// supporting inline JSON, `@<path>` for a file, and `@-` for stdin. It
// returns nil for an empty value so the caller can omit the field from
// the request body entirely. The decoded value must be a JSON object
// (the backend's toolset / output_schema fields are objects); a non-
// object (array, scalar, or JSON `null`) is rejected at the CLI
// rather than after a remote 422. JSON `null` is rejected explicitly
// because json.Unmarshal of `null` into map[string]any sets the map
// to nil without returning an error -- a silent accept would forward
// an empty body field that the backend cannot disambiguate from
// "omitted" (review M3 on PR #1128).
func loadJSONObjectFlag(cmd *cobra.Command, raw, flagName string) (map[string]any, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil, nil
	}
	var blob []byte
	switch {
	case raw == "@-":
		read, err := io.ReadAll(io.LimitReader(cmd.InOrStdin(), jsonObjectCap+1))
		if err != nil {
			return nil, fmt.Errorf("read %s from stdin: %w", flagName, err)
		}
		if int64(len(read)) > jsonObjectCap {
			return nil, fmt.Errorf("%s from stdin exceeds %d-byte cap", flagName, jsonObjectCap)
		}
		blob = read
	case strings.HasPrefix(raw, "@"):
		path := strings.TrimPrefix(raw, "@")
		read, err := readJSONFile(path)
		if err != nil {
			return nil, fmt.Errorf("read %s file %q: %w", flagName, path, err)
		}
		blob = read
	default:
		blob = []byte(raw)
	}
	var out map[string]any
	if err := json.Unmarshal(blob, &out); err != nil {
		return nil, fmt.Errorf("%s must be a JSON object: %w", flagName, err)
	}
	if out == nil {
		return nil, fmt.Errorf("%s must be a JSON object (got null)", flagName)
	}
	return out, nil
}
