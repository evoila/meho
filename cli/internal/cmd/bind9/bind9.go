// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package bind9 hosts the cobra commands under `meho bind9 ...` for
// G3.4-T5 (#591) of Initiative #367. The verb tree is a thin Cobra
// layer over `POST /api/v1/operations/call`, identical pattern to
// `meho vmware ...` (#511) and `meho vault ...` (#550), pre-baking
// the `connector_id="bind9-ssh-9.x"` argument so operators don't
// type the connector ID on every dispatch:
//
//   - `meho bind9 about [--target T]`              — bind9.about
//   - `meho bind9 zone list [--target T]`          — bind9.zone.list
//   - `meho bind9 zone read <zone>`                — bind9.zone.read
//   - `meho bind9 record get <fqdn> [--type T]`    — bind9.record.get
//   - `meho bind9 record add <fqdn> <ip>`          — bind9.record.add
//   - `meho bind9 record remove <fqdn>`            — bind9.record.remove
//   - `meho bind9 config show <file>`              — bind9.config.show
//   - `meho bind9 config apply-views <views> <dir>` — bind9.config.apply_views
//   - `meho bind9 config apply-file <name> <src>`  — bind9.config.apply_file
//   - `meho bind9 config backup [--tag T]`         — bind9.config.backup
//   - `meho bind9 config reload`                   — bind9.config.reload
//
// Every verb is a thin Cobra command that POSTs to
// `/api/v1/operations/call` with a pre-baked connector_id. No new
// backend code; no new HTTP routes — the CLI alias verbs are pure
// operator ergonomics over the existing dispatcher surface (per
// CLAUDE.md postulate 5: agent surface stays narrow-waist meta-tools;
// vendor-specific tooling lives only in the CLI).
//
// The verb tree replaces the consumer's `scripts/bind9-dns.sh`
// wrapper (the 2026-05-04 / 2026-05-05 credential-leak surface
// documented in evoila-bosnia/claude-rdc-hetzner-dc#86). The atomic-
// apply rollback contract — invalid `apply_*` / bad `record.add`
// leaves `/etc/bind/` byte-identical — is enforced by the backend's
// `_atomic.py` primitive (#589); the CLI exposes the result envelope
// (`op_class=write`, `result_state_before`, `result_state_after`) so
// operators can diff successful writes and audit-trace rollbacks.
package bind9

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
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/output"
)

// ConnectorID is the pre-baked connector_id every verb under
// `meho bind9 ...` dispatches against. Exported so the per-verb
// files and tests reference the same constant; a future
// re-versioning lands as a single line edit here.
//
// The id encodes the registry-v2 natural key triple
// “(product="bind9", version="9.x", impl_id="bind9-ssh")“ per the
// connector_id parser convention in
// `backend/src/meho_backplane/operations/_lookup.py::parse_connector_id`:
// the trailing `-9.x` segment is the version; everything before is
// the impl_id (`bind9-ssh`); the product is the first hyphen segment
// of the impl_id (`bind9`). The impl_id discriminator (`-ssh` vs a
// hypothetical `-rndc` / `-rest` sibling) makes room for a future
// non-SSH control surface without breaking the resolver's tie-break
// ladder — same shape vmware-rest's `vmware-rest-9.0` uses to leave
// room for a `vmware-pyvmomi` sibling.
const ConnectorID = "bind9-ssh-9.x"

// NewRootCmd returns the `meho bind9` parent command. cmd/root.go
// grafts this onto the top-level command tree alongside the other
// built-in verb trees (operation / connector / targets / kb /
// retrieval / audit / vmware / vault). The parent itself takes no
// args and prints its own help; every piece of behaviour lives in
// the per-subcommand RunE closures.
//
// Sub-tree layout follows the issue #591 verb table:
//   - `bind9 about`                   — flat verb
//   - `bind9 zone <list|read>`        — sub-tree
//   - `bind9 record <get|add|remove>` — sub-tree
//   - `bind9 config <show|apply-views|apply-file|backup|reload>` — sub-tree
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "bind9",
		Short: "Pre-scoped CLI verbs for the bind9-ssh-9.x connector",
		Long: "bind9 is the operator-facing verb tree for the bind9-ssh-9.x\n" +
			"connector (registry triple (product=\"bind9\", version=\"9.x\",\n" +
			"impl_id=\"bind9-ssh\")). Each verb dispatches through\n" +
			"POST /api/v1/operations/call with connector_id=\"bind9-ssh-9.x\"\n" +
			"pre-baked so operators don't type the connector ID on every\n" +
			"command. Write ops route through the backend's atomic-apply\n" +
			"primitive — invalid input leaves /etc/bind/ byte-identical.\n\n" +
			"Per CLAUDE.md postulate 5, these alias verbs are operator-only\n" +
			"ergonomics — they are not mirrored on the MCP surface. Agents\n" +
			"continue to use search_operations / call_operation against the\n" +
			"narrow-waist meta-tool contract.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newAboutCmd())
	cmd.AddCommand(newZoneCmd())
	cmd.AddCommand(newRecordCmd())
	cmd.AddCommand(newConfigCmd())
	return cmd
}

