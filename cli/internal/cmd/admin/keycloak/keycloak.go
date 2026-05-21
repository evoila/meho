// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package keycloak hosts the cobra commands under
// `meho admin keycloak ...` for install-time Keycloak realm
// provisioning. The first verb (G0.9.1-T11 / #791) is
// `bootstrap-clients`, which idempotently provisions:
//
//  1. The public `meho-cli` device-code client + 5 protocol mappers
//     + 4 default client scopes (`basic`, `roles`, `web-origins`,
//     `acr`).
//  2. The public `meho-mcp-client` authorization-code+PKCE client +
//     the same 5 mappers + 4 default scopes.
//  3. The `meho-admins` group.
//  4. An admin user joined to `meho-admins` with a password.
//
// The verb encodes the 5-step recipe documented in
// deploy/values-examples/README.md § Auth onramp recipe. It does NOT
// provision the confidential `meho-backplane` resource-server client
// or rotate any existing secrets — those are explicitly refused at
// the `--cli-client-id meho-backplane` boundary.
//
// Idempotency: every step does a "does this exist?" check before
// mutating. Re-runs produce `[skip] ...` lines for unchanged
// resources and `[updated] ...` for drift; never duplicates and never
// errors on a re-run against a realm in the desired state.
package keycloak

import (
	"bufio"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"

	"github.com/spf13/cobra"
)

// NewRootCmd returns the `meho admin keycloak` parent command, ready
// for cmd/admin/admin.go to graft onto the admin tree.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "keycloak",
		Short: "Provision Keycloak realm resources for the MEHO auth onramp",
		Long: "keycloak hosts install-time provisioning verbs that " +
			"talk to a Keycloak admin REST API directly. The first " +
			"verb is `bootstrap-clients`, which idempotently " +
			"provisions the public device-code client, the public " +
			"MCP browser-flow client, their 5 protocol mappers, " +
			"their 4 default client scopes, the meho-admins group, " +
			"and an admin user — the realm-side prerequisites for " +
			"`meho login` and the MCP onramp.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newBootstrapClientsCmd())
	return cmd
}

