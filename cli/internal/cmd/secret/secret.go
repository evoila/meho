// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package secret hosts the cobra commands under `meho secret ...` for
// G0.22-T4 (#1580) of Initiative #581 (the secret broker). The verb
// tree is a thin Cobra layer over `POST /api/v1/operations/call`,
// pre-baking the synthetic broker connector_id so operators don't type
// it on every dispatch:
//
//   - `meho secret move --from <kind>:<ref> --to <kind>:<ref> --reason R`
//     → secret.move
//
// The move is references-not-values: the operator names a `--from` and a
// `--to` `<kind>:<ref>` reference and a `--reason`; the backplane reads
// the credential, transfers it, and re-writes it entirely server-side.
// The secret value is never a CLI argument, flag, env var, or prompt —
// it never lands in argv, shell history, ps output, or the op params.
// The response carries only the move status, the value's SHA-256, and
// its byte length, which is all this verb renders.
//
// `secret.move` is change-class (requires_approval=True +
// safety_level="dangerous"), so a dispatch parks at
// status=awaiting_approval until a human approves through the queue
// (G11.7-T1 #1401); the verb surfaces that status verbatim rather than
// treating it as an error (see move.go's renderMoveResult).
//
// Per CLAUDE.md postulate 5 this alias verb is operator-only — it is not
// mirrored on the MCP surface. Agents reach `secret.move` through the
// narrow-waist search_operations / call_operation meta-tools.
package secret

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

// ConnectorID is the pre-baked connector_id `meho secret move` dispatches
// against. It encodes the synthetic broker's registry-v2 natural key
// triple `(product="secret", version="1.x", impl_id="secret-broker")` per
// the connector_id parser convention in
// `backend/src/meho_backplane/operations/_lookup.py::parse_connector_id`:
// the trailing `-1.x` segment is the version (digit-led, as the parser
// requires); everything before is the impl_id (`secret-broker`); the
// product is the first hyphen segment of the impl_id (`secret`). T1
// (#1577) registers this exact triple, so `secret-broker-1.x`
// round-trips through parse_connector_id back to
// `("secret", "1.x", "secret-broker")`.
const ConnectorID = "secret-broker-1.x"

// conn binds this package's pre-baked connector_id to the shared
// dispatch core (cli/internal/dispatch), mirroring the keycloak / vault
// verb trees.
var conn = dispatch.New(ConnectorID)

// NewRootCmd returns the `meho secret` parent command. cmd/root.go grafts
// this onto the top-level command tree alongside the other built-in verb
// trees. The parent itself takes no args and prints its own help; the
// behaviour lives in the `move` sub-command's RunE closure.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "secret",
		Short: "Secret-broker verbs for the secret-broker-1.x connector",
		Long: "secret is the operator-facing verb tree for the synthetic\n" +
			"secret-broker-1.x connector (registry triple\n" +
			"(product=\"secret\", version=\"1.x\", impl_id=\"secret-broker\")).\n" +
			"It dispatches through POST /api/v1/operations/call with\n" +
			"connector_id=\"secret-broker-1.x\" pre-baked.\n\n" +
			"The single verb today is `move`, which copies a credential from\n" +
			"one store to another server-side. The move is\n" +
			"references-not-values: you name a --from and --to '<kind>:<ref>'\n" +
			"reference and a --reason; the backplane reads, transfers, and\n" +
			"re-writes the material. The secret value is NEVER passed on the\n" +
			"command line, so it never lands in shell history, ps output, or\n" +
			"the op params; the response returns only the move status, the\n" +
			"value's SHA-256, and its byte length.\n\n" +
			"move is change-class (it requires approval): an unapproved\n" +
			"dispatch parks at status=awaiting_approval until a human approves\n" +
			"through the approval queue.\n\n" +
			"Per CLAUDE.md postulate 5 these alias verbs are operator-only —\n" +
			"agents reach secret.move via search_operations / call_operation.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newMoveCmd())
	return cmd
}

// errNoBackplaneConfigured wraps auth.ErrConfigNotFound so callers can
// distinguish "operator never logged in" (→ auth_expired exit code 2 —
// the right fix is `meho login`) from URL-parse failures (→ unexpected
// exit code 4 — the right fix is correcting argv). Mirrors the keycloak /
// vault sibling helper (the cmd/* packages can't import one another
// without an import cycle, so the helper is duplicated per dir).
type errNoBackplaneConfigured struct{ inner error }

func (e *errNoBackplaneConfigured) Error() string {
	return "no backplane URL configured; run `meho login <url>` first or pass --backplane <url>"
}
func (e *errNoBackplaneConfigured) Unwrap() error { return e.inner }

// resolveBackplane mirrors the keycloak / vault sibling helpers:
// --backplane override flag wins; otherwise read the URL the most recent
// `meho login` wrote to config.json.
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
// output.StructuredError category: missing-config → auth_expired;
// everything else → unexpected.
func classifyBackplaneError(err error) *output.StructuredError {
	if errors.Is(err, auth.ErrConfigNotFound) {
		return output.AuthExpired(err.Error())
	}
	return output.Unexpected(err.Error())
}

// normaliseURL strips trailing slashes + parses the URL to fail fast on
// garbage input. Mirrors the keycloak / vault siblings.
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

// renderRequestError translates an error from the dispatch core into the
// right output.RenderError category: token-not-found / no-refresh-token →
// auth_expired; HTTP-error → unexpected; transport → unreachable. Same
// classification ladder as the keycloak / vault siblings.
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
