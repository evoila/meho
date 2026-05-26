// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package scheduler hosts the cobra commands under `meho scheduler ...`
// for G11.3-T5 (#826) of Initiative #804 (the P2 scheduler). v0.2 ships
// three operator-facing verbs that wrap the T5 REST surface
// (`backend/src/meho_backplane/api/v1/scheduler.py`):
//
//   - `meho scheduler list [--kind <kind>] [--status <s>] [--tenant <id>]
//     [--limit N] [--offset N] [--json]` — list triggers via
//     GET /api/v1/scheduler/triggers. Role: operator (operator role is
//     locked to its own tenant; --tenant is a tenant_admin-only filter).
//   - `meho scheduler create --kind <kind> --agent-definition <id>
//     [--cron-expr <expr>] [--fire-at <ts>] [--event-filter <json>]
//     [--timezone <tz>] [--inputs <json>] [--identity-sub <sub>]
//     [--in-flight-policy <p>] [--tenant <id>] [--json]` — create one
//     trigger via POST /api/v1/scheduler/triggers. Role: tenant_admin.
//   - `meho scheduler cancel <trigger_id> [--tenant <id>] [--json]` —
//     cancel one trigger via DELETE /api/v1/scheduler/triggers/{id}.
//     Role: tenant_admin.
//
// Authentication piggybacks on the token meho login wrote — same
// pattern as `meho agent`, `meho kb`, etc. RBAC at the backend rejects
// non-tenant_admin write callers with HTTP 403; the verbs render this
// as insufficient_role.
//
// The implementation follows the in-package HTTP helper pattern the
// sibling verb trees use (a local doAuthedRequest / renderRequestError
// pair) rather than a shared client package, for the import-cycle
// reason every sibling cites.
package scheduler

import (
	"bytes"
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

// jsonObjectCap bounds a --event-filter / --inputs @<path> or @- read
// so an adversarial / malformed file or pipe can't pin a create verb in
// unbounded ReadAll. 256 KiB is generous for any realistic filter or
// payload.
const jsonObjectCap int64 = 256 << 10

// readJSONFile is the file-read seam — a var so the unit tests can stub
// it deterministically without touching the filesystem. The
// implementation enforces jsonObjectCap so a multi-GiB JSON file
// passed via `@<path>` cannot OOM the CLI (review M4 on PR #1128).
// It mirrors the stdin branch's io.LimitReader discipline.
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
// the request body entirely. The decoded value must be a JSON object;
// a non-object (array, scalar, or JSON `null`) is rejected at the CLI
// rather than after a remote 422. Mirrors the agent package's helper
// of the same name. JSON `null` is rejected explicitly because
// json.Unmarshal of `null` into map[string]any sets the map to nil
// without returning an error -- a silent accept would forward an
// empty body field that the backend cannot disambiguate from
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

// NewRootCmd returns the `meho scheduler` parent command. Grafted onto
// the top-level meho tree by cmd/root.go alongside `meho agent`,
// `meho kb`, etc.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "scheduler",
		Short: "Manage scheduled triggers (list / create / cancel)",
		Long: "Manage tenant-scoped scheduled triggers wired by G11.3. " +
			"A scheduled trigger fires a P1 agent run on a cron " +
			"expression (kind=cron), at a single wall-clock instant " +
			"(kind=one_off), or on a matching MEHO-internal event " +
			"(kind=event; outbox-backed via G11.3-T3). Write verbs " +
			"(create / cancel) require tenant_admin; the read verb " +
			"(list) is operator-level. Tenant scoping is enforced " +
			"server-side via the JWT; tenant_admin callers may use " +
			"--tenant to act on another tenant's triggers.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newListCmd())
	cmd.AddCommand(newCreateCmd())
	cmd.AddCommand(newCancelCmd())
	return cmd
}

// Trigger mirrors the backend ScheduledTriggerRead pydantic model
// (`backend/src/meho_backplane/scheduler/schemas.py`). Hand-written
// rather than aliased to a generated client type so the scheduler
// package stays decoupled from oapi-codegen churn — the stance the
// agent / kb / audit / broadcast packages take.
type Trigger struct {
	ID                string         `json:"id"`
	TenantID          string         `json:"tenant_id"`
	AgentDefinitionID string         `json:"agent_definition_id"`
	Kind              string         `json:"kind"`
	CronExpr          *string        `json:"cron_expr"`
	Timezone          string         `json:"timezone"`
	FireAt            *string        `json:"fire_at"`
	EventFilter       map[string]any `json:"event_filter"`
	Status            string         `json:"status"`
	InFlightPolicy    string         `json:"in_flight_policy"`
	NextFireAt        *string        `json:"next_fire_at"`
	LastFiredAt       *string        `json:"last_fired_at"`
	Inputs            map[string]any `json:"inputs"`
	IdentitySub       string         `json:"identity_sub"`
	CreatedBySub      string         `json:"created_by_sub"`
	CreatedAt         string         `json:"created_at"`
	UpdatedAt         string         `json:"updated_at"`
}