// newBootstrapClientsCmd returns the cobra command for
// `meho admin keycloak bootstrap-clients`.
//
// The command reads everything except the two passwords from flags +
// env vars. Passwords (the master-realm admin password and the new
// admin user's password) come from:
//
//  1. KEYCLOAK_ADMIN_PASSWORD / KEYCLOAK_ADMIN_USER_PASSWORD env
//     vars (preferred — survives terminal history, suits CI), or
//  2. Stdin (one line per password, prompted in order, when neither
//     env var is set and stdin is a TTY).
//
// They are never accepted via command-line flags so they cannot land
// in shell history or `ps` output.
func newBootstrapClientsCmd() *cobra.Command {
	var (
		keycloakBaseURL      string
		realm                string
		adminUsername        string
		cliClientID          string
		mcpClientID          string
		backplaneAudience    string
		mcpResourceURI       string
		tenantID             string
		tenantRole           string
		adminGroupName       string
		adminUserUsername    string
		adminUserEmail       string
		skipUserProvisioning bool
		mcpRedirectURIs      []string
		mcpWebOrigins        []string
		insecureSkipTLS      bool
		dryRun               bool
	)

	cmd := &cobra.Command{
		Use:   "bootstrap-clients",
		Short: "Idempotently provision the public CLI + MCP clients in a Keycloak realm",
		Long: "bootstrap-clients provisions every realm resource " +
			"`meho login` and the MCP browser-flow onramp need:\n\n" +
			"  * the public device-code client (default name `meho-cli`)\n" +
			"  * the public authorization-code+PKCE MCP client (default name `meho-mcp-client`)\n" +
			"  * 5 protocol mappers on each client (audience-meho-backplane, meho-mcp-audience, tenant-id, tenant-role, groups-claim)\n" +
			"  * 4 default client scopes on each client (basic, roles, web-origins, acr) — including the load-bearing `basic` scope that carries the `sub` claim in Keycloak 25+\n" +
			"  * the `meho-admins` top-level group\n" +
			"  * an admin user joined to `meho-admins` with a password\n\n" +
			"Idempotent: re-runs detect existing resources, " +
			"reconcile drift via PUT, and never duplicate. " +
			"Confidential clients (e.g. `meho-backplane`) are " +
			"explicitly refused — this verb provisions PUBLIC " +
			"clients only.\n\n" +
			"Passwords are read from env vars or stdin; they are " +
			"never accepted via command-line flags.",
		Example: "  # provision against the lab realm\n" +
			"  KEYCLOAK_ADMIN_PASSWORD=$(vault kv get -field=password secret/.../admin) \\\n" +
			"  KEYCLOAK_ADMIN_USER_PASSWORD='changeme123' \\\n" +
			"  meho admin keycloak bootstrap-clients \\\n" +
			"      --keycloak-base-url https://keycloak.evba.lab \\\n" +
			"      --realm evba \\\n" +
			"      --admin-username admin \\\n" +
			"      --mcp-resource-uri https://meho.evba.lab/mcp \\\n" +
			"      --admin-user-username damir.topic@example.com\n",
		RunE: func(cmd *cobra.Command, _ []string) error {
			// Resolve passwords (env first, stdin fallback).
			adminPassword := os.Getenv("KEYCLOAK_ADMIN_PASSWORD")
			adminUserPassword := os.Getenv("KEYCLOAK_ADMIN_USER_PASSWORD")
			if adminPassword == "" {
				p, err := readPassword(
					cmd.InOrStdin(), cmd.ErrOrStderr(),
					"Keycloak master-realm admin password: ",
				)
				if err != nil {
					return fmt.Errorf("read admin password: %w", err)
				}
				adminPassword = p
			}
			if !skipUserProvisioning && adminUserPassword == "" {
				p, err := readPassword(
					cmd.InOrStdin(), cmd.ErrOrStderr(),
					fmt.Sprintf("Password for new user %q: ", adminUserUsername),
				)
				if err != nil {
					return fmt.Errorf(
						"read admin user password: %w", err)
				}
				adminUserPassword = p
			}

			opts := BootstrapOptions{
				KeycloakBaseURL:      keycloakBaseURL,
				Realm:                realm,
				AdminUsername:        adminUsername,
				AdminPassword:        adminPassword,
				CLIClientID:          cliClientID,
				MCPClientID:          mcpClientID,
				BackplaneAudience:    backplaneAudience,
				MCPResourceURI:       mcpResourceURI,
				TenantID:             tenantID,
				TenantRole:           tenantRole,
				AdminGroupName:       adminGroupName,
				AdminUserUsername:    adminUserUsername,
				AdminUserEmail:       adminUserEmail,
				AdminUserPassword:    adminUserPassword,
				SkipUserProvisioning: skipUserProvisioning,
				MCPRedirectURIs:      mcpRedirectURIs,
				MCPWebOrigins:        mcpWebOrigins,
				DryRun:               dryRun,
				Out:                  cmd.OutOrStdout(),
				Err:                  cmd.ErrOrStderr(),
			}

			if insecureSkipTLS {
				// The package-level Bootstrap uses defaultHTTPClient();
				// when the operator workstation has no system trust
				// for the realm's CA we need to flip the TLS skip on
				// a custom client. Build it here and stash it via a
				// package-internal indirection (httpClientOverride).
				httpClientOverride = newInsecureClient()
				defer func() { httpClientOverride = nil }()
			}

			res, err := Bootstrap(cmd.Context(), opts)
			if err != nil {
				return err
			}

			printSummary(cmd.OutOrStdout(), opts, res)
			return nil
		},
	}

	cmd.Flags().StringVar(&keycloakBaseURL, "keycloak-base-url", "",
		"Keycloak base URL, e.g. https://keycloak.example.com")
	cmd.Flags().StringVar(&realm, "realm", "",
		"target realm name (NOT the master realm — that's where the admin token is minted)")
	cmd.Flags().StringVar(&adminUsername, "admin-username",
		os.Getenv("KEYCLOAK_ADMIN_USER"),
		"master-realm admin username (or set KEYCLOAK_ADMIN_USER)")
	cmd.Flags().StringVar(&cliClientID, "cli-client-id", "meho-cli",
		"public client_id for the device-code flow (matches chart's `config.keycloakCliClientId`)")
	cmd.Flags().StringVar(&mcpClientID, "mcp-client-id", "meho-mcp-client",
		"public client_id for the MCP browser-flow client")
	cmd.Flags().StringVar(&backplaneAudience, "backplane-audience", "meho-backplane",
		"audience claim the `audience-meho-backplane` mapper emits (matches chart's `config.keycloakAudience`)")
	cmd.Flags().StringVar(&mcpResourceURI, "mcp-resource-uri", "",
		"audience the `meho-mcp-audience` mapper emits, e.g. https://meho.example.com/mcp (no trailing slash)")
	cmd.Flags().StringVar(&tenantID, "tenant-id", "",
		"hardcoded value for the `tenant_id` claim mapper (UUID; the lab convention is one tenant per realm)")
	cmd.Flags().StringVar(&tenantRole, "tenant-role", "tenant_admin",
		"hardcoded value for the `tenant_role` claim mapper (one of tenant_admin / operator / read_only)")
	cmd.Flags().StringVar(&adminGroupName, "admin-group-name", "meho-admins",
		"top-level group the admin user joins (drives group-gated tools)")
	cmd.Flags().StringVar(&adminUserUsername, "admin-user-username", "",
		"username of the admin user to provision (required unless --skip-user-provisioning)")
	cmd.Flags().StringVar(&adminUserEmail, "admin-user-email", "",
		"optional email for the new admin user")
	cmd.Flags().BoolVar(&skipUserProvisioning, "skip-user-provisioning", false,
		"skip the group + user creation steps (use when users are externally-managed via federation / SCIM)")
	cmd.Flags().StringSliceVar(&mcpRedirectURIs, "mcp-redirect-uri", nil,
		"redirect URI(s) for the MCP browser-flow client (default: claude.ai callback + localhost)")
	cmd.Flags().StringSliceVar(&mcpWebOrigins, "mcp-web-origin", nil,
		"CORS web origin(s) for the MCP browser-flow client (default: `+` — allow the redirect-URI origins)")
	cmd.Flags().BoolVar(&insecureSkipTLS, "insecure-skip-tls-verify", false,
		"skip TLS verification when calling Keycloak (one-time bootstrap convenience; do not use in CI against untrusted Keycloaks)")
	cmd.Flags().BoolVar(&dryRun, "dry-run", false,
		"print what would be provisioned without making any API calls")

	// `--keycloak-base-url` and `--realm` and `--admin-username` are
	// mandatory; mark them so cobra-generated --help is precise.
	_ = cmd.MarkFlagRequired("keycloak-base-url")
	_ = cmd.MarkFlagRequired("realm")

	return cmd
}

