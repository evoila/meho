// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package serviceprincipal

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// NewReadScopeCmd returns `meho service-principals read-scope`: the targets a
// service principal may read. Without a read scope a service principal reads
// every target of its tenant. Only a person can set or clear one.
func NewReadScopeCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "read-scope",
		Short:        "Limit which targets a service principal may read (operator)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newReadScopeSetCmd(), newReadScopeShowCmd(), newReadScopeClearCmd())
	return cmd
}

func newReadScopeSetCmd() *cobra.Command {
	var targets, selectors []string
	var noTarget, jsonOut bool
	var backplaneOverride string
	cmd := &cobra.Command{Use: "set <principal-sub>", Short: "Set (replace) the read scope of a service principal (operator)", Args: cobra.ExactArgs(1), SilenceUsage: true, SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			body, err := buildReadScopeBody(targets, selectors, noTarget)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), jsonOut)
			}
			url, err := backplane.Resolve(backplaneOverride)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
			}
			resp, err := setReadScope(cmd.Context(), url, args[0], body)
			if err != nil {
				return renderRequestError(cmd, url, err, jsonOut)
			}
			if resp.StatusCode() != http.StatusOK {
				return renderHTTPStatus(cmd, url, resp.StatusCode(), resp.Body, jsonOut)
			}
			if jsonOut {
				return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
			}
			printReadScope(cmd.OutOrStdout(), resp.JSON200)
			return nil
		},
	}
	cmd.Flags().StringArrayVar(&targets, "target", nil, "target UUID the principal may read (repeatable)")
	cmd.Flags().StringArrayVar(&selectors, "selector", nil, "target selector `product=<product>` and/or `name=<fnmatch glob>`, joined by a comma (repeatable); matches targets registered later too")
	cmd.Flags().BoolVar(&noTarget, "no-target", false, "also allow safe calls that have no target")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit raw ServiceReadScopeRead JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "", "backplane URL (defaults to the URL from `meho login`)")
	return cmd
}

// buildReadScopeBody turns the flags into the PUT body. Every --target and
// every --selector is one entry; --no-target adds the "no target" entry.
func buildReadScopeBody(targets, selectors []string, noTarget bool) (api.ServiceReadScopeSet, error) {
	body := api.ServiceReadScopeSet{}
	for _, target := range targets {
		id, err := uuid.Parse(target)
		if err != nil {
			return api.ServiceReadScopeSet{}, fmt.Errorf("--target %q is not a valid UUID: %w", target, err)
		}
		body.Entries = append(body.Entries, api.ReadScopeEntry{TargetId: &id})
	}
	for _, selector := range selectors {
		entry, err := parseSelector(selector)
		if err != nil {
			return api.ServiceReadScopeSet{}, err
		}
		body.Entries = append(body.Entries, entry)
	}
	if noTarget {
		yes := true
		body.Entries = append(body.Entries, api.ReadScopeEntry{NoTarget: &yes})
	}
	if len(body.Entries) == 0 {
		return api.ServiceReadScopeSet{}, fmt.Errorf("give at least one of --target, --selector or --no-target; a scope without entries would refuse every read")
	}
	return body, nil
}

// parseSelector reads `product=<p>`, `name=<glob>` or `product=<p>,name=<glob>`.
func parseSelector(selector string) (api.ReadScopeEntry, error) {
	var entry api.ReadScopeEntry
	for _, part := range strings.Split(selector, ",") {
		key, value, ok := strings.Cut(strings.TrimSpace(part), "=")
		if !ok || value == "" {
			return api.ReadScopeEntry{}, fmt.Errorf("--selector %q: expected product=<product> and/or name=<glob>", selector)
		}
		switch key {
		case "product":
			if entry.TargetProduct != nil {
				return api.ReadScopeEntry{}, fmt.Errorf("--selector %q: product given twice", selector)
			}
			product := value
			entry.TargetProduct = &product
		case "name":
			if entry.TargetNamePattern != nil {
				return api.ReadScopeEntry{}, fmt.Errorf("--selector %q: name given twice", selector)
			}
			name := value
			entry.TargetNamePattern = &name
		default:
			return api.ReadScopeEntry{}, fmt.Errorf("--selector %q: unknown key %q (use product or name)", selector, key)
		}
	}
	return entry, nil
}

func setReadScope(ctx context.Context, url, principal string, body api.ServiceReadScopeSet) (*api.SetReadScopeApiV1ServicePrincipalsReadScopesPrincipalSubPutResponse, error) {
	authed, err := newAuthedClient(ctx, url)
	if err != nil {
		return nil, err
	}
	return retryOn401(ctx, authed, func(ctx context.Context) (*api.SetReadScopeApiV1ServicePrincipalsReadScopesPrincipalSubPutResponse, error) {
		return authed.SetReadScopeApiV1ServicePrincipalsReadScopesPrincipalSubPutWithResponse(ctx, principal, nil, body)
	}, func(r *api.SetReadScopeApiV1ServicePrincipalsReadScopesPrincipalSubPutResponse) int {
		return r.StatusCode()
	})
}

