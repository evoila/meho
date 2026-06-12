// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package targets

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
	"sort"
	"strings"

	"github.com/spf13/cobra"
	"gopkg.in/yaml.v3"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// --- YAML shape --------------------------------------------------------

// importDoc is the on-disk root: a single `targets:` list, matching
// the consumer's existing `targets.yaml` shape (see
// docs/cross-repo/targets-yaml.md and the consumer source-of-truth
// file at
// https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/rdc-hetzner-dc/targets.yaml).
//
// We decode into a generic per-entry map (rather than a typed struct)
// so the mapping logic in importEntry / entryToCreateBody can decide
// per-key whether each YAML key maps to a known top-level column on
// the API's TargetCreate / TargetUpdate models or spills into the
// `extras` JSONB column. A typed struct would force every unknown
// field through a `mapstructure`-style fallback and lose source-line
// information from the YAML node — the generic map keeps the path
// open to per-key warnings without bookkeeping at the parser layer.
type importDoc struct {
	Targets []map[string]any `yaml:"targets"`
}

// knownTopLevel is the set of YAML keys that map 1:1 to columns on
// the API's TargetCreate body (see
// backend/src/meho_backplane/targets/schemas.py).
//
// Anything not in this set (and not in skipSilent) is spilled into
// the `extras` JSONB column on the POST body. Sorted alphabetically
// for diff stability.
//
// Post-#477 amendment: `preferred_impl_id` is a real top-level
// column on both TargetCreate and TargetUpdate. The amendment also
// added `fingerprint`, but that column is server-managed (the probe
// verb is the only writer; the API rejects any value via
// `extra='forbid'`); see skipSilent below for the corresponding
// skip rule.
var knownTopLevel = map[string]struct{}{
	"aliases":           {},
	"auth_model":        {},
	"extras":            {},
	"fqdn":              {},
	"host":              {},
	"name":              {},
	"notes":             {},
	"port":              {},
	"preferred_impl_id": {},
	"product":           {},
	"secret_ref":        {},
	"vpn_required":      {},
}

// skipSilent is the set of YAML keys we deliberately drop on the
// floor with a warning log line rather than passing through to the
// API. The only entry today is `fingerprint`: the backplane probe
// verb is the sole legitimate writer to `targets.fingerprint`
// (G0.3-T1.5 #477 amendment); the API rejects any caller-supplied
// value with 422 via `model_config = ConfigDict(extra='forbid')`.
// Skipping with a warning is friendlier than letting the operator's
// import abort on a 422 they can't fix without editing the source
// `targets.yaml`.
var skipSilent = map[string]struct{}{
	"fingerprint": {},
}

// --- Plan model --------------------------------------------------------

// action describes what the import would do for one entry.
type action string

const (
	actionCreate action = "CREATE"
	actionUpdate action = "UPDATE"
	actionSkip   action = "SKIP"
)

// planEntry captures one decision: what to do with one YAML entry.
//
// Body is the JSON request payload (already shaped for the chosen
// route). It's a map[string]any rather than a typed TargetCreate /
// TargetUpdate so the PATCH path can emit a sparse body (only keys
// present in the YAML) — see entryToUpdateBody. The CREATE path
// could use the typed shape but uses the same untyped map for
// uniformity, which also sidesteps the generated client's
// out-of-date snapshot (the openapi.json on main predates the
// #477 amendment, so client.gen.go has no `preferred_impl_id`
// field on TargetCreate yet).
type planEntry struct {
	Name   string         `json:"name"`
	Action action         `json:"action"`
	Body   map[string]any `json:"body,omitempty"`
	// Warnings collects per-entry advisory messages (e.g. "skipped
	// field `fingerprint`: server-managed"). Surfaced in dry-run
	// output and on apply.
	Warnings []string `json:"warnings,omitempty"`
}

// plan is the full set of actions, partitioned for `--json` output.
type plan struct {
	Create []planEntry `json:"create"`
	Update []planEntry `json:"update"`
	Skip   []planEntry `json:"skip"`
}

