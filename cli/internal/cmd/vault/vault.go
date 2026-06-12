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
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/dispatch"
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

// renderRequestError translates an error from conn.Call into the
// right output.RenderError category. Same classification ladder as
// the vmware sibling: token-not-found / no-refresh-token →
// auth_expired with `meho login` hints; non-2xx HTTP →
// unexpected_response; everything else (transport) → unreachable.
//
// Matches *dispatch.APIResponseError instead of a local httpError
// sentinel after G0.12-T16 #1274 promoted the authed transport into
// the shared dispatch package.
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
	var apiErr *dispatch.APIResponseError
	if errors.As(err, &apiErr) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("call %s: HTTP %d: %s",
				backplaneURL, apiErr.StatusCode, apiErr.Body)),
			jsonOut,
		)
	}
	return output.RenderError(cmd.ErrOrStderr(),
		output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, err)),
		jsonOut,
	)
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