// ListResponse mirrors the backend ScheduledTriggerListResponse
// envelope (`{"triggers": [...]}`).
type ListResponse struct {
	Triggers []Trigger `json:"triggers"`
}

// validKinds mirrors the backend ScheduledTriggerKind enum.
var validKinds = map[string]bool{"cron": true, "one_off": true, "event": true}

// validStatuses mirrors the backend ScheduledTriggerStatus enum.
var validStatuses = map[string]bool{
	"active":    true,
	"paused":    true,
	"cancelled": true,
	"fired":     true,
}

// validInFlightPolicies mirrors the backend ScheduledTriggerInFlightPolicy enum.
var validInFlightPolicies = map[string]bool{
	"fail_into_audit": true,
	"resume":          true,
}

// errMissingAccessToken is the sentinel doAuthedRequest returns when
// the stored token row exists but its access_token is empty.
var errMissingAccessToken = errors.New("meho: stored token has no access_token")

// renderRequestError translates a request error into the right
// output.StructuredError category, mirroring the agent package shape.
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
	var he *httpError
	if errors.As(err, &he) {
		return renderHTTPError(cmd, backplaneURL, he, jsonOut)
	}
	return output.RenderError(cmd.ErrOrStderr(),
		output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, err)),
		jsonOut,
	)
}

// renderHTTPError classifies a non-2xx response.
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
		// `scheduler cancel` on an absent id surfaces here;
		// `trigger_not_found` covers both genuine absence and
		// cross-tenant probes (no existence leak across tenants).
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(decodeDetailString(he.Body)),
			jsonOut,
		)
	case http.StatusConflict:
		// `trigger_already_fired` on a cancel against a terminal one-off.
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

// detailEnvelope models FastAPI's HTTPException JSON shape.
type detailEnvelope struct {
	Detail json.RawMessage `json:"detail"`
}

// decodeDetailString pulls the `detail` field out of a FastAPI error body.
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
// with bearer injection and one-shot 401-refresh-retry.
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
	if resp.StatusCode == http.StatusNoContent {
		return nil, nil
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, &httpError{StatusCode: resp.StatusCode, Body: strings.TrimSpace(string(raw))}
	}
	return raw, nil
}

// responseBodyCap bounds the response body the CLI will read. 1 MiB
// is comfortable for a paginated trigger list (limit 500 with full row
// payloads stays well under).
const responseBodyCap int64 = 1 << 20

// httpError carries a non-2xx response.
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

// printTriggerSummary renders one trigger as a key-value summary.
func printTriggerSummary(w io.Writer, t *Trigger) {
	if t == nil {
		return
	}
	fmt.Fprintf(w, "%-22s %s\n", "id:", t.ID)
	fmt.Fprintf(w, "%-22s %s\n", "tenant_id:", t.TenantID)
	fmt.Fprintf(w, "%-22s %s\n", "agent_definition_id:", t.AgentDefinitionID)
	fmt.Fprintf(w, "%-22s %s\n", "kind:", t.Kind)
	fmt.Fprintf(w, "%-22s %s\n", "status:", t.Status)
	fmt.Fprintf(w, "%-22s %s\n", "in_flight_policy:", t.InFlightPolicy)
	if t.CronExpr != nil {
		fmt.Fprintf(w, "%-22s %s\n", "cron_expr:", *t.CronExpr)
		fmt.Fprintf(w, "%-22s %s\n", "timezone:", t.Timezone)
	}
	if t.FireAt != nil {
		fmt.Fprintf(w, "%-22s %s\n", "fire_at:", *t.FireAt)
	}
	if t.NextFireAt != nil {
		fmt.Fprintf(w, "%-22s %s\n", "next_fire_at:", *t.NextFireAt)
	}
	if t.LastFiredAt != nil {
		fmt.Fprintf(w, "%-22s %s\n", "last_fired_at:", *t.LastFiredAt)
	}
	fmt.Fprintf(w, "%-22s %s\n", "identity_sub:", t.IdentitySub)
	fmt.Fprintf(w, "%-22s %s\n", "created_by:", t.CreatedBySub)
	fmt.Fprintf(w, "%-22s %s\n", "created_at:", t.CreatedAt)
}