// summary returns a one-line header for human render.
func (p *plan) summary() string {
	return fmt.Sprintf(
		"Plan: %d to create, %d to update, %d to skip",
		len(p.Create), len(p.Update), len(p.Skip),
	)
}

// --- cobra surface -----------------------------------------------------

// newImportCmd returns the `meho targets import` subcommand.
//
// CLI shape:
//
//	meho targets import <file>
//	  [--update]              # PATCH existing targets instead of erroring on duplicates
//	  [--dry-run]             # print the plan; no API calls
//	  [--json]                # output the plan as JSON (use with --dry-run)
//	  [--backplane <url>]     # override the backplane URL
//
// Exit codes:
//   - 0   import (or dry-run) succeeded
//   - 1   default mode hit a duplicate name (use --update to PATCH)
//   - 2   auth_expired (operator never ran `meho login`, or refresh
//     failed without a refresh_token)
//   - 3   unreachable (transport-level failure against the backplane)
//   - 4   unexpected (file read / YAML parse / unhandled response shape)
func newImportCmd() *cobra.Command {
	var (
		updateMode        bool
		dryRun            bool
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "import <file>",
		Short: "Bulk-import targets from a targets.yaml file",
		Long: "import reads a YAML file shaped as `targets: [<entry>, ...]` and " +
			"applies each entry against the backplane.\n\n" +
			"Mapping rules. Top-level columns recognised on the API's " +
			"TargetCreate / TargetUpdate models are mapped 1:1: name, aliases, " +
			"product, host, port, fqdn, secret_ref, auth_model, vpn_required, " +
			"notes, preferred_impl_id. Any other field is spilled into the " +
			"`extras` JSONB column. `fingerprint` is server-managed and " +
			"skipped with a warning if present in the YAML (the probe verb " +
			"is the only legitimate writer).\n\n" +
			"Idempotency. Default mode aborts the whole import if any entry's " +
			"`name` already exists in the tenant (no partial write — the plan " +
			"is built before any API call fires). `--update` PATCHes existing " +
			"targets with the fields present in the YAML and POSTs new ones, " +
			"mixed-mode-safe. `--dry-run` prints the plan and returns without " +
			"calling the apply path. `--json` formats the plan as a structured " +
			"object (use with --dry-run).\n\n" +
			"Authentication. Uses the token `meho login` wrote, with the same " +
			"401-refresh-retry behaviour as `meho status` and `meho operation " +
			"call`. The tenant is the operator's JWT-bound tenant — there is no " +
			"`--tenant` flag in v0.2.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runImport(cmd, importOptions{
				File:              args[0],
				Update:            updateMode,
				DryRun:            dryRun,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().BoolVar(&updateMode, "update", false,
		"PATCH existing targets instead of erroring on duplicate names")
	cmd.Flags().BoolVar(&dryRun, "dry-run", false,
		"print the plan; no API calls")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"output the plan as JSON (use with --dry-run)")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to import into (defaults to the URL recorded by `meho login`)")
	return cmd
}

type importOptions struct {
	File              string
	Update            bool
	DryRun            bool
	JSONOut           bool
	BackplaneOverride string
}

