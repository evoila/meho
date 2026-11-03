// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package connector

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// newTenantSettingCmd returns the `meho connector tenant-setting` group.
//
// A built-in operation's on/off flag is the install default, shared by
// every tenant. A tenant setting turns a built-in operation on or off for
// one tenant only (evoila/meho#5029):
//
//	meho connector tenant-setting show <connector_id> [--tenant-id <uuid>]
//	meho connector tenant-setting set <connector_id> <op_id> --on|--off|--default [--tenant-id <uuid>]
//	meho connector tenant-setting set-group <connector_id> <group_key> --on|--off|--default [--tenant-id <uuid>]
func newTenantSettingCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "tenant-setting",
		Short: "Turn built-in operations on or off for one tenant only",
		Long: "A built-in operation has one on/off flag for the whole install: the\n" +
			"install default. A tenant setting turns a built-in operation on or off\n" +
			"for one tenant only. Other tenants keep the install default.\n\n" +
			"  show       list the operations this tenant turned on or off,\n" +
			"             with the install default next to each one.\n" +
			"  set        turn one operation on (--on), off (--off), or back to\n" +
			"             the install default (--default).\n" +
			"  set-group  the same for every operation in one group.\n\n" +
			"`meho connector review` also shows the tenant setting of every\n" +
			"operation (the `ten` column).\n\n" +
			"--tenant-id acts on another tenant; that needs platform_admin.\n" +
			"A connector the tenant imported for itself has its own on/off flags;\n" +
			"use `meho connector edit-op` for those.",
	}
	cmd.AddCommand(newTenantSettingShowCmd())
	cmd.AddCommand(newTenantSettingSetCmd(false))
	cmd.AddCommand(newTenantSettingSetCmd(true))
	return cmd
}

type tenantSettingFlags struct {
	tenantID          string
	jsonOut           bool
	backplaneOverride string
}

func (f *tenantSettingFlags) register(cmd *cobra.Command) {
	cmd.Flags().StringVar(&f.tenantID, "tenant-id", "",
		"act on this tenant instead of your own (needs platform_admin)")
	cmd.Flags().BoolVar(&f.jsonOut, "json", false,
		"emit machine-readable JSON to stdout instead of the human summary")
	cmd.Flags().StringVar(&f.backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
}

// parseTenantID returns nil for an empty flag, else the parsed UUID.
func (f *tenantSettingFlags) parseTenantID() (*uuid.UUID, error) {
	if f.tenantID == "" {
		return nil, nil
	}
	id, err := uuid.Parse(f.tenantID)
	if err != nil {
		return nil, fmt.Errorf("--tenant-id %q is not a UUID", f.tenantID)
	}
	return &id, nil
}

func newTenantSettingShowCmd() *cobra.Command {
	var flags tenantSettingFlags
	cmd := &cobra.Command{
		Use:   "show <connector_id>",
		Short: "List the operations this tenant turned on or off, with the install default",
		Long: "show calls GET /api/v1/connectors/<connector_id>/tenant-settings.\n\n" +
			"It lists every built-in operation the tenant has its own setting for.\n" +
			"Each line shows the install default, the tenant setting and the\n" +
			"value that applies. Operations not listed use the install default.\n\n" +
			"Role: operator.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runTenantSettingShow(cmd, args[0], flags)
		},
	}
	flags.register(cmd)
	return cmd
}

