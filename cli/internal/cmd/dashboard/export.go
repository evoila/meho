// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package dashboard

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"

	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newExportCmd returns the `meho dashboard export` command.
//
//	meho dashboard export <name> [--output FILE] [--tenant T] [--backplane <url>]
//
// Role: operator. Writes the dashboard and its sensors as one JSON file.
func newExportCmd() *cobra.Command {
	var opts exportOptions
	cmd := &cobra.Command{
		Use:   "export <name>",
		Short: "Write one dashboard and its sensors to a file",
		Long: "export calls GET /api/v1/checks/dashboard-files/export and " +
			"writes the dashboard and its sensors as one JSON file: the " +
			"operation, params, rule, cadence and severity of each sensor. " +
			"The file has no ids, no state and no mail address.\n\n" +
			"Each sensor's target name is a placeholder. `meho dashboard " +
			"import <file> --map-target <placeholder>=<target>` maps it to " +
			"a target at the other site.\n\n" +
			"Without --output the file goes to stdout. An unknown name " +
			"returns 404 dashboard_not_found.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			opts.Name = args[0]
			return runExport(cmd, opts)
		},
	}
	f := cmd.Flags()
	f.StringVarP(&opts.Output, "output", "o", "", "write the file here instead of stdout")
	f.StringVar(&opts.Tenant, "tenant", "",
		"target tenant UUID (platform_admin cross-tenant export)")
	f.BoolVar(&opts.JSONOut, "json", false, "emit errors as structured JSON")
	f.StringVar(&opts.BackplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type exportOptions struct {
	Name              string
	Output            string
	Tenant            string
	JSONOut           bool
	BackplaneOverride string
}

func runExport(cmd *cobra.Command, opts exportOptions) error {
	if opts.Name == "" {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("export requires a non-empty <name> argument"), opts.JSONOut)
	}
	tenantFilter, err := parseTenant(opts.Tenant)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := getDashboardFile(cmd.Context(), backplaneURL, opts.Name, tenantFilter)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	var pretty bytes.Buffer
	if err := json.Indent(&pretty, resp.Body, "", "  "); err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("call %s: the file is not valid JSON: %v", backplaneURL, err)),
			opts.JSONOut)
	}
	pretty.WriteByte('\n')
	if opts.Output == "" {
		_, err = cmd.OutOrStdout().Write(pretty.Bytes())
		return err
	}
	if err := os.WriteFile(opts.Output, pretty.Bytes(), 0o600); err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("write %s: %v", opts.Output, err)), opts.JSONOut)
	}
	fmt.Fprintf(cmd.ErrOrStderr(), "wrote dashboard %s to %s\n", sanitizeCell(opts.Name), opts.Output)
	return nil
}

// parseTenant turns --tenant into a UUID; an empty value means "my tenant".
func parseTenant(raw string) (*openapi_types.UUID, error) {
	if raw == "" {
		return nil, nil
	}
	var parsed openapi_types.UUID
	if err := parsed.UnmarshalText([]byte(raw)); err != nil {
		return nil, fmt.Errorf("--tenant is not a valid UUID: %v", err)
	}
	return &parsed, nil
}

// getDashboardFile calls GET /api/v1/checks/dashboard-files/export.
func getDashboardFile(
	ctx context.Context,
	backplaneURL string,
	name string,
	tenantFilter *openapi_types.UUID,
) (*api.ExportDashboardFileApiV1ChecksDashboardFilesExportGetResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	params := &api.ExportDashboardFileApiV1ChecksDashboardFilesExportGetParams{
		Name:         name,
		TenantFilter: tenantFilter,
	}
	return retryOn401(
		ctx,
		authed,
		func(ctx context.Context) (*api.ExportDashboardFileApiV1ChecksDashboardFilesExportGetResponse, error) {
			return authed.ExportDashboardFileApiV1ChecksDashboardFilesExportGetWithResponse(ctx, params)
		},
		func(r *api.ExportDashboardFileApiV1ChecksDashboardFilesExportGetResponse) int {
			return r.StatusCode()
		},
	)
}