// runImport is the cobra entry point's body. Split out so tests can
// exercise the orchestration without the cobra harness.
func runImport(cmd *cobra.Command, opts importOptions) error {
	data, err := os.ReadFile(opts.File)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("read %s: %v", opts.File, err)),
			opts.JSONOut)
	}
	entries, err := parseTargetsYAML(data)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(err.Error()), opts.JSONOut)
	}

	// Dry-run path: resolve nothing against the backplane. Existence
	// is unknown, so every entry plans as CREATE. Operators wanting
	// "what would change" against a live tenant pass --dry-run after
	// running once with --update, or just look at `meho targets list`
	// before running. v0.2 keeps the no-API-call contract strict; a
	// future `--dry-run --against-tenant` could relax it.
	if opts.DryRun {
		p := buildOfflinePlan(entries, opts.Update)
		return renderPlan(cmd, p, opts.JSONOut)
	}

	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	doer := authedDoer(backplaneURL)

	p, err := buildLivePlan(cmd.Context(), doer, entries, opts.Update)
	if err != nil {
		return renderImportRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}

	// Default mode: duplicates abort the whole import.
	if !opts.Update && len(p.Update) > 0 {
		names := make([]string, len(p.Update))
		for i, e := range p.Update {
			names[i] = e.Name
		}
		sort.Strings(names)
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"%d target(s) already exist in the tenant: %s\n"+
					"Re-run with --update to PATCH them, or remove the conflicts from the YAML.",
				len(names), strings.Join(names, ", "),
			)),
			opts.JSONOut)
	}

	if err := executePlan(cmd.Context(), doer, p); err != nil {
		return renderImportRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	return renderApplyResult(cmd, p, opts.JSONOut)
}

// --- YAML parsing ------------------------------------------------------

// parseTargetsYAML decodes the file contents into a list of generic
// entry maps, validates required fields per entry, and returns the
// entries in source order. Errors carry the offending entry's name
// (when known) and the file's source coordinates where possible.
//
// Why generic maps + manual validation rather than a typed struct:
// the mapping rules in this file partition every YAML key into
// (top-level column / extras spill / skip). A typed struct would
// either ignore unknown keys silently (losing the extras spill
// contract) or fail strict-decode (losing the consumer's existing
// file). The generic-map shape keeps both contracts on the same
// codepath.
func parseTargetsYAML(data []byte) ([]map[string]any, error) {
	var doc importDoc
	// KnownFields is intentionally not used here — see comment above.
	if err := yaml.Unmarshal(data, &doc); err != nil {
		return nil, fmt.Errorf("parse YAML: %w", err)
	}
	if len(doc.Targets) == 0 {
		return nil, errors.New("parse YAML: no `targets:` list found, or list is empty")
	}
	// Validate required-field contract per entry. The API rejects
	// missing `name`/`product`/`host` with 422, but a local check
	// here lets us abort before the first HTTP request, which is the
	// contract the issue body calls out for malformed entries.
	for i, e := range doc.Targets {
		name, _ := e["name"].(string)
		if name == "" {
			return nil, fmt.Errorf("entry %d: missing or empty `name` field", i)
		}
		if _, ok := e["product"].(string); !ok {
			return nil, fmt.Errorf("entry %q: missing or non-string `product` field", name)
		}
		if _, ok := e["host"].(string); !ok {
			return nil, fmt.Errorf("entry %q: missing or non-string `host` field", name)
		}
	}
	return doc.Targets, nil
}

// entryToCreateBody maps one YAML entry to a JSON body for
// POST /api/v1/targets. Known top-level keys land at the top level;
// everything else lands inside `extras`. `fingerprint` is dropped
// with a warning. Returns the body plus any per-entry warnings.
//
// Note: an `extras` key explicitly present in YAML *replaces* (does
// not merge with) the spilled-extras map. The decision is the same
// the backplane's PATCH semantics enforce — passing `extras: {...}`
// is a wholesale replacement, not a deep merge.
func entryToCreateBody(entry map[string]any) (map[string]any, []string) {
	body, warnings := mapEntry(entry)
	return body, warnings
}

// entryToUpdateBody maps one YAML entry to a sparse JSON body for
// PATCH /api/v1/targets/{name}. Only keys present in the YAML
// appear in the body — the API's `model_dump(exclude_unset=True)`
// path on the route handler then only touches the listed columns.
// `name` and `product` are stripped because the API rejects them on
// PATCH (rename = delete + create per the v0.2 decision).
//
// This is the load-bearing piece of the sparse-PATCH contract the
// review on PR #362 called out — without this stripping the import
// would wipe every column the YAML omits, because Pydantic v2's
// "explicit null counts as set" rule combined with `setattr` in the
// route handler is PUT-shaped, not PATCH-shaped. The previous
// implementation populated every field (defaulted aliases to [],
// vpn_required to false, etc.) and then `--update` overwrote rich
// existing state on every run.
func entryToUpdateBody(entry map[string]any) (map[string]any, []string) {
	body, warnings := mapEntry(entry)
	// `name` and `product` are immutable post-create — strip them
	// rather than send them and trip 422 on the backplane.
	delete(body, "name")
	delete(body, "product")
	return body, warnings
}