func newTenantSettingSetCmd(group bool) *cobra.Command {
	var (
		flags   tenantSettingFlags
		on      bool
		off     bool
		useDflt bool
	)
	verb, argsUse, short, target, route := "set", "<connector_id> <op_id>",
		"Turn one built-in operation on or off for this tenant",
		"the operation",
		"PUT /api/v1/connectors/<connector_id>/tenant-settings/operations/<op_id>"
	if group {
		verb, argsUse, short, target, route = "set-group", "<connector_id> <group_key>",
			"Turn every built-in operation in one group on or off for this tenant",
			"every operation in the group",
			"PUT /api/v1/connectors/<connector_id>/tenant-settings/groups/<group_key>"
	}
	cmd := &cobra.Command{
		Use:   verb + " " + argsUse,
		Short: short,
		Long: fmt.Sprintf("%s calls %s.\n\n", verb, route) +
			fmt.Sprintf("--on turns %s on for this tenant, --off turns it off,\n", target) +
			"--default removes the tenant setting so the install default applies\n" +
			"again. Other tenants are not affected. Each change writes an audit row.\n\n" +
			"Role: tenant_admin (platform_admin with --tenant-id for another tenant).",
		Args:          cobra.ExactArgs(2),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			if !on && !off && !useDflt {
				return output.RenderError(cmd.ErrOrStderr(),
					output.Unexpected("pass one of --on, --off or --default"), flags.jsonOut)
			}
			var enabled *bool
			if on || off {
				v := on
				enabled = &v
			}
			return runTenantSettingSet(cmd, args[0], args[1], group, enabled, flags)
		},
	}
	cmd.Flags().BoolVar(&on, "on", false, "turn it on for this tenant")
	cmd.Flags().BoolVar(&off, "off", false, "turn it off for this tenant")
	cmd.Flags().BoolVar(&useDflt, "default", false, "use the install default again")
	cmd.MarkFlagsMutuallyExclusive("on", "off", "default")
	flags.register(cmd)
	return cmd
}

func runTenantSettingShow(cmd *cobra.Command, connectorID string, flags tenantSettingFlags) error {
	tenantID, err := flags.parseTenantID()
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), flags.jsonOut)
	}
	backplaneURL, err := resolveBackplane(flags.backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), flags.jsonOut)
	}
	resp, err := getTenantSettings(cmd.Context(), backplaneURL, connectorID, tenantID)
	if err != nil {
		return renderTenantSettingError(cmd, backplaneURL, err, flags.jsonOut)
	}
	if flags.jsonOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp)
	}
	w := cmd.OutOrStdout()
	fmt.Fprintf(w, "%s — tenant %s\n", resp.ConnectorId, resp.TenantId)
	if len(resp.Items) == 0 {
		fmt.Fprintln(w, "No tenant settings. Every operation uses the install default.")
		return nil
	}
	printTenantSettingItems(w, resp.Items)
	return nil
}

func runTenantSettingSet(
	cmd *cobra.Command,
	connectorID, key string,
	group bool,
	enabled *bool,
	flags tenantSettingFlags,
) error {
	tenantID, err := flags.parseTenantID()
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), flags.jsonOut)
	}
	backplaneURL, err := resolveBackplane(flags.backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), flags.jsonOut)
	}
	body := api.TenantSettingBody{Enabled: enabled}
	var (
		items  []api.TenantOperationSettingItem
		result any
	)
	if group {
		resp, gerr := putTenantGroupSetting(cmd.Context(), backplaneURL, connectorID, key, tenantID, body)
		if gerr != nil {
			return renderTenantSettingError(cmd, backplaneURL, gerr, flags.jsonOut)
		}
		items, result = resp.Items, resp
	} else {
		item, oerr := putTenantOperationSetting(cmd.Context(), backplaneURL, connectorID, key, tenantID, body)
		if oerr != nil {
			return renderTenantSettingError(cmd, backplaneURL, oerr, flags.jsonOut)
		}
		items, result = []api.TenantOperationSettingItem{*item}, item
	}
	if flags.jsonOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	fmt.Fprintf(cmd.OutOrStdout(), "%s/%s — tenant setting is now %s\n",
		connectorID, key, tenantSettingWord(enabled))
	printTenantSettingItems(cmd.OutOrStdout(), items)
	return nil
}

func renderTenantSettingError(cmd *cobra.Command, backplaneURL string, err error, jsonOut bool) error {
	var he *httpResponseError
	if errors.As(err, &he) {
		return renderHTTPStatus(cmd, backplaneURL, he.statusCode, he.body, jsonOut)
	}
	return renderRequestError(cmd, backplaneURL, err, jsonOut)
}

// tenantSettingWord renders a tenant setting: on, off, or default.
func tenantSettingWord(v *bool) string {
	switch {
	case v == nil:
		return "default"
	case *v:
		return "on"
	default:
		return "off"
	}
}

