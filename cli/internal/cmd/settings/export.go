// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package settings

import (
	"fmt"
	"os"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	openapi_types "github.com/oapi-codegen/runtime/types"

	"github.com/evoila/meho/cli/internal/output"
)

// newExportCmd returns `meho settings export`.
//
// Exit codes: 0 written, 2 auth_expired, 3 unreachable, 4 unexpected
// (stored data holds a secret, or the file cannot be written),
// 5 insufficient_role.
func newExportCmd() *cobra.Command {
	var outFile, backplaneOverride string
	var jsonOut bool
	cmd := &cobra.Command{
		Use:   "export",
		Short: "Write the settings file from the live state",
		Long: "export calls GET /api/v1/settings/export and writes the file to " +
			"stdout, or to --out. A tenant_admin gets their own tenant, a " +
			"platform_admin every tenant. Stored data that looks like a secret " +
			"stops the export, with one sentence per path.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			backplaneURL, err := ResolveBackplane(cmd, backplaneOverride, jsonOut)
			if err != nil {
				return err
			}
			data, err := Export(cmd.Context(), backplaneURL, "")
			if err != nil {
				return RenderError(cmd, backplaneURL, err, jsonOut)
			}
			return writeFile(cmd, outFile, data, jsonOut)
		},
	}
	cmd.Flags().StringVar(&outFile, "out", "", "file to write (default: stdout)")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "print errors as JSON; the file itself stays YAML")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by 'meho login')")
	return cmd
}

// writeFile writes data to path, or to stdout when path is empty. The
// file holds no secret, but it does name hosts and policies, so it is
// private to its owner.
func writeFile(cmd *cobra.Command, path string, data []byte, jsonOut bool) error {
	if path == "" {
		_, err := cmd.OutOrStdout().Write(data)
		return err
	}
	if err := os.WriteFile(path, data, 0o600); err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("write %s: %v", path, err)), jsonOut)
	}
	fmt.Fprintf(cmd.ErrOrStderr(), "Wrote %s.\n", path)
	return nil
}

// parseUUID reads a tenant id from the command line.
func parseUUID(text string) (openapi_types.UUID, error) {
	id, err := uuid.Parse(text)
	if err != nil {
		return openapi_types.UUID{}, &usageError{message: fmt.Sprintf("%q is not a tenant id (a UUID)", text)}
	}
	return id, nil
}
