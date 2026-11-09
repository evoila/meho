// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package settings is the `meho settings` verb tree: export, import
// with a preview, and history of the settings file (#4048). The file,
// the checks and the endpoints are in docs/codebase/settings.md; this
// package only moves the file and renders the answers.
package settings

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// responseLimit caps an answer. The largest is a preview diff of a full
// 4 MiB file, which holds before and after values.
const responseLimit = 32 << 20

// NewRootCmd returns the `meho settings` parent command.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "settings",
		Short: "Export, import and review the MEHO settings file (export / import / history)",
		Long: "The settings file holds, per tenant, the policies, the conventions " +
			"and the targets. It never holds a secret. Export it from one MEHO, " +
			"check it with `import --preview`, import it into another, and read " +
			"what changed with `history`. All three need tenant_admin or " +
			"platform_admin; an import also needs a person, not a machine login.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newExportCmd())
	cmd.AddCommand(newImportCmd())
	cmd.AddCommand(newHistoryCmd())
	return cmd
}

// HTTPError is a non-2xx answer from a settings route.
type HTTPError struct {
	Status int
	Body   []byte
}

func (e *HTTPError) Error() string {
	return fmt.Sprintf("HTTP %d: %s", e.Status, strings.TrimSpace(string(e.Body)))
}

// client builds the authed client every verb uses.
func client(ctx context.Context, backplaneURL string) (*api.AuthedClient, error) {
	authed, err := api.NewAuthedClient(ctx, backplaneURL, api.AuthedClientOptions{ResponseBodyLimit: responseLimit})
	if err != nil {
		return nil, err
	}
	if authed.AccessToken() == "" {
		return nil, errNoAccessToken
	}
	return authed, nil
}

var errNoAccessToken = errors.New("meho: stored token has no access_token")

// call runs one request, and once more after a token refresh on 401.
func call[R any](
	ctx context.Context,
	backplaneURL string,
	do func(ctx context.Context, authed *api.AuthedClient) (*R, error),
	statusOf func(*R) int,
) (*R, error) {
	authed, err := client(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	resp, err := do(ctx, authed)
	if err != nil {
		return nil, err
	}
	if statusOf(resp) != http.StatusUnauthorized {
		return resp, nil
	}
	if err := authed.Refresh(ctx); err != nil {
		return nil, err
	}
	return do(ctx, authed)
}

// Export fetches the settings file. tenantID, when not empty, narrows a
// platform_admin's export to one tenant. A non-2xx answer is an
// *HTTPError; render any error with RenderError.
func Export(ctx context.Context, backplaneURL, tenantID string) ([]byte, error) {
	params := &api.ExportApiV1SettingsExportGetParams{}
	if tenantID != "" {
		id, err := parseUUID(tenantID)
		if err != nil {
			return nil, err
		}
		params.TenantId = &id
	}
	resp, err := call(ctx, backplaneURL,
		func(ctx context.Context, authed *api.AuthedClient) (*api.ExportApiV1SettingsExportGetResponse, error) {
			return authed.ExportApiV1SettingsExportGetWithResponse(ctx, params)
		},
		func(r *api.ExportApiV1SettingsExportGetResponse) int { return r.StatusCode() },
	)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode() != http.StatusOK {
		return nil, &HTTPError{Status: resp.StatusCode(), Body: resp.Body}
	}
	return resp.Body, nil
}

// ResolveBackplane is backplane.Resolve with the error rendered.
func ResolveBackplane(cmd *cobra.Command, override string, jsonOut bool) (string, error) {
	url, err := backplane.Resolve(override)
	if err != nil {
		return "", output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	return url, nil
}

// RenderError turns an error from this package into the right exit
// code from output/format.go.
func RenderError(cmd *cobra.Command, backplaneURL string, err error, jsonOut bool) error {
	return output.RenderError(cmd.ErrOrStderr(), classify(backplaneURL, err), jsonOut)
}

func classify(backplaneURL string, err error) *output.StructuredError {
	var he *HTTPError
	var usage *usageError
	switch {
	case errors.As(err, &usage):
		return output.Usage(usage.Error())
	case errors.As(err, &he):
		return classifyHTTP(backplaneURL, he)
	case errors.Is(err, errNoAccessToken), api.IsTokenNotFound(err), api.IsNoRefreshToken(err):
		return output.AuthExpired(fmt.Sprintf(
			"no usable stored credentials for %s; run `meho login %s`", backplaneURL, backplaneURL))
	default:
		return output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, err))
	}
}

func classifyHTTP(backplaneURL string, he *HTTPError) *output.StructuredError {
	detail := detailText(he.Body)
	switch he.Status {
	case http.StatusUnauthorized:
		return output.AuthExpired(fmt.Sprintf("backplane rejected the stored token; run `meho login %s`", backplaneURL))
	case http.StatusForbidden:
		return output.InsufficientRole(detail)
	default:
		return output.Unexpected(fmt.Sprintf("call %s: HTTP %d: %s", backplaneURL, he.Status, detail))
	}
}

// detailText reads FastAPI's `detail`: a string, or the
// {"kind", "mistakes"} of a refused export, which becomes one line per
// mistake. Any other body comes back as it is.
func detailText(body []byte) string {
	var envelope struct {
		Detail json.RawMessage `json:"detail"`
	}
	if json.Unmarshal(body, &envelope) != nil || envelope.Detail == nil {
		return strings.TrimSpace(string(body))
	}
	var text string
	if json.Unmarshal(envelope.Detail, &text) == nil {
		return text
	}
	var refused struct {
		Mistakes []string `json:"mistakes"`
	}
	if json.Unmarshal(envelope.Detail, &refused) == nil && len(refused.Mistakes) > 0 {
		return "the export was refused:\n  - " + strings.Join(refused.Mistakes, "\n  - ")
	}
	return string(envelope.Detail)
}

// usageError is a wrong value on the command line.
type usageError struct{ message string }

func (e *usageError) Error() string { return e.message }
