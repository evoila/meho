// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package dashboard

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"

	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// maxDashboardFileBytes bounds the file `meho dashboard import` reads. A file
// holds at most 200 sensors with small settings, so 8 MiB is far above any
// real file.
const maxDashboardFileBytes int64 = 8 << 20

// newImportCmd returns the `meho dashboard import` command.
//
//	meho dashboard import <file> [--map-target P=T ...] [--dry-run] [--force]
//	  [--tenant T] [--json] [--backplane <url>]
//
// Role: tenant_admin. Creates or updates the dashboard and its sensors.
func newImportCmd() *cobra.Command {
	var opts importOptions
	cmd := &cobra.Command{
		Use:   "import <file>",
		Short: "Create or update a dashboard and its sensors from a file (tenant_admin)",
		Long: "import reads a file that `meho dashboard export` wrote and calls " +
			"POST /api/v1/checks/dashboard-files/import. Tenant_admin only.\n\n" +
			"Every target placeholder in the file needs --map-target " +
			"<placeholder>=<target>, with a target of this site. Repeat the " +
			"flag for each placeholder.\n\n" +
			"MEHO checks the whole file first. It refuses the file, and " +
			"changes nothing, when a placeholder is not mapped, a target is " +
			"not found, an operation is unknown here or is not a safe read, " +
			"or a connector does not fit its target (--force imports it " +
			"anyway). The error lists every problem.\n\n" +
			"Sensors and the dashboard are matched by name: a missing one is " +
			"created, a different one is updated, an equal one stays. The " +
			"dashboard's members become the sensors of the file; a member " +
			"that is not in the file leaves the dashboard but is not deleted. " +
			"So importing the same file twice changes nothing the second " +
			"time. The mail address of the dashboard is not in the file and " +
			"stays as it is.\n\n" +
			"--dry-run shows what the import would change and writes nothing. " +
			"Each import writes an audit row.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			opts.Path = args[0]
			return runImport(cmd, opts)
		},
	}
	f := cmd.Flags()
	f.StringArrayVar(&opts.MapTargets, "map-target", nil,
		"map a placeholder to a target of this site: <placeholder>=<target> (repeatable)")
	f.BoolVar(&opts.DryRun, "dry-run", false, "show what would change and write nothing")
	f.BoolVar(&opts.Force, "force", false,
		"import a sensor even when its connector does not fit the mapped target")
	f.StringVar(&opts.Tenant, "tenant", "",
		"target tenant UUID (platform_admin cross-tenant import)")
	f.BoolVar(&opts.JSONOut, "json", false, "emit the raw import result JSON instead of the summary")
	f.StringVar(&opts.BackplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type importOptions struct {
	Path              string
	MapTargets        []string
	DryRun            bool
	Force             bool
	Tenant            string
	JSONOut           bool
	BackplaneOverride string
}

// importBody is the request body. The file is sent as it was read, so no
// field of it is lost on the way.
type importBody struct {
	File      json.RawMessage     `json:"file"`
	TargetMap map[string]string   `json:"target_map"`
	DryRun    bool                `json:"dry_run"`
	Force     bool                `json:"force"`
	TenantID  *openapi_types.UUID `json:"tenant_id,omitempty"`
}

// parseTargetMap turns repeated --map-target P=T values into a map.
func parseTargetMap(raw []string) (map[string]string, error) {
	out := make(map[string]string, len(raw))
	for _, entry := range raw {
		placeholder, target, ok := strings.Cut(entry, "=")
		placeholder, target = strings.TrimSpace(placeholder), strings.TrimSpace(target)
		if !ok || placeholder == "" || target == "" {
			return nil, fmt.Errorf("--map-target %q must look like <placeholder>=<target>", entry)
		}
		if previous, seen := out[placeholder]; seen && previous != target {
			return nil, fmt.Errorf("--map-target gives two targets for %q: %q and %q",
				placeholder, previous, target)
		}
		out[placeholder] = target
	}
	return out, nil
}

// readDashboardFile reads the file and checks that it is one JSON object.
func readDashboardFile(path string) (json.RawMessage, error) {
	handle, err := os.Open(path)
	if err != nil {
		return nil, fmt.Errorf("read %s: %v", path, err)
	}
	defer handle.Close()
	data, err := io.ReadAll(io.LimitReader(handle, maxDashboardFileBytes+1))
	if err != nil {
		return nil, fmt.Errorf("read %s: %v", path, err)
	}
	if int64(len(data)) > maxDashboardFileBytes {
		return nil, fmt.Errorf("%s is larger than %d bytes", path, maxDashboardFileBytes)
	}
	var probe map[string]json.RawMessage
	if err := json.Unmarshal(data, &probe); err != nil {
		return nil, fmt.Errorf("%s is not a dashboard file (a JSON object): %v", path, err)
	}
	return json.RawMessage(data), nil
}

func runImport(cmd *cobra.Command, opts importOptions) error {
	targetMap, err := parseTargetMap(opts.MapTargets)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}
	file, err := readDashboardFile(opts.Path)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}
	tenantID, err := parseTenant(opts.Tenant)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}
	payload, err := json.Marshal(importBody{
		File: file, TargetMap: targetMap, DryRun: opts.DryRun, Force: opts.Force, TenantID: tenantID,
	})
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := postDashboardFile(cmd.Context(), backplaneURL, payload)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() == http.StatusUnprocessableEntity {
		if problems := importProblems(resp.Body); len(problems) > 0 {
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected("the file was not imported:\n  - "+strings.Join(problems, "\n  - ")),
				opts.JSONOut)
		}
	}
	if resp.StatusCode() != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	if resp.JSON200 == nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("call %s: HTTP 200 without an import result", backplaneURL)),
			opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
	}
	printImportResult(cmd.OutOrStdout(), resp.JSON200)
	output.WriteAuditReceipt(cmd.OutOrStdout(), output.AuditIDFromResponse(resp.HTTPResponse))
	return nil
}

