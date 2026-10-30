// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package tenants

import (
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

type useOptions struct {
	jsonOut           bool
	backplaneOverride string
}

// newUseCmd returns `meho tenants use [<name|id>]` (#5241).
func newUseCmd() *cobra.Command {
	var opts useOptions
	cmd := &cobra.Command{
		Use:   "use [<name|id>]",
		Short: "Choose the tenant every command works in; without a name, list your tenants",
		Long: "One login can be in several tenants. MEHO works in one tenant at a time, " +
			"and every command sends it in the Meho-Tenant header.\n\n" +
			"`meho tenants use <name>` checks the name (or tenant ID) against your tenants " +
			"and stores the choice in config.json, next to the backplane address. Every " +
			"command then works in that tenant. No new login is needed: your login already " +
			"holds all of your tenants. An unknown name is refused, and your tenants are listed.\n\n" +
			"`meho tenants use` without a name lists your tenants with your role in each, " +
			"and marks the current one.\n\n" +
			"The variable MEHO_TENANT (a tenant name or ID) wins over the stored choice " +
			"for one shell. A login in one tenant needs none of this.\n\n" +
			"Exit codes: 0 ok; 1 unknown tenant; 2 auth_expired; 3 unreachable; " +
			"4 unexpected; 5 insufficient_role.",
		Args:          cobra.MaximumNArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runUse(cmd, args, opts)
		},
	}
	cmd.Flags().BoolVar(&opts.jsonOut, "json", false,
		"emit the tenants (or the chosen tenant) as JSON instead of the human summary")
	cmd.Flags().StringVar(&opts.backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runUse(cmd *cobra.Command, args []string, opts useOptions) error {
	backplaneURL, err := backplane.Resolve(opts.backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.jsonOut)
	}
	tenants, err := fetchTenants(cmd, backplaneURL, opts.jsonOut)
	if err != nil {
		return err
	}
	cfg, err := auth.LoadConfig()
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf("cannot read the stored config (%v); run `meho login %s`", err, backplaneURL)),
			opts.jsonOut)
	}
	if len(args) == 0 {
		return printTenants(cmd.OutOrStdout(), tenants, currentTenant(cfg, backplaneURL), opts.jsonOut)
	}
	return chooseTenant(cmd, cfg, backplaneURL, tenants, args[0], opts.jsonOut)
}

// fetchTenants reads the person's tenants from the status endpoint.
// It sends no Meho-Tenant header, so a stored tenant the person has
// left does not hide the list. A login in several tenants gets the
// list without any tenant work on the backplane.
func fetchTenants(cmd *cobra.Command, backplaneURL string, jsonOut bool) ([]api.TenantEntry, error) {
	authed, err := newAuthedClient(cmd.Context(), backplaneURL)
	if err != nil {
		return nil, renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	resp, err := authed.GetHealth(api.WithTenant(cmd.Context(), ""))
	if err != nil {
		return nil, renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	if resp.JSON200 == nil {
		return nil, renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, jsonOut)
	}
	if resp.JSON200.Operator.Tenants == nil {
		return nil, nil
	}
	return *resp.JSON200.Operator.Tenants, nil
}

// currentTenant is the tenant commands work in now: MEHO_TENANT, else
// the stored choice for this backplane, else "".
func currentTenant(cfg auth.Config, backplaneURL string) string {
	if v := strings.TrimSpace(os.Getenv(api.TenantEnv)); v != "" {
		return v
	}
	if cfg.Tenant != nil && sameURL(cfg.BackplaneURL, backplaneURL) {
		return cfg.Tenant.ID
	}
	return ""
}

func sameURL(a, b string) bool {
	na, errA := backplane.NormaliseURL(a)
	nb, errB := backplane.NormaliseURL(b)
	return errA == nil && errB == nil && na == nb
}

// matchTenant finds the tenant that wanted names, by ID or by name.
// Tenant names are lowercase, as the backplane compares them.
func matchTenant(tenants []api.TenantEntry, wanted string) (api.TenantEntry, bool) {
	wanted = strings.TrimSpace(wanted)
	for _, t := range tenants {
		if t.TenantId.String() == strings.ToLower(wanted) || t.Name == strings.ToLower(wanted) {
			return t, true
		}
	}
	return api.TenantEntry{}, false
}

func chooseTenant(
	cmd *cobra.Command,
	cfg auth.Config,
	backplaneURL string,
	tenants []api.TenantEntry,
	wanted string,
	jsonOut bool,
) error {
	if !sameURL(cfg.BackplaneURL, backplaneURL) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Usage(fmt.Sprintf("the choice is stored for the backplane of your last login (%s); "+
				"run `meho login %s` first to work with that backplane", cfg.BackplaneURL, backplaneURL)),
			jsonOut)
	}
	if len(tenants) == 0 {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Usage("your login is not in several tenants, so there is no tenant to choose. "+
				"Every command already works in your one tenant."),
			jsonOut)
	}
	chosen, ok := matchTenant(tenants, wanted)
	if !ok {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Usage(fmt.Sprintf("you are not in a tenant named %q. Your tenants: %s. "+
				"Pick one with `meho tenants use <name>`.", wanted, tenantList(tenants))),
			jsonOut)
	}
	cfg.Tenant = &auth.Tenant{ID: chosen.TenantId.String(), Name: chosen.Name}
	if err := auth.SaveConfig(cfg); err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), jsonOut)
	}
	if env := strings.TrimSpace(os.Getenv(api.TenantEnv)); env != "" {
		fmt.Fprintf(cmd.ErrOrStderr(),
			"warning: %s is set to %q. In this shell it wins over the stored choice.\n", api.TenantEnv, env)
	}
	if jsonOut {
		chosen.Current = true
		return output.PrintJSON(cmd.OutOrStdout(), chosen)
	}
	fmt.Fprintf(cmd.OutOrStdout(), "Now working in tenant %s (%s).\n", chosen.Name, chosen.TenantRole)
	return nil
}

// tenantList renders "lab-a (read_only), lab-b (operator)".
func tenantList(tenants []api.TenantEntry) string {
	parts := make([]string, 0, len(tenants))
	for _, t := range tenants {
		parts = append(parts, fmt.Sprintf("%s (%s)", t.Name, t.TenantRole))
	}
	return strings.Join(parts, ", ")
}

// printTenants lists the tenants with the role in each and marks the
// one commands work in now.
func printTenants(w io.Writer, tenants []api.TenantEntry, current string, jsonOut bool) error {
	marked := make([]api.TenantEntry, 0, len(tenants))
	for _, t := range tenants {
		_, isCurrent := matchTenant([]api.TenantEntry{t}, current)
		t.Current = current != "" && isCurrent
		marked = append(marked, t)
	}
	if jsonOut {
		return output.PrintJSON(w, marked)
	}
	if len(marked) == 0 {
		fmt.Fprintln(w, "Your login is not in several tenants. There is no tenant to choose.")
		return nil
	}
	fmt.Fprintln(w, "Your tenants:")
	chosen := false
	for _, t := range marked {
		mark := ""
		if t.Current {
			mark, chosen = "  (current)", true
		}
		fmt.Fprintf(w, "  %s  %s%s\n", t.Name, t.TenantRole, mark)
	}
	if !chosen && len(marked) > 1 {
		fmt.Fprintln(w, "No tenant chosen. Pick one with `meho tenants use <name>`.")
	}
	return nil
}