// errNoBackplaneConfigured wraps auth.ErrConfigNotFound so callers
// can distinguish "operator never logged in" (→ auth_expired exit
// code 2 — the right fix is `meho login`) from URL-parse failures
// (→ unexpected exit code 4 — the right fix is correcting argv).
// Same shape as the vmware / vault siblings; kept independent
// because cmd/{operation,connector,kb,vmware,vault,bind9} can't
// import each other without an import cycle (cmd/root.go grafts each
// onto the tree).
type errNoBackplaneConfigured struct{ inner error }

func (e *errNoBackplaneConfigured) Error() string {
	return "no backplane URL configured; run `meho login <url>` first or pass --backplane <url>"
}
func (e *errNoBackplaneConfigured) Unwrap() error { return e.inner }

// resolveBackplane mirrors the vmware / vault sibling helpers:
// --backplane override flag wins; otherwise read the URL the most
// recent `meho login` wrote to config.json. Missing config surfaces
// as errNoBackplaneConfigured so classifyBackplaneError can route
// it to auth_expired.
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
// output.StructuredError category. Identical routing to the vmware /
// vault siblings: missing-config → auth_expired; everything else
// (parse errors, fs errors) → unexpected.
func classifyBackplaneError(err error) *output.StructuredError {
	if errors.Is(err, auth.ErrConfigNotFound) {
		return output.AuthExpired(err.Error())
	}
	return output.Unexpected(err.Error())
}

// normaliseURL strips trailing slashes + parses the URL to fail fast
// on garbage input. Mirrors the vmware / vault siblings.
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

// renderRequestError translates an error from doAuthedRequest into
// the right output.RenderError category. Same classification ladder
// as the vmware sibling: token-not-found / no-refresh-token →
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
// vmware / vault siblings.
type httpError struct {
	StatusCode int
	Body       string
}

func (e *httpError) Error() string {
	return fmt.Sprintf("HTTP %d: %s", e.StatusCode, e.Body)
}

// doAuthedRequest issues a single HTTP request against the backplane
// with bearer injection + one-shot 401-refresh-retry. Mirrors the
// vmware / vault siblings verbatim (duplicated to avoid an import
// cycle — cmd/root.go grafts each onto the tree). Centralised
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

	// 1 MiB cap matches the vmware sibling. bind9 `zone.read` on a
	// production zone with thousands of records can be hundreds of
	// KiB; the cap leaves headroom while bounding pathological
	// payloads.
	raw, readErr := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if readErr != nil {
		return nil, fmt.Errorf("read response: %w", readErr)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, &httpError{StatusCode: resp.StatusCode, Body: strings.TrimSpace(string(raw))}
	}
	return raw, nil
}

// sendRequest builds + fires the HTTP request. Mirrors the vmware
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

// readLocalFile loads a local file path as UTF-8 bytes. Used by the
// `config apply-file` / `config apply-views` verbs to stage local
// content for the atomic-apply primitive. Returns the bytes verbatim
// — the handler's parameter_schema asserts string content but
// asyncssh's transport happily ferries bytes through the dispatcher
// when the JSON encoder re-marshals; the caller passes the string
// form.
func readLocalFile(path string) (string, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return "", fmt.Errorf("read %q: %w", path, err)
	}
	return string(raw), nil
}

// truncate cuts s to maxLen runes, appending an ellipsis when
// truncation happened. Operates on runes (not bytes) so multi-byte
// UTF-8 in zone names survives without producing an invalid UTF-8
// cut. Same implementation as the vmware / vault siblings —
// duplicated to avoid import cycle.
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

// jsonUnmarshalStrict is a thin wrapper over json.Unmarshal that
// keeps the call sites readable when the shape is unsigned. Same
// semantics as json.Unmarshal — kept as a wrapper so we can swap in
// a stricter decoder (DisallowUnknownFields) on a future audit pass
// without touching every call site. Mirrors the vmware sibling.
func jsonUnmarshalStrict(raw []byte, out any) error {
	return json.Unmarshal(raw, out)
}
