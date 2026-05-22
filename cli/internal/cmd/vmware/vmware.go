// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package vmware hosts the cobra commands under `meho vmware ...` for
// G3.1-T7 (#511) of Initiative #227. v0.2 ships the operator-facing
// alias verbs the issue #227 §10 verb table names, each pre-baking
// the `connector_id="vmware-rest-9.0"` argument so operators don't
// type the connector ID on every dispatch:
//
//   - `meho vmware about [--target T]`              — GET:/api/about
//   - `meho vmware vm list [--target T]`            — GET:/vcenter/vm
//   - `meho vmware vm info <name-or-id>`            — name → moid then GET:/vcenter/vm/{vm}
//   - `meho vmware vm create --spec @file`          — vmware.composite.vm.create (T6)
//   - `meho vmware host list`                       — GET:/vcenter/host
//   - `meho vmware host evacuate <name>`            — vmware.composite.host.evacuate (T6)
//   - `meho vmware cluster list`                    — GET:/vcenter/cluster
//   - `meho vmware cluster patch <name>`            — vmware.composite.cluster.patch (T6)
//   - `meho vmware datacenter list`                 — GET:/vcenter/datacenter
//   - `meho vmware datastore list`                  — GET:/vcenter/datastore
//   - `meho vmware network list`                    — GET:/vcenter/network
//   - `meho vmware operation search "<query>"`      — search_operations pre-scoped
//   - `meho vmware operation call <op_id> ...`      — call_operation pre-scoped
//
// Every verb is a thin Cobra command that POSTs to
// `/api/v1/operations/call` (or GET /api/v1/operations/search for
// the meta-tool wrapper) with a pre-baked connector_id. No new
// backend code; no new HTTP routes — CLI alias verbs are pure
// operator ergonomics over the existing dispatcher surface (per
// CLAUDE.md postulate 5: agent surface stays narrow-waist meta-tools;
// vendor-specific tooling lives only in the CLI).
//
// The composite-backed verbs (`vm create`, `host evacuate`,
// `cluster patch`) dispatch their composite op_ids verbatim. Pre-
// merge of #508 (T5 read composites) and #509 (T6 write composites),
// the dispatcher returns a "operation not found" status which the
// verb surfaces with operator-readable text via the same renderer
// the success path uses. PR-1 (this) ships the verb tree on top of
// T1 (#498); the composite-backed verbs become end-to-end
// dispatchable once #508 + #509 merge.
package vmware

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

// ConnectorID is the pre-baked connector_id every verb under
// `meho vmware ...` dispatches against. Exported so the per-verb
// files and tests reference the same constant; a future
// re-versioning (vmware-rest-9.1) lands as a single line edit here.
const ConnectorID = "vmware-rest-9.0"

// NewRootCmd returns the `meho vmware` parent command. cmd/root.go
// grafts this onto the top-level command tree alongside the other
// built-in verb trees (operation / connector / targets / kb /
// retrieval / audit). The parent itself takes no args and prints its
// own help; every piece of behaviour lives in the per-subcommand
// RunE closures.
//
// Sub-tree layout follows the issue #227 §10 verb table:
//   - `vmware about`              — flat verb
//   - `vmware vm <list|info|create>`     — sub-tree
//   - `vmware host <list|evacuate>`      — sub-tree
//   - `vmware cluster <list|patch>`      — sub-tree
//   - `vmware datacenter list`           — flat verb
//   - `vmware datastore list`            — flat verb
//   - `vmware network list`              — flat verb
//   - `vmware operation <search|call>`   — sub-tree (meta-tool wrappers)
//
// Sub-tree roots delegate to their own NewRootCmd-style factories in
// nested subpackages (cmd/vmware/vm, cmd/vmware/host, etc.) so each
// noun's verbs live next to their tests without bloating this file.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "vmware",
		Short: "Pre-scoped CLI verbs for the vmware-rest-9.0 connector",
		Long: "vmware is the operator-facing verb tree for the vmware-rest-9.0\n" +
			"connector. Each verb dispatches through POST /api/v1/operations/call\n" +
			"with connector_id=\"vmware-rest-9.0\" pre-baked so operators don't\n" +
			"type the connector ID on every command. Verbs that accept human-\n" +
			"readable names (vm info, host evacuate, cluster patch) resolve\n" +
			"name → moid client-side via the appropriate /vcenter/<kind>?filter\n" +
			"call before dispatching the target operation.\n\n" +
			"Per CLAUDE.md postulate 5, these alias verbs are operator-only\n" +
			"ergonomics — they are not mirrored on the MCP surface. Agents\n" +
			"continue to use search_operations / call_operation against the\n" +
			"narrow-waist meta-tool contract.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newAboutCmd())
	cmd.AddCommand(newVMCmd())
	cmd.AddCommand(newHostCmd())
	cmd.AddCommand(newClusterCmd())
	cmd.AddCommand(newDatacenterCmd())
	cmd.AddCommand(newDatastoreCmd())
	cmd.AddCommand(newNetworkCmd())
	cmd.AddCommand(newOperationCmd())
	return cmd
}

// renderRequestError translates an error from doAuthedRequest into
// the right output.RenderError category. Same classification ladder
// as the operation sibling: token-not-found / no-refresh-token →
// auth_expired with `meho login` hints; HTTP-error → unexpected;
// everything else (transport) → unreachable.
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

// httpError carries a non-2xx response so renderRequestError can
// pick the right StructuredError category. Same shape as the
// operation / connector siblings.
type httpError struct {
	StatusCode int
	Body       string
}

func (e *httpError) Error() string {
	return fmt.Sprintf("HTTP %d: %s", e.StatusCode, e.Body)
}

// doAuthedRequest issues a single HTTP request against the backplane
// with bearer injection + one-shot 401-refresh-retry. Mirrors the
// operation / connector siblings verbatim (duplicated to avoid an
// import cycle — cmd/root.go grafts each onto the tree). Centralised
// per-package so the per-verb runners stay small.
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

	// 1 MiB cap matches the operation sibling. Vendor responses (vm
	// lists on busy targets) can be hundreds of KiB; the cap leaves
	// headroom while still bounding pathological payloads.
	raw, readErr := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if readErr != nil {
		return nil, fmt.Errorf("read response: %w", readErr)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, &httpError{StatusCode: resp.StatusCode, Body: strings.TrimSpace(string(raw))}
	}
	return raw, nil
}

// sendRequest builds + fires the HTTP request. Mirrors the operation
// sibling; split out so the 401-refresh-retry path can reuse the
// same body bytes without re-marshalling.
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

// loadParamsFlag parses the --params flag value. Prefixing with '@'
// loads the named file as JSON; otherwise the value itself is parsed
// as inline JSON. Returns nil for an empty value so the caller can
// omit the "params" key from the JSON body. Same shape as the
// operation sibling.
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

// jsonUnmarshalStrict is a thin wrapper over json.Unmarshal that
// keeps the call sites readable when the shape is unsigned (`if err
// := jsonUnmarshalStrict(raw, &x); err == nil && x.Foo != ""`).
// Same semantics as json.Unmarshal — kept as a wrapper so we can
// swap in a stricter decoder (DisallowUnknownFields) on a future
// audit pass without touching every call site.
func jsonUnmarshalStrict(raw []byte, out any) error {
	return json.Unmarshal(raw, out)
}

// truncate cuts s to maxLen runes, appending an ellipsis when
// truncation happened. Operates on runes (not bytes) so multi-byte
// UTF-8 in vmware-side names survives without producing an invalid
// UTF-8 cut. Same implementation as the operation / connector
// siblings — duplicated to avoid import cycle.
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