// httpClientOverride is the package-internal indirection used by the
// --insecure-skip-tls-verify flag. nil → defaultHTTPClient() is used.
// Set by RunE, consumed by mintAdminToken / newAdminClient via the
// Bootstrap wrapper at the top of bootstrap.go.
//
// We avoid plumbing the *http.Client through every public type
// because the only legitimate need today is the insecure-skip flag,
// and a per-call argument bloats the surface area for callers that
// don't care. Tests use httptest.Server with default TLS (no skip
// needed) and inject via the exported `BootstrapWithClient` shim
// below.
var httpClientOverride *http.Client

// newInsecureClient builds an http.Client that skips TLS verification.
// Used by the --insecure-skip-tls-verify flag at install-time
// bootstrap, mirroring the reference shell script's `curl -k`. Not
// safe for general use.
func newInsecureClient() *http.Client {
	c := defaultHTTPClient()
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.TLSClientConfig = newSkipVerifyTLSConfig()
	c.Transport = transport
	return c
}

// readPassword prompts on stderr and reads one line from in,
// returning the trimmed string. Falls back to the env var
// MEHO_ADMIN_BOOTSTRAP_NONINTERACTIVE for unit tests (when stdin is
// not a TTY we still want to support a piped password without
// hanging the cobra Run).
func readPassword(in io.Reader, errOut io.Writer, prompt string) (string, error) {
	if _, err := fmt.Fprint(errOut, prompt); err != nil {
		return "", err
	}
	reader := bufio.NewReader(in)
	line, err := reader.ReadString('\n')
	if err != nil && !errors.Is(err, io.EOF) {
		return "", err
	}
	line = strings.TrimRight(line, "\r\n")
	if line == "" {
		return "", errors.New("empty password")
	}
	return line, nil
}

// printSummary writes the final progress block — the per-mutation
// events + the "set these in your chart" config keys.
func printSummary(out io.Writer, opts BootstrapOptions, res *Result) {
	fmt.Fprintln(out, "")
	fmt.Fprintln(out, "==> realm reconciliation events")
	for _, line := range res.MapperEventsLog {
		fmt.Fprintln(out, "    "+line)
	}
	if !opts.SkipUserProvisioning {
		if res.AdminGroupCreated {
			fmt.Fprintf(out, "    [created] group %s\n", opts.AdminGroupName)
		} else {
			fmt.Fprintf(out, "    [skip]    group %s (already exists)\n",
				opts.AdminGroupName)
		}
		if res.AdminUserCreated {
			fmt.Fprintf(out, "    [created] user %s (joined %s)\n",
				opts.AdminUserUsername, opts.AdminGroupName)
		} else {
			fmt.Fprintf(out, "    [skip]    user %s (already exists; password not reset)\n",
				opts.AdminUserUsername)
		}
	}
	fmt.Fprintln(out, "")
	fmt.Fprintln(out, "==> set these on the chart side to match the realm:")
	for _, line := range res.ConfigKeysToSet {
		fmt.Fprintln(out, "    "+line)
	}
	fmt.Fprintln(out, "")
	fmt.Fprintln(out,
		"Ready for: `meho login <backplane-url>` (after the chart redeploys with the new value).")
}