// mapEntry is the shared per-key partition routine: top-level →
// passthrough, extras → spill, fingerprint → skip-with-warning,
// extras-from-YAML → merge with spilled.
//
// Cognitive-complexity is intentionally kept low here by handling
// each rule in a single switch arm — past iterations of this file
// fanned out to per-key helpers and tripped SonarCloud's threshold.
func mapEntry(entry map[string]any) (map[string]any, []string) {
	body := make(map[string]any, len(entry))
	extras := map[string]any{}
	var warnings []string

	// First pass: extract an explicit `extras:` block from the YAML,
	// if present. We need it on hand so unknown-key spills can
	// merge into it (rather than overwriting the operator's
	// intentional payload).
	if rawExtras, ok := entry["extras"].(map[string]any); ok {
		for k, v := range rawExtras {
			extras[k] = v
		}
	}

	// Second pass: partition every other key per the mapping rules.
	for k, v := range entry {
		if k == "extras" {
			// Handled in the pre-pass above. Skip here so the
			// extras dict isn't double-counted.
			continue
		}
		if _, skip := skipSilent[k]; skip {
			warnings = append(warnings,
				fmt.Sprintf("skipped field %q: server-managed, set via probe verb", k))
			continue
		}
		if _, known := knownTopLevel[k]; known {
			body[k] = v
			continue
		}
		// Unknown key → spill into extras.
		extras[k] = v
	}

	if len(extras) > 0 {
		body["extras"] = extras
	}
	return body, warnings
}

// --- plan building -----------------------------------------------------

// buildOfflinePlan partitions entries without consulting the
// backplane: every entry plans as CREATE (existence is unknown).
// Used by --dry-run.
func buildOfflinePlan(entries []map[string]any, _ bool) *plan {
	p := &plan{}
	for _, e := range entries {
		name, _ := e["name"].(string)
		body, warnings := entryToCreateBody(e)
		p.Create = append(p.Create, planEntry{
			Name:     name,
			Action:   actionCreate,
			Body:     body,
			Warnings: warnings,
		})
	}
	return p
}

// httpDoer is the function shape buildLivePlan / executePlan use to
// talk to the backplane. Production code passes doAuthedRequest;
// tests pass a closure that drives an httptest.Server, sidestepping
// the auth/token-store machinery (which would otherwise require
// staging a fake $XDG_CONFIG_HOME with a stub credentials file).
//
// `import.go` keeps its own untyped HTTP plumbing (rather than the
// generated typed client every sibling verb now uses) because the
// YAML-to-API mapping in entryToCreateBody / entryToUpdateBody emits
// a sparse `map[string]any` body to preserve the partial-PATCH /
// extras-spill semantics — coercing through `api.TargetCreate` /
// `api.TargetUpdate` would either send unspecified fields as
// pydantic defaults (overwriting rich existing state on a partial
// patch — the PR #362 regression that motivated the sparse-body
// fix) or require per-field option plumbing for every patchable
// column. The untyped path is the smaller blast radius for v0.2.
type httpDoer func(ctx context.Context, method, path string, body []byte) ([]byte, error)

// httpError carries a non-2xx response from the import-path HTTP
// plumbing so renderRequestError can map it to the right exit-code
// class. Not exported and not shared with the verb-side typed-client
// renderHTTPStatus, which acts on the typed response envelope's
// (statusCode, body) pair directly.
type httpError struct {
	StatusCode int
	Body       string
}

func (e *httpError) Error() string {
	return fmt.Sprintf("HTTP %d: %s", e.StatusCode, e.Body)
}

