// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package gcloud hosts the cobra commands under `meho gcloud ...` for
// G3.7-T6 (#851) of Initiative #370. The verb tree is a thin Cobra
// layer over `POST /api/v1/operations/call`, pre-baking
// `connector_id="gcloud-rest-1.0"` so operators don't type the
// connector ID on every dispatch:
//
//   - `meho gcloud about [--target T]`                  — gcloud.about
//   - `meho gcloud project describe [--target T]`       — gcloud.project.describe
//   - `meho gcloud services list [--target T]`          — gcloud.services.list
//   - `meho gcloud iam sa list [--target T]`            — gcloud.iam.service_accounts.list
//   - `meho gcloud iam policy read [--target T]`        — gcloud.iam.policy.read
//   - `meho gcloud compute instances list [--target T]` — gcloud.compute.instances.list
//   - `meho gcloud compute networks list [--target T]`  — gcloud.compute.networks.list
//   - `meho gcloud compute subnets list [--target T]`   — gcloud.compute.subnetworks.list
//
// Every verb is a thin Cobra command that POSTs to
// `/api/v1/operations/call` with a pre-baked connector_id. No new
// backend code; no new HTTP routes — the CLI alias verbs are pure
// operator ergonomics over the existing dispatcher surface (per
// CLAUDE.md postulate 5: agent surface stays narrow-waist meta-tools;
// vendor-specific tooling lives only in the CLI).
//
// Auth model: GCP Application Default Credentials + Service Account
// Impersonation. The connector refuses SA JSON key material in
// `secret_ref` — org policy `constraints/iam.disableServiceAccountKeyCreation`
// is in force. The CLI is auth-agnostic (it POSTs to the backplane);
// the backend connector enforces the key refusal.
//
// The verb tree replaces `scripts/gcloud.sh` for the read-only
// workflows the operator runs daily (identity check, service audit,
// IAM review, VM inventory).
package gcloud

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
// `meho gcloud ...` dispatches against. Exported so the per-verb
// files and tests reference the same constant; a future re-versioning
// lands as a single-line edit here.
//
// The id encodes the registry-v2 natural key triple
// `(product="gcloud", version="1.0", impl_id="gcloud-rest")` per the
// connector_id parser convention in
// `backend/src/meho_backplane/operations/_lookup.py::parse_connector_id`.
const ConnectorID = "gcloud-rest-1.0"

// NewRootCmd returns the `meho gcloud` parent command. cmd/root.go
// grafts this onto the top-level command tree. The parent itself takes
// no args and prints its own help; every piece of behaviour lives in
// the per-subcommand RunE closures.
//
// Sub-tree layout follows the gcloud op groupings (Initiative #370):
//
//	gcloud about                             — identity / health check
//	gcloud project <describe>                — CRM project resource
//	gcloud services <list>                   — enabled APIs audit
//	gcloud iam <sa list | policy read>       — IAM inventory
//	gcloud compute <instances|networks|subnets> <list> — Compute inventory
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "gcloud",
		Short: "Pre-scoped CLI verbs for the gcloud-rest-1.0 connector",
		Long: "gcloud is the operator-facing verb tree for the gcloud-rest-1.0\n" +
			"connector (registry triple (product=\"gcloud\", version=\"1.0\",\n" +
			"impl_id=\"gcloud-rest\")). Each verb dispatches through\n" +
			"POST /api/v1/operations/call with connector_id=\"gcloud-rest-1.0\"\n" +
			"pre-baked so operators don't type the connector ID on every\n" +
			"command. Auth uses GCP Application Default Credentials + Service\n" +
			"Account Impersonation — SA JSON key material is refused by the\n" +
			"backend (org policy disableServiceAccountKeyCreation).\n\n" +
			"Per CLAUDE.md postulate 5, these alias verbs are operator-only\n" +
			"ergonomics — they are not mirrored on the MCP surface. Agents\n" +
			"continue to use search_operations / call_operation against the\n" +
			"narrow-waist meta-tool contract.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newAboutCmd())
	cmd.AddCommand(newProjectCmd())
	cmd.AddCommand(newServicesCmd())
	cmd.AddCommand(newIamCmd())
	cmd.AddCommand(newComputeCmd())
	return cmd
}

// errNoBackplaneConfigured wraps auth.ErrConfigNotFound so callers can
// distinguish "operator never logged in" from URL-parse failures.
// Same shape as the bind9 / k8s siblings; kept independent because
// cmd packages can't import each other without an import cycle
// (cmd/root.go grafts each onto the tree).
type errNoBackplaneConfigured struct{ inner error }

func (e *errNoBackplaneConfigured) Error() string {
	return "no backplane URL configured; run `meho login <url>` first or pass --backplane <url>"
}
func (e *errNoBackplaneConfigured) Unwrap() error { return e.inner }

// resolveBackplane mirrors the bind9 / k8s sibling helpers:
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
// output.StructuredError category. Mirrors the bind9 / k8s siblings.
func classifyBackplaneError(err error) *output.StructuredError {
	if errors.Is(err, auth.ErrConfigNotFound) {
		return output.AuthExpired(err.Error())
	}
	return output.Unexpected(err.Error())
}

// normaliseURL strips trailing slashes + parses the URL to fail fast
// on garbage input. Mirrors the bind9 / k8s siblings.
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

// renderRequestError translates a doAuthedRequest error into the right
// output.RenderError category. Same classification ladder as the
// bind9 / k8s siblings.
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
// UTF-8 in GCP resource names survives. Same implementation as the
// bind9 / k8s siblings.
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
