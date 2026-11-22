// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package targets

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newOutsideFolderCmd returns the `meho targets outside-folder` command.
//
//	meho targets outside-folder [--tenant <uuid>] [--json] [--backplane <url>]
//
// It calls GET /api/v1/targets/checks/outside-tenant-folder and lists the targets
// whose secret path is outside the tenant's folder in the secret store.
func newOutsideFolderCmd() *cobra.Command {
	var (
		tenant            string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "outside-folder",
		Short: "List targets whose secret path is outside the tenant's folder",
		Long: "outside-folder lists the targets whose secret path is outside the " +
			"tenant's folder in the secret store (Vault or OpenBao). A tenant admin " +
			"sees their own tenant. A platform admin sees every tenant, or one tenant " +
			"with --tenant. For a tenant you are a member of, use `meho tenants use` " +
			"instead, so your own role there applies. Each row shows the target name, the tenant, the secret_ref " +
			"and why it is outside. Move each secret into the folder, then update the " +
			"target's secret_ref. When the folder check is off on this install, the " +
			"command says so and lists nothing.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runOutsideFolder(cmd, outsideFolderOptions{
				Tenant:            tenant,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&tenant, "tenant", "",
		"tenant id to list (platform admin only, for a tenant you are not a member of; default: your own tenant, or every tenant for a platform admin)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit machine-readable JSON to stdout instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type outsideFolderOptions struct {
	Tenant            string
	JSONOut           bool
	BackplaneOverride string
}

func runOutsideFolder(cmd *cobra.Command, opts outsideFolderOptions) error {
	params := &api.OutsideTenantFolderApiV1TargetsChecksOutsideTenantFolderGetParams{}
	if opts.Tenant != "" {
		id, err := uuid.Parse(opts.Tenant)
		if err != nil {
			return output.RenderError(
				cmd.ErrOrStderr(),
				output.Unexpected(fmt.Sprintf("--tenant must be a tenant id (a UUID); got %q", opts.Tenant)),
				opts.JSONOut,
			)
		}
		params.TenantId = &id
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	authed, err := newAuthedClient(cmd.Context(), backplaneURL)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	rsp, err := retryHTTPOn401(cmd.Context(), authed,
		func(ctx context.Context) (*http.Response, error) {
			return authed.OutsideTenantFolderApiV1TargetsChecksOutsideTenantFolderGet(ctx, params)
		},
	)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	defer func() { _ = rsp.Body.Close() }()
	body, err := io.ReadAll(rsp.Body)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, fmt.Errorf("read response body: %w", err), opts.JSONOut)
	}
	if rsp.StatusCode != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, rsp.StatusCode, body, opts.JSONOut)
	}
	var list api.OutsideTenantFolderList
	if err := json.Unmarshal(body, &list); err != nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("call %s: decode outside-folder list: %v", backplaneURL, err)),
			opts.JSONOut,
		)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), list)
	}
	printOutsideFolderTable(cmd.OutOrStdout(), list)
	return nil
}

// printOutsideFolderTable renders the list as a table. The tenant column
// shows only when the list spans every tenant.
func printOutsideFolderTable(w io.Writer, list api.OutsideTenantFolderList) {
	if list.CheckOff {
		msg := "The folder check is off on this install"
		if list.Message != nil {
			msg = *list.Message
		}
		fmt.Fprintln(w, msg)
		return
	}
	if len(list.Items) == 0 {
		fmt.Fprintln(w, "every target secret path is inside the tenant's folder")
		return
	}
	if list.AllTenants {
		fmt.Fprintf(w, "%-30s %-36s %-40s %s\n", "NAME", "TENANT", "SECRET_REF", "WHY")
	} else {
		fmt.Fprintf(w, "%-30s %-40s %s\n", "NAME", "SECRET_REF", "WHY")
	}
	for _, item := range list.Items {
		if list.AllTenants {
			fmt.Fprintf(w, "%-30s %-36s %-40s %s\n",
				truncate(item.Name, 30), item.TenantId.String(), truncate(item.SecretRef, 40), item.Reason)
		} else {
			fmt.Fprintf(w, "%-30s %-40s %s\n",
				truncate(item.Name, 30), truncate(item.SecretRef, 40), item.Reason)
		}
	}
}