// authedDoer adapts doAuthedRequest to the httpDoer shape with the
// backplane URL pre-bound.
func authedDoer(backplaneURL string) httpDoer {
	return func(ctx context.Context, method, path string, body []byte) ([]byte, error) {
		return doAuthedRequest(ctx, backplaneURL, method, path, body)
	}
}

// doAuthedRequest issues a single HTTP request against the backplane
// with bearer injection + one-shot 401-refresh-retry. Returns the
// response body bytes (already drained) on a 2xx outcome, or an
// *httpError when the backplane returned a non-2xx, or an error
// categorised by api.IsTokenNotFound / api.IsNoRefreshToken / generic
// transport so renderRequestError can pick the right StructuredError
// category.
//
// Mirrors cli/internal/cmd/operation/operation.go::doAuthedRequest.
// Kept independent of the operation package for the import-cycle
// reason called out on resolveBackplane.
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
		// One-shot refresh + retry, mirroring api.AuthedClient.GetHealth
		// and operation/operation.go doAuthedRequest.
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
	defer resp.Body.Close() //nolint:errcheck

	raw, readErr := io.ReadAll(io.LimitReader(resp.Body, 1<<20)) // 1 MiB cap
	if readErr != nil {
		return nil, fmt.Errorf("read response: %w", readErr)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, &httpError{StatusCode: resp.StatusCode, Body: strings.TrimSpace(string(raw))}
	}
	return raw, nil
}

// sendRequest is the bottom of the stack: build the http.Request,
// stamp bearer + content headers, fire it. Split out so the
// 401-refresh-retry path in doAuthedRequest can reuse the same body
// bytes without re-marshalling.
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

// renderImportRequestError translates an error from one of the
// import-path HTTP helpers (`doAuthedRequest`, `buildLivePlan`,
// `executePlan`) into the right output.StructuredError category. The
// HTTP-failure case routes through the same renderHTTPStatus the
// verb-side helpers use (same 401/403/404/409/501 ladder); the
// transport / auth-state cases delegate to the shared
// renderRequestError. Errors from `listExistingNames` /
// `executePlan` arrive wrapped in `fmt.Errorf(... "%w", ...)`; we
// use `errors.As` to unwrap to the underlying *httpError.
func renderImportRequestError(
	cmd *cobra.Command,
	backplaneURL string,
	err error,
	jsonOut bool,
) error {
	var he *httpError
	if errors.As(err, &he) {
		return renderHTTPStatus(cmd, backplaneURL, he.StatusCode, []byte(he.Body), jsonOut)
	}
	return renderRequestError(cmd, backplaneURL, err, jsonOut)
}

// buildLivePlan partitions entries against the live backplane:
// existing targets plan as UPDATE (when --update is set) or are
// recorded in Update for the duplicate-detection branch (when
// --update is unset).
func buildLivePlan(
	ctx context.Context,
	doer httpDoer,
	entries []map[string]any,
	updateMode bool,
) (*plan, error) {
	existing, err := listExistingNames(ctx, doer)
	if err != nil {
		return nil, err
	}
	p := &plan{}
	for _, e := range entries {
		name, _ := e["name"].(string)
		if _, ok := existing[name]; ok {
			body, warnings := entryToUpdateBody(e)
			pe := planEntry{Name: name, Action: actionUpdate, Body: body, Warnings: warnings}
			if !updateMode {
				// In non-update mode we still record the entry so
				// the caller can render the conflict list — but we
				// will not apply it.
				p.Update = append(p.Update, pe)
				continue
			}
			p.Update = append(p.Update, pe)
			continue
		}
		body, warnings := entryToCreateBody(e)
		p.Create = append(p.Create, planEntry{
			Name: name, Action: actionCreate, Body: body, Warnings: warnings,
		})
	}
	return p, nil
}

// --- rendering ---------------------------------------------------------

