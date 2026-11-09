// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package targets

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"strings"

	"github.com/google/uuid"
	"github.com/spf13/cobra"
	"gopkg.in/yaml.v3"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/cmd/settings"
	"github.com/evoila/meho/cli/internal/output"
)

// newExportCmd returns `meho targets export`. It takes the entries from
// the settings export instead of building them from the target list, so
// they are the entries of `meho settings export` by construction:
// explicit `extras`, top-level `version`, no server-managed fields, no
// `secret_ref` MEHO can work out from the name. `meho targets import`
// reads the result back. Like the settings export, it needs
// tenant_admin or platform_admin.
//
// Exit codes: 0 written, 1 --tenant is needed or wrong, 2 auth_expired,
// 3 unreachable, 4 unexpected, 5 insufficient_role.
func newExportCmd() *cobra.Command {
	var outFile, tenant, backplaneOverride string
	var jsonOut bool
	cmd := &cobra.Command{
		Use:   "export",
		Short: "Write the tenant's targets as a targets.yaml file",
		Long: "export writes `targets: [<entry>, ...]` to stdout, or to --out, in " +
			"exactly the entry shape of `spec.tenants[].targets` in the settings " +
			"file. `meho targets import` reads it back; a target's password is " +
			"never in it.\n\n" +
			"The tenant is your own, or the one this command works in: " +
			"$MEHO_TENANT, or the one set with `meho tenants use` for this " +
			"backplane. A platform_admin with neither, or with a tenant name " +
			"instead of an id, passes --tenant.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			backplaneURL, err := settings.ResolveBackplane(cmd, backplaneOverride, jsonOut)
			if err != nil {
				return err
			}
			if tenant == "" {
				tenant = selectedTenantID(cmd.Context())
			}
			data, err := settings.Export(cmd.Context(), backplaneURL, tenant)
			if err != nil {
				return settings.RenderError(cmd, backplaneURL, err, jsonOut)
			}
			doc, failure := targetsDocument(data)
			if failure != nil {
				return output.RenderError(cmd.ErrOrStderr(), failure, jsonOut)
			}
			if outFile == "" {
				_, err := cmd.OutOrStdout().Write(doc)
				return err
			}
			if err := os.WriteFile(outFile, doc, 0o600); err != nil {
				return output.RenderError(cmd.ErrOrStderr(),
					output.Unexpected(fmt.Sprintf("write %s: %v", outFile, err)), jsonOut)
			}
			fmt.Fprintf(cmd.ErrOrStderr(), "Wrote %s.\n", outFile)
			return nil
		},
	}
	cmd.Flags().StringVar(&outFile, "out", "", "file to write (default: stdout)")
	cmd.Flags().StringVar(&tenant, "tenant", "", "tenant id (defaults to $MEHO_TENANT, then the one set with 'meho tenants use')")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "print errors as JSON; the file itself stays YAML")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to export from (defaults to the URL recorded by 'meho login')")
	return cmd
}

// selectedTenantID is the tenant this command works in, the one the
// Meho-Tenant header carries: $MEHO_TENANT, or the stored choice when
// the command uses that choice's backplane. So the header and tenant_id
// never name different tenants.
//
// A name returns "": tenant_id takes only an id. The header still
// selects the tenant for a tenant_admin, and a platform_admin's export
// of several tenants stops at the --tenant question.
func selectedTenantID(ctx context.Context) string {
	tenant := api.TenantFrom(ctx)
	if _, err := uuid.Parse(tenant); err != nil {
		return ""
	}
	return tenant
}

// targetsDocument cuts `targets:` out of a settings file. It works on
// YAML nodes, so the key order and the quoting of `version: "8.0"`
// survive unchanged.
func targetsDocument(settingsFile []byte) ([]byte, *output.StructuredError) {
	var file struct {
		Spec struct {
			Tenants []struct {
				ID      string    `yaml:"id"`
				Targets yaml.Node `yaml:"targets"`
			} `yaml:"tenants"`
		} `yaml:"spec"`
	}
	if err := yaml.Unmarshal(settingsFile, &file); err != nil {
		return nil, output.Unexpected(fmt.Sprintf("parse the export: %v", err))
	}
	tenants := file.Spec.Tenants
	switch {
	case len(tenants) == 0:
		return nil, output.Unexpected("the export names no tenant")
	case len(tenants) > 1:
		ids := make([]string, len(tenants))
		for i, tenant := range tenants {
			ids[i] = tenant.ID
		}
		return nil, output.Usage("this export covers several tenants; pass --tenant with one of: " +
			strings.Join(ids, ", "))
	}
	var doc bytes.Buffer
	encoder := yaml.NewEncoder(&doc)
	encoder.SetIndent(2)
	if err := encoder.Encode(map[string]*yaml.Node{"targets": &tenants[0].Targets}); err != nil {
		return nil, output.Unexpected(fmt.Sprintf("write the targets: %v", err))
	}
	return doc.Bytes(), nil
}
