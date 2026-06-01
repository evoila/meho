// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package keycloak hosts the cobra commands under `meho keycloak ...`
// for G3.13-T3 (#1395) of Initiative #1388. The verb tree is a thin
// Cobra layer over `POST /api/v1/operations/call`, identical pattern to
// `meho pfsense ...` (#850) and `meho gcloud ...` (#851), pre-baking the
// `connector_id="keycloak-admin-26.x"` argument so operators don't type
// the connector ID on every dispatch:
//
//   - `meho keycloak realm get [--target T]`         — keycloak.realm.get
//   - `meho keycloak client list [--client-id C] [--max N] [--target T]` — keycloak.client.list
//   - `meho keycloak client get --id UUID [--target T]` — keycloak.client.get
//   - `meho keycloak client-scope list [--target T]`  — keycloak.client_scope.list
//   - `meho keycloak user list [--username U] [--max N] [--target T]` — keycloak.user.list
//   - `meho keycloak role-mapping get --id UUID [--target T]` — keycloak.role_mapping.get
//
// Every verb is a thin Cobra command that POSTs to
// `/api/v1/operations/call` with a pre-baked connector_id. No new
// backend code; no new HTTP routes — the CLI alias verbs are pure
// operator ergonomics over the existing dispatcher surface (per
// CLAUDE.md postulate 5: agent surface stays narrow-waist meta-tools;
// vendor-specific tooling lives only in the CLI).
//
// The `meho keycloak` surface is operator-only — the MCP / agent
// surface continues to reach Keycloak ops via the narrow-waist
// search_operations + call_operation meta-tools. The connector
// authenticates to the Keycloak Admin REST API with a separate Vault-
// sourced admin credential (the admin-vs-operator split documented in
// docs/cross-repo/keycloak-onboarding.md); the dispatch path the CLI
// rides over is the same admin-token Bearer path the agent surface uses.
//
// This is distinct from the `meho admin keycloak ...` deployer-onramp
// subtree (#791), which bootstraps Keycloak clients during initial
// deployment; this tree reads the managed realm's live configuration
// through the registered `keycloak-admin-26.x` connector.
package keycloak

import (
	"errors"
	"fmt"
	"net/url"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/dispatch"
	"github.com/evoila/meho/cli/internal/output"
)

// ConnectorID is the pre-baked connector_id every verb under
// `meho keycloak ...` dispatches against. Exported so the per-verb
// files and tests reference the same constant; a future re-versioning
// (keycloak-admin-27.x) lands as a single line edit here.
//
// The id encodes the registry-v2 natural key triple
// "(product="keycloak", version="26.x", impl_id="keycloak-admin")" per
// the connector_id parser convention in
// `backend/src/meho_backplane/operations/_lookup.py::parse_connector_id`:
// the trailing `-26.x` segment is the version; everything before is the
// impl_id (`keycloak-admin`); the product is the first hyphen segment of
// the impl_id (`keycloak`).
const ConnectorID = "keycloak-admin-26.x"

// NewRootCmd returns the `meho keycloak` parent command. cmd/root.go
// grafts this onto the top-level command tree alongside the other
// built-in verb trees. The parent itself takes no args and prints its
// own help; every piece of behaviour lives in the per-subcommand RunE
// closures.
//
// Sub-tree layout follows the issue #1395 verb table:
//   - `keycloak realm get`           — flat sub-tree
//   - `keycloak client <list|get>`   — sub-tree
//   - `keycloak client-scope list`   — sub-tree
//   - `keycloak user list`           — sub-tree
//   - `keycloak role-mapping get`    — sub-tree
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "keycloak",
		Short: "Pre-scoped CLI verbs for the keycloak-admin-26.x connector",
		Long: "keycloak is the operator-facing verb tree for the\n" +
			"keycloak-admin-26.x connector (registry triple\n" +
			"(product=\"keycloak\", version=\"26.x\", impl_id=\"keycloak-admin\")).\n" +
			"Each verb dispatches through POST /api/v1/operations/call with\n" +
			"connector_id=\"keycloak-admin-26.x\" pre-baked so operators don't\n" +
			"type the connector ID on every command. All shipped ops are\n" +
			"read-only; the write surface is the deferred approval-gated T4\n" +
			"follow-up (#1406).\n\n" +
			"Per CLAUDE.md postulate 5, these alias verbs are operator-only\n" +
			"ergonomics — they are not mirrored on the MCP surface. Agents\n" +
			"continue to use search_operations / call_operation against the\n" +
			"narrow-waist meta-tool contract.\n\n" +
			"Auth note: the connector authenticates to the Keycloak Admin REST\n" +
			"API with a Vault-sourced admin credential, NOT the operator's\n" +
			"OIDC token (the admin-vs-operator split — see\n" +
			"docs/cross-repo/keycloak-onboarding.md). The operator's session\n" +
			"only authorises the Vault read that backs the admin-token mint.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newRealmCmd())
	cmd.AddCommand(newClientCmd())
	cmd.AddCommand(newClientScopeCmd())
	cmd.AddCommand(newUserCmd())
	cmd.AddCommand(newRoleMappingCmd())
	return cmd
}

// errNoBackplaneConfigured wraps auth.ErrConfigNotFound so callers can
// distinguish "operator never logged in" (→ auth_expired exit code 2 —
// the right fix is `meho login`) from URL-parse failures (→ unexpected
// exit code 4 — the right fix is correcting argv).
type errNoBackplaneConfigured struct{ inner error }

func (e *errNoBackplaneConfigured) Error() string {
	return "no backplane URL configured; run `meho login <url>` first or pass --backplane <url>"
}
func (e *errNoBackplaneConfigured) Unwrap() error { return e.inner }

// resolveBackplane mirrors the pfsense / gcloud sibling helpers:
// --backplane override flag wins; otherwise read the URL the most
// recent `meho login` wrote to config.json.
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
// output.StructuredError category. Identical routing to the pfsense /
// gcloud siblings: missing-config → auth_expired; everything else →
// unexpected.
func classifyBackplaneError(err error) *output.StructuredError {
	if errors.Is(err, auth.ErrConfigNotFound) {
		return output.AuthExpired(err.Error())
	}
	return output.Unexpected(err.Error())
}

// normaliseURL strips trailing slashes + parses the URL to fail fast on
// garbage input. Mirrors the pfsense / gcloud siblings.
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

// renderRequestError translates an error from the dispatch core into
// the right output.RenderError category. Same classification ladder as
// the pfsense / gcloud siblings: token-not-found / no-refresh-token →
// auth_expired; HTTP-error → unexpected; transport → unreachable.
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

// truncate cuts s to maxLen runes, appending an ellipsis when
// truncation happened. Operates on runes (not bytes) so multi-byte
// UTF-8 survives without producing an invalid UTF-8 cut. Same
// implementation as the pfsense / gcloud siblings.
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

// stringField pulls a string field from a row entry, returning empty
// string when the field is missing or wrong type.
func stringField(e map[string]any, key string) string {
	v, ok := e[key]
	if !ok {
		return ""
	}
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}

// boolField pulls a bool field from a row entry, defaulting to false
// when the field is missing or wrong type.
func boolField(e map[string]any, key string) bool {
	if v, ok := e[key].(bool); ok {
		return v
	}
	return false
}