// importProblems reads the list of problems out of a 422 body: one line per
// entry, as the server wrote it. Empty when the body is not that list.
func importProblems(body []byte) []string {
	var env struct {
		Detail []struct {
			Msg string `json:"msg"`
		} `json:"detail"`
	}
	if err := json.Unmarshal(body, &env); err != nil {
		return nil
	}
	out := make([]string, 0, len(env.Detail))
	for _, entry := range env.Detail {
		if entry.Msg != "" {
			out = append(out, sanitizeCell(entry.Msg))
		}
	}
	return out
}

func joinNames(names *[]string) string {
	if names == nil || len(*names) == 0 {
		return ""
	}
	clean := make([]string, len(*names))
	for i, name := range *names {
		clean[i] = sanitizeCell(name)
	}
	return strings.Join(clean, ", ")
}

// printImportResult renders the import result as plain lines.
func printImportResult(w io.Writer, r *api.DashboardImportResult) {
	if r.DryRun {
		fmt.Fprintln(w, "dry run: nothing was written")
	}
	line := fmt.Sprintf("dashboard %s: %s", sanitizeCell(r.DashboardName), r.DashboardAction)
	if fields := joinNames(r.DashboardChangedFields); fields != "" {
		line += " (" + fields + ")"
	}
	if r.DashboardId != nil {
		line += " id=" + r.DashboardId.String()
	}
	fmt.Fprintln(w, line)
	for _, s := range r.Sensors {
		line := fmt.Sprintf("sensor %s: %s", sanitizeCell(s.Name), s.Action)
		if fields := joinNames(s.ChangedFields); fields != "" {
			line += " (" + fields + ")"
		}
		fmt.Fprintln(w, line)
	}
	if added := joinNames(r.MembersAdded); added != "" {
		fmt.Fprintf(w, "members added: %s\n", added)
	}
	if removed := joinNames(r.MembersRemoved); removed != "" {
		fmt.Fprintf(w, "members removed (not deleted): %s\n", removed)
	}
	if !r.Changed {
		fmt.Fprintln(w, "nothing changed")
	}
}

// postDashboardFile calls POST /api/v1/checks/dashboard-files/import.
func postDashboardFile(
	ctx context.Context,
	backplaneURL string,
	payload []byte,
) (*api.ImportDashboardFileApiV1ChecksDashboardFilesImportPostResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	params := &api.ImportDashboardFileApiV1ChecksDashboardFilesImportPostParams{}
	return retryOn401(
		ctx,
		authed,
		func(ctx context.Context) (*api.ImportDashboardFileApiV1ChecksDashboardFilesImportPostResponse, error) {
			// A new reader for each try: a retry after a 401 sends the body again.
			return authed.ImportDashboardFileApiV1ChecksDashboardFilesImportPostWithBodyWithResponse(
				ctx, params, "application/json", bytes.NewReader(payload))
		},
		func(r *api.ImportDashboardFileApiV1ChecksDashboardFilesImportPostResponse) int {
			return r.StatusCode()
		},
	)
}