func onOff(v bool) string {
	if v {
		return "on"
	}
	return "off"
}

func printTenantSettingItems(w io.Writer, items []api.TenantOperationSettingItem) {
	fmt.Fprintf(w, "  %-42s %-8s %-8s %-8s\n", "op_id", "default", "tenant", "applies")
	for _, item := range items {
		fmt.Fprintf(w, "  %-42s %-8s %-8s %-8s\n",
			truncate(item.OpId, 42),
			onOff(item.DefaultEnabled),
			tenantSettingWord(item.TenantEnabled),
			onOff(item.Enabled),
		)
	}
}

func getTenantSettings(
	ctx context.Context,
	backplaneURL, connectorID string,
	tenantID *uuid.UUID,
) (*api.TenantOperationSettingsResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	params := &api.ListTenantSettingsEndpointApiV1ConnectorsConnectorIdTenantSettingsGetParams{TenantId: tenantID}
	resp, err := retryOn401(ctx, authed,
		func(ctx context.Context) (*api.ListTenantSettingsEndpointApiV1ConnectorsConnectorIdTenantSettingsGetResponse, error) {
			return authed.ListTenantSettingsEndpointApiV1ConnectorsConnectorIdTenantSettingsGetWithResponse(
				ctx, connectorID, params)
		},
		func(r *api.ListTenantSettingsEndpointApiV1ConnectorsConnectorIdTenantSettingsGetResponse) int {
			return r.StatusCode()
		},
	)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode() != http.StatusOK || resp.JSON200 == nil {
		return nil, &httpResponseError{statusCode: resp.StatusCode(), body: resp.Body}
	}
	return resp.JSON200, nil
}

func putTenantOperationSetting(
	ctx context.Context,
	backplaneURL, connectorID, opID string,
	tenantID *uuid.UUID,
	body api.TenantSettingBody,
) (*api.TenantOperationSettingItem, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	params := &api.SetTenantOperationSettingEndpointApiV1ConnectorsConnectorIdTenantSettingsOperationsOpIdPutParams{TenantId: tenantID}
	resp, err := retryOn401(ctx, authed,
		func(ctx context.Context) (*api.SetTenantOperationSettingEndpointApiV1ConnectorsConnectorIdTenantSettingsOperationsOpIdPutResponse, error) {
			return authed.SetTenantOperationSettingEndpointApiV1ConnectorsConnectorIdTenantSettingsOperationsOpIdPutWithResponse(
				ctx, connectorID, opID, params, body)
		},
		func(r *api.SetTenantOperationSettingEndpointApiV1ConnectorsConnectorIdTenantSettingsOperationsOpIdPutResponse) int {
			return r.StatusCode()
		},
	)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode() != http.StatusOK || resp.JSON200 == nil {
		return nil, &httpResponseError{statusCode: resp.StatusCode(), body: resp.Body}
	}
	return resp.JSON200, nil
}

func putTenantGroupSetting(
	ctx context.Context,
	backplaneURL, connectorID, groupKey string,
	tenantID *uuid.UUID,
	body api.TenantSettingBody,
) (*api.TenantGroupSettingResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	params := &api.SetTenantGroupSettingEndpointApiV1ConnectorsConnectorIdTenantSettingsGroupsGroupKeyPutParams{TenantId: tenantID}
	resp, err := retryOn401(ctx, authed,
		func(ctx context.Context) (*api.SetTenantGroupSettingEndpointApiV1ConnectorsConnectorIdTenantSettingsGroupsGroupKeyPutResponse, error) {
			return authed.SetTenantGroupSettingEndpointApiV1ConnectorsConnectorIdTenantSettingsGroupsGroupKeyPutWithResponse(
				ctx, connectorID, groupKey, params, body)
		},
		func(r *api.SetTenantGroupSettingEndpointApiV1ConnectorsConnectorIdTenantSettingsGroupsGroupKeyPutResponse) int {
			return r.StatusCode()
		},
	)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode() != http.StatusOK || resp.JSON200 == nil {
		return nil, &httpResponseError{statusCode: resp.StatusCode(), body: resp.Body}
	}
	return resp.JSON200, nil
}