func renderPlan(cmd *cobra.Command, p *plan, jsonOut bool) error {
	if jsonOut {
		return output.PrintJSON(cmd.OutOrStdout(), p)
	}
	w := cmd.OutOrStdout()
	fmt.Fprintln(w, p.summary())
	fmt.Fprintln(w)
	for _, e := range p.Create {
		fmt.Fprintf(w, "  CREATE  %s\n", e.Name)
		for _, msg := range e.Warnings {
			fmt.Fprintf(w, "          (%s)\n", msg)
		}
	}
	for _, e := range p.Update {
		fmt.Fprintf(w, "  UPDATE  %s\n", e.Name)
		for _, msg := range e.Warnings {
			fmt.Fprintf(w, "          (%s)\n", msg)
		}
	}
	for _, e := range p.Skip {
		fmt.Fprintf(w, "  SKIP    %s\n", e.Name)
	}
	fmt.Fprintln(w)
	fmt.Fprintln(w, "Run without --dry-run to apply.")
	return nil
}

func renderApplyResult(cmd *cobra.Command, p *plan, jsonOut bool) error {
	if jsonOut {
		return output.PrintJSON(cmd.OutOrStdout(), p)
	}
	w := cmd.OutOrStdout()
	fmt.Fprintf(w, "Applied: %d created, %d updated.\n",
		len(p.Create), len(p.Update))
	return nil
}

// --- HTTP plumbing -----------------------------------------------------

// listExistingNames fetches `GET /api/v1/targets` with full keyset
// pagination and returns a set of target names in the operator's
// tenant. Used by buildLivePlan to decide CREATE vs UPDATE.
func listExistingNames(ctx context.Context, doer httpDoer) (map[string]struct{}, error) {
	existing := map[string]struct{}{}
	cursor := ""
	for {
		path := "/api/v1/targets?limit=500"
		if cursor != "" {
			path += "&cursor=" + url.QueryEscape(cursor)
		}
		raw, err := doer(ctx, http.MethodGet, path, nil)
		if err != nil {
			return nil, fmt.Errorf("list existing targets: %w", err)
		}
		var page []struct {
			Name string `json:"name"`
		}
		if err := json.Unmarshal(raw, &page); err != nil {
			return nil, fmt.Errorf("decode list response: %w", err)
		}
		if len(page) == 0 {
			break
		}
		for _, t := range page {
			existing[t.Name] = struct{}{}
		}
		// Keyset cursor is the last returned name. When the page is
		// shorter than the limit we've consumed every row.
		if len(page) < 500 {
			break
		}
		cursor = page[len(page)-1].Name
	}
	return existing, nil
}

// executePlan applies the plan: POST for each Create, PATCH for
// each Update. Aborts on the first error, returning it. The plan
// has already been ordered by buildLivePlan (and validated by the
// caller for non-update conflicts), so no further bookkeeping is
// needed here.
//
// One quirk worth pinning: we serialise apply rather than fanning
// out. The 25-target consumer file applies in ~5s sequentially.
// Concurrency would help large imports but introduces ordering
// surprises (audit log row interleaving, partial-failure
// rollback semantics); v0.2 deliberately stays serial.
func executePlan(ctx context.Context, doer httpDoer, p *plan) error {
	for _, e := range p.Create {
		body, err := json.Marshal(e.Body)
		if err != nil {
			return fmt.Errorf("marshal create body for %q: %w", e.Name, err)
		}
		if _, err := doer(ctx, http.MethodPost, "/api/v1/targets", body); err != nil {
			return fmt.Errorf("create %q: %w", e.Name, err)
		}
	}
	for _, e := range p.Update {
		body, err := json.Marshal(e.Body)
		if err != nil {
			return fmt.Errorf("marshal update body for %q: %w", e.Name, err)
		}
		path := "/api/v1/targets/" + url.PathEscape(e.Name)
		if _, err := doer(ctx, http.MethodPatch, path, body); err != nil {
			return fmt.Errorf("update %q: %w", e.Name, err)
		}
	}
	return nil
}
