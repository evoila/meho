// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package runnerprincipal

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newListCmd returns the `meho runner-principal list` command.
func newListCmd() *cobra.Command {
	var (
		includeRevoked    bool
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List runner principals in your tenant",
		Long: "list calls GET /api/v1/runner-principals and renders the " +
			"runner principals registered in the operator's tenant, name-sorted. " +
			"Revoked principals are excluded by default; pass --include-revoked " +
			"to show them too. Role: operator.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runList(cmd, listOptions{
				IncludeRevoked:    includeRevoked,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().BoolVar(&includeRevoked, "include-revoked", false,
		"include revoked principals in the listing (default false)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw ListResponse JSON instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type listOptions struct {
	IncludeRevoked    bool
	JSONOut           bool
	BackplaneOverride string
}

func runList(cmd *cobra.Command, opts listOptions) error {
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := getList(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
	}
	printListTable(cmd.OutOrStdout(), resp.JSON200)
	return nil
}

// listQueryParams maps the CLI flags onto the generated query-param shape.
// include_revoked is omitted from the wire when the operator didn't ask
// for it so the backplane's own default (excluding revoked rows) applies.
func listQueryParams(opts listOptions) *api.ListRunnerPrincipalsApiV1RunnerPrincipalsGetParams {
	params := &api.ListRunnerPrincipalsApiV1RunnerPrincipalsGetParams{}
	if opts.IncludeRevoked {
		flag := true
		params.IncludeRevoked = &flag
	}
	return params
}

func getList(
	ctx context.Context,
	backplaneURL string,
	opts listOptions,
) (*api.ListRunnerPrincipalsApiV1RunnerPrincipalsGetResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	params := listQueryParams(opts)
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.ListRunnerPrincipalsApiV1RunnerPrincipalsGetResponse, error) {
			return authed.ListRunnerPrincipalsApiV1RunnerPrincipalsGetWithResponse(ctx, params)
		},
		func(r *api.ListRunnerPrincipalsApiV1RunnerPrincipalsGetResponse) int { return r.StatusCode() },
	)
}

func printListTable(w io.Writer, r *api.RunnerPrincipalListResponse) {
	if r == nil || len(r.Principals) == 0 {
		fmt.Fprintln(w, "no runner principals registered in this tenant")
		return
	}
	withSetAside := hasSetAside(r.Principals)
	header := fmt.Sprintf("%-32s %-8s %-20s %-6s %-19s", "NAME", "REVOKED", "OWNER", "WIRE", "PACKAGES")
	if withSetAside {
		header += fmt.Sprintf(" %-9s %-20s", "SET ASIDE", "LAST SET ASIDE")
	}
	fmt.Fprintf(w, "%s %s\n", header, "KEYCLOAK CLIENT ID")
	for _, e := range r.Principals {
		row := fmt.Sprintf("%-32s %-8v %-20s %-6s %-19s", e.Name, e.Revoked, e.OwnerSub,
			wireVersionText(e.WireVersion), packageSetText(e.PackageSetDigest))
		if withSetAside {
			row += fmt.Sprintf(" %-9s %-20s", setAsideCountText(e.SetAsideCount), setAsideTimeText(e.SetAsideLastAt))
		}
		fmt.Fprintf(w, "%s %s\n", row, e.KeycloakClientId)
	}
}

// hasSetAside says whether the answer holds the set-aside counts. The server
// sends them only with RUNNER_CHECKS_ENABLED on; without them the table has
// no such columns.
func hasSetAside(principals []api.RunnerPrincipalRead) bool {
	for _, e := range principals {
		if e.SetAsideCount != nil {
			return true
		}
	}
	return false
}

// setAsideCountText renders how many result batches the runner set aside.
func setAsideCountText(v *int) string {
	if v == nil {
		return "-"
	}
	return strconv.Itoa(*v)
}

// setAsideTimeText renders when the runner set the newest batch aside, or "-"
// when it set none aside.
func setAsideTimeText(v *time.Time) string {
	if v == nil {
		return "-"
	}
	return v.UTC().Format("2006-01-02T15:04:05Z")
}

// packageSetText renders the start of the connector package set digest the
// runner sent last ("sha256:" and 12 hex characters), or "-" when it has not
// called since the server started to record it. `-o json` shows the full
// digest.
func packageSetText(v *string) string {
	const shown = len("sha256:") + 12
	if v == nil || *v == "" {
		return "-"
	}
	if len(*v) > shown {
		return (*v)[:shown]
	}
	return *v
}

// wireVersionText renders the wire version the runner sent last, or "-"
// when it has not called since the server started to record it.
func wireVersionText(v *string) string {
	if v == nil || *v == "" {
		return "-"
	}
	return *v
}
