// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package tenants

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newMailRecipientPolicyCmd returns `meho tenants mail-recipient-policy`.
func newMailRecipientPolicyCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "mail-recipient-policy",
		Short:        "Manage the tenant's mail-recipient allowlist (tenant_admin)",
		Long:         "Read/write the operator's own tenant mail-recipient allowlist (#3499).",
		SilenceUsage: true,
	}
	cmd.AddCommand(newMailSetCmd())
	return cmd
}

type mailSetOptions struct {
	allowlist         string
	allowlistSet      bool
	clear             bool
	jsonOut           bool
	backplaneOverride string
}

// newMailSetCmd returns `meho tenants mail-recipient-policy set`.
//
//	meho tenants mail-recipient-policy set
//	  --allowlist "oncall@ops.test,alerts.example.com"  # the tenant's own recipient space
//	  --allowlist ""                                     # deny (no mail for this tenant)
//	  --clear                                            # clear back to inherit the instance floor
//	  [--json] [--backplane <url>]
//
// Tenant-scoped (the caller's own tenant, from the JWT). tenant_admin only.
// The tenant allowlist NARROWS on top of the deployment-level
// MAIL_RECIPIENT_ALLOWLIST instance floor: it can restrict a tenant to no mail
// (or a smaller recipient set) but never widen past the floor, which the
// backplane still applies at send time. Pass exactly one of --allowlist /
// --clear.
//
// Exit codes: 0 ok; 2 auth_expired; 3 unreachable; 4 unexpected (incl. 422 /
// 404); 5 insufficient_role.
func newMailSetCmd() *cobra.Command {
	var opts mailSetOptions
	cmd := &cobra.Command{
		Use:   "set",
		Short: "Set or clear the tenant mail-recipient allowlist (tenant_admin)",
		Long: "set PATCHes /api/v1/tenants/mail-recipient-policy for the operator's own " +
			"tenant. tenant_admin only — operator / read_only land as 403 insufficient_role.\n\n" +
			"--allowlist sets the tenant's recipient space (comma-separated full addresses " +
			"and/or domains); an empty --allowlist=\"\" denies all mail for the tenant. " +
			"--clear removes the per-tenant override so the tenant inherits the deployment " +
			"MAIL_RECIPIENT_ALLOWLIST instance floor. The tenant allowlist only narrows: the " +
			"instance floor is still applied at send time, so a tenant can never widen past " +
			"it. Pass exactly one of --allowlist / --clear.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			opts.allowlistSet = cmd.Flags().Changed("allowlist")
			return runMailSet(cmd, opts)
		},
	}
	cmd.Flags().StringVar(&opts.allowlist, "allowlist", "",
		"the tenant's permitted recipient space (comma-separated addresses/domains); "+
			"empty string denies all mail for the tenant")
	cmd.Flags().BoolVar(&opts.clear, "clear", false,
		"clear the per-tenant override back to inheriting the instance floor")
	cmd.Flags().BoolVar(&opts.jsonOut, "json", false,
		"emit the resolved policy as JSON instead of the human summary")
	cmd.Flags().StringVar(&opts.backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

// buildMailBody assembles the sparse PATCH body from the flags the operator
// set. A nil map value marshals to JSON null (an explicit clear-to-inherit); a
// present string (including "") sets the tenant allowlist verbatim.
func buildMailBody(opts mailSetOptions) (map[string]any, error) {
	if opts.allowlistSet && opts.clear {
		return nil, fmt.Errorf("--allowlist and --clear are mutually exclusive")
	}
	body := map[string]any{}
	switch {
	case opts.allowlistSet:
		body["mail_recipient_allowlist"] = opts.allowlist
	case opts.clear:
		body["mail_recipient_allowlist"] = nil // explicit null -> inherit
	default:
		return nil, fmt.Errorf("nothing to change; pass --allowlist <value> or --clear")
	}
	return body, nil
}

func runMailSet(cmd *cobra.Command, opts mailSetOptions) error {
	body, err := buildMailBody(opts)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.jsonOut)
	}
	backplaneURL, err := backplane.Resolve(opts.backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.jsonOut)
	}
	payload, err := json.Marshal(body)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("encode request body: %v", err)), opts.jsonOut)
	}
	resp, err := patchMailPolicy(cmd.Context(), backplaneURL, payload)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.jsonOut)
	}
	if resp.StatusCode != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode, resp.Body, opts.jsonOut)
	}
	var policy api.TenantMailRecipientPolicy
	if err := json.Unmarshal(resp.Body, &policy); err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("decode policy response: %v", err)), opts.jsonOut)
	}
	if opts.jsonOut {
		return output.PrintJSON(cmd.OutOrStdout(), policy)
	}
	printMailPolicySummary(cmd.OutOrStdout(), &policy)
	return nil
}

func patchMailPolicy(ctx context.Context, backplaneURL string, payload []byte) (*rawResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	return doRequest(ctx, authed, func(ctx context.Context) (*http.Response, error) {
		return authed.UpdateMailRecipientPolicyApiV1TenantsMailRecipientPolicyPatchWithBody(
			ctx,
			&api.UpdateMailRecipientPolicyApiV1TenantsMailRecipientPolicyPatchParams{},
			"application/json",
			bytes.NewReader(payload),
		)
	})
}

// printMailPolicySummary renders the resolved policy as a compact confirmation.
// A NULL renders "inherit"; an empty string renders "deny" so the operator sees
// the tri-state resolution, not an ambiguous blank.
func printMailPolicySummary(w io.Writer, p *api.TenantMailRecipientPolicy) {
	if p == nil {
		return
	}
	allow := "inherit (instance floor)"
	if p.MailRecipientAllowlist != nil {
		if *p.MailRecipientAllowlist == "" {
			allow = "deny (empty — no mail for this tenant)"
		} else {
			allow = *p.MailRecipientAllowlist
		}
	}
	fmt.Fprintln(w, "updated mail-recipient policy")
	fmt.Fprintf(w, "%-12s %s\n", "tenant_id:", p.TenantId.String())
	fmt.Fprintf(w, "%-12s %s\n", "allowlist:", allow)
}