func newReadScopeShowCmd() *cobra.Command {
	var jsonOut bool
	var backplaneOverride string
	cmd := &cobra.Command{Use: "show <principal-sub>", Short: "Show the read scope of a service principal (operator)", Args: cobra.ExactArgs(1), SilenceUsage: true, SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			url, err := backplane.Resolve(backplaneOverride)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
			}
			resp, err := showReadScope(cmd.Context(), url, args[0])
			if err != nil {
				return renderRequestError(cmd, url, err, jsonOut)
			}
			if resp.StatusCode() == http.StatusNotFound {
				if jsonOut {
					return output.PrintJSON(cmd.OutOrStdout(), map[string]string{"principal_sub": args[0], "status": "not_set"})
				}
				fmt.Fprintf(cmd.OutOrStdout(), "no read scope for %q: it may read every target of its tenant\n", args[0])
				return nil
			}
			if resp.StatusCode() != http.StatusOK {
				return renderHTTPStatus(cmd, url, resp.StatusCode(), resp.Body, jsonOut)
			}
			if jsonOut {
				return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
			}
			printReadScope(cmd.OutOrStdout(), resp.JSON200)
			return nil
		},
	}
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit raw ServiceReadScopeRead JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "", "backplane URL (defaults to the URL from `meho login`)")
	return cmd
}

func showReadScope(ctx context.Context, url, principal string) (*api.ShowReadScopeApiV1ServicePrincipalsReadScopesPrincipalSubGetResponse, error) {
	authed, err := newAuthedClient(ctx, url)
	if err != nil {
		return nil, err
	}
	return retryOn401(ctx, authed, func(ctx context.Context) (*api.ShowReadScopeApiV1ServicePrincipalsReadScopesPrincipalSubGetResponse, error) {
		return authed.ShowReadScopeApiV1ServicePrincipalsReadScopesPrincipalSubGetWithResponse(ctx, principal, nil)
	}, func(r *api.ShowReadScopeApiV1ServicePrincipalsReadScopesPrincipalSubGetResponse) int {
		return r.StatusCode()
	})
}

func newReadScopeClearCmd() *cobra.Command {
	var confirm, jsonOut bool
	var backplaneOverride string
	cmd := &cobra.Command{Use: "clear <principal-sub>", Short: "Clear the read scope of a service principal, so it reads every target again (operator)", Args: cobra.ExactArgs(1), SilenceUsage: true, SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			if !confirm {
				fmt.Fprintf(cmd.OutOrStdout(), "Clear the read scope of %q; it will read every target of its tenant. Continue? [y/N]: ", args[0])
				var answer string
				if _, err := fmt.Fscanln(cmd.InOrStdin(), &answer); err != nil || !isAffirmative(answer) {
					if jsonOut {
						return output.PrintJSON(cmd.OutOrStdout(), map[string]string{"principal_sub": args[0], "status": "declined"})
					}
					fmt.Fprintf(cmd.OutOrStdout(), "declined: read scope of %q not cleared\n", args[0])
					return nil
				}
			}
			url, err := backplane.Resolve(backplaneOverride)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
			}
			resp, err := clearReadScope(cmd.Context(), url, args[0])
			if err != nil {
				return renderRequestError(cmd, url, err, jsonOut)
			}
			if resp.StatusCode() != http.StatusNoContent {
				return renderHTTPStatus(cmd, url, resp.StatusCode(), resp.Body, jsonOut)
			}
			if jsonOut {
				return output.PrintJSON(cmd.OutOrStdout(), map[string]string{"principal_sub": args[0], "status": "cleared"})
			}
			fmt.Fprintf(cmd.OutOrStdout(), "cleared the read scope of %q\n", args[0])
			return nil
		},
	}
	cmd.Flags().BoolVar(&confirm, "confirm", false, "confirm the clear")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit a machine-readable result JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "", "backplane URL (defaults to the URL from `meho login`)")
	return cmd
}

func clearReadScope(ctx context.Context, url, principal string) (*api.ClearReadScopeApiV1ServicePrincipalsReadScopesPrincipalSubDeleteResponse, error) {
	authed, err := newAuthedClient(ctx, url)
	if err != nil {
		return nil, err
	}
	return retryOn401(ctx, authed, func(ctx context.Context) (*api.ClearReadScopeApiV1ServicePrincipalsReadScopesPrincipalSubDeleteResponse, error) {
		return authed.ClearReadScopeApiV1ServicePrincipalsReadScopesPrincipalSubDeleteWithResponse(ctx, principal, nil)
	}, func(r *api.ClearReadScopeApiV1ServicePrincipalsReadScopesPrincipalSubDeleteResponse) int {
		return r.StatusCode()
	})
}

func printReadScope(w io.Writer, scope *api.ServiceReadScopeRead) {
	if scope == nil {
		return
	}
	fmt.Fprintf(w, "principal_sub:       %s\nset_by:              %s\nset_at:              %s\nentries:\n", scope.PrincipalSub, scope.SetBySub, scope.SetAt.UTC().Format("2006-01-02T15:04:05Z07:00"))
	for _, entry := range scope.Entries {
		fmt.Fprintf(w, "  - %s\n", describeEntry(entry))
	}
}

func describeEntry(entry api.ReadScopeEntry) string {
	if entry.NoTarget != nil && *entry.NoTarget {
		return "calls without a target"
	}
	if entry.TargetId != nil {
		return "target " + entry.TargetId.String()
	}
	var parts []string
	if entry.TargetProduct != nil {
		parts = append(parts, "product="+*entry.TargetProduct)
	}
	if entry.TargetNamePattern != nil {
		parts = append(parts, "name="+*entry.TargetNamePattern)
	}
	return "selector " + strings.Join(parts, ",")
}
